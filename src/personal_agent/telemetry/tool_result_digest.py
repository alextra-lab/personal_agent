"""Tool-result digest telemetry — durable JSONL + bus publish (ADR-0085, FRE-475).

One :class:`ToolResultDigestRecord` per intra-turn tool-result digestion. Dual-write
order is durable file write first, bus publish second (ADR-0054 §D4); bus failures
are logged-and-swallowed (ADR-0054 §D6); durable failures propagate. Pattern mirrors
``telemetry/within_session_compression.py``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from personal_agent.events.models import (
    STREAM_CONTEXT_TOOL_RESULT_DIGESTED,
    ToolResultDigestEvent,
)

if TYPE_CHECKING:
    from personal_agent.events.bus import EventBus

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ToolResultDigestRecord:
    """One intra-turn tool-result digestion (ADR-0085 §D7).

    Attributes:
        trace_id: Originating request trace identifier.
        session_id: Originating session identifier.
        tool_name: Name of the tool whose result was digested.
        tool_call_id: Identifier of the digested tool call.
        bytes_in: Exact byte length of the verbatim result.
        tokens_in: Estimated tokens of the verbatim result.
        tokens_out: Estimated tokens of the digest message content.
        format: Digest body discriminator (``bash`` / ``read`` / ``json`` / ``text``).
        persisted: Whether the full bytes were durably stored in R2.
        r2_key: Canonical R2 key where the full bytes live.
        content_hash: Full SHA-256 hex of the verbatim bytes.
        digested_at: UTC timestamp when the digestion completed.
    """

    trace_id: str
    session_id: str
    tool_name: str
    tool_call_id: str
    bytes_in: int
    tokens_in: int
    tokens_out: int
    format: str
    persisted: bool
    r2_key: str
    content_hash: str
    digested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def tokens_saved(self) -> int:
        """Estimated tokens removed by the digest (always ≥ 0)."""
        return max(0, self.tokens_in - self.tokens_out)


def _default_output_dir() -> Path:
    """Return the default ``TRD-<YYYY-MM-DD>.jsonl`` directory."""
    return Path("telemetry/tool_result_digest")


def _jsonl_line(record: ToolResultDigestRecord) -> str:
    """Serialise a record as one JSON line for the durable file."""
    payload = asdict(record)
    payload["digested_at"] = record.digested_at.isoformat()
    payload["tokens_saved"] = record.tokens_saved
    return json.dumps(payload, sort_keys=True)


def _append_durable(record: ToolResultDigestRecord, output_dir: Path) -> Path:
    """Append the record to the per-day JSONL file (ADR-0054 §D4)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    day = record.digested_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
    fp = output_dir / f"TRD-{day}.jsonl"
    with fp.open("a", encoding="utf-8") as fh:
        fh.write(_jsonl_line(record))
        fh.write("\n")
    return fp


async def record_digest(
    record: ToolResultDigestRecord,
    bus: EventBus | None,
    *,
    output_dir: Path | None = None,
) -> None:
    """Dual-write a tool-result digest record (ADR-0085 §D7).

    Order is durable file first, bus publish second (ADR-0054 §D4). Bus failures are
    logged and swallowed (ADR-0054 §D6); durable failures propagate so observability
    gaps are visible at the source.

    Side effects:
        - Appends one JSON line to ``TRD-<YYYY-MM-DD>.jsonl``.
        - Publishes :class:`ToolResultDigestEvent` to
          ``stream:context.tool_result_digested``.

    Args:
        record: The completed digest record.
        bus: Event bus used to publish the typed event. ``None`` skips the bus
            publish (durable write still happens).
        output_dir: Override for the JSONL output directory. Defaults to
            ``telemetry/tool_result_digest``.
    """
    target_dir = output_dir or _default_output_dir()
    try:
        path = _append_durable(record, target_dir)
    except OSError as exc:
        log.warning(
            "tool_result_digest_durable_write_failed",
            trace_id=record.trace_id,
            session_id=record.session_id,
            error=str(exc),
        )
        raise

    log.info(
        "tool_result_digest_recorded",
        trace_id=record.trace_id,
        session_id=record.session_id,
        tool_name=record.tool_name,
        tool_call_id=record.tool_call_id,
        bytes_in=record.bytes_in,
        tokens_in=record.tokens_in,
        tokens_out=record.tokens_out,
        format=record.format,
        persisted=record.persisted,
        tokens_saved=record.tokens_saved,
        path=str(path),
    )

    if bus is None:
        return

    event = ToolResultDigestEvent(
        trace_id=record.trace_id,
        session_id=record.session_id,
        tool_name=record.tool_name,
        tool_call_id=record.tool_call_id,
        bytes_in=record.bytes_in,
        tokens_in=record.tokens_in,
        tokens_out=record.tokens_out,
        digest_format=record.format,
        persisted=record.persisted,
        tokens_saved=record.tokens_saved,
    )
    try:
        await bus.publish(STREAM_CONTEXT_TOOL_RESULT_DIGESTED, event)
    except Exception as exc:
        log.warning(
            "tool_result_digest_publish_failed",
            trace_id=record.trace_id,
            session_id=record.session_id,
            error=str(exc),
        )
