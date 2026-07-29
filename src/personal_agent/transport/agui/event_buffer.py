"""Postgres-backed event buffer for WebSocket reconnect replay.

Events are durably stored in the ``session_events`` table with monotonic
``seq`` values allocated **per session** from ``sessions.last_event_seq``.
On reconnect the client sends its ``last_seq``; the server replays all
events with ``seq > last_seq`` from this table, then switches to the live
asyncio.Queue drain.

The numbering is per-session because the client dispatches only a
*contiguous* run of ``seq`` for the session it is attached to. Until
FRE-1040 these values came from one global Postgres sequence shared by
every session, so a second live conversation consumed numbers inside this
session's series and the client stalled forever on a hole that could never
be filled on its own socket.

A background cleanup task deletes rows older than the configured TTL
(default 24 hours).

See: docs/architecture_decisions/ADR-0075-websocket-transport.md
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.exceptions import UnknownSessionError
from personal_agent.service.models import SessionEventModel
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class SessionEventBuffer:
    """Append-only Postgres buffer for AG-UI transport events.

    Args:
        db: Async SQLAlchemy session scoped to the current request/task.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize with an async SQLAlchemy session."""
        self._db = db

    async def append(
        self,
        session_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        """Persist an event and return its per-session ``seq``.

        The counter bump and the insert share one transaction. That matters
        twice over: the ``UPDATE`` takes a row lock, so concurrent appends for
        the same session serialise (different sessions never contend), and a
        failed insert rolls the allocation back instead of consuming a number
        and leaving a permanent hole in the series — the one failure mode the
        old ``nextval`` default could not avoid.

        Args:
            session_id: Target session.
            event_type: AG-UI event type (e.g. ``TEXT_DELTA``).
            payload: Full JSON envelope to replay on reconnect.

        Returns:
            The next sequence number for this session.

        Raises:
            UnknownSessionError: If *session_id* names no session row, so no
                sequence could be allocated. Silently dropping the event would
                leave the client's series intact but the response undelivered.
        """
        allocated = await self._db.execute(
            text(
                "UPDATE sessions SET last_event_seq = last_event_seq + 1 "
                "WHERE session_id = :sid "
                "RETURNING last_event_seq"
            ),
            {"sid": session_id},
        )
        row = allocated.first()
        if row is None:
            await self._db.rollback()
            raise UnknownSessionError(
                f"cannot append session_event for unknown session {session_id}"
            )
        seq = int(row[0])

        await self._db.execute(
            text(
                "INSERT INTO session_events (session_id, seq, event_type, payload, created_at) "
                "VALUES (:sid, :seq, :etype, CAST(:payload AS jsonb), NOW())"
            ),
            {
                "sid": session_id,
                "seq": seq,
                "etype": event_type,
                "payload": _json_dumps(payload),
            },
        )
        await self._db.commit()
        return seq

    async def replay(
        self,
        session_id: UUID,
        after_seq: int,
    ) -> list[dict[str, Any]]:
        """Return all events with ``seq > after_seq`` in insertion order.

        Args:
            session_id: Target session.
            after_seq: Sequence number of the last event the client received.

        Returns:
            List of dicts with ``seq`` and ``payload`` keys.
        """
        result = await self._db.execute(
            select(SessionEventModel.seq, SessionEventModel.payload)
            .where(
                SessionEventModel.session_id == session_id,
                SessionEventModel.seq > after_seq,
            )
            .order_by(SessionEventModel.seq),
        )
        return [{"seq": row.seq, "payload": row.payload} for row in result.all()]

    async def oldest_available_seq(self, session_id: UUID) -> int | None:
        """Return the smallest ``seq`` still retained for the session.

        Returns:
            The oldest seq, or ``None`` if no events exist.
        """
        result = await self._db.execute(
            select(SessionEventModel.seq)
            .where(SessionEventModel.session_id == session_id)
            .order_by(SessionEventModel.seq)
            .limit(1),
        )
        row = result.first()
        return row.seq if row is not None else None

    async def cleanup_expired(self, ttl_hours: int = 24) -> int:
        """Delete events older than *ttl_hours*.

        Returns:
            Number of rows deleted.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
        cursor = await self._db.execute(
            delete(SessionEventModel).where(SessionEventModel.created_at < cutoff),
        )
        await self._db.commit()
        deleted = int(getattr(cursor, "rowcount", 0) or 0)
        if deleted > 0:
            log.info("session_events.cleanup", rows_deleted=deleted, ttl_hours=ttl_hours)
        return deleted


def _json_dumps(obj: Any) -> str:
    """Serialize to JSON string for Postgres JSONB insertion."""
    import json

    return json.dumps(obj, default=str)
