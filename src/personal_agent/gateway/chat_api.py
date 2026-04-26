"""Cloud-native chat endpoint for the Seshat Gateway.

Accepts a user message via ``POST /chat`` and streams the Anthropic response
into the AG-UI event queue for the given session.  The client should connect
to ``GET /stream/{session_id}`` immediately after this call returns to receive
``TEXT_DELTA`` events as the model generates its reply.

Session persistence: the user message is written synchronously in ``chat()``
before the background task is launched (no cancellation risk).  The assistant
message uses the same Redis-or-direct pattern as ``service.app``: when a
``RedisStreamBus`` is active the bus consumer (``build_session_writer_handler``)
performs the write; on the ``NoOpBus`` path the background task writes directly.
This invariant prevents double-writes when ``gateway_mount_local=True``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import anthropic
from fastapi import APIRouter, Form, HTTPException, Request

from personal_agent.config.settings import get_settings
from personal_agent.service.database import AsyncSessionLocal
from personal_agent.service.models import SessionModel
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.telemetry import get_logger
from personal_agent.transport.agui.endpoint import get_event_queue
from personal_agent.transport.events import TextDeltaEvent

log = get_logger(__name__)
router = APIRouter(tags=["chat"])

_SYSTEM_PROMPT = (
    "You are Seshat, a personal AI assistant with persistent memory "
    "and knowledge graph capabilities. You are helpful, thoughtful, and concise."
)
_CLOUD_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 8192


# ---------------------------------------------------------------------------
# Background streaming task
# ---------------------------------------------------------------------------


async def _stream_to_queue(
    trace_id: str,
    session_uuid: UUID,
    anthropic_messages: list[Any],
    api_key: str,
) -> None:
    """Stream an Anthropic response into the AG-UI per-session event queue.

    Runs as an ``asyncio.Task``; errors surface as a final error TEXT_DELTA
    rather than propagating as unhandled exceptions.  Persists the assistant
    message using the same Redis-or-direct pattern as ``service.app``:
    - ``RedisStreamBus``: emits ``RequestCompletedEvent``; the
      ``build_session_writer_handler`` consumer performs the DB write.
    - ``NoOpBus``: writes directly to PostgreSQL (no consumer running).

    The user message is **not** persisted here — ``chat()`` does that
    synchronously before launching this task to avoid cancellation races.

    Args:
        trace_id: Correlation identifier for this request.
        session_uuid: Target session UUID for DB writes and event routing.
        anthropic_messages: Full conversation history (prior + new user turn)
            in Anthropic wire format (``role`` + ``content`` only).
        api_key: Anthropic API key.
    """
    session_id_str = str(session_uuid)
    queue = get_event_queue(session_id_str)

    # --- Stream from Anthropic --------------------------------------------
    full_text = ""
    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        async with client.messages.stream(
            model=_CLOUD_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=anthropic_messages,
        ) as stream:
            async for text in stream.text_stream:
                full_text += text
                await queue.put(TextDeltaEvent(text=text, session_id=session_id_str))

        # --- Persist assistant message: bus consumer or direct write ------
        try:
            from personal_agent.events.bus import get_event_bus
            from personal_agent.events.models import STREAM_REQUEST_COMPLETED, RequestCompletedEvent
            from personal_agent.events.redis_backend import RedisStreamBus

            bus = get_event_bus()
            if isinstance(bus, RedisStreamBus):
                # Consumer (build_session_writer_handler) will write the
                # assistant message — do NOT write directly here to avoid
                # double-write when gateway_mount_local=True.
                await bus.publish(
                    STREAM_REQUEST_COMPLETED,
                    RequestCompletedEvent(
                        trace_id=trace_id,
                        session_id=session_id_str,
                        assistant_response=full_text,
                        trace_summary={
                            "model": _CLOUD_MODEL,
                            "steps_count": 1,
                            "final_state": "COMPLETED",
                        },
                        trace_breakdown=[],
                        source_component="gateway.chat_api",
                    ),
                )
            else:
                # NoOpBus — no consumer running; write directly.
                assistant_payload: dict[str, Any] = {
                    "role": "assistant",
                    "content": full_text,
                    "trace_id": trace_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metadata": {"source": "gateway.chat_api", "model": _CLOUD_MODEL},
                }
                async with AsyncSessionLocal() as db:
                    await SessionRepository(db).append_message(session_uuid, assistant_payload)
        except Exception as exc:
            log.error(
                "chat.persist_assistant_message_failed",
                trace_id=trace_id,
                session_id=session_id_str,
                error=str(exc),
            )

        log.info(
            "chat.streaming_completed",
            trace_id=trace_id,
            session_id=session_id_str,
        )

    except anthropic.APIError as exc:
        log.error(
            "chat.anthropic_api_error",
            trace_id=trace_id,
            session_id=session_id_str,
            status=getattr(exc, "status_code", None),
            error=str(exc),
        )
        await queue.put(TextDeltaEvent(text=f"\n\n[API error: {exc}]", session_id=session_id_str))
    except Exception as exc:
        log.error(
            "chat.stream_failed",
            trace_id=trace_id,
            session_id=session_id_str,
            error=str(exc),
        )
        await queue.put(TextDeltaEvent(text=f"\n\n[Error: {exc}]", session_id=session_id_str))
    finally:
        # Signal stream end regardless of success/failure.
        await queue.put(None)


# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------


@router.post("/chat")
async def chat(
    request: Request,
    message: str = Form(...),
    session_id: str = Form(...),
    profile: str = Form(default="cloud"),
) -> dict[str, str]:
    """Accept a user message and begin streaming the assistant response.

    Loads (or creates) the session from PostgreSQL, prepends conversation
    history, launches a background streaming task, and returns immediately.
    The response is delivered asynchronously via ``GET /stream/{session_id}``.

    Args:
        request: FastAPI request (unused; reserved for future auth/context).
        message: User message text.
        session_id: Client-generated session UUID.
        profile: Execution profile (informational; cloud path only here).

    Returns:
        ``{"session_id": ..., "trace_id": ..., "status": "streaming"}`` on success.

    Raises:
        HTTPException: 503 if the Anthropic API key is not configured.
        HTTPException: 422 if ``session_id`` is not a valid UUID.
    """
    trace_id = str(uuid4())
    settings = get_settings()
    api_key = settings.anthropic_api_key
    if not api_key:
        log.warning(
            "chat.api_key_missing",
            trace_id=trace_id,
            session_id=session_id,
        )
        raise HTTPException(status_code=503, detail="Anthropic API key not configured on this host")

    # Validate session_id format early.
    try:
        session_uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="session_id must be a valid UUID v4") from exc

    # --- Load or create session ------------------------------------------
    async with AsyncSessionLocal() as db:
        repo = SessionRepository(db)
        session = await repo.get(session_uuid)
        if not session:
            now = datetime.now(timezone.utc)
            session = SessionModel(
                session_id=session_uuid,
                created_at=now,
                last_active_at=now,
                mode="NORMAL",
                channel="CHAT",
                metadata_={},
                messages=[],
            )
            db.add(session)
            await db.commit()
            await db.refresh(session)
            log.info(
                "chat.session_created",
                trace_id=trace_id,
                session_id=session_id,
            )

        # Build prior messages: strip to wire format only (role + content).
        raw_messages: list[Any] = list(session.messages) if session.messages else []
        prior_messages: list[dict[str, Any]] = [
            {"role": str(m["role"]), "content": str(m["content"])}
            for m in raw_messages
            if isinstance(m, dict) and m.get("role") and m.get("content")
        ]

    anthropic_messages: list[Any] = prior_messages + [{"role": "user", "content": message}]

    # Persist user message synchronously before launching the background task
    # so that (a) the write is never lost to task cancellation and (b) a
    # follow-up request finds the user turn in session history immediately.
    now_iso = datetime.now(timezone.utc).isoformat()
    user_payload: dict[str, Any] = {
        "role": "user",
        "content": message,
        "trace_id": trace_id,
        "timestamp": now_iso,
        "metadata": {"source": "gateway.chat_api"},
    }
    try:
        async with AsyncSessionLocal() as db:
            await SessionRepository(db).append_message(session_uuid, user_payload)
    except Exception as exc:
        log.error(
            "chat.persist_user_message_failed",
            trace_id=trace_id,
            session_id=session_id,
            error=str(exc),
        )

    asyncio.create_task(
        _stream_to_queue(
            trace_id=trace_id,
            session_uuid=session_uuid,
            anthropic_messages=anthropic_messages,
            api_key=api_key,
        )
    )

    log.info(
        "chat.streaming_started",
        trace_id=trace_id,
        session_id=session_id,
        profile=profile,
        prior_message_count=len(prior_messages),
    )
    return {"session_id": session_id, "trace_id": trace_id, "status": "streaming"}
