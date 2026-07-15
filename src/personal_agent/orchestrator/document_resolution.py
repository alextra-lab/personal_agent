"""Resolve current-turn PDF attachments to content blocks (ADR-0102 T3, FRE-683).

Tiered selector (ADR-0102 §1): text extraction (Tier 1) when the document's
aggregate text density is at or above a configured floor, else vision (Tier 2).
Content-block construction (§4) for Tier 2 delivers either a native PDF
document block or rasterized image blocks, reusing the ADR-0101 image block
path. Page budget + salience selection (§4) and the four guardrail dimensions
(§5) are enforced fail-closed, mirroring ``attachment_resolution.py``'s
transform-or-reject-with-disclosure pattern.

**Scope boundary:** the Tier-2 delivery mode is resolved by a caller-supplied
``resolve_tier2_delivery`` callback, not computed here. Routing/delivery
selection — reading ``ModelDefinition.supports_pdf_document`` /
``supports_vision`` and deciding which mode to request, with fail-closed
escalation — is T4 (FRE-684), wired into the ``executor.py`` routing seam.
The callback is invoked lazily, only once a specific document is actually
classified Tier 2 (ADR-0102 §1: Tier 1 must work on **any** model, no
capability check should ever fire for a document that resolves to plain
text). This module never touches ``ExecutionContext`` or ``user_message``.
"""

from __future__ import annotations

import asyncio
import base64
import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

import pypdfium2 as pdfium
import structlog
from PIL import Image

from personal_agent.config import settings
from personal_agent.documents.pdf_utils import (
    PageSalienceScore,
    build_page_subset,
    compute_page_scores,
    extract_page_text,
    get_outline_page_indices,
    rasterize_page,
    select_pages_by_salience,
)
from personal_agent.exceptions import AttachmentUnsupportedError
from personal_agent.orchestrator.types import AttachmentRef, DocumentContinuationOffer
from personal_agent.storage import ArtifactStoreError, get_artifact_store

log = structlog.get_logger(__name__)

PDF_CONTENT_TYPES: frozenset[str] = frozenset({"application/pdf"})

# Anthropic's documented hard limit for the native PDF document block — an
# external provider constraint, not owner-tunable config (ADR-0102 §4).
_PROVIDER_MAX_PAGES = 100

# Rank-only boost applied to an outline page's char_count before ranking so it
# dominates selection; does not affect the underlying PageSalienceScore data.
_OUTLINE_BONUS = 10_000


@dataclass(frozen=True)
class ResolvedDocuments:
    """Result of resolving a turn's PDF attachments to content blocks.

    Attributes:
        blocks: One ``text``, ``document``, or ``image_url`` block per
            surviving document/page, in submitted order.
        disclosures: User-facing strings describing any trim/drop/reject
            applied by a guardrail (ADR-0102 §5, disclose-on-alter).
        used_tier2: ``True`` iff at least one document in this turn was
            classified Tier 2 and the caller's ``resolve_tier2_delivery``
            callback was invoked. Callers use this to know whether a
            document-driven capability/routing decision actually happened —
            ``False`` means every document resolved via Tier 1 (text), so no
            model-capability requirement was introduced this turn.
        native_pdf_page_count: Total selected pages actually delivered via a
            native-PDF ``document`` block this turn (ADR-0102 §7b / FRE-686).
            One ``document`` block can represent many pages, so callers that
            need a page-multiplied cost estimate cannot derive this from
            ``len(blocks)`` alone. ``0`` for text-only or rasterize-only
            turns — rasterized pages are already one ``image_url`` block
            each, so their count is ``len(blocks)``, not this field.
        continuation_offers: One ``DocumentContinuationOffer`` per Tier-2
            document that still has pages the per-turn page budget did not
            include (ADR-0102 §4 / FRE-685) — empty when every Tier-2
            document in the turn was fully delivered. Callers persist these
            as durable pending state so a follow-up turn can serve the
            dropped pages without a re-upload.
    """

    blocks: tuple[dict[str, Any], ...]
    disclosures: tuple[str, ...]
    used_tier2: bool
    native_pdf_page_count: int = 0
    continuation_offers: tuple[DocumentContinuationOffer, ...] = ()


def _select_pages(
    scores: Sequence[PageSalienceScore],
    budget: int,
    outline: frozenset[int],
) -> list[int]:
    """Select pages by T1's salience scorer, boosted by outline membership.

    A thin wrapper over ``select_pages_by_salience`` — does not modify T1.
    Pages named in the PDF outline/TOC rank first (ADR-0102 §4: "prefer pages
    the PDF outline/TOC marks as substantive").
    """
    if not outline:
        return select_pages_by_salience(scores, budget)
    boosted = [
        replace(s, char_count=s.char_count + _OUTLINE_BONUS) if s.page_index in outline else s
        for s in scores
    ]
    return select_pages_by_salience(boosted, budget)


def _format_page_ranges(one_indexed_pages: Sequence[int]) -> str:
    """Compress a 1-indexed page-number list into human-readable range notation.

    E.g. ``[1, 2, 3, 7, 9, 10]`` -> ``"1-3, 7, 9-10"`` (ADR-0102 §4: the
    disclosure must name specific ranges, not just counts).

    Args:
        one_indexed_pages: 1-indexed page numbers, any order, may contain
            duplicates.

    Returns:
        Comma-separated ranges in ascending order, or ``""`` if empty.
    """
    pages = sorted(set(one_indexed_pages))
    if not pages:
        return ""
    ranges: list[str] = []
    start = prev = pages[0]
    for page in pages[1:]:
        if page == prev + 1:
            prev = page
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = page
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(ranges)


def _downscale_page_if_needed(image: Image.Image) -> tuple[Image.Image, bool]:
    """Downscale a rasterized page below the pixel cap if either dimension exceeds it."""
    if max(image.size) <= settings.document_page_max_pixels:
        return image, False
    resized = image.copy()
    resized.thumbnail(
        (settings.document_page_max_pixels, settings.document_page_max_pixels),
        Image.Resampling.LANCZOS,
    )
    return resized, True


def _open_pdf(raw: bytes, *, title: str) -> pdfium.PdfDocument:
    try:
        pdf = pdfium.PdfDocument(io.BytesIO(raw))
    except Exception as exc:
        raise AttachmentUnsupportedError(
            f"Document '{title}' could not be opened as a PDF: {exc}."
        ) from exc
    if len(pdf) == 0:
        raise AttachmentUnsupportedError(f"Document '{title}' has no pages.")
    return pdf


def _extract_all_page_texts(pdf: pdfium.PdfDocument) -> list[str]:
    return [extract_page_text(page) for page in pdf]


def _classify_tier(page_texts: Sequence[str]) -> Literal["text", "vision"]:
    """Classify a document Tier 1 (text) vs Tier 2 (vision) (ADR-0102 §1).

    Generalizes ``is_text_dense``'s per-string floor to a whole document by
    scaling the floor by page count.
    """
    total_chars = sum(len(t.strip()) for t in page_texts)
    floor = settings.document_text_density_floor_per_page * max(len(page_texts), 1)
    return "text" if total_chars >= floor else "vision"


def _build_text_block(full_text: str, *, title: str) -> tuple[dict[str, Any], str | None]:
    cap = settings.document_max_extracted_text_chars
    if len(full_text) > cap:
        return (
            {"type": "text", "text": full_text[:cap]},
            f"Extracted text for '{title}' was trimmed to {cap} characters (guardrail).",
        )
    return {"type": "text", "text": full_text}, None


def _build_native_pdf_block(
    raw: bytes,
    selected_pages: Sequence[int],
    *,
    title: str,
    running_total_bytes: int,
) -> tuple[dict[str, Any] | None, str | None]:
    sub_pdf = build_page_subset(raw, selected_pages)
    encoded = base64.b64encode(sub_pdf).decode("ascii")
    if running_total_bytes + len(encoded) > settings.document_max_total_payload_bytes:
        return None, (
            f"Document '{title}' was not included because it exceeds the total "
            "per-turn attachment payload limit."
        )
    block = {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
    }
    return block, None


def _rasterize_pages(
    pdf: pdfium.PdfDocument,
    selected_pages: Sequence[int],
    *,
    title: str,
    running_total_bytes: int,
) -> tuple[list[dict[str, Any]], list[str], int]:
    blocks: list[dict[str, Any]] = []
    disclosures: list[str] = []
    total = running_total_bytes

    for page_index in selected_pages:
        image = rasterize_page(pdf[page_index])
        image, was_downscaled = _downscale_page_if_needed(image)
        if was_downscaled:
            disclosures.append(
                f"Page {page_index + 1} of '{title}' was downscaled to fit the size limit."
            )

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")

        if len(encoded) > settings.document_page_max_bytes:
            disclosures.append(
                f"Page {page_index + 1} of '{title}' was dropped: too large even after downscaling."
            )
            continue

        if total + len(encoded) > settings.document_max_total_payload_bytes:
            remaining = len(selected_pages) - selected_pages.index(page_index)
            disclosures.append(
                f"{remaining} page(s) of '{title}' were dropped because the total "
                "per-turn attachment payload limit was reached."
            )
            break

        total += len(encoded)
        blocks.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
        )

    return blocks, disclosures, total


async def _resolve_one_document(
    attachment: AttachmentRef,
    *,
    resolve_tier2_delivery: Callable[[], Literal["native_pdf", "rasterize"]],
    running_total_bytes: int,
    remaining_page_budget: int,
    store: Any,
    trace_id: str | None,
    session_id: str | None,
    task_id: str | None,
) -> tuple[list[dict[str, Any]], list[str], int, int, bool, int, DocumentContinuationOffer | None]:
    """Resolve one PDF attachment.

    Returns ``(blocks, disclosures, running_total_bytes, remaining_page_budget,
    used_tier2, native_pdf_pages, continuation_offer)`` — the running values
    threaded across documents in the turn by the caller, mirroring the
    total-payload-byte accounting (ADR-0102 §4: the page budget is per-turn,
    not per-document). ``resolve_tier2_delivery`` is invoked at most once,
    only if this document classifies Tier 2 — a Tier-1 (text) document never
    calls it (ADR-0102 §1: Tier 1 works on any model). ``native_pdf_pages``
    is the count of pages actually delivered via a native-PDF ``document``
    block for this document (0 for text/rasterize/rejected — ADR-0102 §7b /
    FRE-686, threaded up so the caller can price a page-multiplied cost
    estimate). ``continuation_offer`` is non-``None`` whenever this document
    still has Tier-2 pages the per-turn budget did not include (ADR-0102 §4 /
    FRE-685) — set from either the salience auto-selection's dropped pages or
    a continuation request's own further-truncated remainder.

    If ``attachment.requested_pages`` is set (a continuation request for an
    already-stored document — ADR-0102 §4 / FRE-685), those exact 1-indexed
    pages are selected (clipped to the document's actual page count and the
    remaining budget) instead of running the salience selector — a
    continuation already knows exactly which pages it wants.
    """
    try:
        raw = await store.get(
            attachment.r2_key, trace_id=trace_id, session_id=session_id, task_id=task_id
        )
    except ArtifactStoreError as exc:
        raise AttachmentUnsupportedError(
            f"Document '{attachment.title}' could not be fetched from storage: {exc}."
        ) from exc

    pdf = await asyncio.to_thread(_open_pdf, raw, title=attachment.title)
    page_texts = await asyncio.to_thread(_extract_all_page_texts, pdf)
    tier = _classify_tier(page_texts)

    if tier == "text":
        full_text = "\n\n".join(page_texts)
        text_block, text_disclosure = _build_text_block(full_text, title=attachment.title)
        text_disclosures = [text_disclosure] if text_disclosure else []
        return (
            [text_block],
            text_disclosures,
            running_total_bytes,
            remaining_page_budget,
            False,
            0,
            None,
        )

    if remaining_page_budget <= 0:
        return (
            [],
            [
                f"Document '{attachment.title}' was not included: the per-turn page "
                "budget was already used by earlier attachments."
            ],
            running_total_bytes,
            remaining_page_budget,
            False,
            0,
            None,
        )

    budget_disclosures: list[str] = []
    continuation_offer: DocumentContinuationOffer | None = None

    if attachment.requested_pages:
        total_page_count = len(pdf)
        requested_0idx = sorted(
            {p - 1 for p in attachment.requested_pages if 1 <= p <= total_page_count}
        )
        if not requested_0idx:
            return (
                [],
                [
                    f"Document '{attachment.title}': the requested page range is out "
                    "of bounds; no pages included."
                ],
                running_total_bytes,
                remaining_page_budget,
                False,
                0,
                None,
            )
        selected_pages = requested_0idx[:remaining_page_budget]
        new_remaining_page_budget = remaining_page_budget - len(selected_pages)

        delivered_1idx = [p + 1 for p in selected_pages]
        budget_disclosures.append(
            f"'{attachment.title}': delivered pp. {_format_page_ranges(delivered_1idx)} "
            "(continuation)."
        )
        still_dropped_1idx = sorted({p + 1 for p in requested_0idx} - set(delivered_1idx))
        if still_dropped_1idx:
            budget_disclosures.append(
                f"'{attachment.title}': pp. {_format_page_ranges(still_dropped_1idx)} "
                "still not included (per-turn page budget) — ask again for that range."
            )
            continuation_offer = DocumentContinuationOffer(
                artifact_id=attachment.artifact_id,
                content_type=attachment.content_type,
                title=attachment.title,
                r2_key=attachment.r2_key,
                processing_target=attachment.processing_target,
                dropped_pages=tuple(still_dropped_1idx),
            )
    else:
        scores = await asyncio.to_thread(compute_page_scores, pdf)
        outline = get_outline_page_indices(pdf)
        selected_pages = _select_pages(scores, remaining_page_budget, outline)
        new_remaining_page_budget = remaining_page_budget - len(selected_pages)

        if len(scores) > len(selected_pages):
            included_1idx = [p + 1 for p in selected_pages]
            dropped_1idx = sorted(set(range(1, len(scores) + 1)) - set(included_1idx))
            budget_disclosures.append(
                f"'{attachment.title}': included pp. {_format_page_ranges(included_1idx)}; "
                f"pp. {_format_page_ranges(dropped_1idx)} not included (per-turn page "
                "budget) — ask for that page range to see it next."
            )
            continuation_offer = DocumentContinuationOffer(
                artifact_id=attachment.artifact_id,
                content_type=attachment.content_type,
                title=attachment.title,
                r2_key=attachment.r2_key,
                processing_target=attachment.processing_target,
                dropped_pages=tuple(dropped_1idx),
            )

    tier2_delivery = resolve_tier2_delivery()

    if tier2_delivery == "native_pdf":
        native_block, reject_disclosure = await asyncio.to_thread(
            _build_native_pdf_block,
            raw,
            selected_pages,
            title=attachment.title,
            running_total_bytes=running_total_bytes,
        )
        if native_block is None:
            return (
                [],
                [*budget_disclosures, reject_disclosure]
                if reject_disclosure
                else budget_disclosures,
                running_total_bytes,
                new_remaining_page_budget,
                True,
                0,
                continuation_offer,
            )
        encoded_len = len(native_block["source"]["data"])
        return (
            [native_block],
            budget_disclosures,
            running_total_bytes + encoded_len,
            new_remaining_page_budget,
            True,
            len(selected_pages),
            continuation_offer,
        )

    page_blocks, page_disclosures, new_total = await asyncio.to_thread(
        _rasterize_pages,
        pdf,
        selected_pages,
        title=attachment.title,
        running_total_bytes=running_total_bytes,
    )
    return (
        page_blocks,
        [*budget_disclosures, *page_disclosures],
        new_total,
        new_remaining_page_budget,
        True,
        0,
        continuation_offer,
    )


async def resolve_documents(
    attachments: Sequence[AttachmentRef],
    *,
    resolve_tier2_delivery: Callable[[], Literal["native_pdf", "rasterize"]],
    trace_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> ResolvedDocuments:
    """Resolve a turn's PDF attachments into tiered, guardrailed content blocks.

    Fetches bytes via the credentialed R2 ``store.get`` path, classifies each
    document as Tier 1 (text extraction) or Tier 2 (vision) by aggregate text
    density, and builds the appropriate block(s). Non-PDF attachments are
    silently ignored — every other content type is
    ``attachment_resolution.py``'s concern, not this module's.

    Args:
        attachments: The turn's structured attachment carrier (FRE-661).
        resolve_tier2_delivery: Callback returning which Tier-2 content-block
            shape to build (``"native_pdf"`` or ``"rasterize"``). Invoked
            lazily, at most once per document that actually classifies Tier 2
            — never for a Tier-1 (text) document (ADR-0102 §1: Tier 1 must
            work on any model, so no capability check may fire for it). T4
            (FRE-684) supplies this, reading model capability from the
            executor's routing seam — this module never decides it and never
            calls it speculatively.
        trace_id: Originating request trace_id, threaded onto ``store.get``
            and resolution/failure logs (ADR-0074 identity threading).
        session_id: Originating session id, threaded onto ``store.get`` and
            resolution/failure logs.
        task_id: Sub-agent task id, threaded onto ``store.get`` and
            resolution/failure logs — ``None`` at the turn level.

    Returns:
        ``ResolvedDocuments`` with the surviving blocks, any disclosure
        strings for trimmed/dropped/rejected content, and ``used_tier2``
        recording whether ``resolve_tier2_delivery`` was ever invoked.

    Raises:
        AttachmentUnsupportedError: R2 storage is not configured, a PDF fails
            to open, a PDF has zero pages, or ``resolve_tier2_delivery``
            itself raises it (a capability/routing fail-closed decision).
    """
    documents = [a for a in attachments if a.content_type in PDF_CONTENT_TYPES]
    if not documents:
        log.info(
            "document_resolution_completed",
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            attachment_count=len(attachments),
            resolved_count=0,
            disclosure_count=0,
        )
        return ResolvedDocuments(
            blocks=(),
            disclosures=(),
            used_tier2=False,
            native_pdf_page_count=0,
            continuation_offers=(),
        )

    store = get_artifact_store()
    if store is None:
        log.warning(
            "document_resolution_failed",
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            artifact_id=None,
            reason="store_unconfigured",
        )
        raise AttachmentUnsupportedError(
            "Document attachments cannot be processed: artifact storage is not configured."
        )

    blocks: list[dict[str, Any]] = []
    disclosures: list[str] = []
    running_total_bytes = 0
    remaining_page_budget = min(settings.document_max_pages_per_turn, _PROVIDER_MAX_PAGES)
    used_tier2 = False
    native_pdf_page_count = 0
    continuation_offers: list[DocumentContinuationOffer] = []

    for attachment in documents:
        try:
            (
                doc_blocks,
                doc_disclosures,
                running_total_bytes,
                remaining_page_budget,
                doc_used_tier2,
                doc_native_pdf_pages,
                doc_continuation_offer,
            ) = await _resolve_one_document(
                attachment,
                resolve_tier2_delivery=resolve_tier2_delivery,
                running_total_bytes=running_total_bytes,
                remaining_page_budget=remaining_page_budget,
                store=store,
                trace_id=trace_id,
                session_id=session_id,
                task_id=task_id,
            )
            used_tier2 = used_tier2 or doc_used_tier2
            native_pdf_page_count += doc_native_pdf_pages
            if doc_continuation_offer is not None:
                continuation_offers.append(doc_continuation_offer)
        except AttachmentUnsupportedError:
            log.warning(
                "document_resolution_failed",
                trace_id=trace_id,
                session_id=session_id,
                task_id=task_id,
                artifact_id=attachment.artifact_id,
                reason="resolution_error",
            )
            raise
        blocks.extend(doc_blocks)
        disclosures.extend(doc_disclosures)

    log.info(
        "document_resolution_completed",
        trace_id=trace_id,
        session_id=session_id,
        task_id=task_id,
        attachment_count=len(attachments),
        resolved_count=len(blocks),
        disclosure_count=len(disclosures),
    )
    return ResolvedDocuments(
        blocks=tuple(blocks),
        disclosures=tuple(disclosures),
        used_tier2=used_tier2,
        native_pdf_page_count=native_pdf_page_count,
        continuation_offers=tuple(continuation_offers),
    )
