"""Within-session compression telemetry — durable JSONL + bus publish (ADR-0061).

One ``WithinSessionCompressionRecord`` per compression pass.  Dual-write
order is durable file write first, bus publish second (ADR-0054 §D4); bus
failures are logged-and-swallowed (ADR-0054 §D6); durable failures
propagate.  Pattern copied from ``telemetry/context_quality.py`` minus the
``IncidentTracker`` (no Phase 2 governance counter at this level — ADR-0061
Phase 2 will read the ADR-0059 tracker, not introduce its own).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from personal_agent.events.models import (
    STREAM_CONTEXT_WITHIN_SESSION_COMPRESSED,
    WithinSessionCompressionEvent,
)

if TYPE_CHECKING:
    from personal_agent.events.bus import EventBus

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WithinSessionCompressionRecord:
    """One within-session compression pass (ADR-0061 §D7).

    Attributes:
        trace_id: Originating request trace identifier.
        session_id: Originating session identifier.
        trigger: ``"soft"`` (async between turns) or ``"hard"`` (synchronous
            mid-orchestration).
        head_tokens: Tokens kept in the head (system + first user message).
        middle_tokens_in: Tokens in the middle band before pre-pass + LLM.
        middle_tokens_out: Tokens in the middle after compression.
        tail_tokens: Tokens kept verbatim in the tail.
        pre_pass_replacements: Count of large tool messages replaced with
            1-line descriptors during the pre-pass.
        summariser_called: Whether the LLM compressor was invoked.
        summariser_duration_ms: Wall time of the compressor call; ``0``
            when summariser was not invoked.
        compressed_at: UTC timestamp when the compression completed.
    """

    trace_id: str
    session_id: str
    trigger: Literal["soft", "hard"]
    head_tokens: int
    middle_tokens_in: int
    middle_tokens_out: int
    tail_tokens: int
    pre_pass_replacements: int
    summariser_called: bool
    summariser_duration_ms: int
    compressed_at: datetime

    @property
    def tokens_saved(self) -> int:
        """Tokens removed from the middle band (always ≥ 0)."""
        return max(0, self.middle_tokens_in - self.middle_tokens_out)


def _default_output_dir() -> Path:
    """Return the default ``WSC-<YYYY-MM-DD>.jsonl`` directory."""
    return Path("telemetry/within_session_compression")


def _jsonl_line(record: WithinSessionCompressionRecord) -> str:
    """Serialise a record as one JSON line for the durable file."""
    payload = asdict(record)
    payload["compressed_at"] = record.compressed_at.isoformat()
    payload["tokens_saved"] = record.tokens_saved
    return json.dumps(payload, sort_keys=True)


def _append_durable(
    record: WithinSessionCompressionRecord, output_dir: Path
) -> Path:
    """Append the record to the per-day JSONL file (ADR-0054 §D4).

    Returns:
        Path of the file that was appended to.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    day = record.compressed_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
    fp = output_dir / f"WSC-{day}.jsonl"
    with fp.open("a", encoding="utf-8") as fh:
        fh.write(_jsonl_line(record))
        fh.write("\n")
    return fp


async def record_compression(
    record: WithinSessionCompressionRecord,
    bus: EventBus | None,
    *,
    output_dir: Path | None = None,
) -> None:
    """Dual-write a within-session compression record (ADR-0061 §D7).

    Order is durable file first, bus publish second (ADR-0054 §D4).  Bus
    failures are logged and swallowed (ADR-0054 §D6); durable failures
    propagate so observability gaps are visible at the source.

    Side effects:
        - Appends one JSON line to ``WSC-<YYYY-MM-DD>.jsonl``.
        - Publishes ``WithinSessionCompressionEvent`` to
          ``stream:context.within_session_compressed``.

    Args:
        record: The completed compression record.
        bus: Event bus used to publish the typed event.  ``None`` skips the
            bus publish (durable write still happens).
        output_dir: Override for the JSONL output directory.  Defaults to
            ``telemetry/within_session_compression``.
    """
    target_dir = output_dir or _default_output_dir()
    try:
        path = _append_durable(record, target_dir)
    except OSError as exc:
        log.warning(
            "within_session_compression_durable_write_failed",
            trace_id=record.trace_id,
            session_id=record.session_id,
            error=str(exc),
        )
        raise

    log.info(
        "within_session_compression_recorded",
        trace_id=record.trace_id,
        session_id=record.session_id,
        trigger=record.trigger,
        head_tokens=record.head_tokens,
        middle_tokens_in=record.middle_tokens_in,
        middle_tokens_out=record.middle_tokens_out,
        tail_tokens=record.tail_tokens,
        pre_pass_replacements=record.pre_pass_replacements,
        summariser_called=record.summariser_called,
        summariser_duration_ms=record.summariser_duration_ms,
        tokens_saved=record.tokens_saved,
        path=str(path),
    )

    if bus is None:
        return

    event = WithinSessionCompressionEvent(
        trace_id=record.trace_id,
        session_id=record.session_id,
        trigger=record.trigger,
        head_tokens=record.head_tokens,
        middle_tokens_in=record.middle_tokens_in,
        middle_tokens_out=record.middle_tokens_out,
        tail_tokens=record.tail_tokens,
        pre_pass_replacements=record.pre_pass_replacements,
        summariser_called=record.summariser_called,
        summariser_duration_ms=record.summariser_duration_ms,
        tokens_saved=record.tokens_saved,
    )
    try:
        await bus.publish(STREAM_CONTEXT_WITHIN_SESSION_COMPRESSED, event)
    except Exception as exc:
        log.warning(
            "within_session_compression_publish_failed",
            trace_id=record.trace_id,
            session_id=record.session_id,
            error=str(exc),
        )
