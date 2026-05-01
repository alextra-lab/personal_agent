"""Per-attempt telemetry writer for the consolidation pipeline (FRE-307).

Writes a row to ``consolidation_attempts`` for every entity-extraction /
promotion attempt and emits a matching structured log so the
"Extraction Retry Health" Kibana panel can aggregate without a Postgres
join. ``attempt_number`` is sequential per ``(trace_id, role)``: derived
from ``MAX(attempt_number) + 1`` at write time.

The DB row is the durable audit trail; the log line is what Kibana reads.
Both carry the same fields so a join on ``trace_id`` is the same answer
in either system.

Uses raw asyncpg (mirrors ``cost_tracker.py`` and the cost gate) rather
than the long-lived ``AsyncSessionLocal`` because this writer runs from
multiple async contexts (the consolidator, plus tests across distinct
event loops), and SQLAlchemy's module-scoped engine binds its connections
to the first loop it sees.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

import asyncpg
import structlog

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

log = structlog.get_logger(__name__)


# Outcome enum — narrow Literal so callers can't drift to free-form strings.
ConsolidationOutcome = Literal[
    "success",
    "budget_denied",
    "model_error",
    "extraction_returned_fallback",
    "transient_failure",
    "dead_letter",
]


def _coerce_trace_id(trace_id: UUID | str) -> UUID:
    return trace_id if isinstance(trace_id, UUID) else UUID(trace_id)


async def record_consolidation_attempt(
    *,
    trace_id: UUID | str,
    role: str,
    started_at: datetime,
    outcome: ConsolidationOutcome,
    denial_reason: str | None = None,
    completed_at: datetime | None = None,
) -> int:
    """Write a ``consolidation_attempts`` row + emit a structured log.

    Args:
        trace_id: Originating capture's trace_id.
        role: Consumer / pipeline role (e.g. ``entity_extraction``).
        started_at: When the attempt began.
        outcome: Terminal state for this attempt.
        denial_reason: Set when ``outcome='budget_denied'`` — one of the
            ``DenialReason`` enum values.
        completed_at: When the attempt finished. Defaults to ``now(UTC)``.

    Returns:
        The ``attempt_number`` assigned to this row (1-based).
    """
    if completed_at is None:
        completed_at = datetime.now(timezone.utc)

    trace_uuid = _coerce_trace_id(trace_id)

    conn = await asyncpg.connect(_normalize_asyncpg_dsn(settings.database_url))
    try:
        async with conn.transaction():
            prev_max = await conn.fetchval(
                """
                SELECT COALESCE(MAX(attempt_number), 0)
                  FROM consolidation_attempts
                 WHERE trace_id = $1 AND role = $2
                """,
                trace_uuid,
                role,
            )
            attempt_number = int(prev_max or 0) + 1

            await conn.execute(
                """
                INSERT INTO consolidation_attempts
                    (trace_id, attempt_number, role, started_at, completed_at,
                     outcome, denial_reason)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                trace_uuid,
                attempt_number,
                role,
                started_at,
                completed_at,
                outcome,
                denial_reason,
            )
    finally:
        await conn.close()

    log.info(
        "consolidation_attempt_recorded",
        trace_id=str(trace_uuid),
        role=role,
        attempt_number=attempt_number,
        outcome=outcome,
        denial_reason=denial_reason,
        duration_ms=int((completed_at - started_at).total_seconds() * 1000),
    )
    return attempt_number


async def previous_attempt_count(*, trace_id: UUID | str, role: str) -> int:
    """Return how many ``consolidation_attempts`` rows already exist for ``(trace_id, role)``.

    Used by the consolidator to compute ``previous_failure_count`` for log
    lines when the next attempt is the one being recorded.
    """
    trace_uuid = _coerce_trace_id(trace_id)
    conn = await asyncpg.connect(_normalize_asyncpg_dsn(settings.database_url))
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM consolidation_attempts WHERE trace_id = $1 AND role = $2",
            trace_uuid,
            role,
        )
    finally:
        await conn.close()
    return int(count or 0)
