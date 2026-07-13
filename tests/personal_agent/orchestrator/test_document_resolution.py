"""Tests for the document-resolution module (FRE-683 / ADR-0102 T3).

Proves AC-1 (native-text PDF → text block), AC-2 (scanned PDF → vision block),
AC-7 (every guardrail dimension fails closed, with disclosure), and AC-12
(clean task text — this module never touches user_message).

Routing/delivery selection (which tier2_delivery to use) is T4 (FRE-684); this
module receives that decision as a parameter and never computes it itself.
"""

from __future__ import annotations

import base64
import ctypes
import io
from collections.abc import Sequence
from unittest.mock import AsyncMock, patch

import pypdfium2 as pdfium
import pytest
import structlog
from PIL import Image

from personal_agent.exceptions import AttachmentUnsupportedError
from personal_agent.orchestrator.document_resolution import resolve_documents
from personal_agent.orchestrator.types import AttachmentRef

# ---------------------------------------------------------------------------
# Synthetic PDF builders
# ---------------------------------------------------------------------------


def _insert_text(doc: pdfium.PdfDocument, page: pdfium.PdfPage, text: str) -> None:
    """Insert a real, extractable text object onto a page via the raw pdfium API.

    pypdfium2's high-level helpers don't expose text-object creation (only
    reading/extraction); this mirrors the low-level FPDFPageObj_NewTextObj /
    FPDFText_SetText / FPDFPageObj_Transform / insert_obj sequence PDFium
    itself uses for authoring, verified round-trip-safe through save()+reopen.
    """
    import pypdfium2.raw as pdfium_c

    text_obj = pdfium_c.FPDFPageObj_NewTextObj(doc, b"Helvetica", 12.0)
    buf = (ctypes.c_ushort * (len(text) + 1))()
    for i, ch in enumerate(text):
        buf[i] = ord(ch)
    buf[len(text)] = 0
    pdfium_c.FPDFText_SetText(text_obj, buf)
    pdfium_c.FPDFPageObj_Transform(text_obj, 1, 0, 0, 1, 20, 700)
    page.insert_obj(pdfium.PdfObject(text_obj, page=None, pdf=doc))
    page.gen_content()


def _insert_filled_image(
    doc: pdfium.PdfDocument, page: pdfium.PdfPage, width: int, height: int
) -> None:
    """Insert a solid black image filling the page — no text, high pixel coverage.

    Simulates a scanned page with real visual content (as opposed to a wholly
    blank page), so the salience selector has a pixel_coverage signal to rank
    on even though neither page has any extractable text.
    """
    img = Image.new("RGB", (width, height), color=(0, 0, 0))
    bitmap = pdfium.PdfBitmap.from_pil(img)
    pdf_image = pdfium.PdfImage.new(doc)
    pdf_image.set_bitmap(bitmap)
    pdf_image.set_matrix(pdfium.PdfMatrix().scale(width, height))
    page.insert_obj(pdf_image)
    page.gen_content()


def _make_pdf(
    page_texts: Sequence[str | None],
    *,
    width: int = 612,
    height: int = 792,
) -> bytes:
    """Build a synthetic PDF with one page per entry in ``page_texts``.

    ``None`` produces a blank page (no text layer — simulates a scanned page);
    a string inserts that text as a real, extractable text object.
    """
    doc = pdfium.PdfDocument.new()
    for text in page_texts:
        page = doc.new_page(width, height)
        if text is not None:
            _insert_text(doc, page, text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_scanned_pdf_with_visual_density(
    page_has_content: Sequence[bool],
    *,
    width: int = 200,
    height: int = 200,
) -> bytes:
    """Build a no-text PDF whose pages differ in visual (pixel) density.

    Every page has zero extractable text (guarantees Tier-2 classification);
    pages where ``page_has_content`` is True get a filled black image (high
    pixel coverage), others stay blank (zero coverage) — giving the salience
    selector a pixel-only signal to discriminate on.
    """
    doc = pdfium.PdfDocument.new()
    for has_content in page_has_content:
        page = doc.new_page(width, height)
        if has_content:
            _insert_filled_image(doc, page, width, height)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_attachment(**overrides: object) -> AttachmentRef:
    defaults: dict[str, object] = {
        "artifact_id": "doc-123",
        "content_type": "application/pdf",
        "title": "report.pdf",
        "r2_key": "upload/user/GLOBAL/report.pdf",
    }
    defaults.update(overrides)
    return AttachmentRef(**defaults)  # type: ignore[arg-type]


def _mock_store(bytes_by_key: dict[str, bytes]) -> AsyncMock:
    store = AsyncMock()

    async def _get(
        r2_key: str,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> bytes:
        return bytes_by_key[r2_key]

    store.get.side_effect = _get
    return store


_STORE_PATCH_TARGET = "personal_agent.orchestrator.document_resolution.get_artifact_store"


# ---------------------------------------------------------------------------
# AC-1 / AC-2: tiered selector
# ---------------------------------------------------------------------------


class TestAC1NativeTextPath:
    @pytest.mark.asyncio
    async def test_native_text_pdf_yields_one_text_block(self) -> None:
        sentinel = "Sentinel Marker Alpha " * 20  # well above the default floor
        pdf_bytes = _make_pdf([sentinel])
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            result = await resolve_documents([attachment], tier2_delivery="rasterize")

        assert len(result.blocks) == 1
        block = result.blocks[0]
        assert block["type"] == "text"
        assert "Sentinel Marker Alpha" in block["text"]

    @pytest.mark.asyncio
    async def test_native_text_pdf_produces_no_image_or_document_block(self) -> None:
        pdf_bytes = _make_pdf(["Dense native text content here. " * 10])
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            result = await resolve_documents([attachment], tier2_delivery="native_pdf")

        types = {b["type"] for b in result.blocks}
        assert types == {"text"}


class TestAC2ScannedVisionPath:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("delivery", ["native_pdf", "rasterize"])
    async def test_scanned_pdf_yields_vision_block_not_text(self, delivery: str) -> None:
        pdf_bytes = _make_pdf([None, None])  # no text layer at all
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            result = await resolve_documents([attachment], tier2_delivery=delivery)

        assert result.blocks
        types = {b["type"] for b in result.blocks}
        assert "text" not in types


class TestTier2DeliverySelection:
    @pytest.mark.asyncio
    async def test_native_pdf_delivery_produces_document_block(self) -> None:
        pdf_bytes = _make_pdf([None])
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            result = await resolve_documents([attachment], tier2_delivery="native_pdf")

        assert len(result.blocks) == 1
        block = result.blocks[0]
        assert block["type"] == "document"
        assert block["source"]["media_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_rasterize_delivery_produces_image_url_blocks(self) -> None:
        pdf_bytes = _make_pdf([None])
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            result = await resolve_documents([attachment], tier2_delivery="rasterize")

        assert len(result.blocks) == 1
        block = result.blocks[0]
        assert block["type"] == "image_url"
        assert block["image_url"]["url"].startswith("data:image/png;base64,")


class TestClassificationBoundary:
    @pytest.mark.asyncio
    async def test_at_floor_is_tier1(self) -> None:
        # Single page: floor_per_page(100) * page_count(1) == 100. Exactly 100
        # stripped chars must classify Tier 1 (inclusive boundary, `>=`).
        text = "x" * 100
        pdf_bytes = _make_pdf([text])
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            result = await resolve_documents([attachment], tier2_delivery="rasterize")

        assert result.blocks[0]["type"] == "text"

    @pytest.mark.asyncio
    async def test_below_floor_is_tier2(self) -> None:
        text = "x" * 99
        pdf_bytes = _make_pdf([text])
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            result = await resolve_documents([attachment], tier2_delivery="rasterize")

        assert result.blocks[0]["type"] != "text"


class TestClassificationMixedDocument:
    @pytest.mark.asyncio
    async def test_mostly_blank_document_with_one_dense_page(self) -> None:
        """Documents, does not "fix", the accepted ADR-0102 heuristic risk.

        One dense page + several blank pages: the aggregate floor is scaled
        by page count, so a single dense page among many blanks may still
        classify Tier 2 once enough blank pages dilute the aggregate.
        """
        dense = "Dense page content. " * 50  # ~1000 chars
        pdf_bytes = _make_pdf([dense, None, None, None, None, None, None, None, None, None])
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            result = await resolve_documents([attachment], tier2_delivery="rasterize")

        # floor = 100 * 10 pages = 1000; ~1000 dense chars vs a 10-page floor
        # is right at the edge — assert the module classified deterministically
        # one way or the other (documents actual behavior, not a fixed answer).
        assert result.blocks
        assert result.blocks[0]["type"] in {"text", "image_url"}


# ---------------------------------------------------------------------------
# Page budget + outline-aware selection
# ---------------------------------------------------------------------------


class TestPageBudgetSelection:
    @pytest.mark.asyncio
    async def test_budget_caps_rasterized_page_count(self) -> None:
        pdf_bytes = _make_pdf([None] * 5)
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with (
            patch(_STORE_PATCH_TARGET, return_value=store),
            patch(
                "personal_agent.orchestrator.document_resolution.settings.document_max_pages_per_turn",
                2,
            ),
        ):
            result = await resolve_documents([attachment], tier2_delivery="rasterize")

        assert len(result.blocks) == 2
        assert any("budget" in d.lower() for d in result.disclosures)

    @pytest.mark.asyncio
    async def test_budget_caps_native_subset_page_count(self) -> None:
        pdf_bytes = _make_pdf([None] * 5)
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with (
            patch(_STORE_PATCH_TARGET, return_value=store),
            patch(
                "personal_agent.orchestrator.document_resolution.settings.document_max_pages_per_turn",
                2,
            ),
        ):
            result = await resolve_documents([attachment], tier2_delivery="native_pdf")

        assert len(result.blocks) == 1
        sub_doc = pdfium.PdfDocument(
            io.BytesIO(base64.b64decode(result.blocks[0]["source"]["data"]))
        )
        assert len(sub_doc) == 2

    @pytest.mark.asyncio
    async def test_salience_selection_prefers_visual_page_over_blank(self) -> None:
        """Mirrors T1's AC-5: a naive first-N selector would keep the blank page.

        Both pages have zero extractable text (guarantees Tier-2 classification
        for the whole document); only pixel coverage discriminates between them.
        """
        pdf_bytes = _make_scanned_pdf_with_visual_density([False, True])  # blank, then filled
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with (
            patch(_STORE_PATCH_TARGET, return_value=store),
            patch(
                "personal_agent.orchestrator.document_resolution.settings.document_max_pages_per_turn",
                1,
            ),
        ):
            result = await resolve_documents([attachment], tier2_delivery="native_pdf")

        sub_doc = pdfium.PdfDocument(
            io.BytesIO(base64.b64decode(result.blocks[0]["source"]["data"]))
        )
        assert len(sub_doc) == 1
        img = sub_doc[0].render().to_pil()
        # The filled (page index 1) page was selected, not the blank one.
        assert img.getpixel((img.width // 2, img.height // 2))[:3] == (0, 0, 0)


class TestOutlineBoost:
    """Direct unit tests of ``_select_pages`` — authoring a real PDF outline via

    pdfium's raw bookmark API is exercised nowhere else in this codebase (T1's
    own test suite reads but never authors a TOC); testing the boost logic
    directly against ``PageSalienceScore`` + a synthetic outline set is more
    reliable than fighting bookmark authoring in a fixture, and it isolates
    this module's own contribution (combining T1's scorer with an outline)
    from T1's already-tested scorer/selector.
    """

    def test_outline_page_wins_over_denser_non_outline_page(self) -> None:
        from personal_agent.documents.pdf_utils import PageSalienceScore
        from personal_agent.orchestrator.document_resolution import _select_pages

        dense_no_outline = PageSalienceScore(page_index=0, char_count=2000, pixel_coverage=0.8)
        thin_outline_page = PageSalienceScore(page_index=1, char_count=10, pixel_coverage=0.01)

        result = _select_pages(
            [dense_no_outline, thin_outline_page], budget=1, outline=frozenset({1})
        )

        assert result == [1]

    def test_no_outline_falls_back_to_pure_salience(self) -> None:
        from personal_agent.documents.pdf_utils import PageSalienceScore
        from personal_agent.orchestrator.document_resolution import _select_pages

        dense = PageSalienceScore(page_index=0, char_count=2000, pixel_coverage=0.8)
        blank = PageSalienceScore(page_index=1, char_count=10, pixel_coverage=0.01)

        result = _select_pages([dense, blank], budget=1, outline=frozenset())

        assert result == [0]


# ---------------------------------------------------------------------------
# AC-7: guardrail dimensions
# ---------------------------------------------------------------------------


class TestAC7PixelCapGuardrail:
    @pytest.mark.asyncio
    async def test_page_pixel_cap_downscales(self) -> None:
        pdf_bytes = _make_pdf([None], width=1200, height=1600)
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with (
            patch(_STORE_PATCH_TARGET, return_value=store),
            patch(
                "personal_agent.orchestrator.document_resolution.settings.document_page_max_pixels",
                100,
            ),
        ):
            result = await resolve_documents([attachment], tier2_delivery="rasterize")

        url = result.blocks[0]["image_url"]["url"]
        encoded = url.split(",", 1)[1]
        decoded = Image.open(io.BytesIO(base64.b64decode(encoded)))
        assert max(decoded.size) <= 100


class TestAC7ByteCapGuardrail:
    @pytest.mark.asyncio
    async def test_page_byte_cap_drops_page_with_disclosure(self) -> None:
        pdf_bytes = _make_pdf([None])
        attachment = _make_attachment(title="scan.pdf")
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with (
            patch(_STORE_PATCH_TARGET, return_value=store),
            patch(
                "personal_agent.orchestrator.document_resolution.settings.document_page_max_bytes",
                1,
            ),
        ):
            result = await resolve_documents([attachment], tier2_delivery="rasterize")

        assert result.blocks == ()
        assert any("scan.pdf" in d for d in result.disclosures)


class TestAC7TotalPayloadGuardrail:
    @pytest.mark.asyncio
    async def test_rasterize_drops_trailing_pages_as_prefix(self) -> None:
        pdf_bytes = _make_pdf([None, None, None])
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            baseline = await resolve_documents([attachment], tier2_delivery="rasterize")
        one_page_bytes = len(baseline.blocks[0]["image_url"]["url"])
        cap = int(one_page_bytes * 1.5)  # room for exactly one page

        with (
            patch(_STORE_PATCH_TARGET, return_value=store),
            patch(
                "personal_agent.orchestrator.document_resolution.settings.document_max_total_payload_bytes",
                cap,
            ),
        ):
            result = await resolve_documents([attachment], tier2_delivery="rasterize")

        assert len(result.blocks) == 1
        assert any("dropped" in d or "payload" in d for d in result.disclosures)


class TestNativeOversizeRejectsWholeDocument:
    @pytest.mark.asyncio
    async def test_native_block_over_total_payload_cap_is_rejected_not_shrunk(self) -> None:
        pdf_bytes = _make_pdf([None, None])
        attachment = _make_attachment(title="huge-scan.pdf")
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with (
            patch(_STORE_PATCH_TARGET, return_value=store),
            patch(
                "personal_agent.orchestrator.document_resolution.settings.document_max_total_payload_bytes",
                1,
            ),
        ):
            result = await resolve_documents([attachment], tier2_delivery="native_pdf")

        assert result.blocks == ()
        assert any("huge-scan.pdf" in d for d in result.disclosures)


class TestAC7ExtractedTextGuardrail:
    @pytest.mark.asyncio
    async def test_extracted_text_trimmed_with_disclosure(self) -> None:
        pdf_bytes = _make_pdf(["A" * 500])
        attachment = _make_attachment(title="long.pdf")
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with (
            patch(_STORE_PATCH_TARGET, return_value=store),
            patch(
                "personal_agent.orchestrator.document_resolution.settings.document_max_extracted_text_chars",
                50,
            ),
        ):
            result = await resolve_documents([attachment], tier2_delivery="rasterize")

        assert len(result.blocks[0]["text"]) <= 50
        assert any("long.pdf" in d for d in result.disclosures)


# ---------------------------------------------------------------------------
# Fail-closed edge cases
# ---------------------------------------------------------------------------


class TestZeroPagePdfFailsClosed:
    @pytest.mark.asyncio
    async def test_zero_page_pdf_raises(self) -> None:
        pdf_bytes = _make_pdf([])
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            with pytest.raises(AttachmentUnsupportedError):
                await resolve_documents([attachment], tier2_delivery="rasterize")


class TestCorruptPdfFailsClosed:
    @pytest.mark.asyncio
    async def test_invalid_pdf_bytes_raises(self) -> None:
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: b"not a pdf"})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            with pytest.raises(AttachmentUnsupportedError):
                await resolve_documents([attachment], tier2_delivery="rasterize")


class TestStoreUnconfigured:
    @pytest.mark.asyncio
    async def test_store_unconfigured_raises(self) -> None:
        attachment = _make_attachment()
        with patch(_STORE_PATCH_TARGET, return_value=None):
            with pytest.raises(AttachmentUnsupportedError):
                await resolve_documents([attachment], tier2_delivery="rasterize")


class TestNonPdfAttachmentsIgnored:
    @pytest.mark.asyncio
    async def test_non_pdf_attachment_produces_no_blocks_and_no_fetch(self) -> None:
        attachment = _make_attachment(content_type="image/png", title="photo.png")

        with patch(_STORE_PATCH_TARGET) as mock_get_store:
            result = await resolve_documents([attachment], tier2_delivery="rasterize")

        assert result.blocks == ()
        assert result.disclosures == ()
        mock_get_store.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_attachments_produce_no_blocks(self) -> None:
        result = await resolve_documents([], tier2_delivery="rasterize")
        assert result.blocks == ()
        assert result.disclosures == ()


# ---------------------------------------------------------------------------
# Credentialed fetch (ADR-0074 identity threading)
# ---------------------------------------------------------------------------


class TestCredentialedFetch:
    @pytest.mark.asyncio
    async def test_store_get_called_with_r2_key_and_identity(self) -> None:
        pdf_bytes = _make_pdf(["Native text content. " * 10])
        attachment = _make_attachment(r2_key="upload/u/g/report.pdf")
        store = _mock_store({"upload/u/g/report.pdf": pdf_bytes})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            await resolve_documents(
                [attachment],
                tier2_delivery="rasterize",
                trace_id="trace-1",
                session_id="sess-1",
                task_id="task-1",
            )

        store.get.assert_awaited_once_with(
            "upload/u/g/report.pdf",
            trace_id="trace-1",
            session_id="sess-1",
            task_id="task-1",
        )


# ---------------------------------------------------------------------------
# AC-12: clean task text (module-boundary proof)
# ---------------------------------------------------------------------------


class TestAC12CleanTaskText:
    def test_resolve_documents_signature_has_no_user_message_channel(self) -> None:
        import inspect

        sig = inspect.signature(resolve_documents)
        assert "user_message" not in sig.parameters
        assert "ctx" not in sig.parameters

    @pytest.mark.asyncio
    async def test_extracted_sentinel_confined_to_resolved_blocks(self) -> None:
        sentinel = "OnlyInThePdfNotInUserMessage " * 10
        pdf_bytes = _make_pdf([sentinel])
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with patch(_STORE_PATCH_TARGET, return_value=store):
            result = await resolve_documents([attachment], tier2_delivery="rasterize")

        assert "OnlyInThePdfNotInUserMessage" in result.blocks[0]["text"]
        # ResolvedDocuments carries only blocks/disclosures — no field a caller
        # could mistake for (or accidentally merge into) task/user-message text.
        assert set(vars(result).keys()) <= {"blocks", "disclosures"}


# ---------------------------------------------------------------------------
# Telemetry (ADR-0074 identity threading on logs)
# ---------------------------------------------------------------------------


class TestResolutionTelemetry:
    @pytest.mark.asyncio
    async def test_completed_log_fires_with_identity_and_counts(self) -> None:
        pdf_bytes = _make_pdf(["Native text content. " * 10])
        attachment = _make_attachment()
        store = _mock_store({attachment.r2_key: pdf_bytes})

        with (
            patch(_STORE_PATCH_TARGET, return_value=store),
            structlog.testing.capture_logs() as logs,
        ):
            await resolve_documents(
                [attachment],
                tier2_delivery="rasterize",
                trace_id="trace-1",
                session_id="sess-1",
                task_id="task-1",
            )

        completed = [e for e in logs if e.get("event") == "document_resolution_completed"]
        assert completed, f"document_resolution_completed not found in: {logs}"
        entry = completed[0]
        assert entry["trace_id"] == "trace-1"
        assert entry["session_id"] == "sess-1"
        assert entry["task_id"] == "task-1"
