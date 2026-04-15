"""Cloud-native chat endpoint for the Seshat Gateway.

Accepts a user message via ``POST /chat`` and streams the Anthropic response
into the AG-UI event queue for the given session.  The client should connect
to ``GET /stream/{session_id}`` immediately after this call returns to receive
``TEXT_DELTA`` events as the model generates its reply.

Note: conversation history is maintained by the PWA client.  The backend
processes one turn at a time (stateless per request).  Persistent server-side
history is a future improvement (requires resolving session UUID ownership).
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import anthropic
from fastapi import APIRouter, Form, HTTPException, Request

from personal_agent.config.settings import get_settings
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
    session_id: str,
    messages: list[dict[str, str]],
    api_key: str,
) -> None:
    """Stream an Anthropic response into the AG-UI per-session event queue.

    Runs as an ``asyncio.Task``; errors surface as a final error TEXT_DELTA
    rather than propagating as unhandled exceptions.

    Args:
        session_id: Target session for event routing.
        messages: Full conversation history including the latest user message.
        api_key: Anthropic API key.
    """
    queue = get_event_queue(session_id)

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        async with client.messages.stream(
            model=_CLOUD_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                await queue.put(TextDeltaEvent(text=text, session_id=session_id))

    except anthropic.APIError as exc:
        log.error(
            "chat.anthropic_api_error",
            session_id=session_id,
            status=getattr(exc, "status_code", None),
            error=str(exc),
        )
        await queue.put(
            TextDeltaEvent(text=f"\n\n[API error: {exc}]", session_id=session_id)
        )
    except Exception as exc:
        log.error("chat.stream_failed", session_id=session_id, error=str(exc))
        await queue.put(
            TextDeltaEvent(text=f"\n\n[Error: {exc}]", session_id=session_id)
        )
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

    The response is delivered asynchronously via ``GET /stream/{session_id}``.
    Returns as soon as the background streaming task is launched.

    Args:
        request: FastAPI request (unused; reserved for future auth/context).
        message: User message text.
        session_id: Client-generated session UUID.
        profile: Execution profile (informational; cloud path only here).

    Returns:
        ``{"session_id": ..., "status": "streaming"}`` on success.

    Raises:
        HTTPException: 503 if the Anthropic API key is not configured.
        HTTPException: 422 if ``session_id`` is not a valid UUID.
    """
    settings = get_settings()
    api_key = settings.anthropic_api_key
    if not api_key:
        raise HTTPException(
            status_code=503, detail="Anthropic API key not configured on this host"
        )

    # Validate session_id format early.
    try:
        UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="session_id must be a valid UUID v4"
        ) from exc

    messages: list[dict[str, str]] = [{"role": "user", "content": message}]

    asyncio.create_task(
        _stream_to_queue(
            session_id=session_id,
            messages=messages,
            api_key=api_key,
        )
    )

    log.info("chat.streaming_started", session_id=session_id, profile=profile)
    return {"session_id": session_id, "status": "streaming"}
