"""Consumer handlers for ``request.completed`` (FRE-158 / ADR-0041 Phase 2)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from personal_agent.events.models import EventBase, RequestCompletedEvent
from personal_agent.events.session_write_waiter import release_session_write_wait
from personal_agent.security import sanitize_error_message
from personal_agent.service.database import AsyncSessionLocal
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.es_handler import ElasticsearchHandler

log = get_logger(__name__)


def build_request_trace_es_handler(es_handler: ElasticsearchHandler | None) -> Any:
    """Build handler that indexes request trace from ``RequestCompletedEvent``."""

    async def handler(event: EventBase) -> None:
        if not isinstance(event, RequestCompletedEvent):
            return
        if not es_handler or not getattr(es_handler, "_connected", False):
            return
        await es_handler.es_logger.index_request_trace_from_snapshot(
            trace_id=event.trace_id,
            trace_summary=event.trace_summary,
            trace_breakdown=event.trace_breakdown,
            session_id=event.session_id,
        )

    return handler


def build_session_writer_handler() -> Any:
    """Build handler that appends assistant message and releases the session waiter."""

    async def handler(event: EventBase) -> None:
        if not isinstance(event, RequestCompletedEvent):
            return
        sid = event.session_id
        try:
            async with AsyncSessionLocal() as db:
                repo = SessionRepository(db)
                await repo.append_message(
                    UUID(sid),
                    {"role": "assistant", "content": event.assistant_response},
                )
        except Exception as e:
            log.error(
                "session_writer_append_failed",
                trace_id=event.trace_id,
                session_id=sid,
                error=sanitize_error_message(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise
        release_session_write_wait(sid)

    return handler
