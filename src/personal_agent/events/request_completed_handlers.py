"""Consumer handlers for ``request.completed`` (FRE-158 / ADR-0041 Phase 2)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from personal_agent.config.model_loader import resolve_active_attribution
from personal_agent.events.models import EventBase, RequestCompletedEvent
from personal_agent.events.session_write_waiter import release_session_write_wait
from personal_agent.security import sanitize_error_message
from personal_agent.service.database import AsyncSessionLocal
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


def build_session_writer_handler() -> Any:
    """Build handler that appends assistant message and releases the session waiter."""

    async def handler(event: EventBase) -> None:
        if not isinstance(event, RequestCompletedEvent):
            return
        sid = event.session_id
        # ADR-0074 (FRE-376) — assistant messages must carry model attribution.
        # FRE-1773: this consumer runs outside the turn's context, so it cannot see
        # the turn's model selection; the publisher resolves the model in the turn
        # and carries it on the event. An event without it falls back to the binding.
        primary_model_id, config_path_str = resolve_active_attribution(
            trace_id=event.trace_id,
        )
        if event.primary_model is not None:
            primary_model_id = event.primary_model
        message: dict[str, Any] = {
            "role": "assistant",
            "content": event.assistant_response,
            "trace_id": event.trace_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "source": event.source_component,
                "model": primary_model_id,
                "model_role": "primary",
                "model_config_path": config_path_str,
            },
        }
        # FRE-1543: the folded call history rides on the message, so REST hydration of a
        # reloaded page renders the panel the live turn showed.
        if event.turn_summary is not None:
            message["turn_summary"] = event.turn_summary.model_dump()
        try:
            async with AsyncSessionLocal() as db:
                repo = SessionRepository(db)
                await repo.append_message(UUID(sid), message)
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
