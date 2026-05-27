"""Postgres-backed event buffer for WebSocket reconnect replay.

Events are durably stored in the ``session_events`` table with a global
Postgres sequence providing monotonic ``seq`` values.  On reconnect the
client sends its ``last_seq``; the server replays all events with
``seq > last_seq`` from this table, then switches to the live
asyncio.Queue drain.

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
        """Persist an event and return its Postgres-assigned ``seq``.

        Args:
            session_id: Target session.
            event_type: AG-UI event type (e.g. ``TEXT_DELTA``).
            payload: Full JSON envelope to replay on reconnect.

        Returns:
            The sequence number assigned by the ``session_events_seq``
            Postgres sequence.
        """
        result = await self._db.execute(
            text(
                "INSERT INTO session_events (session_id, event_type, payload, created_at) "
                "VALUES (:sid, :etype, CAST(:payload AS jsonb), NOW()) "
                "RETURNING seq"
            ),
            {
                "sid": session_id,
                "etype": event_type,
                "payload": _json_dumps(payload),
            },
        )
        seq: int = result.scalar_one()
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
