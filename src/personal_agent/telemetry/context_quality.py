"""Context quality stream — Level 3 self-observability (ADR-0059).

Implements the per-incident half of Stream 7 (Compaction Quality Detection):
when the recall controller's substring match in Stage 4b detects that a
recently dropped entity overlaps with a noun phrase from the current user
message, this module dual-writes a durable JSONL line and publishes a
``CompactionQualityIncidentEvent`` on the event bus.

Composes with ADR-0056 (Error Pattern Monitoring), which captures the same
``compaction_quality.poor`` warning at cluster granularity (24 h rolling
window).  Distinct fingerprints by construction; ADR-0030 dedup at
``CaptainLogManager.save_entry()`` merges any overlap.

Also exposes an ``IncidentTracker`` singleton that Stage 7 (Budget) reads
under the ``context_quality_governance_*`` flags to tighten ``max_tokens``
for sessions with sustained incident counts (Phase 2 governance).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from personal_agent.events.models import (
    STREAM_CONTEXT_COMPACTION_QUALITY_POOR,
    CompactionQualityIncidentEvent,
)

if TYPE_CHECKING:
    from personal_agent.events.bus import EventBus

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Layer A — In-memory incident record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionQualityIncident:
    """One detected compaction-quality incident (ADR-0059 Layer A).

    Attributes:
        fingerprint: sha256(noun_phrase:dropped_entity:component)[:16].
        trace_id: Originating request trace identifier.
        session_id: Originating session identifier.
        noun_phrase: Cue extracted from the user message.
        dropped_entity: Identifier of the entity dropped earlier in session.
        recall_cue: Regex cue that triggered Stage 4b.
        tier_affected: ``"near"`` | ``"episodic"`` | ``"long_term"``.
        tokens_removed: Tokens removed by the originating compaction event;
            ``0`` when not available at detection time.
        detected_at: UTC timestamp when the incident was detected.
    """

    fingerprint: str
    trace_id: str
    session_id: str
    noun_phrase: str
    dropped_entity: str
    recall_cue: str
    tier_affected: str
    tokens_removed: int
    detected_at: datetime


def fingerprint_incident(noun_phrase: str, dropped_entity: str, component: str) -> str:
    """Compute the fingerprint for a compaction-quality incident.

    Per ADR-0059 §D4 — sha256(noun_phrase:dropped_entity:component)[:16].
    Finer-grained than ADR-0056's cluster fingerprint so the per-incident
    Captain's Log entry distinguishes by the specific recalled-vs-dropped
    overlap.

    Args:
        noun_phrase: Lowercased noun phrase from the recall controller.
        dropped_entity: Identifier of the dropped entity.
        component: Dotted module path of the detector component.

    Returns:
        16-hex-character fingerprint.
    """
    raw = f"{noun_phrase}:{dropped_entity}:{component}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Layer B + C — Dual-write (durable JSONL + bus publish)
# ---------------------------------------------------------------------------


def _default_output_dir() -> Path:
    """Return the default directory for ``CQ-<YYYY-MM-DD>.jsonl`` files."""
    return Path("telemetry/context_quality")


def _jsonl_line(incident: CompactionQualityIncident) -> str:
    """Serialise an incident as one JSON line for the durable JSONL file."""
    payload = asdict(incident)
    payload["detected_at"] = incident.detected_at.isoformat()
    return json.dumps(payload, sort_keys=True)


def _append_durable(incident: CompactionQualityIncident, output_dir: Path) -> Path:
    """Append the incident to the per-day JSONL file (ADR-0054 D4 ordering).

    Returns:
        Path of the file that was appended to.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    day = incident.detected_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
    fp = output_dir / f"CQ-{day}.jsonl"
    with fp.open("a", encoding="utf-8") as fh:
        fh.write(_jsonl_line(incident))
        fh.write("\n")
    return fp


async def record_incident(
    incident: CompactionQualityIncident,
    bus: EventBus | None,
    *,
    output_dir: Path | None = None,
) -> None:
    """Dual-write a compaction-quality incident (ADR-0059 §D3, §D8).

    Order is durable file write first, bus publish second (ADR-0054 §D4).
    Bus failures are logged and swallowed (ADR-0054 §D6).  Durable write
    failures are logged and propagate — losing observability on the loop
    is preferable to silent swallowing at the source.

    Side effects:
        - Appends one JSON line to ``CQ-<YYYY-MM-DD>.jsonl``.
        - Publishes ``CompactionQualityIncidentEvent`` to
          ``stream:context.compaction_quality_poor``.
        - Registers the incident with the global ``IncidentTracker`` so the
          Stage 7 governance hook can count it.

    Args:
        incident: The detected incident.
        bus: Event bus used to publish the typed event.  ``None`` skips the
            bus publish (durable + tracker registration still happen).
        output_dir: Override for the JSONL output directory.  Defaults to
            ``telemetry/context_quality``.
    """
    target_dir = output_dir or _default_output_dir()
    try:
        path = _append_durable(incident, target_dir)
    except OSError as exc:
        log.warning(
            "context_quality_incident_durable_write_failed",
            fingerprint=incident.fingerprint,
            session_id=incident.session_id,
            error=str(exc),
        )
        raise

    get_incident_tracker().register(incident.session_id, incident.detected_at)

    log.info(
        "context_quality_incident_recorded",
        fingerprint=incident.fingerprint,
        trace_id=incident.trace_id,
        session_id=incident.session_id,
        noun_phrase=incident.noun_phrase,
        dropped_entity=incident.dropped_entity,
        recall_cue=incident.recall_cue,
        tier_affected=incident.tier_affected,
        tokens_removed=incident.tokens_removed,
        path=str(path),
    )

    if bus is None:
        return

    event = CompactionQualityIncidentEvent(
        trace_id=incident.trace_id,
        session_id=incident.session_id,
        fingerprint=incident.fingerprint,
        noun_phrase=incident.noun_phrase,
        dropped_entity=incident.dropped_entity,
        recall_cue=incident.recall_cue,
        tier_affected=incident.tier_affected,
        tokens_removed=incident.tokens_removed,
        detected_at=incident.detected_at,
    )
    try:
        await bus.publish(STREAM_CONTEXT_COMPACTION_QUALITY_POOR, event)
    except Exception as exc:
        log.warning(
            "context_quality_incident_publish_failed",
            fingerprint=incident.fingerprint,
            session_id=incident.session_id,
            error=str(exc),
        )


def schedule_record_incident(
    incident: CompactionQualityIncident,
    bus: EventBus | None,
) -> None:
    """Fire-and-forget wrapper for sync callers (Stage 4b recall controller).

    Tries to schedule :func:`record_incident` on the running event loop. When
    no loop is running (e.g. unit tests calling the recall controller
    directly), runs :func:`record_incident` synchronously via
    :func:`asyncio.run` so the durable write still happens.

    Args:
        incident: The detected incident.
        bus: Event bus or ``None`` (durable-only).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(record_incident(incident, bus))
        except Exception as exc:
            log.warning(
                "context_quality_incident_sync_record_failed",
                fingerprint=incident.fingerprint,
                error=str(exc),
            )
        return

    loop.create_task(record_incident(incident, bus))


# ---------------------------------------------------------------------------
# IncidentTracker — Phase 2 governance counter (ADR-0059 §D6)
# ---------------------------------------------------------------------------


_TRACKER_LRU_CAP = 1024
_TRACKER_RETENTION_HOURS = 24


class IncidentTracker:
    """Per-session rolling counter of compaction-quality incidents.

    Stage 7 (Budget) reads ``count_in_window(session_id, hours=24)`` to
    decide whether to tighten ``max_tokens`` (Phase 2 governance, flag
    ``context_quality_governance_enabled``).  Bounded LRU at
    ``_TRACKER_LRU_CAP`` sessions; entries older than
    ``_TRACKER_RETENTION_HOURS`` are dropped on each ``register``.

    Thread/async safety: callers must not race ``register`` and
    ``count_in_window`` from different threads.  In the request gateway
    pipeline, both run on the same asyncio loop so ordering is preserved.
    """

    def __init__(
        self,
        *,
        capacity: int = _TRACKER_LRU_CAP,
        retention_hours: int = _TRACKER_RETENTION_HOURS,
    ) -> None:
        """Initialise an empty tracker.

        Args:
            capacity: Maximum number of distinct sessions to retain.  Older
                sessions are evicted from the LRU when the capacity is
                exceeded.
            retention_hours: Per-session retention window.  Incidents older
                than this are dropped on every ``register`` call.
        """
        self._capacity = capacity
        self._retention = timedelta(hours=retention_hours)
        self._sessions: OrderedDict[str, deque[datetime]] = OrderedDict()

    def register(self, session_id: str, when: datetime | None = None) -> None:
        """Record one incident for ``session_id``.

        Args:
            session_id: Session that produced the incident.
            when: UTC timestamp.  Defaults to ``datetime.now(timezone.utc)``.
        """
        if not session_id:
            return
        ts = when or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        bucket = self._sessions.get(session_id)
        if bucket is None:
            bucket = deque()
            self._sessions[session_id] = bucket
        bucket.append(ts)
        self._sessions.move_to_end(session_id)

        cutoff = ts - self._retention
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        while len(self._sessions) > self._capacity:
            evicted_id, _ = self._sessions.popitem(last=False)
            log.debug(
                "context_quality_incident_tracker_evicted",
                session_id=evicted_id,
                capacity=self._capacity,
            )

    def count_in_window(self, session_id: str, hours: int) -> int:
        """Return incidents for ``session_id`` within the trailing window.

        Args:
            session_id: Session to look up.
            hours: Trailing window in hours.

        Returns:
            Incident count (``0`` for unknown sessions).
        """
        bucket = self._sessions.get(session_id)
        if not bucket:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return sum(1 for ts in bucket if ts >= cutoff)

    def reset(self) -> None:
        """Clear all tracked sessions (test-only convenience)."""
        self._sessions.clear()


_global_incident_tracker: IncidentTracker | None = None


def get_incident_tracker() -> IncidentTracker:
    """Return the process-global ``IncidentTracker``.

    Lazily instantiated on first call so unit tests that never touch the
    Stream 7 path don't pay the small in-memory cost.
    """
    global _global_incident_tracker
    if _global_incident_tracker is None:
        _global_incident_tracker = IncidentTracker()
    return _global_incident_tracker


def reset_incident_tracker() -> None:
    """Reset the process-global ``IncidentTracker`` (test-only convenience)."""
    global _global_incident_tracker
    _global_incident_tracker = None
