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
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import anthropic
from fastapi import APIRouter, Depends, Form, HTTPException, Request

from personal_agent.config.settings import get_settings
from personal_agent.llm_client.message_content import get_text_content
from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.service.database import AsyncSessionLocal
from personal_agent.service.models import SessionModel
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.telemetry import get_logger
from personal_agent.transport.agui.transport import _push_event
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
    reservation_id: UUID | None = None,
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
        reservation_id: Cost-gate reservation token from ``chat()``; the
            stream's success path commits it with the actual cost, the
            failure path refunds it.
    """
    session_id_str = str(session_uuid)
    start_time = time.time()

    # --- Stream from Anthropic --------------------------------------------
    full_text = ""
    final_message: Any = None
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
                await _push_event(
                    TextDeltaEvent(text=text, session_id=session_id_str), session_id_str
                )
            # Capture the final message so we can settle the reservation
            # against actual input/output token counts (not the estimate).
            try:
                final_message = await stream.get_final_message()
            except Exception:
                final_message = None

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
        await _push_event(
            TextDeltaEvent(text=f"\n\n[API error: {exc}]", session_id=session_id_str),
            session_id_str,
        )
        if reservation_id is not None:
            await _refund_reservation_safe(reservation_id, trace_id)
    except Exception as exc:
        log.error(
            "chat.stream_failed",
            trace_id=trace_id,
            session_id=session_id_str,
            error=str(exc),
        )
        await _push_event(
            TextDeltaEvent(text=f"\n\n[Error: {exc}]", session_id=session_id_str),
            session_id_str,
        )
        if reservation_id is not None:
            await _refund_reservation_safe(reservation_id, trace_id)
    else:
        # Successful stream — commit the reservation with the actual cost.
        if reservation_id is not None:
            await _commit_reservation_safe(
                reservation_id=reservation_id,
                trace_id=trace_id,
                final_message=final_message,
            )
        # ADR-0078 D4 / FRE-405: close the gateway telemetry dark path. This
        # call previously bypassed canonical telemetry entirely; emit the
        # canonical model_call_completed so cost/cache/quality are attributable.
        _emit_gateway_model_call_completed(
            trace_id=trace_id,
            session_id=session_id_str,
            latency_ms=int((time.time() - start_time) * 1000),
            final_message=final_message,
        )
    finally:
        # Persist DONE to Postgres (so reconnect replay delivers it) then push
        # the None sentinel to close the live WS drain loop — serialized under
        # the per-session emit lock so the DONE seq + sentinel stay ordered
        # behind every prior live emit (FRE-518).
        from personal_agent.transport.agui.transport import emit_done  # noqa: PLC0415

        await emit_done(session_id_str)


async def _refund_reservation_safe(reservation_id: UUID, trace_id: str) -> None:
    """Refund a chat reservation; swallow + log any error rather than crash the task."""
    try:
        from personal_agent.cost_gate import get_default_gate_or_none

        gate = get_default_gate_or_none()
        if gate is not None:
            await gate.refund(reservation_id, trace_id=trace_id)
    except Exception as exc:
        log.error(
            "chat.refund_failed",
            trace_id=trace_id,
            reservation_id=str(reservation_id),
            error=str(exc),
        )


async def _commit_reservation_safe(
    *, reservation_id: UUID, trace_id: str, final_message: Any
) -> None:
    """Commit the reservation against the actual cost from the streamed response.

    Pricing comes from ``litellm.model_cost`` keyed on
    ``anthropic/<model>``. If usage data is missing or pricing isn't
    available, fall back to committing the original estimate (no settle) —
    the reaper would otherwise sweep the reservation and refund it
    incorrectly.
    """
    from decimal import Decimal as _Decimal

    try:
        import litellm  # noqa: PLC0415

        from personal_agent.cost_gate import get_default_gate_or_none

        gate = get_default_gate_or_none()
        if gate is None:
            return

        actual_cost = _Decimal("0")
        if final_message is not None and getattr(final_message, "usage", None) is not None:
            usage = final_message.usage
            pricing = getattr(litellm, "model_cost", {}).get(f"anthropic/{_CLOUD_MODEL}", {})
            input_price = _Decimal(str(pricing.get("input_cost_per_token", "0")))
            output_price = _Decimal(str(pricing.get("output_cost_per_token", "0")))
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            actual_cost = (
                _Decimal(input_tokens) * input_price + _Decimal(output_tokens) * output_price
            ).quantize(_Decimal("0.000001"))

        await gate.commit(reservation_id, actual_cost, trace_id=trace_id)
    except Exception as exc:
        log.error(
            "chat.commit_failed",
            trace_id=trace_id,
            reservation_id=str(reservation_id),
            error=str(exc),
        )


def _emit_gateway_model_call_completed(
    *,
    trace_id: str,
    session_id: str,
    latency_ms: int,
    final_message: Any,
) -> None:
    """Emit the canonical ``model_call_completed`` for the gateway chat path.

    The gateway talks to the Anthropic SDK directly and historically bypassed
    :mod:`personal_agent.llm_client.telemetry` entirely (ADR-0078 Context #3).
    This stamps the call with ``callsite="gateway.chat"`` and the
    ``gateway_persona`` component so the gateway path joins the rest of the
    harness in ES. Best-effort: any failure is logged, never raised, so a
    telemetry hiccup cannot break the user's stream.

    Args:
        trace_id: Correlation id for the request.
        session_id: Session id string for identity threading.
        latency_ms: Wall-clock latency of the streamed call.
        final_message: Anthropic final message (carries ``usage``), or None.
    """
    try:
        from personal_agent.llm_client.prompt_identity import derive_prompt_identity
        from personal_agent.llm_client.telemetry import emit_model_call_completed
        from personal_agent.telemetry.trace import TraceContext

        usage = getattr(final_message, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else None
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else None
        total = (
            (input_tokens or 0) + (output_tokens or 0)
            if (input_tokens is not None or output_tokens is not None)
            else None
        )

        identity = derive_prompt_identity(
            "gateway.chat",
            static_prefix=_SYSTEM_PROMPT,
            full_prompt=_SYSTEM_PROMPT,
            component_ids=("gateway_persona",),
        )
        emit_model_call_completed(
            log=log,
            role="primary",
            model=f"anthropic/{_CLOUD_MODEL}",
            endpoint="anthropic",
            trace_ctx=TraceContext(trace_id=trace_id, session_id=session_id, profile="cloud"),
            span_id=uuid4().hex,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            prompt_identity=identity,
            total_tokens=total,
        )
    except Exception as exc:
        log.error("chat.telemetry_emit_failed", trace_id=trace_id, error=str(exc))


# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------


@router.post("/chat")
async def chat(
    request: Request,
    message: str = Form(...),
    session_id: str = Form(...),
    profile: str = Form(default="cloud"),
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
) -> dict[str, str]:
    """Accept a user message and begin streaming the assistant response.

    Loads (or creates) the session from PostgreSQL **scoped to the caller's
    user_id**, prepends conversation history, launches a background
    streaming task, and returns immediately. The response is delivered
    asynchronously via ``GET /stream/{session_id}``.

    Args:
        request: FastAPI request (reserved for future context).
        message: User message text.
        session_id: Client-generated session UUID. Must belong to the
            authenticated user; cross-user collisions return 404 (existence
            of other users' sessions is never confirmed).
        profile: Execution profile (informational; cloud path only here).
        request_user: Resolved user identity (injected by FastAPI from CF
            Access header). Closes the cross-user data leak: a holder of
            the bearer token cannot read or create sessions owned by
            another user.

    Returns:
        ``{"session_id": ..., "trace_id": ..., "status": "streaming"}`` on success.

    Raises:
        HTTPException: 503 if the Anthropic API key is not configured.
        HTTPException: 422 if ``session_id`` is not a valid UUID.
        HTTPException: 404 if ``session_id`` already exists under a
            different user (do not confirm existence cross-user).
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
        # Scoped read: returns None if session belongs to another user.
        session = await repo.get(session_uuid, user_id=request_user.user_id)
        if not session:
            # Disambiguate "doesn't exist" from "owned by someone else": if
            # an unscoped lookup finds a row, the requester is trying to
            # impersonate — return 404 (not 403) to avoid confirming.
            other = await repo.get(session_uuid)
            if other is not None:
                log.warning(
                    "chat.session_cross_user_attempt",
                    trace_id=trace_id,
                    session_id=session_id,
                    requester_user_id=str(request_user.user_id),
                )
                raise HTTPException(status_code=404, detail="Session not found")
            now = datetime.now(timezone.utc)
            session = SessionModel(
                session_id=session_uuid,
                user_id=request_user.user_id,
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
                user_id=str(request_user.user_id),
            )

        # Build prior messages: strip to wire format only (role + content).
        raw_messages: list[Any] = list(session.messages) if session.messages else []
        prior_messages: list[dict[str, Any]] = [
            {"role": str(m["role"]), "content": get_text_content(m["content"])}
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

    # ── Cost Check Gate reservation (ADR-0065 / FRE-306) ────────────────
    # The streaming chat path bypasses LiteLLMClient (it talks to the
    # Anthropic SDK directly), so the gate must be invoked here. Reserve
    # before launching the background task; on BudgetDenied the FastAPI
    # exception handler in service/app.py renders a structured 503 (the
    # PWA's "budget denied" card) instead of an empty assistant turn —
    # which was the regression that motivated this whole ADR.
    reservation_id: UUID | None = None
    try:
        from decimal import Decimal as _Decimal

        from personal_agent.cost_gate import get_default_gate_or_none, load_budget_config
        from personal_agent.llm_client.cost_estimator import estimate_reservation_for_call

        gate = get_default_gate_or_none()
        if gate is not None:
            reservation_amount = estimate_reservation_for_call(
                role="main_inference",
                model=f"anthropic/{_CLOUD_MODEL}",
                messages=anthropic_messages,
                max_tokens=_MAX_TOKENS,
                config=load_budget_config(),
                trace_id=trace_id,
            )
            reservation_id = await gate.reserve(
                role="main_inference",
                amount=_Decimal(reservation_amount),
                trace_id=UUID(trace_id),
            )
        else:
            log.warning(
                "chat.cost_gate_not_initialized",
                trace_id=trace_id,
                note="streaming call proceeding without gate; check lifespan startup",
            )
    except Exception:
        # BudgetDenied (preferred) and other cost-gate errors propagate up
        # to the FastAPI exception handler that renders the 503.
        raise

    asyncio.create_task(
        _stream_to_queue(
            trace_id=trace_id,
            session_uuid=session_uuid,
            anthropic_messages=anthropic_messages,
            api_key=api_key,
            reservation_id=reservation_id,
        )
    )

    log.info(
        "chat.streaming_started",
        trace_id=trace_id,
        session_id=session_id,
        profile=profile,
        prior_message_count=len(prior_messages),
        reservation_id=str(reservation_id) if reservation_id else None,
    )
    return {"session_id": session_id, "trace_id": trace_id, "status": "streaming"}
