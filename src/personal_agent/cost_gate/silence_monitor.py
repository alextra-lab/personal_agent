"""main_inference silence detector (FRE-1117).

``main_inference`` (the ``primary`` role's budget lane) legitimately shows
zero ``budget_reservations`` rows for days at a time whenever primary is
selected local — that is the current, correct, everyday operating state, not
an anomaly. A naive "counter is empty" check would fire constantly and train
the owner to ignore it. The one case actually worth a human's attention: a
session had ``primary``-role turns *and* that session's own model selection
was cloud, yet no reservation exists for it — a call that should have at
least attempted to book against the lane and didn't.

Scoped to ``primary``/``main_inference`` only — the ticket's actual subject,
not generalized to every budget-gated role. See the plan doc
(``docs/superpowers/plans/2026-08-01-fre-1117-main-inference-silence-detector.md``)
for the investigation that established this and the codex plan review that
shaped the design below (notably: the reservation check is per-session, not
a lane-wide count — ``main_inference`` is shared by ``primary``,
``sub_agent``, ``compressor``, ``vision`` and others, so a lane-wide count
would let one role's activity mask another session's genuine silence).

Started by the FastAPI lifespan hook alongside the cost-gate reaper and
snapshotter, and cancelled at shutdown the same way.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.llm_client.models import ModelConfig, Placement

log = structlog.get_logger(__name__)

DEFAULT_SILENCE_MONITOR_INTERVAL_SECONDS = 3600.0

# Sessions with primary-role turns on the audited day that booked nothing to
# ``main_inference`` that day. The reservation check is per-session (``NOT
# EXISTS`` correlated on ``session_id``) rather than a lane-wide count:
# ``main_inference`` is shared by ``primary``, ``sub_agent``, ``compressor``,
# ``vision`` and others, so a lane-wide count would let one role's activity
# mask another session's genuine silence. Placement (local vs cloud) is *not*
# filtered here — that lives in the catalog, so the caller filters the rows.
_SILENT_SESSIONS_SQL = (
    "SELECT DISTINCT rt.session_id, sel.deployment_key"
    " FROM route_traces rt"
    # INNER JOIN: a session with no selection row falls back to the role's
    # binding default, which is local — not cloud, so not evidence.
    " JOIN session_model_selections sel"
    "   ON sel.session_id = rt.session_id AND sel.role = 'primary'"
    " WHERE rt.model_role = 'primary'"
    "   AND rt.created_at >= :start AND rt.created_at < :end"
    # Selection changed after the audited day -- the table keeps no history,
    # so it cannot be attributed to this day either way. Skip, don't score.
    "   AND sel.updated_at <= :end"
    "   AND NOT EXISTS ("
    "     SELECT 1 FROM budget_reservations br"
    "     WHERE br.role = 'main_inference' AND br.session_id = rt.session_id"
    "       AND br.created_at >= :start AND br.created_at < :end"
    "   )"
    " ORDER BY rt.session_id"
)


@dataclass(frozen=True)
class SilentInferenceFinding:
    """A day where cloud-selected primary sessions show no main_inference spend.

    Attributes:
        day: The UTC calendar day checked.
        cloud_selected_sessions: Sessions that had primary-role turns that
            day, whose current selection for ``primary`` resolves to a cloud
            deployment, and for which no ``main_inference`` reservation
            exists that day.
    """

    day: date
    cloud_selected_sessions: tuple[UUID, ...]


async def find_silent_main_inference_day(
    db: AsyncSession, model_config: ModelConfig, *, day: date
) -> SilentInferenceFinding | None:
    """Check one UTC day for cloud-selected primary sessions with zero booking.

    Args:
        db: An open async SQLAlchemy session.
        model_config: Loaded model catalog, used to resolve a deployment
            key's placement (local vs cloud).
        day: The UTC calendar day to check.

    Returns:
        A :class:`SilentInferenceFinding` naming the affected sessions, or
        ``None`` when the day is correctly quiet (no primary turns, every
        cloud-selected session already has a reservation, or every session
        that had turns was local-selected).

    Note:
        ``session_model_selections`` carries no history — one mutable row
        per ``(session_id, role)``. A session's *current* selection is used
        as a proxy for "as of `day`", which is skipped (neither flagged nor
        cleared) when its ``updated_at`` falls after `day` ends, since that
        selection cannot be attributed to `day` retroactively. This is an
        accepted, documented approximation for an advisory monitor a human
        reads — not a guarantee.
    """
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    unbooked_rows = (
        await db.execute(text(_SILENT_SESSIONS_SQL), {"start": day_start, "end": day_end})
    ).all()
    silent_sessions: tuple[UUID, ...] = tuple(
        session_id
        for session_id, deployment_key in unbooked_rows
        if model_config.placement_of(deployment_key) is not Placement.LOCAL
    )
    if not silent_sessions:
        return None
    return SilentInferenceFinding(day=day, cloud_selected_sessions=silent_sessions)


async def run_silence_monitor(
    model_config: ModelConfig,
    *,
    interval_seconds: float = DEFAULT_SILENCE_MONITOR_INTERVAL_SECONDS,
) -> None:
    """Check the previous UTC day for silent cloud-selected primary sessions.

    Runs once per fully-elapsed UTC day (checked on a polling cadence, not a
    precise scheduler — an hourly default interval keeps the delay after
    midnight UTC small without needing a dedicated cron). A finding logs a
    single ``warning``-level structured event; a clean day logs nothing, so
    this stays a monitor rather than a heartbeat.

    Args:
        model_config: Loaded model catalog, passed through to
            :func:`find_silent_main_inference_day`.
        interval_seconds: Seconds between polls. Defaults to one hour.

    Cancellation:
        Cancel the task at shutdown. The function suppresses
        ``asyncio.CancelledError`` and exits cleanly, mirroring the
        cost-gate reaper and snapshotter.
    """
    # Deferred: ``service.database`` builds the async engine at import time,
    # and this module is re-exported from ``personal_agent.cost_gate`` — a
    # module-level import would open a connection pool for every importer.
    from personal_agent.service.database import AsyncSessionLocal

    log.info("main_inference_silence_monitor_started", interval_seconds=interval_seconds)
    last_checked_day: date | None = None
    try:
        while True:
            try:
                yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
                if last_checked_day != yesterday:
                    async with AsyncSessionLocal() as db:
                        finding = await find_silent_main_inference_day(
                            db, model_config, day=yesterday
                        )
                    if finding is not None:
                        log.warning(
                            "main_inference_silent_with_cloud_selection",
                            day=str(finding.day),
                            session_count=len(finding.cloud_selected_sessions),
                            session_ids=[str(s) for s in finding.cloud_selected_sessions],
                        )
                    last_checked_day = yesterday
            except Exception as exc:  # noqa: BLE001 — log + continue, mirrors the reaper/snapshotter
                log.error("main_inference_silence_check_failed", error=str(exc), exc_info=True)
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        log.info("main_inference_silence_monitor_stopped")
        with suppress(asyncio.CancelledError):
            raise
