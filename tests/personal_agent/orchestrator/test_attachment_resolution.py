"""Tests for the attachment-resolution module (FRE-666 / ADR-0101 §3, §4, §6).

Proves AC-2 (credentialed fetch, never CF Access), AC-3 (typed image block), and
AC-7 (every guardrail dimension fails closed, with disclosure on downscale/drop).
"""

from __future__ import annotations

import base64
from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from personal_agent.exceptions import AttachmentUnsupportedError
from personal_agent.orchestrator.attachment_resolution import (
    _KNOWN_NON_RASTER_CONTENT_TYPES,
    RASTER_CONTENT_TYPES,
    resolve_attachments,
)
from personal_agent.orchestrator.types import AttachmentRef
from personal_agent.service.uploads_router import ALLOWED_UPLOAD_CONTENT_TYPES


def _make_attachment(**overrides: object) -> AttachmentRef:
    defaults: dict[str, object] = {
        "artifact_id": "abc-123",
        "content_type": "image/png",
        "title": "photo.png",
        "r2_key": "upload/user/GLOBAL/abc.png",
    }
    defaults.update(overrides)
    return AttachmentRef(**defaults)  # type: ignore[arg-type]


def _make_png_bytes(
    size: tuple[int, int] = (10, 10), color: tuple[int, int, int] = (255, 0, 0)
) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _mock_store(bytes_by_key: dict[str, bytes]) -> AsyncMock:
    store = AsyncMock()

    async def _get(r2_key: str, *, trace_id: str | None = None) -> bytes:
        return bytes_by_key[r2_key]

    store.get.side_effect = _get
    return store


class TestNonRasterHandling:
    @pytest.mark.asyncio
    async def test_known_non_raster_attachments_produce_no_blocks(self) -> None:
        attachment = _make_attachment(content_type="application/pdf", r2_key="upload/u/g/doc.pdf")
        with patch(
            "personal_agent.orchestrator.attachment_resolution.get_artifact_store"
        ) as mock_get_store:
            result = await resolve_attachments([attachment])
        assert result.blocks == ()
        assert result.disclosures == ()
        mock_get_store.assert_not_called()

    @pytest.mark.asyncio
    async def test_unrecognized_content_type_rejected(self) -> None:
        attachment = _make_attachment(content_type="image/tiff")
        with pytest.raises(AttachmentUnsupportedError):
            await resolve_attachments([attachment])

    def test_raster_and_non_raster_sets_cover_the_upload_allowlist(self) -> None:
        assert (
            RASTER_CONTENT_TYPES | _KNOWN_NON_RASTER_CONTENT_TYPES == ALLOWED_UPLOAD_CONTENT_TYPES
        )

    @pytest.mark.asyncio
    async def test_empty_attachments_produce_no_blocks(self) -> None:
        result = await resolve_attachments([])
        assert result.blocks == ()
        assert result.disclosures == ()


class TestCredentialedFetch:
    @pytest.mark.asyncio
    async def test_store_get_called_with_r2_key_and_trace_id(self) -> None:
        """AC-2: bytes come from store.get(r2_key), never a public URL fetch."""
        png = _make_png_bytes()
        attachment = _make_attachment(r2_key="upload/u/g/photo.png")
        store = _mock_store({"upload/u/g/photo.png": png})

        with (
            patch(
                "personal_agent.orchestrator.attachment_resolution.get_artifact_store",
                return_value=store,
            ),
            patch("httpx.AsyncClient") as mock_httpx,
        ):
            await resolve_attachments([attachment], trace_id="trace-1")

        store.get.assert_awaited_once_with("upload/u/g/photo.png", trace_id="trace-1")
        mock_httpx.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_unconfigured_raises(self) -> None:
        attachment = _make_attachment()
        with patch(
            "personal_agent.orchestrator.attachment_resolution.get_artifact_store",
            return_value=None,
        ):
            with pytest.raises(AttachmentUnsupportedError):
                await resolve_attachments([attachment])


class TestBlockShape:
    @pytest.mark.asyncio
    async def test_resolved_block_is_openai_image_url_shape(self) -> None:
        """AC-3 (unit level): resolved block is a typed image_url block, not text."""
        png = _make_png_bytes()
        attachment = _make_attachment(r2_key="upload/u/g/photo.png")
        store = _mock_store({"upload/u/g/photo.png": png})

        with patch(
            "personal_agent.orchestrator.attachment_resolution.get_artifact_store",
            return_value=store,
        ):
            result = await resolve_attachments([attachment])

        assert len(result.blocks) == 1
        block = result.blocks[0]
        assert block["type"] == "image_url"
        assert block["image_url"]["url"].startswith("data:image/png;base64,")


class TestPixelCapGuardrail:
    @pytest.mark.asyncio
    async def test_pixel_cap_downscales_and_discloses(self) -> None:
        """AC-7 dimension 1: over-limit pixel dimension downscales and discloses."""
        png = _make_png_bytes(size=(200, 100))
        attachment = _make_attachment(title="wide.png", r2_key="upload/u/g/wide.png")
        store = _mock_store({"upload/u/g/wide.png": png})

        with (
            patch(
                "personal_agent.orchestrator.attachment_resolution.get_artifact_store",
                return_value=store,
            ),
            patch(
                "personal_agent.orchestrator.attachment_resolution.settings.attachment_image_max_pixels",
                50,
            ),
        ):
            result = await resolve_attachments([attachment])

        assert len(result.blocks) == 1
        url = result.blocks[0]["image_url"]["url"]
        encoded = url.split(",", 1)[1]
        decoded = Image.open(BytesIO(base64.b64decode(encoded)))
        assert max(decoded.size) <= 50
        assert any("wide.png" in d for d in result.disclosures)


class TestByteCapGuardrail:
    @pytest.mark.asyncio
    async def test_byte_cap_rejects_after_downscale(self) -> None:
        """AC-7 dimension 2: cap is compared against the base64-encoded length, not raw bytes."""
        png = _make_png_bytes()
        raw_len = len(png)
        encoded_len = len(base64.b64encode(png))
        assert raw_len < encoded_len  # sanity: base64 inflates size

        attachment = _make_attachment(title="huge.png", r2_key="upload/u/g/huge.png")
        store = _mock_store({"upload/u/g/huge.png": png})

        # Cap sits strictly between the raw and encoded length: only an
        # encoded-size check catches this, a raw-byte check would not.
        cap = (raw_len + encoded_len) // 2

        with (
            patch(
                "personal_agent.orchestrator.attachment_resolution.get_artifact_store",
                return_value=store,
            ),
            patch(
                "personal_agent.orchestrator.attachment_resolution.settings.attachment_image_max_bytes",
                cap,
            ),
        ):
            with pytest.raises(AttachmentUnsupportedError, match="huge.png"):
                await resolve_attachments([attachment])


class TestImagesPerTurnCapGuardrail:
    @pytest.mark.asyncio
    async def test_images_per_turn_cap_drops_excess(self) -> None:
        """AC-7 dimension 3: excess images beyond the cap are dropped, in order, with disclosure."""
        attachments = [
            _make_attachment(title=f"img{i}.png", r2_key=f"upload/u/g/img{i}.png") for i in range(5)
        ]
        store = _mock_store({a.r2_key: _make_png_bytes() for a in attachments})

        with (
            patch(
                "personal_agent.orchestrator.attachment_resolution.get_artifact_store",
                return_value=store,
            ),
            patch(
                "personal_agent.orchestrator.attachment_resolution.settings.attachment_max_images_per_turn",
                2,
            ),
        ):
            result = await resolve_attachments(attachments)

        assert len(result.blocks) == 2
        assert store.get.await_count == 2
        assert any("2 images" in d or "dropped" in d for d in result.disclosures)


class TestTotalPayloadCapGuardrail:
    @pytest.mark.asyncio
    async def test_total_payload_cap_drops_trailing(self) -> None:
        """AC-7 dimension 4: trailing images dropped once cumulative encoded size exceeds the cap."""
        png = _make_png_bytes()
        encoded_len = len(base64.b64encode(png))
        attachments = [
            _make_attachment(title=f"img{i}.png", r2_key=f"upload/u/g/img{i}.png") for i in range(3)
        ]
        store = _mock_store({a.r2_key: png for a in attachments})

        # Cap fits exactly two images' encoded payload, not three.
        cap = encoded_len * 2

        with (
            patch(
                "personal_agent.orchestrator.attachment_resolution.get_artifact_store",
                return_value=store,
            ),
            patch(
                "personal_agent.orchestrator.attachment_resolution.settings.attachment_max_total_payload_bytes",
                cap,
            ),
            patch(
                "personal_agent.orchestrator.attachment_resolution.settings.attachment_max_images_per_turn",
                10,
            ),
        ):
            result = await resolve_attachments(attachments)

        assert len(result.blocks) == 2
        assert any("dropped" in d and "total" in d for d in result.disclosures)

    @pytest.mark.asyncio
    async def test_total_payload_cap_stops_processing_at_first_drop(self) -> None:
        """A dropped image must not be skipped past to admit a later smaller one.

        Once an image doesn't fit the remaining budget, resolution must stop —
        not keep scanning for a later, smaller image that happens to fit — so
        the result is always a strict prefix in submitted order ("trailing
        images dropped", per the settings docstring), and no bytes are fetched
        for images that will be dropped anyway.
        """
        full = _make_png_bytes(size=(10, 10))
        tiny = _make_png_bytes(size=(1, 1))
        full_len = len(base64.b64encode(full))
        tiny_len = len(base64.b64encode(tiny))
        assert tiny_len < full_len  # sanity

        attachments = [
            _make_attachment(title="full1.png", r2_key="upload/u/g/full1.png"),
            _make_attachment(title="full2.png", r2_key="upload/u/g/full2.png"),
            _make_attachment(title="tiny.png", r2_key="upload/u/g/tiny.png"),
        ]
        store = _mock_store(
            {
                "upload/u/g/full1.png": full,
                "upload/u/g/full2.png": full,
                "upload/u/g/tiny.png": tiny,
            }
        )

        # Room for exactly one full image plus the tiny one, but not two full
        # images — a continue-based (non-prefix) implementation would skip
        # full2 and still admit tiny; a break-based one stops at full2.
        cap = full_len + tiny_len

        with (
            patch(
                "personal_agent.orchestrator.attachment_resolution.get_artifact_store",
                return_value=store,
            ),
            patch(
                "personal_agent.orchestrator.attachment_resolution.settings.attachment_max_total_payload_bytes",
                cap,
            ),
            patch(
                "personal_agent.orchestrator.attachment_resolution.settings.attachment_max_images_per_turn",
                10,
            ),
        ):
            result = await resolve_attachments(attachments)

        assert len(result.blocks) == 1
        assert store.get.await_count == 2  # tiny is never fetched
