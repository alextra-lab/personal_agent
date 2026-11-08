"""The job card: the payload of the ``dev_job`` event and the sink that sends it (ADR-0159 D10.6; FRE-1688).

Every payload is the complete card state, built from the ``dev_jobs`` row. The newest payload wins,
so a replayed or repeated event cannot corrupt the card. Three paths produce a payload:

* the supervisor's state and end events, through :class:`PoolJobEventSink`;
* the adapter's step events, through the same sink, throttled;
* the attach snapshot, through :class:`JobCardStore`. The attach replay does not replay old events
  after a job ended, so the snapshot is what a page that loads after the end receives.

Delivery is best effort and never changes a job: a failed read or emit is logged and dropped.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Literal, Protocol
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.config import settings
from personal_agent.dev_agent.jobs import END_STATES, JobState

log = structlog.get_logger(__name__)

JobEventKind = Literal["state", "step", "end"]


class EmitDevJob(Protocol):
    """Sends a payload to a session. ``emit_dev_job`` in the transport satisfies it."""

    def __call__(self, *, session_id: str, payload: dict[str, object]) -> Awaitable[bool]:
        """Send the payload.

        Args:
            session_id: The target session.
            payload: The card.

        Returns:
            Whether the event was persisted.
        """
        ...


#: The row of a job with its spend: the final spend once ended, else the committed spend so far.
JOB_CARD_SQL = """
    SELECT j.id, j.session_id, j.mode, j.state, j.failure_reason, j.check_result, j.repo,
           j.result_artifact_id, j.main_moved,
           COALESCE(j.spend_usd, s.committed_usd) AS spend_usd
      FROM dev_jobs j
      LEFT JOIN dev_job_spend s ON s.job_id = j.id
"""

_ENDED = frozenset(state.value for state in END_STATES)

#: The least time between two step events of one job.
STEP_INTERVAL_SECONDS = 2.0


@dataclass(frozen=True)
class CardContext:
    """What the card needs that the job row does not hold.

    Attributes:
        cap_usd: The per-job spend cap in USD, or ``None`` when unknown.
        repo_base_url: The URL of the GitHub organization, without a trailing slash, or ``None``.
    """

    cap_usd: Decimal | None
    repo_base_url: str | None


@lru_cache(maxsize=1)
def card_context() -> CardContext:
    """Return the card context from the broker config and the settings.

    Returns:
        The context. A missing broker config gives no cap, and the card then shows no cap.
    """
    from personal_agent.dev_broker.config import load_broker_config

    try:
        cap: Decimal | None = load_broker_config().job_cap_usd
    except (OSError, ValueError) as exc:
        log.warning("dev_job_card_cap_unknown", error_type=type(exc).__name__, trace_id=None)
        cap = None
    org = settings.dev_agent_github_org
    return CardContext(cap_usd=cap, repo_base_url=f"https://github.com/{org}" if org else None)


def _check_result(value: object) -> object:
    """Return the check result as a JSON object. The asyncpg pool returns JSONB as text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def card_payload(
    row: Mapping[str, object],
    *,
    event: JobEventKind,
    step: str | None,
    context: CardContext,
) -> dict[str, object]:
    """Build the card payload from a job row.

    Args:
        row: A row of :data:`JOB_CARD_SQL`.
        event: Which event this payload belongs to.
        step: The latest step of the OpenCode event stream, or ``None``.
        context: The cap and the repo base URL.

    Returns:
        A JSON-serializable dict. Spend and cap are decimal strings, so no float rounds them.
    """
    spend = row["spend_usd"]
    artifact = row["result_artifact_id"]
    repo = row["repo"]
    state = str(row["state"])
    return {
        "event": event,
        "job_id": str(row["id"]),
        "mode": row["mode"],
        "state": state,
        "ended": state in _ENDED,
        "step": step,
        "spend_usd": None if spend is None else str(spend),
        "cap_usd": None if context.cap_usd is None else str(context.cap_usd),
        "failure_reason": row["failure_reason"],
        "check_result": _check_result(row["check_result"]),
        "repo_url": (f"{context.repo_base_url}/{repo}" if repo and context.repo_base_url else None),
        "result_artifact_id": None if artifact is None else str(artifact),
        "main_moved": row["main_moved"],
    }


class JobEventSink(Protocol):
    """Receives the job's events from the supervisor and the adapter."""

    async def notify(self, job_id: UUID, event: JobEventKind, step: str | None = None) -> None:
        """Publish the job's current card. Never raises.

        Args:
            job_id: The job.
            event: The kind of event.
            step: The latest step, for a ``step`` event.
        """
        ...


class NullJobEventSink:
    """The sink that does nothing."""

    async def notify(self, job_id: UUID, event: JobEventKind, step: str | None = None) -> None:
        """Do nothing."""


class PoolJobEventSink:
    """Reads the job row and sends the card to the job's session."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        emit: EmitDevJob,
        *,
        context: CardContext | None = None,
        clock: Callable[[], float] = time.monotonic,
        step_interval_seconds: float = STEP_INTERVAL_SECONDS,
    ) -> None:
        """Create the sink.

        Args:
            pool: The supervisor's pool.
            emit: Sends a payload to a session.
            context: The cap and the repo base URL. Defaults to :func:`card_context`.
            clock: A monotonic clock, for the step throttle.
            step_interval_seconds: The least time between two step events of one job.
        """
        self._pool = pool
        self._emit = emit
        self._context = context
        self._clock = clock
        self._interval = step_interval_seconds
        self._steps: dict[UUID, str] = {}
        self._last_step_at: dict[UUID, float] = {}

    async def notify(self, job_id: UUID, event: JobEventKind, step: str | None = None) -> None:
        """Send the job's current card. A failure is logged and dropped.

        Args:
            job_id: The job.
            event: The kind of event. A ``step`` event is throttled and skipped when unchanged.
            step: The latest step, for a ``step`` event.
        """
        if event == "step":
            now = self._clock()
            last = self._last_step_at.get(job_id)
            if step is None or step == self._steps.get(job_id):
                return
            if last is not None and now - last < self._interval:
                return
            self._steps[job_id] = step
            self._last_step_at[job_id] = now
        try:
            row = await self._pool.fetchrow(JOB_CARD_SQL + " WHERE j.id = $1", job_id)
            if row is None:
                return
            payload = card_payload(
                row,
                event=event,
                step=self._steps.get(job_id),
                context=self._context or card_context(),
            )
            await self._emit(session_id=str(row["session_id"]), payload=payload)
        except Exception as exc:  # noqa: BLE001 — a lost card never changes a job
            log.warning(
                "dev_job_card_notify_failed",
                job_id=str(job_id),
                card_event=event,
                error_type=type(exc).__name__,
                trace_id=None,
            )
        finally:
            if event == "end":
                self._steps.pop(job_id, None)
                self._last_step_at.pop(job_id, None)


class JobCardStore:
    """Reads the card of a session's newest job through the gateway's SQLAlchemy session."""

    def __init__(self, db: AsyncSession, context: CardContext | None = None) -> None:
        """Create the store.

        Args:
            db: An open session.
            context: The cap and the repo base URL. Defaults to :func:`card_context`.
        """
        self._db = db
        self._context = context

    async def latest(self, session_id: UUID) -> dict[str, object] | None:
        """Return the card of the session's newest job.

        The payload has no step: the step is not stored.

        Args:
            session_id: The session.

        Returns:
            The payload, or ``None`` when the session has no job.
        """
        result = await self._db.execute(
            text(
                JOB_CARD_SQL
                + " WHERE j.session_id = :session_id ORDER BY j.created_at DESC LIMIT 1"
            ),
            {"session_id": session_id},
        )
        row = result.mappings().first()
        if row is None:
            return None
        values: dict[str, object] = dict(row)
        state = JobState(str(values["state"]))
        return card_payload(
            values,
            event="end" if state in END_STATES else "state",
            step=None,
            context=self._context or card_context(),
        )
