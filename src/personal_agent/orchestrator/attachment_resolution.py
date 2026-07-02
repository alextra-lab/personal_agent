"""Resolve current-turn raster attachments to image content blocks.

ADR-0101 §3 (credentialed fetch), §4 (block construction), §6 (guardrails). Ticket 4
of the ADR-0101 chain (FRE-666) — routing (FRE-665) and cost gating (FRE-691) are
separate concerns; this module only turns bytes into a typed, capped image block list.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import structlog
from PIL import Image

from personal_agent.config import settings
from personal_agent.exceptions import AttachmentUnsupportedError
from personal_agent.orchestrator.types import AttachmentRef
from personal_agent.storage import get_artifact_store

log = structlog.get_logger(__name__)

RASTER_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

# The non-raster subset of uploads_router.ALLOWED_UPLOAD_CONTENT_TYPES — attachments of these
# types coexist validly in a turn (ADR-0102 documents, SVG) and are left unresolved by this
# module, not rejected. A test ties this to the upload allowlist so the two can't silently drift.
_KNOWN_NON_RASTER_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "image/svg+xml",
    }
)

_PIL_FORMAT_BY_CONTENT_TYPE: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
}


@dataclass(frozen=True)
class ResolvedAttachments:
    """Result of resolving a turn's raster attachments to content blocks.

    Attributes:
        blocks: OpenAI-style ``image_url`` content blocks, one per resolved image,
            in submitted order.
        disclosures: User-facing strings describing any downscale/drop applied by
            a guardrail (ADR-0101 §6, FRE-690 disclose-on-alter).
    """

    blocks: tuple[dict[str, Any], ...]
    disclosures: tuple[str, ...]


def _downscale_if_needed(image_bytes: bytes, content_type: str) -> tuple[bytes, bool]:
    """Downscale ``image_bytes`` below the pixel cap if either dimension exceeds it.

    Args:
        image_bytes: Original fetched image bytes.
        content_type: Declared raster MIME type (selects the re-encode format).

    Returns:
        ``(bytes, was_downscaled)`` — original bytes unchanged and ``False`` when
        already within the cap, else re-encoded bytes and ``True``.

    Note:
        Pillow's ``thumbnail()`` operates on the currently-loaded frame only; a
        downscaled animated GIF collapses to a still. Accepted v1 simplification —
        no AC on this ticket requires animation preservation.
    """
    with Image.open(BytesIO(image_bytes)) as img:
        if max(img.size) <= settings.attachment_image_max_pixels:
            return image_bytes, False
        resized = img.copy()
        resized.thumbnail(
            (settings.attachment_image_max_pixels, settings.attachment_image_max_pixels),
            Image.Resampling.LANCZOS,
        )
        buf = BytesIO()
        resized.save(buf, format=_PIL_FORMAT_BY_CONTENT_TYPE[content_type])
        return buf.getvalue(), True


async def resolve_attachments(
    attachments: Sequence[AttachmentRef],
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> ResolvedAttachments:
    """Resolve a turn's raster attachments into capped, typed image content blocks.

    Fetches bytes via the credentialed R2 ``store.get`` path (never a public URL),
    enforces the four ADR-0101 §6 guardrails independently against the base64-encoded
    payload size (what actually reaches the model), and builds one OpenAI-style
    ``image_url`` block per surviving image.

    Args:
        attachments: The turn's structured attachment carrier (FRE-661). Known
            non-raster entries (documents, SVG) are left unresolved (out of this
            ADR's v1 scope); a content type in neither set is rejected.
        trace_id: Originating request trace_id, threaded onto the ``store.get`` call
            and resolution/failure logs (ADR-0074 identity threading).
        session_id: Originating session id, threaded onto the ``store.get`` call
            and resolution/failure logs (ADR-0074 §8c joinability, FRE-693).
        task_id: Sub-agent task id, threaded onto the ``store.get`` call and
            resolution/failure logs (ADR-0074 §8c, FRE-693) — ``None`` at the
            turn level (this module is only ever called from turn assembly).

    Returns:
        ``ResolvedAttachments`` with the surviving image blocks and any disclosure
        strings for downscaled/dropped images.

    Raises:
        AttachmentUnsupportedError: A declared content type is neither a supported
            raster type nor a known non-raster type; an image is still over the
            per-image encoded-byte cap after downscale; or R2 storage is not
            configured.
    """
    raster: list[AttachmentRef] = []
    for attachment in attachments:
        if attachment.content_type in RASTER_CONTENT_TYPES:
            raster.append(attachment)
        elif attachment.content_type in _KNOWN_NON_RASTER_CONTENT_TYPES:
            continue
        else:
            log.warning(
                "attachment_resolution_failed",
                trace_id=trace_id,
                session_id=session_id,
                task_id=task_id,
                artifact_id=attachment.artifact_id,
                content_type=attachment.content_type,
                reason="unsupported_content_type",
            )
            raise AttachmentUnsupportedError(
                f"Attachment '{attachment.title}' has an unsupported content type: "
                f"{attachment.content_type}."
            )

    if not raster:
        log.info(
            "attachment_resolution_completed",
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            attachment_count=len(attachments),
            resolved_count=0,
            disclosure_count=0,
        )
        return ResolvedAttachments(blocks=(), disclosures=())

    disclosures: list[str] = []

    if len(raster) > settings.attachment_max_images_per_turn:
        cap = settings.attachment_max_images_per_turn
        dropped = len(raster) - cap
        raster = raster[:cap]
        disclosures.append(
            f"Only the first {cap} images were included; {dropped} image(s) were "
            "dropped (per-turn image limit)."
        )

    store = get_artifact_store()
    if store is None:
        log.warning(
            "attachment_resolution_failed",
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            artifact_id=None,
            content_type=None,
            reason="store_unconfigured",
        )
        raise AttachmentUnsupportedError(
            "Image attachments cannot be processed: artifact storage is not configured."
        )

    blocks: list[dict[str, Any]] = []
    total_encoded_bytes = 0

    for index, attachment in enumerate(raster):
        raw = await store.get(
            attachment.r2_key, trace_id=trace_id, session_id=session_id, task_id=task_id
        )
        image_bytes, was_downscaled = await asyncio.to_thread(
            _downscale_if_needed, raw, attachment.content_type
        )
        encoded = base64.b64encode(image_bytes).decode("ascii")
        encoded_len = len(encoded)

        if encoded_len > settings.attachment_image_max_bytes:
            log.warning(
                "attachment_resolution_failed",
                trace_id=trace_id,
                session_id=session_id,
                task_id=task_id,
                artifact_id=attachment.artifact_id,
                content_type=attachment.content_type,
                reason="oversized_after_downscale",
            )
            raise AttachmentUnsupportedError(
                f"Image '{attachment.title}' is too large ({encoded_len} encoded bytes) "
                f"even after downscaling; the per-image limit is "
                f"{settings.attachment_image_max_bytes} bytes."
            )

        if total_encoded_bytes + encoded_len > settings.attachment_max_total_payload_bytes:
            # Stop rather than skip-and-continue: the result must be a strict
            # prefix in submitted order, so a later smaller image never sneaks
            # in past an earlier one that didn't fit, and no bytes are fetched
            # for images that would be dropped anyway.
            payload_dropped = len(raster) - index
            disclosures.append(
                f"{payload_dropped} image(s) were dropped because the total per-turn "
                "attachment payload limit was reached."
            )
            break

        total_encoded_bytes += encoded_len
        if was_downscaled:
            disclosures.append(f"Image '{attachment.title}' was downscaled to fit the size limit.")

        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{attachment.content_type};base64,{encoded}"},
            }
        )

    log.info(
        "attachment_resolution_completed",
        trace_id=trace_id,
        session_id=session_id,
        task_id=task_id,
        attachment_count=len(attachments),
        resolved_count=len(blocks),
        disclosure_count=len(disclosures),
    )
    return ResolvedAttachments(blocks=tuple(blocks), disclosures=tuple(disclosures))
