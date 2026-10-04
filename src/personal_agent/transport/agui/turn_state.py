"""Durable status snapshot and in-flight turn registry for a page that attaches (FRE-1543).

iPadOS reloads the PWA when the owner switches apps, and a reloaded page holds nothing in
memory. Two server facts let it recover:

* **The status snapshot** — the last ``turn_status`` reading of a session, kept in
  ``sessions.metadata['turn_status']``. It survives a gateway restart, unlike the projector's
  process-local session lane (FRE-1401), and has no TTL, unlike ``session_events``. The write
  folds a reading with no resolved ceiling onto the stored ctx reading, the same rule as the
  PWA ``mergeTurnStatus``: a reading with no ceiling never blanks the headroom figure, and the
  tools lane always comes from the newest reading.
* **The in-flight registry** — the ``seq`` after which the running turn's events start, so an
  attaching page can replay that turn from its start. It is process-local on purpose: a
  gateway restart ends every in-flight turn, so after a restart no turn is in flight.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: The ``sessions.metadata`` key holding the last status reading. A literal constant, never
#: input, so building it into SQL text carries no injection risk (same as
#: ``SessionRepository._save_pending``).
TURN_STATUS_KEY = "turn_status"

_CTX_FIELDS = ("context_tokens", "session_context_tokens", "context_max")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def has_ctx_reading(status: Mapping[str, object]) -> bool:
    """Whether a status holds a real context reading (a resolved, positive ceiling).

    Args:
        status: A ``turn_status`` value.

    Returns:
        True when ``context_max`` is a positive number and ``session_context_tokens`` a number.
    """
    ceiling = status.get("context_max")
    return (
        _is_number(ceiling)
        and cast("float", ceiling) > 0
        and _is_number(status.get("session_context_tokens"))
    )


def merge_turn_status(
    prev: Mapping[str, object] | None, new: Mapping[str, object]
) -> dict[str, object]:
    """Fold a new reading onto the stored one (the PWA ``mergeTurnStatus`` rule).

    Args:
        prev: The stored reading, or ``None`` when the session has none.
        new: The reading that just arrived.

    Returns:
        The reading to store: ``new``, with the ctx fields of ``prev`` carried over when ``new``
        has no ctx reading and ``prev`` has one.
    """
    merged = dict(new)
    if prev is not None and not has_ctx_reading(new) and has_ctx_reading(prev):
        for name in _CTX_FIELDS:
            merged[name] = prev.get(name)
    return merged


class TurnStatusStore:
    """Read and write a session's last status reading in ``sessions.metadata``.

    Args:
        db: Async SQLAlchemy session scoped to the caller.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize with an async SQLAlchemy session."""
        self._db = db

    async def save(self, session_id: UUID, value: Mapping[str, object]) -> None:
        """Merge ``value`` onto the stored reading and store the result.

        The read and the write share one transaction with a row lock, so two writers cannot
        lose each other's ctx reading. A session with no row is a no-op.

        Args:
            session_id: The session the reading belongs to.
            value: The ``turn_status`` value the client was sent.
        """
        row = (
            await self._db.execute(
                text(
                    f"SELECT metadata -> '{TURN_STATUS_KEY}' FROM sessions "
                    "WHERE session_id = :sid FOR UPDATE"
                ),
                {"sid": session_id},
            )
        ).first()
        if row is None:
            await self._db.rollback()
            return
        merged = merge_turn_status(_as_mapping(row[0]), value)
        await self._db.execute(
            text(
                "UPDATE sessions SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), "
                f"'{{{TURN_STATUS_KEY}}}', CAST(:value AS jsonb)) WHERE session_id = :sid"
            ),
            {"value": json.dumps(merged, default=str), "sid": session_id},
        )
        await self._db.commit()

    async def load(self, session_id: UUID) -> dict[str, object] | None:
        """Return the stored reading, or ``None`` when the session has none.

        Args:
            session_id: The session to read.

        Returns:
            The stored ``turn_status`` value, or ``None``.
        """
        row = (
            await self._db.execute(
                text(
                    f"SELECT metadata -> '{TURN_STATUS_KEY}' FROM sessions WHERE session_id = :sid"
                ),
                {"sid": session_id},
            )
        ).first()
        if row is None:
            return None
        stored = _as_mapping(row[0])
        return dict(stored) if stored is not None else None

    async def clear_tools(self, session_id: UUID) -> None:
        """Reset the stored tools lane at a send, keeping the ctx reading (FRE-1538 rule).

        A session with no stored reading is left without one: absent stays absent.

        Args:
            session_id: The session the user sent on.
        """
        await self._db.execute(
            text(
                "UPDATE sessions SET metadata = jsonb_set(jsonb_set(metadata, "
                f"'{{{TURN_STATUS_KEY},tool_iteration}}', 'null'::jsonb), "
                f"'{{{TURN_STATUS_KEY},tool_iteration_max}}', 'null'::jsonb) "
                f"WHERE session_id = :sid AND jsonb_typeof(metadata -> '{TURN_STATUS_KEY}') = 'object'"
            ),
            {"sid": session_id},
        )
        await self._db.commit()


def _as_mapping(raw: object) -> Mapping[str, object] | None:
    """Normalize a JSONB ``->`` result (decoded dict or JSON text) to a mapping."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return cast("Mapping[str, object]", raw) if isinstance(raw, Mapping) else None


# ── In-flight turn registry ───────────────────────────────────────────────────


@dataclass
class _InflightTurns:
    """The running turns of one session: the earliest start and how many run."""

    start_after_seq: int
    count: int


_inflight: dict[str, _InflightTurns] = {}


def turn_started(session_id: str, start_after_seq: int) -> None:
    """Record that a turn of ``session_id`` started after event ``start_after_seq``.

    Overlapping turns on one session keep the earliest start, so a replay covers all of them.

    Args:
        session_id: The session the turn runs on.
        start_after_seq: The session's ``last_event_seq`` before the turn's first event.
    """
    running = _inflight.get(session_id)
    if running is None:
        _inflight[session_id] = _InflightTurns(start_after_seq=start_after_seq, count=1)
        return
    running.start_after_seq = min(running.start_after_seq, start_after_seq)
    running.count += 1


def turn_ended(session_id: str) -> None:
    """Record that one turn of ``session_id`` ended. An extra call is a no-op.

    Args:
        session_id: The session the turn ran on.
    """
    running = _inflight.get(session_id)
    if running is None:
        return
    running.count -= 1
    if running.count <= 0:
        del _inflight[session_id]


def turn_start_seq(session_id: str) -> int | None:
    """Return the seq after which the session's running turn started, or ``None``.

    Args:
        session_id: The session to look up.

    Returns:
        The replay start for the in-flight turn, or ``None`` when no turn runs in this process.
    """
    running = _inflight.get(session_id)
    return running.start_after_seq if running is not None else None
