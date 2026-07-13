"""Resolve current-turn PDF attachments to content blocks (ADR-0102 T3, FRE-683).

Tiered selector (ADR-0102 §1): text extraction (Tier 1) when the document's
aggregate text density is at or above a configured floor, else vision (Tier 2).
Content-block construction (§4) for Tier 2 delivers either a native PDF
document block or rasterized image blocks, reusing the ADR-0101 image block
path. Page budget + salience selection (§4) and the four guardrail dimensions
(§5) are enforced fail-closed, mirroring ``attachment_resolution.py``'s
transform-or-reject-with-disclosure pattern.

**Scope boundary:** ``tier2_delivery`` is caller-supplied, not computed here.
Routing/delivery selection — reading ``ModelDefinition.supports_pdf_document``
/ ``supports_vision`` and deciding which mode to request, with fail-closed
escalation — is T4 (FRE-684), wired into the ``executor.py`` routing seam.
This module never touches ``ExecutionContext`` or ``user_message``.
"""

from __future__ import annotations

import asyncio
import base64
import io
from collections.abc import Sequence
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
from personal_agent.orchestrator.types import AttachmentRef
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
    """

    blocks: tuple[dict[str, Any], ...]
    disclosures: tuple[str, ...]


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
    tier2_delivery: Literal["native_pdf", "rasterize"],
    running_total_bytes: int,
    remaining_page_budget: int,
    store: Any,
    trace_id: str | None,
    session_id: str | None,
    task_id: str | None,
) -> tuple[list[dict[str, Any]], list[str], int, int]:
    """Resolve one PDF attachment.

    Returns ``(blocks, disclosures, running_total_bytes, remaining_page_budget)``
    — both running values threaded across documents in the turn by the caller,
    mirroring the total-payload-byte accounting (ADR-0102 §4: the page budget
    is per-turn, not per-document).
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
        return [text_block], text_disclosures, running_total_bytes, remaining_page_budget

    if remaining_page_budget <= 0:
        return (
            [],
            [
                f"Document '{attachment.title}' was not included: the per-turn page "
                "budget was already used by earlier attachments."
            ],
            running_total_bytes,
            remaining_page_budget,
        )

    scores = await asyncio.to_thread(compute_page_scores, pdf)
    outline = get_outline_page_indices(pdf)
    selected_pages = _select_pages(scores, remaining_page_budget, outline)
    new_remaining_page_budget = remaining_page_budget - len(selected_pages)

    budget_disclosures: list[str] = []
    if len(scores) > len(selected_pages):
        skipped = len(scores) - len(selected_pages)
        budget_disclosures.append(
            f"{skipped} of {len(scores)} page(s) of '{attachment.title}' were not "
            "included (per-turn page budget)."
        )

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
            )
        encoded_len = len(native_block["source"]["data"])
        return (
            [native_block],
            budget_disclosures,
            running_total_bytes + encoded_len,
            new_remaining_page_budget,
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
    )


async def resolve_documents(
    attachments: Sequence[AttachmentRef],
    *,
    tier2_delivery: Literal["native_pdf", "rasterize"],
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
        tier2_delivery: Which Tier-2 content-block shape to build when a
            document classifies as vision. Caller-supplied (T4 computes this
            from model capability) — this module never decides it.
        trace_id: Originating request trace_id, threaded onto ``store.get``
            and resolution/failure logs (ADR-0074 identity threading).
        session_id: Originating session id, threaded onto ``store.get`` and
            resolution/failure logs.
        task_id: Sub-agent task id, threaded onto ``store.get`` and
            resolution/failure logs — ``None`` at the turn level.

    Returns:
        ``ResolvedDocuments`` with the surviving blocks and any disclosure
        strings for trimmed/dropped/rejected content.

    Raises:
        AttachmentUnsupportedError: R2 storage is not configured, a PDF fails
            to open, or a PDF has zero pages.
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
        return ResolvedDocuments(blocks=(), disclosures=())

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

    for attachment in documents:
        try:
            (
                doc_blocks,
                doc_disclosures,
                running_total_bytes,
                remaining_page_budget,
            ) = await _resolve_one_document(
                attachment,
                tier2_delivery=tier2_delivery,
                running_total_bytes=running_total_bytes,
                remaining_page_budget=remaining_page_budget,
                store=store,
                trace_id=trace_id,
                session_id=session_id,
                task_id=task_id,
            )
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
    return ResolvedDocuments(blocks=tuple(blocks), disclosures=tuple(disclosures))
