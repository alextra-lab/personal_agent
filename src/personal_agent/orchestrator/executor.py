"""Orchestrator execution loop and state machine.

This module implements the core orchestrator state machine with step functions.
The executor coordinates task execution through explicit state transitions.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4

from personal_agent.config import settings
from personal_agent.config.env_loader import Environment
from personal_agent.llm_client import ModelRole
from personal_agent.llm_client.message_content import (
    MessageContent,
    get_text_content,
    merge_content,
)
from personal_agent.llm_client.models import Placement
from personal_agent.observability.topology import observe_topology
from personal_agent.orchestrator import compression_manager
from personal_agent.orchestrator.context_window import (
    apply_context_window,
    estimate_messages_tokens,
)
from personal_agent.orchestrator.loop_gate import (
    GateDecision,
    GateResult,
    ToolLoopPolicy,
    stable_hash,
)
from personal_agent.orchestrator.routing import is_memory_recall_query
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call
from personal_agent.orchestrator.types import (
    ExecutionContext,
    OrchestratorResult,
    OrchestratorStep,
    TaskState,
)
from personal_agent.telemetry import (
    LLM_STEP_COMPLETED,
    MODEL_CALL_ERROR,
    ORCHESTRATOR_FATAL_ERROR,
    REPLY_READY,
    STATE_TRANSITION,
    STEP_EXECUTED,
    STEP_PLANNING_COMPLETED,
    STEP_PLANNING_STARTED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_STARTED,
    UNKNOWN_STATE,
    get_logger,
)
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools import ToolExecutionLayer, get_default_registry
from personal_agent.tools.registry import ToolRegistry

log = get_logger(__name__)

# ── Tool loop gate helpers ─────────────────────────────────────────────────

_cached_governance_config: object = None


def _get_cached_governance_config() -> object:
    """Module-level governance config cache. TODO: replace with @lru_cache after config singleton."""
    global _cached_governance_config
    if _cached_governance_config is None:
        from personal_agent.config import load_governance_config  # noqa: PLC0415

        _cached_governance_config = load_governance_config()
    return _cached_governance_config


def _get_tool_loop_policy(tool_name: str) -> ToolLoopPolicy:
    """Returns loop policy for tool_name, or ToolLoopPolicy() defaults if not configured.

    Args:
        tool_name: The name of the tool to look up in governance config.

    Returns:
        ToolLoopPolicy with values from governance config, or defaults if not found.
    """
    try:
        gov_config = _get_cached_governance_config()
        tool_policy = gov_config.tools.get(tool_name)  # type: ignore[attr-defined]
        if tool_policy is None:
            return ToolLoopPolicy()
        return ToolLoopPolicy(
            loop_max_per_signature=tool_policy.loop_max_per_signature,
            loop_max_consecutive=tool_policy.loop_max_consecutive,
            loop_output_sensitive=tool_policy.loop_output_sensitive,
            loop_consecutive_terminal=tool_policy.loop_consecutive_terminal,
        )
    except Exception:  # noqa: BLE001
        return ToolLoopPolicy()


def _resolve_max_iterations(ctx: "ExecutionContext") -> int:
    """Return the effective max-tool-iterations ceiling for this request.

    Uses the per-TaskType limit from settings when the gateway classified a
    task type, falling back to the global orchestrator_max_tool_iterations.
    The global value is the hard upper bound for the *base* ceiling; any
    ``tool_iteration_bonus`` granted by a user "Continue" decision at a
    constraint pause (ADR-0076) is added on top, since the user explicitly
    opted to proceed past the original limit.
    """
    global_max = settings.orchestrator_max_tool_iterations
    base = global_max
    if ctx.gateway_output is not None:
        task_type_val = ctx.gateway_output.intent.task_type.value
        by_type = settings.orchestrator_max_tool_iterations_by_task_type
        if task_type_val in by_type:
            base = min(by_type[task_type_val], global_max)
    return base + ctx.tool_iteration_bonus


# ── Constraint governance (ADR-0076 / FRE-389) ─────────────────────────────


def _is_turn_cancelled(session_id: str) -> bool:
    """Return whether the user requested cancellation for this session (ADR-0076)."""
    from personal_agent.transport.agui.ws_endpoint import is_cancel_requested

    return is_cancel_requested(session_id)


async def _emit_turn_cancelled(*, session_id: str, trace_id: str) -> None:
    """Emit a ``CANCELLED`` event and clear the cancel flag (ADR-0076)."""
    from personal_agent.transport.agui.transport import emit_cancelled
    from personal_agent.transport.agui.ws_endpoint import clear_cancel_flag

    log.info("user_cancel_synthesis", trace_id=trace_id, session_id=session_id)
    await emit_cancelled(session_id=session_id, trace_id=trace_id)
    clear_cancel_flag(session_id)


async def _emit_classified_error(ctx: "ExecutionContext", classified: "ClassifiedError") -> None:
    """Push a ``RUN_ERROR`` event so the PWA renders the classified failure (FRE-398).

    Best-effort: transport failures must never mask the real error or crash
    the executor.

    Args:
        ctx: Execution context providing ``session_id`` and ``trace_id``.
        classified: The structured error description to surface.
    """
    if not ctx.session_id:
        return
    try:
        from personal_agent.transport.agui.transport import emit_classified_error

        await emit_classified_error(
            session_id=ctx.session_id,
            trace_id=ctx.trace_id,
            classified=classified,
        )
    except Exception:
        log.debug(
            "classified_error_emit_failed",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
        )


def _resolve_context_max() -> int:
    """Return the active primary model's context window for the status meter.

    Resolves the profile-active primary model's ``context_length`` (e.g. 200K
    for cloud Sonnet, 131K for local Qwen) so the PWA meter reflects the real
    window instead of the static local budget (FRE-414). Falls back to the
    configured budget when the model config can't be resolved.

    Returns:
        The active model's context length, or ``settings.context_window_max_tokens``.
    """
    try:
        from personal_agent.config.model_loader import resolve_role_target  # noqa: PLC0415
        from personal_agent.config.selection import get_current_selection  # noqa: PLC0415

        _, model_def = resolve_role_target("primary", model_key=get_current_selection("primary"))
        if model_def is not None:
            return model_def.context_length
    except Exception:
        log.debug("context_max_resolve_failed")
    return settings.context_window_max_tokens


async def _report_turn_progress(ctx: "ExecutionContext") -> None:
    """Report live turn progress to the ADR-0088 spine (FRE-513).

    Publishes a best-effort ``turn.progress`` event carrying the executor-side live fields
    (tool iteration, context-window occupancy) the cost boundary cannot see. The live
    projector relays these onto ``turn_status`` (ADR-0076 sink); topologies never emit
    ``turn_status`` directly (ADR-0088 D4). Live cost is carried separately on
    ``turn.model_call_completed`` from the cost boundary (D3).

    Best-effort: a telemetry emission must never break the execution loop.

    Args:
        ctx: Execution context whose live metrics are reported.
    """
    if not ctx.session_id or not ctx.trace_id:
        return
    try:
        from personal_agent.events import get_event_bus
        from personal_agent.events.models import STREAM_TURN_OBSERVED, TurnProgressEvent

        await get_event_bus().publish(
            STREAM_TURN_OBSERVED,
            TurnProgressEvent(
                trace_id=str(ctx.trace_id),
                session_id=str(ctx.session_id),
                tool_iteration=ctx.tool_iteration_count,
                tool_iteration_max=_resolve_max_iterations(ctx),
                context_tokens=estimate_messages_tokens(ctx.messages),
                context_max=_resolve_context_max(),
                topology=ctx.topology,
            ),
            maxlen=settings.turn_observed_stream_maxlen,
        )
    except Exception:
        log.debug("turn_progress_publish_failed", trace_id=ctx.trace_id, session_id=ctx.session_id)


async def _load_constraint_preference(
    user_id: UUID | None,
    constraint_name: str,
    *,
    trace_id: str,
    session_id: str,
) -> str | None:
    """Load a user's standing preference for a constraint, if any.

    Args:
        user_id: Owning user UUID, or None for headless/API usage.
        constraint_name: Constraint name (e.g. ``tool_iteration_limit``).
        trace_id: Trace context identifier for telemetry correlation.
        session_id: Active session identifier for telemetry correlation.

    Returns:
        The stored ``action_id`` / ``always_pause`` string, or None when no
        preference exists or the lookup fails.
    """
    if user_id is None:
        return None
    from personal_agent.service.database import AsyncSessionLocal
    from personal_agent.service.repositories.constraint_preferences_repository import (
        ConstraintPreferencesRepository,
    )

    try:
        async with AsyncSessionLocal() as db:
            repo = ConstraintPreferencesRepository(db)
            return await repo.get_preferred_action(user_id, constraint_name)
    except Exception:
        log.exception(
            "constraint_preference_load_failed",
            constraint=constraint_name,
            trace_id=trace_id,
            session_id=session_id,
        )
        return None


async def _save_constraint_preference(
    user_id: UUID | None,
    constraint_name: str,
    action_id: str,
    *,
    trace_id: str,
    session_id: str,
) -> None:
    """Persist a standing constraint preference (the "Remember this choice" path).

    Args:
        user_id: Owning user UUID, or None for headless/API usage (no-op).
        constraint_name: Constraint name the preference applies to.
        action_id: Stable ``action_id`` chosen by the user.
        trace_id: Trace context identifier for telemetry correlation.
        session_id: Session where the preference was set (audit trail).
    """
    if user_id is None:
        return
    from personal_agent.service.database import AsyncSessionLocal
    from personal_agent.service.repositories.constraint_preferences_repository import (
        ConstraintPreferencesRepository,
    )

    source: UUID | None = None
    try:
        source = UUID(session_id)
    except (ValueError, AttributeError):
        source = None
    try:
        async with AsyncSessionLocal() as db:
            repo = ConstraintPreferencesRepository(db)
            await repo.upsert(
                user_id=user_id,
                constraint_name=constraint_name,
                preferred_action=action_id,
                source_session_id=source,
            )
    except Exception:
        log.exception(
            "constraint_preference_save_failed",
            constraint=constraint_name,
            trace_id=trace_id,
            session_id=session_id,
        )


# ---------------------------------------------------------------------------
# Durable pending cloud-attachment confirmation (FRE-749 / ADR-0101 §8b)
#
# When the pre-flight cloud-vision cost gate pauses on turn 1, the pending
# attachment refs must survive to turn 2 — a *separate HTTP request* served by a
# fresh Orchestrator + in-memory SessionManager (app.py builds one per request).
# The in-memory Session.metadata is therefore useless across the boundary; these
# helpers persist to the durable ``sessions.metadata`` JSONB column via
# ``AsyncSessionLocal`` + ``SessionRepository`` — the same executor→service
# idiom used by ``_save_constraint_preference`` above. Keyed off ``session_id``;
# best-effort with telemetry (invalid UUID / zero-row saves are logged, never
# raised) so the gate never fails a turn on a persistence hiccup.
# ---------------------------------------------------------------------------


def _pending_is_expired(pending: dict[str, Any], now: float) -> bool:
    """Return True when a pending confirmation payload has outlived its TTL.

    Args:
        pending: The stored payload (carries ``created_at`` + ``ttl_seconds``).
        now: Current Unix timestamp to compare against.

    Returns:
        True when ``now - created_at >= ttl_seconds`` (treats missing fields as
        expired, so a malformed record is dropped rather than replayed).
    """
    created_at = pending.get("created_at")
    ttl_seconds = pending.get("ttl_seconds")
    if created_at is None or ttl_seconds is None:
        return True
    return (now - float(created_at)) >= float(ttl_seconds)


async def _save_pending_state(
    session_id: str,
    pending: dict[str, Any],
    *,
    trace_id: str,
    save_repo_method: Callable[["SessionRepository", UUID, dict[str, Any]], Awaitable[int]],
    log_prefix: str,
) -> None:
    """Durably persist a pending-state payload via ``save_repo_method`` (FRE-749 / FRE-685).

    Shared body for the cloud-attachment-confirmation and document-continuation
    pending-state trios — they differ only in which ``SessionRepository``
    method they call and which log-event prefix they use.

    Args:
        session_id: Active session identifier (UUID string).
        pending: JSON-serializable pending-state payload.
        trace_id: Trace context identifier for telemetry correlation.
        save_repo_method: The ``SessionRepository`` save method to invoke.
        log_prefix: Event-name prefix for this pending-state kind's logs.
    """
    try:
        sid = UUID(session_id)
    except (ValueError, AttributeError):
        log.warning(f"{log_prefix}_save_bad_session", trace_id=trace_id, session_id=session_id)
        return

    from personal_agent.service.database import AsyncSessionLocal
    from personal_agent.service.repositories.session_repository import SessionRepository

    try:
        async with AsyncSessionLocal() as db:
            repo = SessionRepository(db)
            rows = await save_repo_method(repo, sid, pending)
        if rows == 0:
            log.warning(f"{log_prefix}_save_no_row", trace_id=trace_id, session_id=session_id)
    except Exception:
        log.exception(f"{log_prefix}_save_failed", trace_id=trace_id, session_id=session_id)


async def _load_pending_state(
    session_id: str,
    *,
    trace_id: str,
    load_repo_method: Callable[["SessionRepository", UUID], Awaitable[dict[str, Any] | None]],
    log_prefix: str,
    clear_fn: Callable[..., Awaitable[None]],
) -> dict[str, Any] | None:
    """Load a durable pending-state payload via ``load_repo_method``, applying TTL.

    Args:
        session_id: Active session identifier (UUID string).
        trace_id: Trace context identifier for telemetry correlation.
        load_repo_method: The ``SessionRepository`` load method to invoke.
        log_prefix: Event-name prefix for this pending-state kind's logs.
        clear_fn: This pending-state kind's own clear function, invoked on
            an expired record.

    Returns:
        The pending payload when present and unexpired; None otherwise. An
        expired record is cleared as a side effect before returning None.
    """
    try:
        sid = UUID(session_id)
    except (ValueError, AttributeError):
        return None

    from personal_agent.service.database import AsyncSessionLocal
    from personal_agent.service.repositories.session_repository import SessionRepository

    try:
        async with AsyncSessionLocal() as db:
            repo = SessionRepository(db)
            pending = await load_repo_method(repo, sid)
    except Exception:
        log.exception(f"{log_prefix}_load_failed", trace_id=trace_id, session_id=session_id)
        return None

    if pending is None:
        return None
    if _pending_is_expired(pending, time.time()):
        await clear_fn(session_id, trace_id=trace_id)
        return None
    return pending


async def _clear_pending_state(
    session_id: str,
    *,
    trace_id: str,
    clear_repo_method: Callable[["SessionRepository", UUID], Awaitable[None]],
    log_prefix: str,
) -> None:
    """Clear a durable pending-state record via ``clear_repo_method``.

    Args:
        session_id: Active session identifier (UUID string).
        trace_id: Trace context identifier for telemetry correlation.
        clear_repo_method: The ``SessionRepository`` clear method to invoke.
        log_prefix: Event-name prefix for this pending-state kind's logs.
    """
    try:
        sid = UUID(session_id)
    except (ValueError, AttributeError):
        return

    from personal_agent.service.database import AsyncSessionLocal
    from personal_agent.service.repositories.session_repository import SessionRepository

    try:
        async with AsyncSessionLocal() as db:
            repo = SessionRepository(db)
            await clear_repo_method(repo, sid)
    except Exception:
        log.exception(f"{log_prefix}_clear_failed", trace_id=trace_id, session_id=session_id)


async def _save_pending_cloud_confirmation(
    session_id: str, pending: dict[str, Any], *, trace_id: str
) -> None:
    """Durably persist a paused turn's pending cloud-attachment confirmation."""
    from personal_agent.service.repositories.session_repository import SessionRepository

    await _save_pending_state(
        session_id,
        pending,
        trace_id=trace_id,
        save_repo_method=SessionRepository.save_pending_confirmation,
        log_prefix="pending_cloud_confirmation",
    )


async def _load_pending_cloud_confirmation(
    session_id: str, *, trace_id: str
) -> dict[str, Any] | None:
    """Load a durable pending cloud-attachment confirmation, applying TTL."""
    from personal_agent.service.repositories.session_repository import SessionRepository

    return await _load_pending_state(
        session_id,
        trace_id=trace_id,
        load_repo_method=SessionRepository.load_pending_confirmation,
        log_prefix="pending_cloud_confirmation",
        clear_fn=_clear_pending_cloud_confirmation,
    )


async def _clear_pending_cloud_confirmation(session_id: str, *, trace_id: str) -> None:
    """Clear the durable pending cloud-attachment confirmation for a session."""
    from personal_agent.service.repositories.session_repository import SessionRepository

    await _clear_pending_state(
        session_id,
        trace_id=trace_id,
        clear_repo_method=SessionRepository.clear_pending_confirmation,
        log_prefix="pending_cloud_confirmation",
    )


async def _save_pending_document_continuation(
    session_id: str, pending: dict[str, Any], *, trace_id: str
) -> None:
    """Durably persist a turn's PDF page-budget continuation offer(s) (ADR-0102 §4 / FRE-685)."""
    from personal_agent.service.repositories.session_repository import SessionRepository

    await _save_pending_state(
        session_id,
        pending,
        trace_id=trace_id,
        save_repo_method=SessionRepository.save_pending_document_continuation,
        log_prefix="pending_document_continuation",
    )


async def _load_pending_document_continuation(
    session_id: str, *, trace_id: str
) -> dict[str, Any] | None:
    """Load a durable pending PDF page-budget continuation, applying TTL (ADR-0102 §4 / FRE-685)."""
    from personal_agent.service.repositories.session_repository import SessionRepository

    return await _load_pending_state(
        session_id,
        trace_id=trace_id,
        load_repo_method=SessionRepository.load_pending_document_continuation,
        log_prefix="pending_document_continuation",
        clear_fn=_clear_pending_document_continuation,
    )


async def _clear_pending_document_continuation(session_id: str, *, trace_id: str) -> None:
    """Clear the durable pending document-continuation record for a session."""
    from personal_agent.service.repositories.session_repository import SessionRepository

    await _clear_pending_state(
        session_id,
        trace_id=trace_id,
        clear_repo_method=SessionRepository.clear_pending_document_continuation,
        log_prefix="pending_document_continuation",
    )


async def _maybe_pause_for_constraint(
    *,
    session_id: str,
    trace_id: str,
    user_id: UUID | None,
    constraint: "ConstraintName",
    context: str,
    timeout_seconds: float | None = None,
    allow_preference: bool = True,
) -> "ConstraintDecision":
    """Pause and ask the user, or apply a stored preference (ADR-0076).

    Checks the user's standing preference first; if one is set (and not
    ``always_pause``) it is applied silently. Otherwise a ``CONSTRAINT_PAUSE``
    event is pushed over the WS transport and the executor blocks until the user
    responds, the user presses Stop, or the timeout fires.

    A momentary absence of a socket is **not** treated as a permanent one
    (FRE-928): the pause is persisted and registered even with no connection
    attached, so a client reconnecting inside the timeout is replayed the card and
    can still answer. A caller that genuinely has no client — headless, CLI — falls
    back to the safe default when the timeout expires.

    Args:
        session_id: Active session identifier.
        trace_id: Trace context identifier for telemetry correlation.
        user_id: Owning user UUID (None for headless usage).
        constraint: Constraint name (must be a key of ``CONSTRAINT_OPTIONS``).
        context: Human-readable description of the situation for the card.
        timeout_seconds: Seconds before the default option auto-applies. Defaults
            to ``settings.constraint_pause_timeout_seconds`` when omitted.
        allow_preference: When ``False``, a stored preference is neither read nor
            written for this pause — the user is always asked and no "remember"
            choice is persisted. Used for the ``attachment_cost`` (spend)
            confirmation so a remembered "always proceed" can never silently spend
            (ADR-0101 §8b / FRE-691). Defaults to ``True`` (all other constraints).

    Returns:
        A :class:`~personal_agent.orchestrator.constraint_options.ConstraintDecision`
        — a ``str`` subclass equal to the resolved ``action_id`` (existing callers
        that pattern-match a bare string are unaffected), carrying ``.resolution``
        for callers that must route differently for a genuine decision versus a
        no-decision fallback (ADR-0122 §4).
    """
    from personal_agent.orchestrator.constraint_options import (
        ConstraintDecision,
        resolve_options_and_default,
    )
    from personal_agent.transport.agui.transport import (
        emit_constraint_resolved,
        register_and_push_constraint,
    )
    from personal_agent.transport.agui.ws_endpoint import WaiterMetadata
    from personal_agent.transport.events import ConstraintPauseEvent

    if timeout_seconds is None:
        timeout_seconds = settings.constraint_pause_timeout_seconds

    # 1. Stored preference bypasses the pause entirely (telemetry-only record).
    #    Checked before resolving options so a preference hit never pays for the
    #    catalog projection a computed constraint (artifact_builder) would build.
    pref = (
        await _load_constraint_preference(
            user_id, constraint, trace_id=trace_id, session_id=session_id
        )
        if allow_preference
        else None
    )
    if pref and pref != "always_pause":
        log.info(
            "constraint_preference_applied",
            constraint=constraint,
            preferred_action=pref,
            trace_id=trace_id,
            session_id=session_id,
        )
        return ConstraintDecision(pref, "preference_applied")

    # 2. Resolve options + safe default — computed from the ADR-0121 catalog for a
    #    computed-options constraint (artifact_builder, ADR-0122 §3) rather than
    #    KeyError-ing the static registry — then register the waiter and push.
    opts, default_id = resolve_options_and_default(constraint)
    request_id = str(uuid4())
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)).isoformat()
    log.info(
        "constraint_pause_emitted",
        constraint=constraint,
        request_id=request_id,
        options=opts,
        trace_id=trace_id,
        session_id=session_id,
    )

    payload = await register_and_push_constraint(
        session_id=session_id,
        request_id=request_id,
        event=ConstraintPauseEvent(
            request_id=request_id,
            session_id=session_id,
            trace_id=trace_id,
            constraint=constraint,
            context=context,
            options=opts,
            default_option=default_id,
            expires_at=expires_at,
        ),
        metadata=WaiterMetadata(
            constraint=constraint,
            options=opts,
            default_option=default_id,
            created_at=time.monotonic(),
        ),
        timeout_seconds=timeout_seconds,
    )

    action_id = str(payload.get("decision", default_id))
    resolution = str(payload.get("resolution", "user_choice"))
    remember = bool(payload.get("remember", False))

    # Defensive: since FRE-928 the constraint waiter no longer returns connection_lost
    # (a disconnect leaves the waiter pending to ride its timeout), so this path is
    # unreachable via the pause transport. Kept because the resolution Literal still
    # admits it — but it is no longer "the no-WS path", which now times out instead.
    if resolution == "connection_lost":
        log.info(
            "constraint_no_ws_default_applied",
            constraint=constraint,
            default_option=default_id,
            trace_id=trace_id,
            session_id=session_id,
        )
        return ConstraintDecision(default_id, "connection_lost")

    log.info(
        "constraint_decision_received",
        constraint=constraint,
        action_id=action_id,
        resolution=resolution,
        request_id=request_id,
        trace_id=trace_id,
        session_id=session_id,
    )

    await emit_constraint_resolved(
        request_id=request_id,
        session_id=session_id,
        constraint=constraint,
        action_id=action_id,
        resolution=resolution,
    )
    log.info(
        "constraint_resolved_emitted",
        constraint=constraint,
        action_id=action_id,
        resolution=resolution,
        request_id=request_id,
        trace_id=trace_id,
        session_id=session_id,
    )

    if resolution == "timeout_default":
        log.info(
            "constraint_timeout_applied",
            constraint=constraint,
            default_option=default_id,
            request_id=request_id,
            trace_id=trace_id,
            session_id=session_id,
        )

    if remember and allow_preference:
        await _save_constraint_preference(
            user_id, constraint, action_id, trace_id=trace_id, session_id=session_id
        )

    return ConstraintDecision(action_id, resolution)


def _build_assistant_tool_calls(
    response_tool_calls: list[Any],
    turn_id: int,
) -> list[dict[str, Any]]:
    """Build the OpenAI-format ``tool_calls`` list for an assistant message.

    Prefixes the server-provided ``id`` with the request-local ``turn_id`` so
    ids are unique across turns within a single request. Server-side parsers
    (e.g. ``tool_call_parser="qwen3"``) typically regenerate ids starting
    from ``call_0`` on each turn; without a per-turn prefix those ids collide
    across rounds and the history sanitiser drops the resulting tool results
    as orphaned, which traps the agent in an unrecoverable re-discovery loop.

    Args:
        response_tool_calls: ToolCall objects from ``LLMResponse["tool_calls"]``.
        turn_id: Monotonically increasing counter for this request — typically
            ``ctx.tool_iteration_count`` taken at assistant-build time.

    Returns:
        List of OpenAI-format tool_call dicts (``id``, ``type``, ``function``,
        ``index``) suitable for assignment to ``assistant_message["tool_calls"]``.
    """
    return [
        {
            "id": f"call_t{turn_id}_{idx}_{tc['id']}" if tc.get("id") else f"call_t{turn_id}_{idx}",
            "type": "function",
            "function": {"name": tc["name"], "arguments": tc["arguments"]},
            "index": idx,  # Required by MLX backend per OpenAI API spec
        }
        for idx, tc in enumerate(response_tool_calls)
    ]


def _gate_blocked_result(
    tool_call_id: str,
    tool_name: str,
    gate_result: GateResult,
) -> dict[str, Any]:
    """Formats a tool result dict for gate-blocked calls.

    Args:
        tool_call_id: The tool call ID from the LLM response.
        tool_name: The name of the blocked tool.
        gate_result: The GateResult that triggered the block.

    Returns:
        A tool result dict suitable for appending to ctx.messages.
    """
    hints: dict[GateDecision, str] = {
        GateDecision.BLOCK_IDENTITY: (
            "Already retrieved these results. Use the previous tool output to answer."
        ),
        GateDecision.BLOCK_OUTPUT: (
            "Retrieved the same result before. Use the previous tool output to answer."
        ),
        GateDecision.BLOCK_CONSECUTIVE: (
            "Same tool called too many times consecutively without converging. "
            "Stop and synthesize from results gathered so far, or report what is missing."
        ),
    }
    return {
        "tool_call_id": tool_call_id,
        "role": "tool",
        "name": tool_name,
        "content": json.dumps(
            {
                "status": "done",
                "hint": hints.get(gate_result.decision, "Tool call blocked by loop gate."),
                "gate_decision": gate_result.decision.value,
            }
        ),
    }


# Entity type keywords for recall intent (ADR-0025) — map words to graph entity_type.
# Values are the ADR-0109 V2 10-type taxonomy (FRE-794); location/person/organization are
# stable across V1->V2, the rest were remapped from the retired Technology/Topic/Concept.
_ENTITY_TYPE_KEYWORDS: dict[str, str] = {
    "location": "Location",
    "locations": "Location",
    "place": "Location",
    "places": "Location",
    "city": "Location",
    "cities": "Location",
    "country": "Location",
    "countries": "Location",
    "person": "Person",
    "people": "Person",
    "someone": "Person",
    "organization": "Organization",
    "org": "Organization",
    "company": "Organization",
    "companies": "Organization",
    "tool": "TechnicalArtifact",
    "tools": "TechnicalArtifact",
    "technology": "TechnicalArtifact",
    "topic": "DomainOrTopic",
    "topics": "DomainOrTopic",
    "concept": "MethodOrConcept",
    "concepts": "MethodOrConcept",
    "phenomenon": "Phenomenon",
    "phenomena": "Phenomenon",
    "quantity": "QuantityMeasure",
    "quantities": "QuantityMeasure",
    "measurement": "QuantityMeasure",
    "measurements": "QuantityMeasure",
}


def _extract_entity_type_hints(user_message: str) -> list[str]:
    """Map words in the query to entity_type values (ADR-0025).

    e.g. "What Greek locations" -> ["Location"]
         "What tools have I used" -> ["TechnicalArtifact"]
         "What have I discussed" -> []
    """
    words = (user_message or "").lower().split()
    types: set[str] = set()
    for w in words:
        clean = w.strip('",.:;!?')
        if clean in _ENTITY_TYPE_KEYWORDS:
            types.add(_ENTITY_TYPE_KEYWORDS[clean])
    return list(types)


def _format_broad_recall(broad: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert query_memory_broad result to memory_context format (ADR-0025).

    The list is injected into the system prompt; keep it concise.
    """
    items: list[dict[str, Any]] = []
    for e in broad.get("entities", []):
        items.append(
            {
                "type": "entity",
                "name": e.get("name", ""),
                "entity_type": e.get("type", ""),
                "mentions": e.get("mentions", 0),
                "description": e.get("description") or "",
            }
        )
    for s in broad.get("sessions", []):
        items.append(
            {
                "type": "session",
                "session_id": s.get("session_id", ""),
                "dominant_entities": s.get("dominant_entities") or [],
                "turn_count": s.get("turn_count", 0),
            }
        )
    return items


# Global tool registry instance (initialized on first use)
_tool_registry: ToolRegistry | None = None
_tool_execution_layer: ToolExecutionLayer | None = None

if TYPE_CHECKING:  # pragma: no cover
    from personal_agent.error_classification import ClassifiedError
    from personal_agent.mcp.gateway import MCPGatewayAdapter
    from personal_agent.orchestrator.constraint_options import ConstraintDecision
    from personal_agent.service.repositories.session_repository import SessionRepository
    from personal_agent.transport.events import ConstraintName

_mcp_adapter: "MCPGatewayAdapter | None" = None


def _normalize_no_think_suffix(suffix: str) -> str:
    """Normalize the no-think suffix to a single token-like string.

    Args:
        suffix: Raw configured suffix (e.g., "/no_think" or " /no_think").

    Returns:
        Normalized suffix string, without trailing whitespace.
    """
    return suffix.strip()


def _validate_and_fix_conversation_roles(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate conversation role alternation and fix if needed for strict models like Mistral.

    Mistral models require:
    - Optional system message at position 0
    - After system (or from start), strict user/assistant alternation
    - Tool messages don't break alternation

    The OpenAI tool-calling pattern is::

        user → assistant{tool_calls} → tool → tool → … → assistant{tool_calls or content} → …

    Two assistants separated by tool messages are a *valid* multi-turn tool flow,
    not a duplicate. Merging them would drop the second assistant's
    ``tool_calls`` (the bug we hit when the agent looped re-calling tools because
    each turn lost the prior turn's tool_calls). Only merge when the immediate
    prior user/assistant message is the same role with NO tool messages between.

    This function:
    1. Preserves system message at start
    2. Ensures user/assistant alternation when truly consecutive
    3. Merges true duplicates (no intervening tools), preserving any tool_calls
    4. Preserves tool messages

    Args:
        messages: Original message list.

    Returns:
        Fixed message list with proper alternation.
    """
    if not messages:
        return messages

    fixed: list[dict[str, Any]] = []
    system_msg: dict[str, Any] | None = None

    # First pass: extract system message and build alternating sequence
    for msg in messages:
        role = msg.get("role")

        # Extract system message (only first one, keep at position 0)
        if role == "system":
            if system_msg is None:
                system_msg = msg
            continue

        # Tool messages: preserve them but don't affect alternation
        if role == "tool":
            fixed.append(msg)
            continue

        # For user/assistant: detect *true* consecutive duplicates.
        # A true duplicate is a same-role message immediately preceded by
        # another same-role user/assistant in `fixed` with no tool messages
        # between them. Tool messages reset the duplicate detector — that's
        # the valid OpenAI tool flow, not a duplicate.
        if role in ("user", "assistant"):
            # Walk fixed in reverse, skip tool messages, find the first
            # user/assistant. If it has the same role AND no tool sat
            # between, treat as a real duplicate.
            prior_idx: int | None = None
            saw_tool_between = False
            for i in range(len(fixed) - 1, -1, -1):
                prior_role = fixed[i].get("role")
                if prior_role == "tool":
                    saw_tool_between = True
                    continue
                if prior_role in ("user", "assistant"):
                    prior_idx = i
                    break

            is_true_duplicate = (
                prior_idx is not None
                and not saw_tool_between
                and fixed[prior_idx].get("role") == role
            )

            if is_true_duplicate:
                # Merge content into the prior message AND preserve tool_calls
                # if the incoming message had any (otherwise we silently
                # disarm a tool round, which is the failure we just fixed).
                assert prior_idx is not None  # narrowed by is_true_duplicate
                prior = fixed[prior_idx]
                old_content = prior.get("content", "")
                new_content = msg.get("content", "")
                # Block-aware merge (ADR-0101 §2, FRE-664): string-interpolating a
                # block list would corrupt it (embeds its Python repr). merge_content
                # concatenates blocks in order instead when either side is a list.
                prior["content"] = merge_content(old_content, new_content)
                # Preserve incoming tool_calls — concatenate when both sides have them.
                incoming_tool_calls = msg.get("tool_calls") or []
                if incoming_tool_calls:
                    existing_tool_calls = prior.get("tool_calls") or []
                    prior["tool_calls"] = list(existing_tool_calls) + list(incoming_tool_calls)
                log.warning(
                    "conversation_role_duplicate_merged",
                    role=role,
                    message_preview=str(new_content)[:50],
                    preserved_tool_calls=len(incoming_tool_calls),
                )
            else:
                fixed.append(msg)

    # Rebuild with system at start
    result: list[dict[str, Any]] = []
    if system_msg:
        result.append(system_msg)
    result.extend(fixed)

    # Final validation: only flag as a fault when two same-role user/assistant
    # messages are immediately adjacent with no tool message between them. Tool
    # messages between same-role assistants are the valid OpenAI tool-call
    # pattern (assistant{tool_calls} → tool → assistant{synthesis}).
    saw_tool_between = False
    prev_user_or_asst: str | None = None
    for i, msg in enumerate(result):
        role = msg.get("role")
        if role == "system":
            continue
        if role == "tool":
            saw_tool_between = True
            continue
        if role in ("user", "assistant"):
            if role == prev_user_or_asst and not saw_tool_between:
                log.error(
                    "conversation_role_alternation_failed",
                    position=i,
                    role=role,
                    message="Failed to fix conversation alternation - consecutive same roles remain",
                )
            prev_user_or_asst = role
            saw_tool_between = False

    return result


def _no_think_applies() -> bool:
    """Whether the ``/no_think`` suffix should be injected for the active model.

    ``/no_think`` is a Qwen control token (FRE-417); it is meaningless noise for
    non-Qwen models such as cloud Sonnet, where it just pollutes the prompt.
    Gate injection to the active primary model being a Qwen model. Defaults to
    ``True`` when the model can't be resolved (preserves prior behaviour).

    Returns:
        True when the active primary model is a Qwen-family model.
    """
    try:
        from personal_agent.config.model_loader import resolve_role_target  # noqa: PLC0415
        from personal_agent.config.selection import get_current_selection  # noqa: PLC0415

        _, model_def = resolve_role_target("primary", model_key=get_current_selection("primary"))
        if model_def is not None:
            return "qwen" in model_def.id.lower()
    except Exception:
        log.debug("no_think_applies_resolve_failed")
    return True


def _append_no_think_to_last_user_message(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append the configured no-think suffix to the last user message.

    This is used for tool-request prompts where the last message is typically the user request.
    The original message list is not mutated.
    """
    suffix = _normalize_no_think_suffix(settings.llm_no_think_suffix)
    if not settings.llm_append_no_think_to_tool_prompts or not suffix or not _no_think_applies():
        return messages

    out = deepcopy(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") != "user":
            continue
        content = out[i].get("content")
        if not isinstance(content, str):
            # Block-list content (e.g. an image attachment, ADR-0101 §2) — do not
            # stringify it, and do not fall through to an OLDER user message
            # either (that would misapply the suffix to an unrelated turn).
            return out
        trimmed = content.rstrip()
        if trimmed.endswith(suffix):
            return out
        # Append /no_think on a new line to clearly separate it from user query
        # This prevents models from misinterpreting it as a directory path
        out[i]["content"] = f"{trimmed}\n{suffix}"
        return out
    return out


def _append_no_think_synthesis_nudge(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure /no_think is the final suffix in post-tool synthesis prompts.

    In synthesis, the last message is often a tool output. To place the suffix at the end of the
    prompt, we append a short user nudge that ends with the suffix. The original message list is
    not mutated.

    Important: Only appends if the last message is NOT a user message, to avoid violating
    conversation alternation rules required by strict models.
    """
    suffix = _normalize_no_think_suffix(settings.llm_no_think_suffix)
    if not settings.llm_append_no_think_to_tool_prompts or not suffix or not _no_think_applies():
        return messages

    out = deepcopy(messages)

    # Check last message role to avoid violating alternation
    if len(out) > 0 and out[-1].get("role") == "user":
        # Last message is already user - just append suffix to it
        content = out[-1].get("content", "")
        if isinstance(content, str) and not content.rstrip().endswith(suffix):
            out[-1]["content"] = f"{content.rstrip()}\n{suffix}"
        return out

    # Safe to append new user message (last was assistant or tool)
    out.append({"role": "user", "content": f"Return the final answer now. {suffix}"})
    return out


_TURN_CONTEXT_OPEN = "<turn_context>"
_TURN_CONTEXT_CLOSE = "</turn_context>"


def _inline_volatile_into_last_user_message(
    messages: list[dict[str, Any]], volatile_block: str
) -> list[dict[str, Any]]:
    """Inline per-turn volatile content into the last user message (ADR-0081 §D2).

    Frozen append-only layout (FRE-434): per-turn volatile content (recalled
    memory + selected skill bodies + D3 highlights) must ride the *current* user
    turn rather than the system head, so prior turns replay byte-identically and
    the local SLM can reuse its KV cache as a strict forward extension.

    The block is wrapped in a single ``<turn_context>`` fence and prepended above
    the existing user content. The function is pure (returns a new list, never
    mutates the input) and byte-stable: an empty or whitespace-only block is a
    no-op (no separator bytes leak onto the frozen side), and re-inlining an
    already-wrapped message does not double-wrap.

    Args:
        messages: Working message list. Not mutated.
        volatile_block: Pre-joined volatile content; empty when there is nothing
            to inline this turn.

    Returns:
        A new message list with the block inlined into the last user message, or
        the input list unchanged when there is nothing to inline, the last user
        content is non-string, or no user message exists.
    """
    block = volatile_block.strip() if volatile_block else ""
    if not block:
        return messages
    out = deepcopy(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") != "user":
            continue
        content = out[i].get("content")
        if not isinstance(content, str):
            return messages
        if content.lstrip().startswith(_TURN_CONTEXT_OPEN):
            # Already wrapped this turn — never double-wrap (byte stability).
            return out
        out[i]["content"] = f"{_TURN_CONTEXT_OPEN}\n{block}\n{_TURN_CONTEXT_CLOSE}\n\n{content}"
        return out
    return messages


def _frozen_backend() -> str:
    """Return the active backend (``"local"``/``"cloud"``) for the scheduler.

    Derived from the resolved ``primary`` deployment's provider placement
    (ADR-0121 T5 — the placement fact now lives on the provider, not a
    profile). Defaults to ``"local"`` when resolution fails — the
    conservative choice, since local has the larger reset cost and the
    longer run cadence.
    """
    try:
        from personal_agent.config.model_loader import (  # noqa: PLC0415
            load_model_config,
            resolve_role_target,
        )
        from personal_agent.config.selection import get_current_selection  # noqa: PLC0415

        config = load_model_config()
        key, _ = resolve_role_target("primary", model_key=get_current_selection("primary"))
        return config.placement_of(key).value
    except Exception:
        return "local"


def _derive_reset_inputs(messages: list[dict[str, Any]], backend: str) -> dict[str, Any]:
    """Derive cache-reset scheduler inputs from the working history (ADR-0081 §D3).

    The frozen layout's deterministic growth makes every term measurable. ``R``
    (reset cost) and ``Δ_turn`` are first-order estimates tuned post-deploy per
    the ADR; the ``min_run`` floor and the token ceiling are the hard operative
    bounds, and ``Q_slope`` defaults to 0 (token-ceiling fallback) until an
    FRE-407 trace is available to fit it.

    Args:
        messages: Current working history.
        backend: ``"local"`` or ``"cloud"``.

    Returns:
        Keyword-argument mapping for
        :func:`cache_reset_scheduler.should_reset`.
    """
    turns_held = sum(1 for m in messages if m.get("role") == "user")
    accumulated = estimate_messages_tokens(messages)
    delta_turn = accumulated / turns_held if turns_held else float(accumulated)
    max_tokens = settings.context_window_max_tokens
    if backend == "cloud":
        min_run = settings.cache_reset_min_run_turns_cloud
        # Cloud caches the frozen prefix; only the rewritten span re-creates.
        reset_cost = max(delta_turn, 1.0)
    else:
        min_run = settings.cache_reset_min_run_turns_local
        # Local pays a full re-prefill of the post-reset prefix (≈ tail floor).
        reset_cost = float(int(settings.within_session_min_tail_ratio * max_tokens))
    return {
        "turns_since_reset": turns_held,
        "accumulated_tokens": accumulated,
        "accum_max_tokens": int(settings.cache_frozen_accum_max_ratio * max_tokens),
        "min_run_turns": min_run,
        "reset_cost_tokens": reset_cost,
        "delta_turn_tokens": delta_turn,
        "quality_token_weight": settings.cache_quality_token_weight,
        # quality_slope: not yet wired from FRE-554/570/572 quality signals;
        # 0.0 means the scheduler runs in the token-ceiling-only degenerate
        # case (c = Δ_turn, quality penalty term = 0) (FRE-576 F3).
        "quality_slope": 0.0,
    }


def _emit_cadence_monitor_doc(
    trace_id: str,
    session_id: str,
    backend: str,
    actual_turns: int,
    optimal_run_length: float,
    reason: str,
) -> None:
    """Emit a cadence monitor ES doc when a frozen reset fires (ADR-0092 §D7, FRE-572).

    Writes to ``agent-monitors-cache-reset-cadence-<date>`` so Kibana can aggregate
    ``actual_turns`` vs ``l_star`` (the computed ADR-0081 optimum) and validate
    whether the scheduler fires at the right cadence in production.

    ``l_star`` and ``deviation_turns`` are ``None`` when ``optimal_run_length``
    is ``math.inf`` (no hold-cost pressure, only the token ceiling drives resets).

    Args:
        trace_id: Turn trace identifier.
        session_id: Owning session identifier.
        backend: SLM backend label (``"llamacpp"`` / ``"mlx"``).
        actual_turns: Turns elapsed since the last reset (from ``turns_since_reset``).
        optimal_run_length: The computed ``L*`` from :func:`should_reset`.
        reason: Reset decision reason (``"optimum"`` / ``"token_ceiling"``).
    """
    import math  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    from personal_agent.captains_log.es_indexer import schedule_es_index  # noqa: PLC0415

    ts = datetime.now(timezone.utc)
    l_star: float | None = None if math.isinf(optimal_run_length) else optimal_run_length
    deviation: float | None = (
        round(actual_turns - optimal_run_length, 2) if l_star is not None else None
    )
    index_name = f"agent-monitors-cache-reset-cadence-{ts.strftime('%Y-%m-%d')}"
    doc = {
        "@timestamp": ts.isoformat(),
        "trace_id": trace_id,
        "session_id": session_id,
        "backend": backend,
        "actual_turns": actual_turns,
        "l_star": l_star,
        "deviation_turns": deviation,
        "reason": reason,
    }
    schedule_es_index(index_name, doc, doc_id=f"{trace_id}:D")


async def _maybe_frozen_reset(ctx: ExecutionContext) -> None:
    """Fire a scheduled frozen-prefix reset when the scheduler decides to.

    ADR-0081 §D3: when the run reaches the cost/quality optimum (or the token
    ceiling), compact ``ctx.messages`` into ``[first user][assistant recap][K
    verbatim turns]`` and stash the volatile salient highlights for this turn.
    Strictly gated on ``cache_frozen_layout_enabled`` so the flag-off path is
    byte-for-byte unchanged.

    Args:
        ctx: Execution context (``ctx.messages`` and ``ctx.salient_highlights``
            are updated in place on a reset).
    """
    if not settings.cache_frozen_layout_enabled or not ctx.session_id:
        return

    import math  # noqa: PLC0415

    from personal_agent.orchestrator.cache_reset_scheduler import (  # noqa: PLC0415
        marginal_hold_cost,
        should_reset,
    )
    from personal_agent.orchestrator.within_session_compression import (  # noqa: PLC0415
        build_frozen_reset,
    )

    backend = _frozen_backend()
    inputs = _derive_reset_inputs(ctx.messages, backend)
    decision = should_reset(**inputs)

    # Log every evaluation so quality_slope inertness and L* are observable
    # even when no reset fires (FRE-576 F3).
    _c = marginal_hold_cost(
        inputs["delta_turn_tokens"],
        inputs.get("quality_slope", 0.0),
        inputs["quality_token_weight"],
    )
    log.info(
        "cache_reset_decision",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        backend=backend,
        should_reset=decision.should_reset,
        reason=decision.reason,
        optimal_run_length=(
            decision.optimal_run_length if decision.optimal_run_length != math.inf else None
        ),
        quality_slope=inputs.get("quality_slope", 0.0),
        marginal_hold_cost=round(_c, 2),
        turns_since_reset=inputs["turns_since_reset"],
    )

    if not decision.should_reset:
        return

    result = await build_frozen_reset(
        ctx.messages,
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
    )
    ctx.messages = result.messages
    ctx.salient_highlights = result.salient_highlights
    log.info(
        "frozen_reset_fired",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        backend=backend,
        reason=decision.reason,
        optimal_run_length=decision.optimal_run_length,
        turns_since_reset=inputs["turns_since_reset"],
        output_messages=len(result.messages),
    )
    # ADR-0092 §D7: cadence monitor ES doc — actual vs L* for Kibana aggregation.
    _emit_cadence_monitor_doc(
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        backend=backend,
        actual_turns=inputs["turns_since_reset"],
        optimal_run_length=decision.optimal_run_length,
        reason=decision.reason,
    )
    # ADR-0092 §D8: emit D marker on stream:turn.observed so the projector can fold it
    # into the session aggregate as a cache_reset_count entry (dedup by fact_id).
    try:
        from personal_agent.events import get_event_bus  # noqa: PLC0415
        from personal_agent.events.models import (  # noqa: PLC0415
            STREAM_TURN_OBSERVED,
            CompactionDMarkerEvent,
        )

        await get_event_bus().publish(
            STREAM_TURN_OBSERVED,
            CompactionDMarkerEvent(
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                reason=decision.reason,
                optimal_run_length=float(decision.optimal_run_length),
                fact_id=f"{ctx.trace_id}:D",
            ),
            maxlen=settings.turn_observed_stream_maxlen,
        )
    except Exception:
        pass  # best-effort; never block the executor turn


def _fallback_reply_from_tool_results(ctx: ExecutionContext, *, lead: str | None = None) -> str:
    """Build a safe, user-facing reply when the model fails to synthesize after tools.

    Args:
        ctx: Execution context whose ``tool_results`` list is inspected.
        lead: Optional opening line; overrides the default "I reached my
            tool-use limit…" text so callers can supply context-appropriate
            framing (e.g. "The model call failed, but here's what I gathered:").
    """
    if not ctx.tool_results:
        return (
            "I couldn't produce a final answer. Try rephrasing your request or being more specific."
        )

    last_results = ctx.tool_results[-3:]
    default_lead = "I reached my tool-use limit before completing a synthesis. Here are the latest tool results:"
    lines: list[str] = [lead if lead is not None else default_lead]
    for r in last_results:
        tool_name = r.get("tool_name", "unknown_tool")
        success = r.get("success", False)
        if success:
            lines.append(f"- {tool_name}: success")
        else:
            err = r.get("error") or "Unknown error"
            lines.append(f"- {tool_name}: failed ({err})")
    return "\n".join(lines)


def _select_no_tool_final_reply(
    ctx: ExecutionContext, response_content: str, reasoning_trace: str | None
) -> str:
    """Choose the final reply for a turn that produced no tool calls.

    Priority: the model's content, then a substantive reasoning trace, then the
    tool-results fallback. Thinking models (Qwen3.6) can emit the entire answer in
    the reasoning/thinking channel with empty content — notably on vision turns
    (ADR-0101) — which otherwise collapses to a generic "Task completed"
    (FRE-734 Defect 2). The reasoning trace is surfaced ONLY when content is empty,
    so it is the answer itself, not internal scratchpad shadowing a real answer.

    Args:
        ctx: Execution context (its ``tool_results`` feed the final fallback).
        response_content: The model's cleaned content for this turn (may be empty).
        reasoning_trace: The model's thinking/reasoning text, if any.

    Returns:
        The user-facing reply string.
    """
    if response_content:
        return response_content
    trace = (reasoning_trace or "").strip()
    if trace:
        return trace
    return _fallback_reply_from_tool_results(ctx)


# FRE-484: minimal placeholder so Anthropic accepts a forced-synthesis call whose
# history references tools, when the active mode currently exposes no tool defs.
# Never invoked — tool_choice is pinned to "none".
_SYNTHESIS_PLACEHOLDER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "noop",
        "description": (
            "Placeholder so Anthropic accepts a no-tool synthesis call whose "
            "history references tools. Never invoked (tool_choice is 'none')."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


def _transcript_has_tool_blocks(messages: Sequence[Mapping[str, Any]]) -> bool:
    """Return True if the transcript already contains tool_use/tool_result blocks.

    Anthropic requires ``tools=`` on any request whose message history references
    tools (assistant ``tool_calls`` or ``role="tool"`` results), even for a no-tool
    synthesis call (FRE-484).

    Args:
        messages: Conversation messages in OpenAI format.

    Returns:
        True if any message carries assistant ``tool_calls`` or is a tool result.
    """
    for msg in messages:
        if msg.get("role") == "tool" or msg.get("tool_calls"):
            return True
    return False


def _forced_synthesis_tool_overrides(
    *,
    provider: str | None,
    messages: Sequence[Mapping[str, Any]],
    tool_defs: Sequence[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Resolve ``(tools, tool_choice)`` for a forced-synthesis model call.

    The forced-synthesis path normally drops ``tools=`` so the model answers from
    gathered results. On Anthropic, a transcript that already contains tool blocks
    makes LiteLLM reject the call with ``UnsupportedParamsError`` when ``tools=`` is
    absent (FRE-484). For that case only, keep a non-empty tool list and pin
    ``tool_choice="none"`` so the model synthesizes instead of calling more tools.
    Prefer the real mode ``tool_defs`` (best prompt-cache continuity); fall back to
    a single placeholder tool when none are available so the call still succeeds.

    Args:
        provider: Cloud provider name; ``"anthropic"`` triggers the workaround.
            ``None`` for the local SLM path.
        messages: Current conversation messages (OpenAI format).
        tool_defs: Tool definitions for the active mode, or ``None``.

    Returns:
        ``(tools, tool_choice)``. Every path except Anthropic-with-tool-history
        returns ``(None, None)`` — identical to the prior drop-tools behavior.
    """
    if provider == "anthropic" and _transcript_has_tool_blocks(messages):
        tools = list(tool_defs) if tool_defs else [dict(_SYNTHESIS_PLACEHOLDER_TOOL)]
        return tools, "none"
    return None, None


def _unwrap_embedded_response_json(response_content: str) -> str:
    """Best-effort: unwrap models that emit router-style JSON with a `response` field."""
    candidate = response_content.strip()
    if not candidate:
        return response_content

    # Remove markdown code fences if present
    if candidate.startswith("```"):
        lines = candidate.split("\n")
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()

    if not (candidate.startswith("{") and candidate.endswith("}")):
        return response_content

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return response_content

    if isinstance(data, dict):
        embedded = data.get("response")
        if isinstance(embedded, str) and embedded.strip():
            return embedded.strip()

    return response_content


def _get_tool_execution_layer() -> ToolExecutionLayer:
    """Get or create the global tool execution layer.

    Returns:
        ToolExecutionLayer instance with MVP tools registered.
    """
    global _tool_execution_layer
    if _tool_execution_layer is None:
        global _tool_registry
        if _tool_registry is None:
            _tool_registry = get_default_registry()
        _tool_execution_layer = ToolExecutionLayer(_tool_registry)
    return _tool_execution_layer


async def _initialize_mcp_gateway() -> None:
    """Initialize MCP Gateway adapter if enabled.

    Called during orchestrator startup to discover and register MCP tools.
    If gateway fails to initialize, logs warning and continues (graceful degradation).
    """
    global _mcp_adapter

    # If already connected, don't re-initialize.
    # Note: If a previous init attempt failed, adapter.client will be None; allow retry.
    if _mcp_adapter is not None and getattr(_mcp_adapter, "client", None) is not None:
        return

    if not settings.mcp_gateway_enabled:
        log.debug("mcp_gateway_not_enabled")
        return

    try:
        from personal_agent.mcp.gateway import (
            MCPGatewayAdapter,
            get_active_mcp_gateway_adapter,
        )

        # Get or create registry
        global _tool_registry
        if _tool_registry is None:
            _tool_registry = get_default_registry()

        existing = get_active_mcp_gateway_adapter()
        if existing is not None and getattr(existing, "client", None) is not None:
            _mcp_adapter = existing
            log.info(
                "mcp_gateway_reusing_existing_adapter",
                tools_count=len(existing._mcp_tool_names),
            )
            return

        _mcp_adapter = MCPGatewayAdapter(_tool_registry)
        await _mcp_adapter.initialize()

    except Exception as e:
        log.error(
            "mcp_gateway_init_failed", error=str(e), error_type=type(e).__name__, exc_info=True
        )
        # Graceful degradation: continue without MCP


async def _shutdown_mcp_gateway() -> None:
    """Shutdown MCP Gateway adapter."""
    global _mcp_adapter

    if _mcp_adapter:
        try:
            await _mcp_adapter.shutdown()
        except Exception as e:
            log.error("mcp_gateway_shutdown_failed", error=str(e), exc_info=True)
        finally:
            _mcp_adapter = None


# ============================================================================
# Helper Functions for Routing
# ============================================================================


def _determine_initial_model_role(ctx: ExecutionContext) -> ModelRole:
    """Determine initial model role based on channel.

    All channels route to PRIMARY (ADR-0033). Coding tasks no longer have a
    dedicated local model role — the primary agent decides whether to handle
    directly or delegate via DelegationPackage (Slice 3).

    Args:
        ctx: Execution context.

    Returns:
        Initial model role to use.
    """
    return ModelRole.PRIMARY


def _resolve_vision_routing_key(ctx: ExecutionContext, role_name: str) -> str:
    """Resolve the model config key for this role, enforcing vision capability.

    No-op (returns the profile-resolved key unchanged) when the turn carries no
    raster-image attachment. Otherwise resolves the pinned ``vision`` role
    (ADR-0121 §5, FRE-920) unconditionally — no per-attachment override, no
    profile, no escalation choice. Vision has exactly one model.

    Args:
        ctx: Execution context carrying ``attachments`` (FRE-661).
        role_name: The model role string (e.g. "primary").

    Returns:
        The model config key to use for this call.

    Raises:
        AttachmentUnsupportedError: The pinned ``vision`` deployment does not
            support vision — a config-drift guard, not a routing choice.
    """
    from personal_agent.config.model_loader import resolve_role_target  # noqa: PLC0415
    from personal_agent.config.selection import get_current_selection  # noqa: PLC0415
    from personal_agent.exceptions import AttachmentUnsupportedError
    from personal_agent.orchestrator.attachment_resolution import RASTER_CONTENT_TYPES

    image_attachments = [a for a in ctx.attachments if a.content_type in RASTER_CONTENT_TYPES]
    if not image_attachments:
        # Must return a real DEPLOYMENT key: callers look the result up in the
        # catalog, and the bare role name stopped being a key under ADR-0121.
        return resolve_role_target(role_name, model_key=get_current_selection(role_name))[0]

    key, model_def = resolve_role_target("vision")
    if model_def is not None and model_def.supports_vision:
        return key
    raise AttachmentUnsupportedError(
        "This turn includes an image, but the configured vision model does not support vision."
    )


def _resolve_document_routing_key(
    ctx: ExecutionContext, role_name: str
) -> tuple[str, Literal["native_pdf", "rasterize"]]:
    """Resolve the model config key + Tier-2 delivery mode for a document turn.

    Called only when a PDF attachment has already been classified Tier 2
    (ADR-0102 §1) — never eagerly for a turn whose documents may resolve to
    Tier 1 (text), which must work on any model. Resolves the pinned ``vision``
    role (ADR-0121 §5, FRE-920) unconditionally, same as
    ``_resolve_vision_routing_key`` — no per-attachment override, no profile.
    Any raster image also present in this turn requires ``supports_vision``
    regardless of the document's own ``supports_pdf_document`` capability
    (ADR-0102 §3).

    Args:
        ctx: Execution context carrying ``attachments`` (FRE-661).
        role_name: Unused since T5 pinned vision unconditionally; kept for
            call-site symmetry with ``_resolve_vision_routing_key``.

    Returns:
        ``(model_config_key, tier2_delivery)``.

    Raises:
        AttachmentUnsupportedError: The pinned ``vision`` deployment cannot
            serve the document (and any co-present image) at the required
            capability.
    """
    from personal_agent.config.model_loader import resolve_role_target  # noqa: PLC0415
    from personal_agent.exceptions import AttachmentUnsupportedError
    from personal_agent.orchestrator.attachment_resolution import RASTER_CONTENT_TYPES

    needs_vision = any(a.content_type in RASTER_CONTENT_TYPES for a in ctx.attachments)

    key, model_def = resolve_role_target("vision")
    if model_def is not None and not (needs_vision and not model_def.supports_vision):
        if model_def.supports_pdf_document:
            return key, "native_pdf"
        if model_def.supports_vision:
            return key, "rasterize"

    raise AttachmentUnsupportedError(
        "This turn includes a document, but the configured vision model does not support it."
    )


def _effective_attachment_routing_key(ctx: ExecutionContext, role_name: str) -> str:
    """Resolve the single effective model key for this turn's attachments.

    Prefers a document-driven routing decision already made at turn assembly
    (``ctx.document_effective_model_key``, set only when a PDF actually
    classified Tier 2 — FRE-684) over independently recomputing image-only
    vision routing, so a document-forced escalation doesn't leave an
    image-only routing check looking at a stale (pre-escalation) key.

    Args:
        ctx: Execution context.
        role_name: The model role string (e.g. "primary").

    Returns:
        The model config key to use for this call.

    Raises:
        AttachmentUnsupportedError: No reachable model can serve the turn's
            attachments (only possible via the image-only fallback path —
            a document-driven failure already raised during turn assembly).
    """
    return ctx.document_effective_model_key or _resolve_vision_routing_key(ctx, role_name)


async def _maybe_confirm_attachment_cost(
    ctx: ExecutionContext,
    resolved_blocks: Sequence[dict[str, Any]],
    native_pdf_page_count: int = 0,
) -> bool:
    """Pre-flight cloud-attachment cost gate (ADR-0101 §8b / FRE-691, ADR-0102 §7b / FRE-686).

    When a turn's resolved attachment blocks route to a *priced cloud* model and the
    pre-flight estimate exceeds ``attachment_cost_confirmation_threshold_usd``, ask the
    user to confirm before any spend (mirrors the §6 disclose-on-alter pattern). The
    gate is **per-turn**: one confirmation authorises the whole turn's cloud-vision
    usage — the per-call ADR-0065 reservation still independently caps every call, so a
    multi-call turn cannot exceed the budget under one confirmation.

    Local/free routing, an unpriced model, or an under-threshold estimate proceed
    silently. A routing error is deferred to ``step_llm_call`` (which surfaces it as
    today) rather than handled here.

    Args:
        ctx: Execution context (carries attachments, session/trace identity, reply).
        resolved_blocks: The turn's resolved image-like content blocks (raw image
            attachments plus any rasterized document pages — both are ``image_url``
            blocks priced identically).
        native_pdf_page_count: Total pages delivered via a native-PDF ``document``
            block this turn (ADR-0102 §7b / FRE-686). A single ``document`` block
            can represent many pages, so this is priced separately from
            ``resolved_blocks`` rather than by block count. ``0`` for turns with no
            native-PDF delivery.

    Returns:
        ``True`` to proceed with the turn; ``False`` to stop with no model call
        (``ctx.final_reply`` is set to the estimate + proceed/keep-local prompt).
    """
    from decimal import Decimal

    from personal_agent.config.model_loader import load_model_config
    from personal_agent.exceptions import AttachmentUnsupportedError
    from personal_agent.llm_client.message_content import (
        DOCUMENT_NATIVE_PAGE_TOKEN_ESTIMATE,
        IMAGE_BLOCK_TOKEN_ESTIMATE,
    )
    from personal_agent.orchestrator.attachment_cost import estimate_attachment_cloud_cost_usd

    # FRE-749: Early guard — if cost already confirmed (pending re-injection), skip gate entirely
    if ctx.attachment_cost_confirmed:
        return True

    try:
        effective_key = _effective_attachment_routing_key(ctx, ModelRole.PRIMARY.value)
    except AttachmentUnsupportedError:
        # Routing can't serve this attachment — let step_llm_call raise it as today.
        return True

    catalog = load_model_config()
    model_def = catalog.models.get(effective_key)
    input_price = model_def.input_cost_per_token if model_def is not None else None
    if (
        model_def is None
        or catalog.placement_of(effective_key) is Placement.LOCAL
        or not input_price
    ):
        # Local/free or unpriced — nothing to gate.
        ctx.attachment_cost_confirmed = True
        return True

    price = Decimal(str(input_price))
    estimate = estimate_attachment_cloud_cost_usd(
        block_count=len(resolved_blocks),
        per_block_tokens=IMAGE_BLOCK_TOKEN_ESTIMATE,
        input_price_per_token=price,
    )
    if native_pdf_page_count:
        estimate += estimate_attachment_cloud_cost_usd(
            block_count=native_pdf_page_count,
            per_block_tokens=DOCUMENT_NATIVE_PAGE_TOKEN_ESTIMATE,
            input_price_per_token=price,
        )
    threshold = Decimal(str(settings.attachment_cost_confirmation_threshold_usd))

    if estimate <= threshold:
        ctx.attachment_cost_confirmed = True
        return True

    description_parts = []
    if resolved_blocks:
        description_parts.append(f"{len(resolved_blocks)} attachment(s)")
    if native_pdf_page_count:
        description_parts.append(f"{native_pdf_page_count} document page(s)")
    description = " and ".join(description_parts)

    decision = await _maybe_pause_for_constraint(
        session_id=ctx.session_id,
        trace_id=ctx.trace_id,
        user_id=ctx.user_id,
        constraint="attachment_cost",
        context=(
            f"This turn sends {description} to the cloud model, estimated "
            f"${estimate:.4f}. Proceed on cloud, or keep it local and free?"
        ),
        allow_preference=False,
    )
    log.info(
        "attachment_cost_gate_decision",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        block_count=len(resolved_blocks),
        native_pdf_page_count=native_pdf_page_count,
        estimate_usd=float(estimate),
        threshold_usd=float(threshold),
        model=effective_key,
        decision=decision,
    )

    if decision == "proceed_cloud":
        ctx.attachment_cost_confirmed = True
        return True

    # keep_local / timeout / no active WS: no cloud spend this turn. Persist the
    # pending confirmation to durable session storage so an affirmative reply on
    # the *next* turn (a separate request) can re-inject the image (FRE-749).
    from dataclasses import asdict

    from personal_agent.orchestrator.types import PendingCloudAttachmentConfirmation

    pending = PendingCloudAttachmentConfirmation(
        attachments=ctx.attachments,
        cloud_vision_model_key=effective_key,
        estimate_usd=float(estimate),
        created_at=time.time(),
        ttl_seconds=600,  # 10-minute TTL for pending confirmation
        original_trace_id=ctx.trace_id,
    )
    await _save_pending_cloud_confirmation(ctx.session_id, asdict(pending), trace_id=ctx.trace_id)
    log.info(
        "pending_cloud_confirmation_saved",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        estimate_usd=float(estimate),
        ttl_seconds=600,
    )

    ctx.final_reply = (
        f"This turn's {description} would cost about "
        f"${estimate:.4f} on the cloud model — above your "
        f"${float(threshold):.2f} confirmation threshold — so I didn't send anything. "
        "Reply to confirm if you'd like me to proceed on the cloud, or keep it local "
        "and free."
    )
    return False


def _render_memory_section(entity_items: list[dict[str, Any]]) -> str:
    """Build the ## Your Memory Graph entity section string.

    Skips entities with None or blank descriptions (FRE-374 D1) so the
    LLM does not receive empty lines like '- [LOCATION] Paris:  (mentioned 328x)'.

    Args:
        entity_items: List of entity dicts from memory_context.

    Returns:
        Formatted memory section string, or empty string if no described entities.
    """
    described = [m for m in entity_items[:15] if (m.get("description") or "").strip()]
    if not described:
        return ""
    entity_lines = [
        f"- [{m.get('entity_type', '')}] {m.get('name', '')}: {m.get('description', '').strip()} "
        f"(mentioned {m.get('mentions', 1)}x)"
        for m in described
    ]
    section = "\n\n## Your Memory Graph — Known Entities\n"
    section += "\n".join(entity_lines)
    section += (
        "\n\nUse this list to directly answer questions about what the user "
        "has previously discussed. Do NOT say you have no memory."
    )
    return section


async def _trigger_captains_log_reflection(ctx: ExecutionContext) -> None:
    """Trigger an LLM-based Captain's Log reflection after task completion.

    This is a non-blocking async function that creates a reflection entry
    with LLM-generated insights.

    Args:
        ctx: Execution context with task details.
    """
    try:
        from personal_agent.captains_log import CaptainLogManager
        from personal_agent.captains_log.reflection import generate_reflection_entry

        manager = CaptainLogManager()

        effective_max = _resolve_max_iterations(ctx)
        hit_iteration_limit = ctx.tool_iteration_count > effective_max
        task_type = (
            ctx.gateway_output.intent.task_type.value if ctx.gateway_output is not None else ""
        )

        if hit_iteration_limit:
            log.warning(
                "captains_log_iteration_limit_reflected",
                trace_id=ctx.trace_id,
                task_type=task_type,
                iteration_count=ctx.tool_iteration_count,
                max_iterations=effective_max,
            )

        # Generate LLM-based reflection (with metrics summary from ADR-0012)
        entry = await generate_reflection_entry(
            user_message=ctx.user_message,
            trace_id=ctx.trace_id,
            steps_count=len(ctx.steps),
            final_state="COMPLETED",  # Task completed successfully if we're here
            reply_length=len(ctx.final_reply or ""),
            metrics_summary=ctx.metrics_summary,  # Request-scoped metrics (ADR-0012)
            hit_iteration_limit=hit_iteration_limit,
            task_type=task_type,
            iteration_count=ctx.tool_iteration_count,
            max_iterations=effective_max,
            session_id=ctx.session_id,
            eval_mode=ctx.eval_mode,
        )

        # Write entry to file
        manager.write_entry(entry)

        # Optionally commit to git (disabled in MVP)
        # manager.commit_to_git(entry.entry_id)

    except Exception as e:
        # Don't let Captain's Log failures break task completion
        log.warning(
            "captains_log_reflection_failed",
            trace_id=ctx.trace_id,
            error=str(e),
        )


async def execute_task(ctx: ExecutionContext, session_manager: SessionManager) -> ExecutionContext:
    """Main execution loop: iterate states until terminal.

    This is the core state machine that drives task execution. It transitions
    through states until reaching a terminal state (COMPLETED or FAILED).

    Includes request-scoped metrics monitoring (ADR-0012) for homeostasis
    control loops and Captain's Log enrichment.

    Args:
        ctx: Execution context containing task state and parameters.
        session_manager: Session manager for accessing session data.

    Returns:
        Updated execution context after state machine completion.
    """
    state = ctx.state
    # Carry user_id / session_id through to tool executors that receive
    # `ctx` (notes_write, notes_search, recall_personal_history). Without
    # this propagation those tools see a None user_id and refuse to run
    # even on fully authenticated CF Access requests.
    trace_ctx = TraceContext(
        trace_id=ctx.trace_id,
        user_id=ctx.user_id,
        session_id=ctx.session_id,
        eval_mode=ctx.eval_mode,
        # FRE-673: propagate auth state to tool executors (search_memory) so their
        # recall threads it into the FRE-229 visibility filter.
        authenticated=ctx.authenticated,
    )

    # ADR-0076: clear any stale Stop-button flag from a prior turn so a new
    # turn starts fresh (the flag lives on the connection, not the request).
    if ctx.session_id:
        from personal_agent.transport.agui.ws_endpoint import clear_cancel_flag

        clear_cancel_flag(ctx.session_id)

    log.info(
        TASK_STARTED,
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        user_message=ctx.user_message,
        mode=ctx.mode.value,
        channel=ctx.channel.value,
    )

    # Start request-scoped metrics monitoring (ADR-0012)
    monitor = None
    if settings.request_monitoring_enabled:
        from personal_agent.brainstem.sensors.metrics_daemon import get_global_metrics_daemon
        from personal_agent.brainstem.sensors.request_monitor import RequestMonitor

        daemon = get_global_metrics_daemon()
        if daemon is None:
            log.warning("request_monitor_skipped_no_metrics_daemon", trace_id=ctx.trace_id)
        else:
            monitor = RequestMonitor(trace_id=ctx.trace_id, daemon=daemon)
        try:
            if monitor is not None:
                await monitor.start()
        except Exception as e:
            # Don't fail task if monitoring fails
            log.warning(
                "request_monitor_start_failed",
                trace_id=ctx.trace_id,
                error=str(e),
                component="executor",
            )
            monitor = None

    # Step function registry
    step_functions = {
        TaskState.INIT: step_init,
        TaskState.PLANNING: step_planning,
        TaskState.LLM_CALL: step_llm_call,
        TaskState.TOOL_EXECUTION: step_tool_execution,
        TaskState.SYNTHESIS: step_synthesis,
    }

    # ADR-0122 §4 (FRE-930): bound the turn-scoped artifact-builder carrier to this
    # turn's lifetime. step_init sets it; the finally reset guarantees no resolution
    # (nor a false-positive pick) outlives the turn into a later async context (AC-10c).
    from personal_agent.orchestrator.constraint_options import (  # noqa: PLC0415
        reset_artifact_builder_resolution,
        reset_decision_disclosures,
        set_artifact_builder_resolution,
        start_decision_disclosures,
    )

    _builder_carrier_token = set_artifact_builder_resolution(None)
    # FRE-928 AC-3: same turn-scoped lifetime for no-decision disclosures.
    _disclosure_carrier_token = start_decision_disclosures()

    previous_state: TaskState | None = None
    async with observe_topology(ctx):
        try:
            while state not in {TaskState.COMPLETED, TaskState.FAILED}:
                log.info(
                    STATE_TRANSITION,
                    trace_id=ctx.trace_id,
                    from_state=(
                        previous_state.value if previous_state is not None else state.value
                    ),
                    to_state=state.value,
                    component="executor",
                )
                ctx.state = state
                previous_state = state

                step_func = step_functions.get(state)
                if not step_func:
                    log.error(
                        UNKNOWN_STATE,
                        trace_id=ctx.trace_id,
                        state=state.value,
                    )
                    ctx.error = ValueError(f"Unknown state: {state}")
                    state = TaskState.FAILED
                    break

                # Execute step function
                state = await step_func(ctx, session_manager, trace_ctx)

            ctx.state = state

            # Stop request-scoped monitoring BEFORE Captain's Log (ADR-0012)
            # This ensures metrics_summary is available for reflection enrichment
            if monitor is not None:
                try:
                    metrics_summary = await monitor.stop()
                    ctx.metrics_summary = metrics_summary

                    # Log summary for analysis
                    log.info(
                        "request_metrics_summary",
                        trace_id=ctx.trace_id,
                        duration_seconds=metrics_summary.get("duration_seconds"),
                        samples_collected=metrics_summary.get("samples_collected"),
                        cpu_avg=metrics_summary.get("cpu_avg"),
                        memory_avg=metrics_summary.get("memory_avg"),
                        gpu_avg=metrics_summary.get("gpu_avg"),
                        threshold_violations=metrics_summary.get("threshold_violations"),
                        component="executor",
                    )
                except Exception as e:
                    # Don't fail task if monitoring cleanup fails
                    log.warning(
                        "request_monitor_stop_failed",
                        trace_id=ctx.trace_id,
                        error=str(e),
                        component="executor",
                    )

            if state == TaskState.COMPLETED:
                log.info(
                    TASK_COMPLETED,
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                    reply_length=len(ctx.final_reply or ""),
                    steps_count=len(ctx.steps),
                )

                # FRE-523: the cognitive pipeline (capture + request.captured event +
                # reflection) runs for eval turns too, so consolidation/entity-extraction
                # can write eval-derived content to the KG. eval_mode is stamped on the
                # capture for provenance; outward-facing side effects stay suppressed
                # elsewhere (tools/linear.py gate, request-trace ES handler, and the
                # promotion pipeline which skips eval-derived entries).
                # Fast capture (Phase 2.2): Write structured capture immediately (no LLM)
                try:
                    from personal_agent.captains_log.capture import TaskCapture, write_capture

                    # Calculate duration from metrics summary if available
                    duration_ms = None
                    if ctx.metrics_summary and "duration_seconds" in ctx.metrics_summary:
                        duration_ms = ctx.metrics_summary["duration_seconds"] * 1000

                    # Extract tools used and accumulate token counts from steps
                    tools_used = []
                    cap_prompt_tokens = 0
                    cap_completion_tokens = 0
                    cap_total_tokens = 0
                    for step in ctx.steps:
                        if step.get("type") == "tool_call":
                            tool_name = (step.get("metadata") or {}).get("tool_name")
                            if tool_name:
                                tools_used.append(tool_name)
                        elif step.get("type") == "llm_call":
                            meta = step.get("metadata") or {}
                            cap_prompt_tokens += meta.get("prompt_tokens", 0)
                            cap_completion_tokens += meta.get("completion_tokens", 0)
                            cap_total_tokens += meta.get("tokens", 0)

                    # FRE-343: TaskCapture.user_id is non-optional. ExecutionContext.user_id
                    # is typed UUID | None for legacy reasons but is always populated in
                    # production by the orchestrator from request_user.user_id (which
                    # get_request_user always resolves). Pydantic validation catches the
                    # None case as a real bug.
                    assert ctx.user_id is not None, (
                        "ExecutionContext.user_id missing — orchestrator should populate it "
                        "from request_user.user_id (FRE-343)"
                    )
                    capture = TaskCapture(
                        trace_id=ctx.trace_id,
                        session_id=ctx.session_id,
                        timestamp=datetime.now(timezone.utc),
                        user_message=ctx.user_message,
                        assistant_response=ctx.final_reply,
                        steps=cast(list[dict[str, Any]], ctx.steps),
                        tools_used=list(set(tools_used)),  # Deduplicate
                        duration_ms=duration_ms,
                        metrics_summary=ctx.metrics_summary,
                        outcome="completed",
                        memory_context_used=bool(ctx.memory_context),
                        memory_conversations_found=len(ctx.memory_context)
                        if ctx.memory_context
                        else 0,
                        input_tokens=cap_prompt_tokens,
                        output_tokens=cap_completion_tokens,
                        total_tokens=cap_total_tokens,
                        tool_results=ctx.tool_results,
                        user_id=ctx.user_id,
                        eval_mode=ctx.eval_mode,
                    )
                    write_capture(capture)

                    # Publish request.captured event (ADR-0041)
                    from personal_agent.captains_log.background import (
                        run_in_background as _run_bg,
                    )
                    from personal_agent.events.bus import get_event_bus
                    from personal_agent.events.models import (
                        STREAM_REQUEST_CAPTURED,
                        RequestCapturedEvent,
                    )

                    event = RequestCapturedEvent(
                        trace_id=ctx.trace_id,
                        session_id=ctx.session_id,
                        source_component="orchestrator.executor",
                    )
                    _run_bg(get_event_bus().publish(STREAM_REQUEST_CAPTURED, event))
                except Exception as e:
                    # Don't fail task if capture fails
                    log.warning(
                        "capture_write_failed",
                        trace_id=ctx.trace_id,
                        error=str(e),
                        exc_info=True,
                    )

                # Trigger Captain's Log reflection (LLM-based, background), gated to a coarser
                # per-session cadence (FRE-710) rather than every turn. eval_mode turns and the
                # cadence-disabled kill switch bypass the gate and always reflect; a turn that
                # hits the iteration limit always bypasses the debounce interval (see
                # ReflectionCadenceGate).
                # Run in background to avoid blocking user response
                # Metrics summary is now available in ctx for reflection enrichment
                from personal_agent.captains_log.background import run_in_background
                from personal_agent.captains_log.reflection_cadence import (
                    get_reflection_cadence_gate,
                )

                hit_iteration_limit = ctx.tool_iteration_count > _resolve_max_iterations(ctx)
                should_reflect = (
                    ctx.eval_mode
                    or not settings.captains_log_reflection_cadence_enabled
                    or get_reflection_cadence_gate().should_reflect(
                        ctx.session_id, hit_iteration_limit=hit_iteration_limit
                    )
                )
                if should_reflect:
                    run_in_background(_trigger_captains_log_reflection(ctx))
                else:
                    log.debug(
                        "captains_log_reflection_skipped_cadence",
                        trace_id=ctx.trace_id,
                        session_id=ctx.session_id,
                    )
            else:
                log.warning(
                    TASK_FAILED,
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                    error=str(ctx.error) if ctx.error else "Unknown error",
                )

        except Exception as e:
            log.error(
                ORCHESTRATOR_FATAL_ERROR,
                trace_id=ctx.trace_id,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            ctx.error = e
            ctx.state = TaskState.FAILED

            # Stop monitoring even on fatal error
            if monitor is not None and ctx.metrics_summary is None:
                try:
                    metrics_summary = await monitor.stop()
                    ctx.metrics_summary = metrics_summary

                    # Log summary for analysis
                    log.info(
                        "request_metrics_summary",
                        trace_id=ctx.trace_id,
                        duration_seconds=metrics_summary.get("duration_seconds"),
                        samples_collected=metrics_summary.get("samples_collected"),
                        cpu_avg=metrics_summary.get("cpu_avg"),
                        memory_avg=metrics_summary.get("memory_avg"),
                        gpu_avg=metrics_summary.get("gpu_avg"),
                        threshold_violations=metrics_summary.get("threshold_violations"),
                        component="executor",
                    )
                except Exception as e:
                    # Don't fail task if monitoring cleanup fails
                    log.warning(
                        "request_monitor_stop_failed",
                        trace_id=ctx.trace_id,
                        error=str(e),
                        component="executor",
                    )
        finally:
            # ADR-0122 §4 (FRE-930): drop the turn-scoped artifact-builder carrier so
            # no resolution outlives this turn into a later async context (AC-10c).
            reset_artifact_builder_resolution(_builder_carrier_token)
            reset_decision_disclosures(_disclosure_carrier_token)

    return ctx


def _is_affirmative_confirmation(message: str) -> bool:
    """Check if a message is an affirmative response to proceed with cloud vision.

    Detects common confirmation phrases while avoiding false positives from
    unrelated messages that happen to contain the word "yes".

    Args:
        message: The user's message text.

    Returns:
        True if the message strongly signals affirmative confirmation, False otherwise.
    """
    import re

    msg_lower = message.lower().strip()

    # Explicit confirmations: detect only clear intent phrases.
    # - "proceed", "confirm", "cloud" at start (strong intent)
    # - "yes"/"ok"/"okay" ONLY if they're the entire message (with optional trailing punctuation)
    #   to avoid false positives like "yes, I agree" or "Is that a yes?"
    patterns = [
        r"^proceed\b",  # "proceed" at start
        r"^confirm\b",  # "confirm" at start
        r"^cloud\b",  # "cloud" at start
        r"^yes[!.]?\s*$",  # "yes" as entire message, with optional punctuation
        r"^ok[!.]?\s*$",  # "ok" as entire message
        r"^okay[!.]?\s*$",  # "okay" as entire message
        r"^yes[!,.]?\s*(?:proceed|cloud)",  # "yes" followed by proceed/cloud with optional punctuation
    ]

    return any(re.search(pattern, msg_lower) for pattern in patterns)


async def _maybe_reinject_pending_cloud_attachment(ctx: ExecutionContext) -> None:
    """Re-inject pending cloud attachment confirmation on affirmative reply (FRE-749).

    When a cloud-attachment cost gate pauses, a pending confirmation record is
    saved to durable session storage. On the next turn (a separate request), if
    the user's message is affirmative, re-inject the pending attachments into the
    context so they flow through to cloud vision routing and mark the cost
    confirmed so the gate does not re-pause. A non-affirmative reply drops the
    pending state (AC-2). The durable load runs on every turn — a single indexed
    primary-key read — because pending presence is only knowable from storage;
    that read is the price of clearing stale pending on a non-affirmative reply.

    Args:
        ctx: Execution context (modified in-place if pending is re-injected).
    """
    pending_dict = await _load_pending_cloud_confirmation(ctx.session_id, trace_id=ctx.trace_id)
    if not pending_dict:
        return

    # Check for affirmative confirmation in the user's message
    if not _is_affirmative_confirmation(ctx.user_message):
        log.info(
            "pending_cloud_confirmation_not_affirmative",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            message_preview=ctx.user_message[:50],
        )
        await _clear_pending_cloud_confirmation(ctx.session_id, trace_id=ctx.trace_id)
        return

    # Re-construct AttachmentRef tuples from the pending dict
    from personal_agent.orchestrator.types import AttachmentRef

    try:
        attachments_data = pending_dict.get("attachments", [])
        ctx.attachments = tuple(
            AttachmentRef(
                artifact_id=a["artifact_id"],
                content_type=a["content_type"],
                title=a["title"],
                r2_key=a["r2_key"],
            )
            for a in attachments_data
        )
        # FRE-749: Set the cost-confirmed flag so the re-injected turn does NOT re-pause at the gate
        ctx.attachment_cost_confirmed = True
        log.info(
            "pending_cloud_confirmation_reinjected",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            attachment_count=len(ctx.attachments),
            estimate_usd=pending_dict.get("estimate_usd"),
            original_trace_id=pending_dict.get("original_trace_id"),
            cost_confirmed_set=True,
        )
    except Exception as e:
        log.warning(
            "pending_cloud_confirmation_reinject_failed",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            error=str(e),
        )
        await _clear_pending_cloud_confirmation(ctx.session_id, trace_id=ctx.trace_id)
        return

    # Clear the pending confirmation after successful re-injection
    await _clear_pending_cloud_confirmation(ctx.session_id, trace_id=ctx.trace_id)


def _parse_requested_page_range(message: str) -> tuple[int, int] | None:
    """Parse a 1-indexed page range from a document-continuation follow-up (ADR-0102 §4 / FRE-685).

    Recognizes "pages 24-40", "page 24 to 40", and a single "page 5" — all
    anchored to a "page(s)" keyword so incidental "N-M" text elsewhere in the
    message (a time like "3-5pm", a phone extension) is never mistaken for a
    page range. A bare "24-40" with no keyword is accepted only when it is the
    *entire* message (a terse reply to the disclosed offer) — not merely a
    substring, for the same reason. Returns the range in ascending order
    regardless of how it was written.

    Args:
        message: The user's message text.

    Returns:
        ``(start, end)`` inclusive 1-indexed page numbers, or None if no range
        or single page is named.
    """
    import re

    def _ordered(start: int, end: int) -> tuple[int, int] | None:
        if start <= 0 or end <= 0:
            return None
        return (start, end) if start <= end else (end, start)

    range_match = re.search(r"pages?\s*(\d+)\s*(?:-|–|to|through)\s*(\d+)", message, re.IGNORECASE)
    if range_match:
        return _ordered(int(range_match.group(1)), int(range_match.group(2)))

    bare_match = re.fullmatch(
        r"\s*(\d+)\s*(?:-|–|to|through)\s*(\d+)\s*[.!?]?\s*", message, re.IGNORECASE
    )
    if bare_match:
        return _ordered(int(bare_match.group(1)), int(bare_match.group(2)))

    single_match = re.search(r"pages?\s*(\d+)\b", message, re.IGNORECASE)
    if single_match:
        page = int(single_match.group(1))
        return (page, page) if page > 0 else None

    return None


async def _maybe_reinject_pending_document_continuation(ctx: ExecutionContext) -> None:
    """Re-inject a pending PDF page-budget continuation offer on a matching follow-up.

    ADR-0102 §4 / FRE-685: when ``resolve_documents`` drops Tier-2 pages under
    the per-turn page budget, the dropped-page offer(s) are saved to durable
    session storage. On a later turn, if the user's message names a page
    range (``_parse_requested_page_range``) or is a broad affirmative
    (``_is_affirmative_confirmation`` — "yes", "continue", etc., meaning "all
    of it"), the matching artifact(s) are re-resolved with
    ``AttachmentRef.requested_pages`` set to exactly the requested/dropped
    intersection — no re-upload needed, since the bytes are already in R2
    under ``r2_key``.

    Unlike the cloud-cost confirmation gate (where a narrow yes/no *is* the
    entire interaction), an unrelated turn in between must not destroy a
    legitimate offer: a message that neither parses as a range nor reads as
    affirmative leaves the pending record untouched — only the TTL (not
    turn-adjacency) bounds its staleness. Only the *requested* pages of an
    offer are consumed; any pages of that same offer the request did not
    cover, plus any other pending offers (e.g. a second over-budget document
    in the same turn), are kept — as a trimmed remainder offer — for a later
    follow-up (code-review finding: a partial-range request used to discard
    the un-requested remainder entirely).

    A cost-gate note: this function may append a newly re-resolved,
    potentially-priced document to ``ctx.attachments`` for a turn that
    already has ``ctx.attachment_cost_confirmed = True`` from an *unrelated*
    prior pending confirmation re-injected moments earlier in the same
    ``step_init`` call (FRE-749's cloud-attachment gate — both flows key off
    the same generic "yes"/affirmative detector). That flag must never carry
    over to content it was never actually about, so re-injecting here always
    resets it to ``False``, forcing ``_maybe_confirm_attachment_cost`` to
    re-evaluate this turn's full block set fresh (code-review finding: a
    single ambiguous "yes" could otherwise resolve both pending states and
    let a re-injected native-PDF page range skip the pre-flight cost gate
    entirely).

    Args:
        ctx: Execution context (modified in-place if a continuation is re-injected).
    """
    pending_dict = await _load_pending_document_continuation(ctx.session_id, trace_id=ctx.trace_id)
    if not pending_dict:
        return

    offers_data = pending_dict.get("offers") or []
    if not offers_data:
        await _clear_pending_document_continuation(ctx.session_id, trace_id=ctx.trace_id)
        return

    requested_range = _parse_requested_page_range(ctx.user_message)
    if requested_range is not None:
        start, end = requested_range
        wanted = set(range(start, end + 1))
    elif _is_affirmative_confirmation(ctx.user_message):
        wanted = None  # sentinel: take every dropped page from every offer
    else:
        log.info(
            "pending_document_continuation_not_matched",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            message_preview=ctx.user_message[:50],
        )
        return

    matched: list[tuple[dict[str, Any], list[int]]] = []
    remaining_offers: list[dict[str, Any]] = []
    for offer in offers_data:
        dropped = list(offer.get("dropped_pages") or [])
        overlap = dropped if wanted is None else [p for p in dropped if p in wanted]
        leftover = [] if wanted is None else [p for p in dropped if p not in wanted]
        if overlap:
            matched.append((offer, overlap))
            if leftover:
                remaining_offers.append({**offer, "dropped_pages": leftover})
        else:
            remaining_offers.append(offer)

    if not matched:
        return

    from personal_agent.orchestrator.types import AttachmentRef

    try:
        injected = tuple(
            AttachmentRef(
                artifact_id=offer["artifact_id"],
                content_type=offer["content_type"],
                title=offer["title"],
                r2_key=offer["r2_key"],
                requested_pages=tuple(pages),
            )
            for offer, pages in matched
        )
    except (KeyError, TypeError) as e:
        log.warning(
            "pending_document_continuation_reinject_failed",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            error=str(e),
        )
        await _clear_pending_document_continuation(ctx.session_id, trace_id=ctx.trace_id)
        return

    ctx.attachments = (*ctx.attachments, *injected)
    # This turn is re-resolving previously-dropped (possibly priced) document
    # pages the user never explicitly confirmed a cost for — never trust a
    # confirmation flag set moments earlier for unrelated content.
    ctx.attachment_cost_confirmed = False
    log.info(
        "pending_document_continuation_reinjected",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        artifact_ids=[a.artifact_id for a in injected],
    )

    if remaining_offers:
        await _save_pending_document_continuation(
            ctx.session_id,
            {**pending_dict, "offers": remaining_offers},
            trace_id=ctx.trace_id,
        )
    else:
        await _clear_pending_document_continuation(ctx.session_id, trace_id=ctx.trace_id)


async def _maybe_resolve_artifact_builder(ctx: ExecutionContext) -> None:
    """Resolve the per-build artifact-builder selection at turn start (ADR-0122 §2/§4/§5).

    Fires only when the gateway predicted an artifact build for this turn — the
    ``artifact_build_intent`` signal (FRE-929). It consults the stored preference and,
    absent one, raises the ADR-0076 DecisionCard, all inside
    :func:`_maybe_pause_for_constraint`; the resolution (a card pick, a silent
    preference, or a safe default on timeout / no socket) lands on
    ``ctx.artifact_builder_resolution`` (authoritative, AC-10a) and on the async
    resolution carrier the build boundary reads (the tool executor receives only a
    ``TraceContext``, ADR-0122 §4). It also derives the resolved deployment's
    effective output budget and context window and stores a planning note on
    ``ctx.artifact_builder_planning_note`` (§5/T6), so the primary can scope the
    ``artifact_draft`` plan to what the builder can actually emit before it writes it,
    rather than discovering the ceiling by overrunning it mid-generation.

    Called **after** the ``attachment_cost`` gate and **before** the gateway block:
    declining that gate short-circuits the turn, so a builder question first would be
    wasted (§3d). The builder decision keeps its own ``request_id``-keyed waiter and is
    resolvable only by an explicit deployment-key option id — never satisfied by
    another pause's answer (the FRE-749 hazard, §3d).

    When there is no signal the carrier is left at its ``None`` default: a build that
    nonetheless reaches ``artifact_draft`` degrades to the configured default and logs
    ``artifact_build_intent_missed`` — a missed prediction (§3b, AC-11). The missed
    turn's request text is already on the ``task_started`` log under the same
    ``trace_id``, so the miss event need not re-log it (avoids duplicating user text).

    Args:
        ctx: The execution context; ``ctx.gateway_output.intent.signals`` carries the
            classifier signals and ``ctx.user_id`` scopes the stored preference.
    """
    from personal_agent.config.model_loader import load_model_config  # noqa: PLC0415
    from personal_agent.orchestrator.constraint_options import (  # noqa: PLC0415
        build_provider_availability,
        effective_artifact_builder_max_tokens,
        resolve_effective_artifact_builder_deployment,
        set_artifact_builder_resolution,
    )

    signals = ctx.gateway_output.intent.signals if ctx.gateway_output is not None else []
    if "artifact_build_intent" not in signals:
        # No prediction — leave the carrier None; a build that still reaches the
        # boundary logs a tunable miss (§3b/AC-11).
        return

    decision = await _maybe_pause_for_constraint(
        session_id=ctx.session_id,
        trace_id=ctx.trace_id,
        user_id=ctx.user_id,
        constraint="artifact_builder",
        context="Choose the model to build this artifact.",
    )
    ctx.artifact_builder_resolution = decision
    set_artifact_builder_resolution(decision)
    log.info(
        "artifact_builder_resolved_at_turn_start",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        action_id=str(decision),
        resolution=decision.resolution,
    )

    # ADR-0122 §5/T6: thread the resolved deployment's effective output budget and
    # context window into the planning step (the primary composes the artifact_draft
    # `plan` argument before any tool runs) — the root-cause fix for the FRE-478
    # class, where the plan discovered the output ceiling by overrunning it
    # mid-generation instead of being scoped to it in advance.
    catalog = load_model_config()
    resolved_key = resolve_effective_artifact_builder_deployment(
        decision, catalog, is_provider_available=build_provider_availability(catalog, settings)
    )
    resolved_definition = catalog.models[resolved_key]
    effective_budget = effective_artifact_builder_max_tokens(
        resolved_definition.max_tokens, int(settings.artifact_draft_max_tokens)
    )
    ctx.artifact_builder_planning_note = (
        f"This turn's artifact builder is `{resolved_key}` — output budget "
        f"{effective_budget} tokens, context window {resolved_definition.context_length} "
        "tokens. If you call artifact_draft, scope the plan's length and detail so the "
        "sub-agent can complete the document within that output budget."
    )


async def step_init(
    ctx: ExecutionContext, session_manager: SessionManager, trace_ctx: TraceContext
) -> TaskState:
    """Initialize: determine intent and next action.

    For the skeleton implementation, this step:
    - Loads session message history
    - Adds the new user message
    - Queries memory graph for relevant context (Phase 2.2)
    - Determines if planning is needed (simple heuristic)
    - Transitions to PLANNING or LLM_CALL

    Args:
        ctx: Execution context.
        session_manager: Session manager.
        trace_ctx: Trace context.

    Returns:
        Next state (PLANNING or LLM_CALL).
    """
    timer = ctx.request_timer

    # Load session and build message history
    session_message_count = 0
    if timer:
        timer.start_span("session_history_load")
    try:
        session = session_manager.get_session(ctx.session_id)
        if session:
            ctx.messages = list(session.messages)
            session_message_count = len(ctx.messages)
    finally:
        if timer:
            timer.end_span("session_history_load", message_count=session_message_count)

    # FRE-749: Check for pending cloud-attachment confirmation from a previous paused turn
    # and re-inject attachments if the user's message is affirmative.
    await _maybe_reinject_pending_cloud_attachment(ctx)

    # ADR-0102 §4 / FRE-685: check for a pending PDF page-budget continuation
    # offer from a previous over-budget document turn and re-inject the
    # requested pages if the user's message names a range (or affirms "all
    # of it") — appends onto whatever cloud-confirmation re-injection above
    # already placed on ctx.attachments.
    await _maybe_reinject_pending_document_continuation(ctx)

    # Add new user message — resolve current-turn raster attachments to image
    # blocks first (ADR-0101 §3/§4/§6, FRE-666), then PDF document attachments
    # (ADR-0102 §1/§3/§4/§5, FRE-684); widens content to a block list only when
    # there is something to inject (FRE-664 MessageContent).
    content: MessageContent = ctx.user_message
    resolved_blocks: tuple[dict[str, Any], ...] = ()  # image-only
    document_blocks: tuple[dict[str, Any], ...] = ()
    document_disclosures: tuple[str, ...] = ()
    native_pdf_page_count = 0
    if ctx.attachments:
        from personal_agent.orchestrator.attachment_resolution import resolve_attachments

        resolved = await resolve_attachments(
            ctx.attachments,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            # Turn-level call — no sub-agent task_id reaches this layer
            # (mirrors the route_traces convention: task_id NULL = turn-level).
            task_id=None,
        )
        resolved_blocks = resolved.blocks

        from personal_agent.orchestrator.document_resolution import (
            PDF_CONTENT_TYPES,
            resolve_documents,
        )

        if any(a.content_type in PDF_CONTENT_TYPES for a in ctx.attachments):
            doc_resolved = await resolve_documents(
                ctx.attachments,
                # Lazy — invoked by resolve_documents only if a document
                # actually classifies Tier 2 (ADR-0102 §1: Tier 1 must work
                # on any model, so this must never fire speculatively).
                resolve_tier2_delivery=lambda: _resolve_document_routing_key(
                    ctx, ModelRole.PRIMARY.value
                )[1],
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                task_id=None,
            )
            document_blocks = doc_resolved.blocks
            document_disclosures = doc_resolved.disclosures
            native_pdf_page_count = doc_resolved.native_pdf_page_count

            if doc_resolved.continuation_offers:
                # ADR-0102 §4 / FRE-685: this turn's page-budget-dropped
                # offer(s). MERGED into (not overwriting) whatever is already
                # pending — e.g. a second over-budget document from an
                # earlier turn that this turn never touched, or the trimmed
                # remainder _maybe_reinject_pending_document_continuation just
                # saved moments earlier in this same step_init call. A blind
                # overwrite here would silently clobber that still-live state
                # with only this turn's own offers (code-review finding).
                # Same artifact_id in both: union the dropped pages — this
                # turn's fresh assessment plus any not-yet-requested remainder.
                from dataclasses import asdict as _asdict

                existing_pending = await _load_pending_document_continuation(
                    ctx.session_id, trace_id=ctx.trace_id
                )
                merged_by_artifact: dict[str, dict[str, Any]] = {
                    o["artifact_id"]: o for o in (existing_pending or {}).get("offers") or []
                }
                for offer in doc_resolved.continuation_offers:
                    fresh = _asdict(offer)
                    prior = merged_by_artifact.get(offer.artifact_id)
                    if prior is not None:
                        fresh["dropped_pages"] = sorted(
                            set(prior.get("dropped_pages") or ()) | set(fresh["dropped_pages"])
                        )
                    merged_by_artifact[offer.artifact_id] = fresh

                await _save_pending_document_continuation(
                    ctx.session_id,
                    {
                        "offers": list(merged_by_artifact.values()),
                        "created_at": time.time(),
                        "ttl_seconds": 600,  # matches the cloud-confirmation pending TTL
                        "original_trace_id": ctx.trace_id,
                    },
                    trace_id=ctx.trace_id,
                )

            # Only force the whole turn onto the document-capable model if a
            # Tier-2 block actually survived into the message — a rejected
            # oversized native-PDF (used_tier2=True, blocks=()) must not drag
            # an otherwise document-free turn onto an escalated/cloud model
            # for no visible reason (code-review finding).
            if doc_resolved.used_tier2 and document_blocks:
                ctx.document_effective_model_key, _ = _resolve_document_routing_key(
                    ctx, ModelRole.PRIMARY.value
                )
                # Recomputed rather than captured off the closure above: the
                # callback's actual contract is to return only the delivery
                # mode to resolve_documents; this second call is cheap and
                # pure (config/profile lookups only, no I/O).

        ctx.attachment_disclosures = list(resolved.disclosures) + list(document_disclosures)
        all_blocks = resolved_blocks + document_blocks
        if all_blocks:
            content = (
                [{"type": "text", "text": ctx.user_message}, *all_blocks]
                if ctx.user_message
                else list(all_blocks)
            )
    ctx.messages.append({"role": "user", "content": content})

    # ADR-0101 §8b / FRE-691 + ADR-0102 §7b / FRE-686: pre-flight cloud-attachment
    # cost confirmation. An over-threshold cloud turn stops here with the estimate
    # + proceed/keep-local prompt and makes no model call until the user confirms
    # (AC-9/AC-10). Rasterized document pages are image_url blocks — cost-shape
    # identical to attachment images — so they fold into the same bucket; a native
    # PDF block is priced separately via native_pdf_page_count (one block can
    # represent many pages).
    cost_gate_blocks = resolved_blocks + tuple(
        b for b in document_blocks if b.get("type") == "image_url"
    )
    if (cost_gate_blocks or native_pdf_page_count) and not await _maybe_confirm_attachment_cost(
        ctx, cost_gate_blocks, native_pdf_page_count=native_pdf_page_count
    ):
        return TaskState.SYNTHESIS

    # --- ADR-0122 §2/§3d/§4/§5 (T5/FRE-930, T6/FRE-931): raise the per-build
    # artifact-builder decision at TURN START — before the first LLM call and any
    # tool runs — off the artifact_build_intent signal the gateway emits (T4/FRE-929).
    # The card determination depends on nothing the turn computes, so asking here (vs
    # the build boundary) moves it from ~117 s after the request to ~0 s (the AC-7
    # failure). Ordered AFTER the attachment_cost gate above: declining it returns
    # SYNTHESIS, so no build follows and a builder question first would be wasted
    # (§3d, AC-14 a/d). Also derives the resolved deployment's effective output
    # budget and context window for the planning step (§5/T6).
    await _maybe_resolve_artifact_builder(ctx)

    # --- Gateway-driven path: skip inline routing and memory ---
    if ctx.gateway_output is not None:
        gw = ctx.gateway_output
        # Use pre-assembled memory context
        if gw.context.memory_context:
            ctx.memory_context = gw.context.memory_context
            log.info(
                "memory_enrichment_completed",
                trace_id=ctx.trace_id,
                conversations_found=len(gw.context.memory_context),
            )
        # Populate operator stanza in gateway path (was only wired for legacy path).
        if ctx.user_id and ctx.user_email:
            try:
                from personal_agent.orchestrator.prompts import get_owner_stanza  # noqa: PLC0415
                from personal_agent.service.app import memory_service as _ms

                if _ms and _ms.connected:
                    ctx.operator_stanza = await get_owner_stanza(
                        memory_service=_ms,
                        user_id=ctx.user_id,
                        email=ctx.user_email,
                        display_name=ctx.user_display_name,
                    )
            except Exception as _stanza_e:
                log.warning("operator_stanza_failed", error=str(_stanza_e), trace_id=ctx.trace_id)
        log.info(
            "step_init_gateway_path",
            trace_id=ctx.trace_id,
            task_type=gw.intent.task_type.value,
            complexity=gw.intent.complexity.value,
            has_memory=gw.context.memory_context is not None,
            has_operator_stanza=bool(ctx.operator_stanza),
        )
        if gw.intent.task_type.value == "memory_recall":
            # Gateway path returns early, so emit broad-recall telemetry here.
            # This keeps CP-26 observable even when inline memory query is skipped.
            log.info(
                "memory_recall_broad_query",
                trace_id=ctx.trace_id,
                entity_type_hints=_extract_entity_type_hints(ctx.user_message),
                entities_found=len(gw.context.memory_context or []),
                source="gateway_context",
            )
        from personal_agent.request_gateway.types import DecompositionStrategy

        if gw.decomposition.strategy == DecompositionStrategy.DELEGATE:
            from personal_agent.request_gateway.delegation import compose_delegation_package

            # Build memory excerpt and pitfalls from gateway context
            mem_items = gw.context.memory_context or []
            memory_excerpt: list[dict[str, str | float]] = [
                {
                    "type": str(item.get("type", "episode")),
                    "summary": str(
                        item.get("summary") or item.get("description") or item.get("name", "")
                    ),
                }
                for item in mem_items[:5]
            ]
            known_pitfalls: list[str] = [
                str(item.get("summary") or item.get("description") or "")
                for item in mem_items
                if item.get("type") == "episode"
            ][:3]

            # Extract acceptance criteria from user message using "with X, Y, Z" split
            raw = ctx.user_message
            acceptance_criteria: list[str] = []
            if " with " in raw.lower():
                after_with = raw[raw.lower().index(" with ") + 6 :]
                parts = [
                    p.strip().rstrip(".,;") for p in after_with.replace(" and ", ",").split(",")
                ]
                acceptance_criteria = [p for p in parts if len(p) > 3][:5]
            if not acceptance_criteria:
                acceptance_criteria = ["Implementation meets requirements described in the task"]

            relevant_files: list[str] = []
            for word in raw.split():
                stripped = word.strip('",.:;!?()')
                if "/" in stripped and stripped.startswith("src/"):
                    relevant_files.append(stripped)

            compose_delegation_package(
                task_description=ctx.user_message,
                trace_id=ctx.trace_id,
                acceptance_criteria=acceptance_criteria,
                known_pitfalls=known_pitfalls or None,
                memory_excerpt=memory_excerpt or None,
                relevant_files=relevant_files or None,
            )
            # Fall through to LLM call — primary agent responds with delegation package

        elif gw.decomposition.strategy in (
            DecompositionStrategy.HYBRID,
            DecompositionStrategy.DECOMPOSE,
        ):
            ctx.expansion_strategy = gw.decomposition.strategy.value
            ctx.expansion_constraints = gw.decomposition.constraints or {}

            if settings.orchestration_mode == "enforced":
                from personal_agent.llm_client.factory import get_llm_client
                from personal_agent.orchestrator.expansion_controller import (
                    ExpansionController,
                )

                llm_client = get_llm_client(role_name=ModelRole.PRIMARY.value)
                controller = ExpansionController()
                # ADR-0088 D4: report progress at dispatch start so tool/context fields are
                # live during the (potentially multi-minute) expansion window. Cost itself
                # climbs from turn.model_call_completed events, not a per-loop accumulator.
                await _report_turn_progress(ctx)
                expansion_result = await controller.execute(
                    query=get_text_content(ctx.messages[-1].get("content", ""))
                    if ctx.messages
                    else "",
                    strategy=gw.decomposition.strategy.value.upper(),
                    llm_client=llm_client,
                    trace_id=ctx.trace_id,
                    messages=ctx.messages,
                    constraints=ctx.expansion_constraints,
                    session_id=ctx.session_id,
                    eval_mode=ctx.eval_mode,
                )

                ctx.expansion_plan = expansion_result.plan
                ctx.sub_agent_results = expansion_result.sub_agent_results
                ctx.expansion_phase_results = expansion_result.phase_results

                # Build synthesis context and append to messages
                if expansion_result.sub_agent_results:
                    synthesis_msg = {
                        "role": "user",
                        "content": (
                            f"{expansion_result.synthesis_context}\n"
                            "The sub-tasks above have been completed. "
                            "Synthesize the results into a coherent response "
                            "for the user's original question."
                        ),
                    }
                    ctx.messages.append(synthesis_msg)

                log.info(
                    "expansion_controller_complete",
                    mode="enforced",
                    plan_is_fallback=expansion_result.plan.is_fallback
                    if expansion_result.plan
                    else None,
                    sub_agent_count=len(expansion_result.sub_agent_results),
                    successful=expansion_result.successful_count,
                    degraded=expansion_result.degraded,
                    trace_id=ctx.trace_id,
                )

                # ADR-0088 D3: FRE-501's per-loop cost rollup is removed — the live meter
                # now climbs from turn.model_call_completed events (every model call,
                # including these sub-agents, publishes one from the cost boundary) and the
                # durable row's authoritative cost is SUM(api_costs). Report progress so the
                # tool/context fields refresh after expansion.
                await _report_turn_progress(ctx)

                # Go directly to synthesis LLM call
                return TaskState.LLM_CALL

            # Autonomous mode — existing behavior
            log.info(
                "step_init_expansion_flagged",
                mode="autonomous",
                strategy=gw.decomposition.strategy.value,
                constraints=gw.decomposition.constraints,
                trace_id=ctx.trace_id,
            )
        return TaskState.LLM_CALL

    # Apply context window controls before LLM usage to prevent overflow.
    input_messages_count = len(ctx.messages)
    estimated_tokens = 0
    if timer:
        timer.start_span("context_window")
    try:
        # Retrieve pre-computed compression summary if available (ADR-0038).
        # ADR-0081 §D3 Decision 4: under the frozen layout the transient
        # re-derivation (re-inserting a popped summary at a fixed index every
        # turn) is itself a cache-buster and is removed — compaction becomes the
        # scheduled reset below. apply_context_window then keeps only its pure
        # truncation role.
        # Dead-by-default: cache_frozen_layout_enabled=True (production default)
        # makes _summary always None; compressed_summary is the legacy pre-ADR-0081
        # path and maybe_trigger_compression is also gated on the same flag
        # (FRE-576 F4).
        _summary = (
            compression_manager.get_summary(ctx.session_id)
            if ctx.session_id and not settings.cache_frozen_layout_enabled
            else None
        )

        ctx.messages = apply_context_window(
            ctx.messages,
            max_tokens=settings.context_window_max_tokens,
            strategy=settings.conversation_context_strategy,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            compressed_summary=_summary,
        )

        # ADR-0081 §D3: cache-aware compaction scheduler. When the run reaches the
        # cost/quality optimum (or the token ceiling), compact to a frozen reset
        # that re-establishes a reusable prefix; otherwise hold (history stays a
        # strict forward extension). No-op when the flag is off.
        await _maybe_frozen_reset(ctx)

        estimated_tokens = estimate_messages_tokens(ctx.messages)
    finally:
        if timer:
            timer.end_span(
                "context_window",
                messages_in=input_messages_count,
                messages_out=len(ctx.messages),
                estimated_tokens=estimated_tokens,
            )

    log.info(
        "conversation_context_loaded",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        total_messages_in_db=session_message_count,
        messages_loaded=len(ctx.messages),
        messages_truncated=max(0, input_messages_count - len(ctx.messages)),
        estimated_tokens=estimated_tokens,
    )

    # Query memory graph for relevant context (Phase 2.2)
    if settings.enable_memory_graph:
        if timer:
            timer.start_span("memory_query")
        try:
            from personal_agent.memory.models import MemoryQuery
            from personal_agent.memory.service import MemoryService

            memory_service: MemoryService | None = None
            global_memory_service: MemoryService | None = None
            try:
                from personal_agent.service.app import memory_service as global_memory_service

                if global_memory_service and global_memory_service.connected:
                    memory_service = global_memory_service
            except (ImportError, AttributeError):
                memory_service = MemoryService()
                await memory_service.connect()

            if memory_service and memory_service.connected:
                conversations_found = 0

                potential_entities: list[str] = []
                if is_memory_recall_query(ctx.user_message):
                    # Broad recall path (ADR-0025): no entity names to match
                    entity_type_hints = _extract_entity_type_hints(ctx.user_message)
                    try:
                        broad = await memory_service.query_memory_broad(
                            entity_types=entity_type_hints or None,
                            recency_days=90,
                            limit=20,
                            trace_id=ctx.trace_id,
                            query_text=ctx.user_message,
                            # FRE-673: thread request identity so 'group'-visibility
                            # memory is revealed by the chokepoint filter (FRE-229).
                            user_id=ctx.user_id,
                            authenticated=ctx.authenticated,
                        )
                        ctx.memory_context = _format_broad_recall(broad)
                        conversations_found = len(ctx.memory_context)
                        log.info(
                            "memory_recall_broad_query",
                            trace_id=ctx.trace_id,
                            entity_type_hints=entity_type_hints,
                            entities_found=len(broad.get("entities", [])),
                        )
                    except Exception as broad_err:
                        log.warning(
                            "memory_recall_broad_query_failed",
                            trace_id=ctx.trace_id,
                            error=str(broad_err),
                        )
                        log.info(
                            "memory_recall_broad_query",
                            trace_id=ctx.trace_id,
                            entity_type_hints=entity_type_hints,
                            entities_found=0,
                            query_error=str(broad_err),
                        )
                else:
                    # Entity-name match path (existing)
                    words = ctx.user_message.split()
                    potential_entities = [
                        w.strip('",.:;!?') for w in words if len(w) > 3 and w[0].isupper()
                    ]
                    if potential_entities:
                        query = MemoryQuery(
                            entity_names=potential_entities[:5],
                            limit=5,
                            recency_days=30,
                        )
                        result = await memory_service.query_memory(
                            query,
                            feedback_key=ctx.session_id,
                            query_text=ctx.user_message,
                            # FRE-698: thread trace+session so the reranker fired inside
                            # query_memory emits join keys for the ADR-0074 probe.
                            trace_id=ctx.trace_id,
                            session_id=ctx.session_id,
                            # FRE-673: thread request identity so 'group'-visibility
                            # memory is revealed by the chokepoint filter (FRE-229).
                            user_id=ctx.user_id,
                            authenticated=ctx.authenticated,
                        )
                        ctx.memory_context = [
                            {
                                "conversation_id": conv.turn_id,
                                "timestamp": conv.timestamp.isoformat(),
                                "user_message": conv.user_message,
                                "summary": conv.summary or conv.user_message[:200],
                                "key_entities": conv.key_entities,
                            }
                            for conv in result.conversations
                        ]
                        conversations_found = len(ctx.memory_context)
                        log.info(
                            "memory_enrichment_completed",
                            trace_id=ctx.trace_id,
                            conversations_found=conversations_found,
                        )

                # Populate operator stanza (FRE-213 / ADR-0052) while service is connected.
                if ctx.user_id and ctx.user_email:
                    from personal_agent.orchestrator.prompts import (
                        get_owner_stanza,  # noqa: PLC0415
                    )

                    ctx.operator_stanza = await get_owner_stanza(
                        memory_service=memory_service,
                        user_id=ctx.user_id,
                        email=ctx.user_email,
                        display_name=ctx.user_display_name,
                    )

                if memory_service != global_memory_service:
                    await memory_service.disconnect()

                if timer:
                    timer.end_span(
                        "memory_query",
                        entities_searched=len(potential_entities) if potential_entities else 0,
                        conversations_found=conversations_found,
                    )
            elif is_memory_recall_query(ctx.user_message):
                # Broad recall intent without a connected MemoryService (e.g. Neo4j
                # used only by second_brain). Still emit telemetry so eval/harness
                # can observe the recall path (ADR-0025).
                log.info(
                    "memory_recall_broad_query",
                    trace_id=ctx.trace_id,
                    entity_type_hints=_extract_entity_type_hints(ctx.user_message),
                    entities_found=0,
                    skipped_reason="memory_service_unavailable",
                )
                if timer:
                    timer.end_span(
                        "memory_query",
                        entities_searched=0,
                        conversations_found=0,
                    )
            else:
                # Memory graph enabled but service not connected and not a recall-only path.
                if timer:
                    timer.end_span(
                        "memory_query",
                        entities_searched=0,
                        conversations_found=0,
                        skipped_reason="memory_service_unavailable",
                    )
        except Exception as e:
            if timer:
                timer.end_span("memory_query", error=str(e))
            log.warning(
                "memory_enrichment_failed",
                trace_id=ctx.trace_id,
                error=str(e),
                exc_info=True,
            )

    needs_planning = False

    if needs_planning:
        return TaskState.PLANNING
    return TaskState.LLM_CALL


async def step_planning(
    ctx: ExecutionContext, session_manager: SessionManager, trace_ctx: TraceContext
) -> TaskState:
    """Use reasoning model to create an execution plan.

    This is a placeholder for future planning functionality.
    For skeleton, just transition to LLM_CALL.

    Args:
        ctx: Execution context.
        session_manager: Session manager.
        trace_ctx: Trace context.

    Returns:
        Next state (LLM_CALL).
    """
    # TODO: Call LLM with planning prompt
    # TODO: Parse plan, store in ctx.current_plan
    ctx.current_plan = {"status": "placeholder"}
    return TaskState.LLM_CALL


async def step_llm_call(
    ctx: ExecutionContext, session_manager: SessionManager, trace_ctx: TraceContext
) -> TaskState:
    """Execute LLM call with the primary model.

    All requests use the PRIMARY model (ADR-0033 two-tier taxonomy).
    Intent classification is handled by the Pre-LLM Gateway; this step
    executes the call and proceeds to TOOL_EXECUTION or SYNTHESIS.

    Args:
        ctx: Execution context.
        session_manager: Session manager.
        trace_ctx: Trace context.

    Returns:
        Next state (TOOL_EXECUTION, SYNTHESIS, or FAILED).
    """
    timer = ctx.request_timer
    llm_span_name: str | None = None  # set once span is started; used to close span on exception

    # ADR-0061 — within-session hard trigger.  Fires synchronously when the
    # working messages list crosses the hard threshold (default 0.85 of the
    # context window).  Layers above Stage 7 (which runs at request entry);
    # this catches in-flight overflow caused by large tool responses.
    from personal_agent.orchestrator.within_session_compression import (
        compress_in_place,
        needs_hard_compression,
    )

    if ctx.session_id and needs_hard_compression(ctx.messages, settings.context_window_max_tokens):
        # ADR-0076: ask before silently summarising history. "Stop here"
        # produces a final answer from current context; "Compress and continue"
        # (the default) runs the existing within-session compression.
        _max_tokens = settings.context_window_max_tokens
        _tokens = estimate_messages_tokens(ctx.messages)
        _pct = (100.0 * _tokens / _max_tokens) if _max_tokens else 0.0
        _compress_action = await _maybe_pause_for_constraint(
            session_id=ctx.session_id,
            trace_id=ctx.trace_id,
            user_id=ctx.user_id,
            constraint="context_compression",
            context=(
                f"Context is at {_pct:.0f}% of the window "
                f"({_tokens:,} / {_max_tokens:,} tokens). "
                "Compressing will summarise older turns."
            ),
        )
        if _compress_action == "stop_here":
            log.info(
                "context_compression_declined",
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
            )
            ctx.force_synthesis_from_limit = True
        else:
            try:
                from personal_agent.events.bus import get_event_bus

                _bus = get_event_bus()
            except Exception:  # event-bus init failure must not block the loop
                _bus = None
            log.info(
                "within_session_compression_hard_trigger",
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                messages=len(ctx.messages),
                max_tokens=settings.context_window_max_tokens,
            )
            try:
                ctx.messages, _ = await compress_in_place(
                    ctx.messages,
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                    trigger="hard",
                    bus=_bus,
                )
            except Exception as exc:
                # Pre-LLM compression must never crash the orchestrator: Stage 7
                # at the next request boundary remains the safety net.
                log.warning(
                    "within_session_compression_hard_failed",
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    # Determine which model to call
    if ctx.gateway_output is not None and ctx.selected_model_role is None:
        # Gateway-driven path: always use PRIMARY role (ADR-0033)
        model_role = ModelRole.PRIMARY
        ctx.selected_model_role = model_role
        log.info(
            "step_llm_call_gateway_model",
            trace_id=ctx.trace_id,
            model_role=model_role.value,
            task_type=ctx.gateway_output.intent.task_type.value,
        )
    elif ctx.selected_model_role is None:
        # First LLM call: always PRIMARY (ADR-0033)
        model_role = _determine_initial_model_role(ctx)
    else:
        # Continuation — use previously selected role
        model_role = ctx.selected_model_role

    system_prompt: str | None = None

    # Inject deployment context so the model doesn't try to access host-only paths.
    # Tool-name hints are appended later, only when tools are actually being passed
    # — otherwise the model sees named tools it can't call and hallucinates pseudo-code.
    if settings.environment == Environment.PRODUCTION:
        system_prompt = (
            "## Deployment Context\n"
            "You are running inside a Docker container on a cloud VPS.\n"
            "- App code is at `/app` — the host's repo mount point is NOT accessible from here\n"
            "- Configuration is injected as environment variables at startup; there is no `.env` file inside the container\n"
            "- Do NOT search for files at host filesystem paths (the host's repo checkout or home directory) — they do not exist inside the container\n"
            "- All backend services are reachable via Docker internal DNS:\n"
            "    postgres:5432  |  neo4j:7687 (bolt) / neo4j:7474 (HTTP)  |  elasticsearch:9200\n"
            "    redis:6379  |  embeddings:8503  |  reranker:8504"
        )

    # Operator identity stanza (FRE-213 / ADR-0052) — populated in step_init.
    # Placed before skill routing and memory sections to sit inside the cached prompt prefix.
    if ctx.operator_stanza:
        if system_prompt:
            system_prompt = f"{system_prompt}\n\n{ctx.operator_stanza}"
        else:
            system_prompt = ctx.operator_stanza

    # Prompt-identity component presence flags (ADR-0078 D1, FRE-405). Set as the
    # corresponding fragments are spliced in; consumed at the respond() call to
    # build the orchestrator.primary PromptIdentity.
    _skill_index_present = False
    _decomposition_added = False
    tool_awareness = ""

    # ADR-0081 D4: the volatile skill-bodies block (selected bodies +
    # <skill_usage_directives>). Assembled in the skill-routing block below but
    # appended to the VOLATILE tail (after the static-prefix capture), alongside
    # memory_section. Declared here so it survives into the try block regardless
    # of whether the prefer_primitives_enabled path runs.
    _skill_bodies_tail = ""

    # Phase B skill routing (FRE-skill-routing, ADR-0063 §D7).
    # Routing mode controls what gets injected:
    #   keyword       — keyword-matched skill bodies only (Phase A legacy behavior)
    #   model_decided — compact skill index only; model calls read_skill on demand
    #   hybrid        — both index AND keyword bodies; bodies suppressed for skills
    #                   already loaded via read_skill this conversation
    # Placed before dynamic content (memory/decomposition) to stay in the cached prefix.
    from personal_agent.orchestrator.skills import (  # noqa: PLC0415
        assemble_skill_index,
        assemble_skill_index_directive,
        assemble_skill_usage_directives,
        get_all_skills,
        get_skill_block,
    )

    if settings.prefer_primitives_enabled:
        _user_message: str | None = None
        for _msg in reversed(ctx.messages):
            if isinstance(_msg, dict) and _msg.get("role") == "user":
                _user_message = get_text_content(_msg.get("content", ""))
                break

        # Priority: per-request override > global setting.
        from personal_agent.config.selection import (  # noqa: PLC0415
            get_skill_routing_mode_override as _get_srm_override,
        )

        _routing_mode = _get_srm_override() or settings.skill_routing_mode

        # Phase C: separate routing call (model_decided + non-empty model key, once per request)
        if (
            _routing_mode == "model_decided"
            and settings.skill_routing_model_key
            and not ctx.skill_routing_done
            and _user_message
        ):
            ctx.skill_routing_done = True
            ctx.skill_routing_model_id = settings.skill_routing_model_key
            try:
                from personal_agent.llm_client.factory import (  # noqa: PLC0415
                    get_llm_client_for_key,
                )
                from personal_agent.orchestrator.skills import route_skills  # noqa: PLC0415

                _routing_client = get_llm_client_for_key(settings.skill_routing_model_key)
                _routing_start = time.time()
                _relevant = await route_skills(
                    user_message=_user_message,
                    routing_client=_routing_client,
                    cap_tokens=settings.skill_index_max_tokens,
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                )
                _routing_latency_ms = int((time.time() - _routing_start) * 1000)
                # Pre-load returned skill bodies — primary agent sees them already in scope
                from personal_agent.orchestrator.skills import get_all_skills  # noqa: PLC0415

                _all = get_all_skills()
                for _name in _relevant:
                    if _name in _all:
                        ctx.loaded_skills.add(_name)

                log.info(
                    "skill_routing_call_completed",
                    routing_model_key=settings.skill_routing_model_key,
                    latency_ms=_routing_latency_ms,
                    skills_returned=_relevant,
                    trace_id=ctx.trace_id,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "skill_routing_call_skipped",
                    error=str(exc),
                    routing_model_key=settings.skill_routing_model_key,
                    trace_id=ctx.trace_id,
                )

        # ADR-0081 D4: split the skill block at its volatility seam.
        #   STABLE  → compact index + <skill_index_directive>  (cached prefix)
        #   VOLATILE → selected bodies + <skill_usage_directives>  (volatile tail)
        # Track the two classes in separate variables so the volatile fragments
        # never enter the static-prefix capture.
        _skill_index_text: str = ""  # STABLE — deterministic catalog render
        _skill_bodies_text: str = ""  # VOLATILE — per-turn selected bodies

        _all_skills = get_all_skills()

        if _routing_mode == "model_decided":
            # Index (stable) + bodies of any pre-loaded (router-selected) skills.
            _skill_index_text = assemble_skill_index(cap_tokens=settings.skill_index_max_tokens)
            _preloaded_bodies: list[str] = []
            if ctx.loaded_skills:
                for _name in sorted(ctx.loaded_skills):
                    _doc = _all_skills.get(_name)
                    if _doc and _doc.body:
                        _preloaded_bodies.append(_doc.body)
            _skill_bodies_text = "\n\n".join(p for p in _preloaded_bodies if p)
        elif _routing_mode == "hybrid":
            _skill_index_text = assemble_skill_index(cap_tokens=settings.skill_index_max_tokens)
            _skill_bodies_text = get_skill_block(
                message=_user_message,
                loaded_skills=ctx.loaded_skills,
            )
        else:  # keyword (default / legacy) — bodies only, no index
            _skill_bodies_text = get_skill_block(message=_user_message)

        _has_index = bool(_skill_index_text)
        _has_bodies = bool(_skill_bodies_text)

        # FRE-337: deterministic directive blocks, partitioned by volatility class.
        # <skill_index_directive> is STABLE → rides the cached index block.
        # <skill_usage_directives> is VOLATILE → rides the body block in the tail.
        # Both gated by settings.skill_nudge_enabled.
        _index_directive = ""
        _usage_directives = ""
        if settings.skill_nudge_enabled:
            if _has_index:
                _index_directive = assemble_skill_index_directive()
            if _has_bodies:
                _usage_directives = assemble_skill_usage_directives(
                    list(ctx.loaded_skills), _all_skills
                )

        # Independent joiners (ADR-0081 D4 caution): the stable side must be
        # byte-identical whether 0 or N bodies are selected, so build each block
        # from its own fragments — the tail's presence never alters prefix bytes.
        _skill_index_block = "\n\n".join(p for p in [_skill_index_text, _index_directive] if p)
        _skill_bodies_tail = "\n\n".join(p for p in [_skill_bodies_text, _usage_directives] if p)

        # STABLE index → cached prefix (before the line-~2259 capture).
        # _skill_index_present reflects ACTUAL index presence only (not bodies),
        # so the skill_index component id is not falsely stamped in keyword mode.
        _skill_index_present = _has_index
        if _skill_index_block:
            if system_prompt:
                system_prompt = f"{system_prompt}\n\n{_skill_index_block}"
            else:
                system_prompt = _skill_index_block

        log.info(
            "skill_index_assembled",
            routing_mode=_routing_mode,
            index_chars=len(_skill_index_text),
            bodies_chars=len(_skill_bodies_text),
            loaded_skills_count=len(ctx.loaded_skills),
            skill_routing_model_key=ctx.skill_routing_model_id or None,
            index_directive_emitted=bool(_index_directive),
            usage_directives_emitted=bool(_usage_directives),
            trace_id=ctx.trace_id,
        )

    # Create span for LLM call
    span_ctx, span_id = trace_ctx.new_span()

    step_start_time = time.time()
    log.info(
        STEP_PLANNING_STARTED,
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        span_id=span_id,
        parent_span_id=trace_ctx.parent_span_id,
        model_role=model_role.value,
        channel=ctx.channel.value,
    )

    try:
        # Create LLM client — dispatches to LocalLLMClient or LiteLLMClient by provider placement
        # ADR-0101 §5/§8a + ADR-0102 §3: an image or document attachment always
        # routes to the pinned `vision` role (ADR-0121 T5) instead of the
        # calling role's own selection — resolved here, inside the try block,
        # so a fail-closed AttachmentUnsupportedError is caught by the except
        # below rather than propagating uncaught above the state machine.
        # Both sides must resolve to a DEPLOYMENT key or the equality below
        # breaks: role_key was a role name while the attachment path returns a
        # catalog key, so every plain turn would take the escalation branch —
        # picking a client via get_llm_client_for_key and logging escalated=True
        # for a turn that escalated nothing.
        from personal_agent.config.model_loader import resolve_role_target  # noqa: PLC0415
        from personal_agent.config.selection import get_current_selection  # noqa: PLC0415
        from personal_agent.llm_client.factory import get_llm_client
        from personal_agent.orchestrator.attachment_resolution import RASTER_CONTENT_TYPES

        role_key, _ = resolve_role_target(
            model_role.value,
            model_key=get_current_selection(model_role.value),
        )
        effective_model_key = _effective_attachment_routing_key(ctx, model_role.value)

        # ADR-0074 §8c / FRE-693: log the routing decision only when this turn
        # carries a raster image (always evaluated by _resolve_vision_routing_key,
        # a real decision point even if it's a no-op) or a document actually
        # forced a routing decision (ctx.document_effective_model_key set — a
        # Tier-1-only PDF never reaches this, so it must not be logged as a
        # routing decision that never happened; code-review finding).
        if (
            any(a.content_type in RASTER_CONTENT_TYPES for a in ctx.attachments)
            or ctx.document_effective_model_key is not None
        ):
            log.info(
                "vision_routing_decision",
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                task_id=None,
                model_role=model_role.value,
                role_key=role_key,
                effective_model_key=effective_model_key,
                escalated=effective_model_key != role_key,
            )

        if effective_model_key == role_key:
            llm_client = get_llm_client(role_name=model_role.value)
        else:
            from personal_agent.cost_gate import budget_role_for
            from personal_agent.llm_client.factory import get_llm_client_for_key

            llm_client = get_llm_client_for_key(
                effective_model_key, budget_role=budget_role_for(model_role.value)
            )

        # Get tools for this model role and mode
        # ReAct loop: always offer tools so the model can chain calls until it
        # decides to synthesize on its own.  Bounded by orchestrator_max_tool_iterations
        # in step_tool_execution, which forces TaskState.SYNTHESIS when the limit is hit.
        is_synthesizing = False

        # ── Strategy-aware tool setup (ADR-0032) ──────────────────────
        from personal_agent.llm_client.models import ToolCallingStrategy

        model_config = llm_client.model_configs.get(effective_model_key)
        tool_strategy = (
            model_config.effective_tool_strategy if model_config else ToolCallingStrategy.NATIVE
        )

        tools: list[dict[str, Any]] | None = None
        _prompt_injected_tool_text: str | None = None  # filled for PROMPT_INJECTED only

        # Forced synthesis: iteration limit fired — disable tools and inject a synthesis prompt
        # so the LLM produces a real answer from gathered results instead of a useless fallback.
        if ctx.force_synthesis_from_limit:
            ctx.force_synthesis_from_limit = False
            is_synthesizing = True
            ctx.messages.append(
                {
                    "role": "user",
                    "content": (
                        "You have reached the tool call limit. "
                        "Do NOT call any more tools. "
                        "Using only the tool results already in this conversation, "
                        "synthesize a complete, helpful answer to the user's original request."
                    ),
                }
            )
            log.info(
                "force_synthesis_injected",
                trace_id=ctx.trace_id,
                iteration=ctx.tool_iteration_count,
            )

        # Budget warning: when 2 calls from the per-TaskType limit, ask the LLM to wrap up
        elif not is_synthesizing and ctx.tool_iteration_count >= _resolve_max_iterations(ctx) - 2:
            _effective_max = _resolve_max_iterations(ctx)
            ctx.messages.append(
                {
                    "role": "user",
                    "content": (
                        f"⚠️ Tool budget: {_effective_max - ctx.tool_iteration_count} "
                        "tool call(s) remaining. Prioritize synthesis — only make additional tool calls "
                        "if they are strictly necessary to answer the user's question."
                    ),
                }
            )
            log.info(
                "tool_budget_warning_injected",
                trace_id=ctx.trace_id,
                remaining=_effective_max - ctx.tool_iteration_count,
            )

        if not is_synthesizing and tool_strategy != ToolCallingStrategy.DISABLED:
            # Load tool definitions from registry
            global _tool_registry
            if _tool_registry is None:
                _tool_registry = get_default_registry()

            # Per ADR-0063 §D1 (FRE-260), governance is mode-only — the
            # TaskType→tool-filter wire is severed. Every turn sees every tool
            # the active mode allows.
            tool_defs = _tool_registry.get_tool_definitions_for_llm(mode=ctx.mode)

            if tool_strategy == ToolCallingStrategy.NATIVE:
                # Pass tools in the API request — model uses native function calling
                tools = tool_defs if tool_defs else None
            elif tool_strategy == ToolCallingStrategy.PROMPT_INJECTED:
                # Render tools as text for the system prompt instead of the API parameter.
                # The model's chat template doesn't support the tools array.
                from personal_agent.llm_client.tool_prompt_renderer import render_tools_for_prompt

                _prompt_injected_tool_text = render_tools_for_prompt(tool_defs)
                tools = None  # do NOT send tools array in the API request

            log.debug(
                "tools_passed_to_llm",
                trace_id=ctx.trace_id,
                model_role=model_role.value,
                tool_strategy=tool_strategy.value,
                tool_count=len(tool_defs) if tool_defs else 0,
                tool_names=[t.get("function", {}).get("name") for t in (tool_defs or [])],
                mode=ctx.mode.value,
                prompt_injected=(_prompt_injected_tool_text is not None),
            )
        else:
            log.debug(
                "tools_not_passed",
                trace_id=ctx.trace_id,
                model_role=model_role.value,
                tool_strategy=tool_strategy.value,
                reason="synthesizing" if is_synthesizing else "disabled",
            )

        # FRE-484: Anthropic rejects a forced-synthesis call whose history already
        # contains tool blocks unless tools= is present. Keep a non-empty tool list
        # and pin tool_choice="none" so synthesis still happens. No-op on every other
        # path (local SLM, or no tool history) → (None, None) preserves prior behavior.
        tool_choice: str | dict[str, Any] | None = None
        if is_synthesizing:
            _provider = getattr(llm_client, "provider", None)
            _synthesis_tool_defs = (
                get_default_registry().get_tool_definitions_for_llm(mode=ctx.mode)
                if _provider == "anthropic"
                else None
            )
            tools, tool_choice = _forced_synthesis_tool_overrides(
                provider=_provider,
                messages=ctx.messages,
                tool_defs=_synthesis_tool_defs,
            )
            if tools:
                log.info(
                    "force_synthesis_tools_retained",
                    trace_id=ctx.trace_id,
                    provider=_provider,
                    tool_count=len(tools),
                )

        # ADR-0081 D1: Volatility-gradient layout — build memory_section locally
        # without injecting it yet; it will be appended last as the VOLATILE tail.
        # This ensures the KV-cache boundary sits between the stable prefix and
        # the per-turn dynamic content, fixing the cross-turn reuse ≈ 0 issue.
        memory_section: str | None = None
        if ctx.memory_context and len(ctx.memory_context) > 0:
            if ctx.memory_context[0].get("type") in ("entity", "session"):
                # Broad recall path — format as direct knowledge summary
                entity_items = [m for m in ctx.memory_context if m.get("type") == "entity"]
                memory_section = _render_memory_section(entity_items) or None
            else:
                # Task-assist path — inject conversation summaries
                _ms = "\n\n## Relevant Past Conversations\n"
                _ms += (
                    "The following past conversations may be relevant to the current request:\n\n"
                )
                for i, mem in enumerate(ctx.memory_context[:3], 1):  # Limit to top 3
                    _ms += f"{i}. {mem.get('summary', mem.get('user_message', ''))[:150]}...\n"
                    if mem.get("key_entities"):
                        _ms += f"   Entities: {', '.join(mem['key_entities'][:5])}\n"
                _ms += "\nYou can reference these past conversations to provide more context-aware responses."
                memory_section = _ms

        # If we are passing tools (native or prompt-injected), include tool-use guidance
        # in the system prompt to reduce malformed tool calls and looping (ADR-0032).
        # STATIC tool rules go FIRST (primacy + cached), then SEMI-STATIC tool awareness
        # and the base system body. Memory (VOLATILE) is appended last — see below.
        if tools or _prompt_injected_tool_text:
            from personal_agent.orchestrator.prompts import (
                TOOL_USE_NATIVE_PROMPT,
                TOOL_USE_PROMPT_INJECTED,
                get_tool_awareness_prompt,
            )

            # Select the prompt variant that matches the strategy
            if tool_strategy == ToolCallingStrategy.PROMPT_INJECTED:
                tool_prompt = TOOL_USE_PROMPT_INJECTED
                # Append the rendered tool definitions after the behavioural prompt
                tool_prompt = f"{tool_prompt}\n{_prompt_injected_tool_text}"
            else:
                tool_prompt = TOOL_USE_NATIVE_PROMPT

            # Add tool awareness so agent can answer questions about its capabilities
            tool_awareness = get_tool_awareness_prompt()

            # tool_prompt (STATIC) first, tool_awareness (SEMI-STATIC) next, base last
            if system_prompt:
                system_prompt = f"{tool_prompt}\n\n{tool_awareness}\n\n{system_prompt}"
            else:
                system_prompt = f"{tool_prompt}\n\n{tool_awareness}"

        # HYBRID decomposition prompt (autonomous mode only — enforced mode
        # uses the expansion controller which has already run by this point).
        if (
            ctx.expansion_strategy is not None
            and ctx.sub_agent_results is None
            and settings.orchestration_mode == "autonomous"
        ):
            hybrid_prompt = (
                "\n\n## Decomposition Instructions\n"
                "Break your response into a numbered list of independent sub-tasks "
                "(1. ..., 2. ..., 3. ...). Each item should be a self-contained "
                "task that can be researched or answered independently. "
                "Keep to 2-4 sub-tasks. After the sub-tasks complete, you will "
                "synthesize their results into a final answer."
            )
            _decomposition_added = True
            if system_prompt:
                system_prompt = f"{system_prompt}{hybrid_prompt}"
            else:
                system_prompt = hybrid_prompt.strip()

        # Capture the cacheable prefix AFTER all STATIC/SEMI-STATIC assembly
        # (incl. the stable skill index) and BEFORE the VOLATILE tail — this is
        # what the static_prefix_hash covers (ADR-0081 D1 + D4).
        inner_system_before_memory = system_prompt or ""

        _frozen_layout = settings.cache_frozen_layout_enabled
        if _frozen_layout:
            # ADR-0081 §D2 (FRE-434): frozen append-only layout. Per-turn volatile
            # (selected skill bodies + usage-directives + recalled memory; D3
            # salient highlights join in PR2) rides the CURRENT user turn, not the
            # system head. message[0] stays exactly inner_system_before_memory, so
            # the wire prefix is byte-stable and prior turns replay as a strict
            # forward extension — the property local KV reuse requires.
            #
            # The block is inlined into ctx.messages in place so the history
            # persisted at end of turn (update_session) equals the wire form
            # sent now. _inline_… is a no-op when the block is empty or the last
            # message is not a user turn (e.g. post-tool synthesis, where the
            # current user query — already inlined on the tool-request call —
            # still carries the volatile earlier in the sequence).
            # Order (ADR-0081 §D4/§D3): skill bodies + usage-directives → recalled
            # memory → D3 salient highlights → the ADR-0122 §5 artifact-builder
            # planning note, the latter two closest to the query.
            _volatile_block = "\n\n".join(
                p
                for p in (
                    _skill_bodies_tail,
                    memory_section or "",
                    ctx.salient_highlights,
                    ctx.artifact_builder_planning_note or "",
                )
                if p
            )
            ctx.messages = _inline_volatile_into_last_user_message(ctx.messages, _volatile_block)
        else:
            # D1/D4 head-layout (unchanged when the flag is off). VOLATILE tail
            # order: selected skill bodies → <skill_usage_directives> → recalled
            # memory → the ADR-0122 §5 artifact-builder planning note; appended
            # after the capture so none enters static_prefix_hash.
            if _skill_bodies_tail:
                if system_prompt:
                    system_prompt = f"{system_prompt}\n\n{_skill_bodies_tail}"
                else:
                    system_prompt = _skill_bodies_tail

            if memory_section:
                if system_prompt:
                    system_prompt = f"{system_prompt}\n{memory_section}"
                else:
                    system_prompt = memory_section

            if ctx.artifact_builder_planning_note:
                if system_prompt:
                    system_prompt = f"{system_prompt}\n\n{ctx.artifact_builder_planning_note}"
                else:
                    system_prompt = ctx.artifact_builder_planning_note

        # Call LocalLLMClient.respond()
        # Pass previous_response_id for stateful /v1/responses API
        max_retries_override: int | None = 1 if tools else None

        # /no_think injection for tool flow (per user preference):
        # - Tool-request call: append suffix to the last user message.
        # - Post-tool synthesis: append a short user nudge ending with the suffix (tool outputs are last).
        #   IMPORTANT: Skip synthesis nudge for Mistral models - they expect direct synthesis after tool results
        #   Note: We always inject the suffix when tools are present. LM Studio ignores extra_body
        #   chat_template_kwargs, so the suffix is the only working thinking control for Qwen3.5.
        request_messages = ctx.messages

        if tools:
            request_messages = _append_no_think_to_last_user_message(request_messages)

        # Validate and fix conversation role alternation for strict models (e.g., Mistral).
        request_messages = _validate_and_fix_conversation_roles(request_messages)

        # Debug: log message roles for conversation validation
        message_roles = [msg.get("role", "unknown") for msg in request_messages]
        log.info(
            "llm_call_messages_debug",
            trace_id=ctx.trace_id,
            span_id=span_id,
            model_role=model_role.value,
            message_count=len(request_messages),
            message_roles=message_roles,
            messages_preview=[
                {
                    "role": msg.get("role"),
                    "content_preview": get_text_content(msg.get("content", ""))[:100] or None,
                    "has_tool_calls": bool(msg.get("tool_calls")),
                }
                for msg in request_messages
            ],
        )
        # Timer span
        llm_span_name = f"llm_call:{model_role.value}"
        if timer:
            timer.start_span(llm_span_name)

        from personal_agent.llm_client.concurrency import InferencePriority
        from personal_agent.llm_client.prompt_identity import derive_prompt_identity

        # Build the orchestrator.primary PromptIdentity (ADR-0078 D1/D4, FRE-405).
        # After ADR-0081 D1, inner_system_before_memory IS the full cacheable prefix:
        # tool rules (STATIC) → tool awareness (SEMI-STATIC) → base system body → decomposition.
        # The volatile memory tail is appended after this capture point.
        _static_prefix = inner_system_before_memory
        _component_ids: list[str] = []
        if tool_awareness:
            _component_ids.append("tool_awareness")
        if settings.environment == Environment.PRODUCTION:
            _component_ids.append("deployment_context")
        if ctx.operator_stanza:
            _component_ids.append("operator_stanza")
        if _skill_index_present:
            _component_ids.append("skill_index")
        if _skill_bodies_tail:
            # ADR-0081 D4: distinct VOLATILE marker — these bytes feed dynamic_hash,
            # never static_prefix_hash (they are appended after the capture point).
            _component_ids.append("skill_bodies")
        if ctx.memory_context:
            _component_ids.append("memory_section")
        if ctx.artifact_builder_planning_note:
            # ADR-0122 §5/T6: distinct VOLATILE marker — turn-scoped, never enters
            # static_prefix_hash.
            _component_ids.append("artifact_builder_planning_note")
        if tool_awareness:
            _component_ids.append("tool_use_rules")
        if _decomposition_added:
            _component_ids.append("decomposition_instructions")
        _prompt_identity = derive_prompt_identity(
            "orchestrator.primary",
            static_prefix=_static_prefix,
            full_prompt=system_prompt or "",
            component_ids=tuple(_component_ids),
        )

        response = await llm_client.respond(
            role=model_role,
            messages=request_messages,
            system_prompt=system_prompt,
            tools=tools if tools else None,
            tool_choice=tool_choice,
            trace_ctx=span_ctx,
            previous_response_id=ctx.last_response_id,
            max_retries=max_retries_override,
            priority=InferencePriority.USER_FACING,
            prompt_identity=_prompt_identity,
        )

        # Extract response content and tool calls
        response_content = response["content"] or ""
        response_tool_calls = response["tool_calls"] or []

        # Track response_id for stateful /v1/responses API
        if response.get("response_id"):
            ctx.last_response_id = response["response_id"]

        duration_ms = int((time.time() - step_start_time) * 1000)
        total_tokens = response.get("usage", {}).get("total_tokens", 0)
        prompt_tokens = response.get("usage", {}).get("prompt_tokens", 0)
        completion_tokens = response.get("usage", {}).get("completion_tokens", 0)

        # Accumulate the primary loop's per-call cost — this feeds the durable row's
        # cost_live_usd for primary turns (ADR-0088 D3); the live meter itself climbs from
        # turn.model_call_completed events. Report progress so tool/context refresh.
        ctx.turn_cost_usd += float(response.get("cost_usd") or 0.0)
        await _report_turn_progress(ctx)

        if timer:
            timer.end_span(
                llm_span_name,
                model_role=model_role.value,
                tokens=total_tokens,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        log.info(
            LLM_STEP_COMPLETED,
            trace_id=ctx.trace_id,
            span_id=span_id,
            duration_ms=duration_ms,
            model_role=model_role.value,
            tokens=total_tokens,
        )
        # Record step
        step: OrchestratorStep = {
            "type": "llm_call",
            "description": f"LLM call with {model_role.value} model",
            "metadata": {
                "model_role": model_role.value,
                "span_id": span_id,
                "duration_ms": duration_ms,
                "tokens": total_tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        }
        ctx.steps.append(step)

        # Some reasoning models may emit router-style JSON with a `response` field.
        # Unwrap it to avoid returning JSON to the user.
        response_content = _unwrap_embedded_response_json(response_content)

        # --- HYBRID expansion hook (autonomous mode only) ---
        if (
            ctx.expansion_strategy is not None
            and ctx.sub_agent_results is None
            and settings.orchestration_mode == "autonomous"
        ):
            from personal_agent.orchestrator.expansion import (
                execute_hybrid,
                parse_decomposition_plan,
            )

            max_sub = (ctx.expansion_constraints or {}).get("max_sub_agents", 3)
            specs = parse_decomposition_plan(
                plan_text=response_content,
                max_sub_agents=max_sub,
            )

            # Phase B: inherit skill index + loaded_skills from parent context
            if specs and settings.prefer_primitives_enabled:
                from dataclasses import replace  # noqa: PLC0415

                from personal_agent.orchestrator.skills import assemble_skill_index  # noqa: PLC0415

                _sub_index = assemble_skill_index(cap_tokens=settings.skill_index_max_tokens)
                _parent_loaded = frozenset(ctx.loaded_skills)
                specs = [
                    replace(spec, skill_index_block=_sub_index, loaded_skills=_parent_loaded)
                    for spec in specs
                ]

            if specs:
                # ADR-0088 D4: report progress at dispatch start (the meter is already
                # lit by the seam's turn.topology_entered + primary model_call_completed
                # events; cost is not accumulated per-loop).
                await _report_turn_progress(ctx)
                results = await execute_hybrid(
                    specs=specs,
                    trace_id=ctx.trace_id,
                    max_concurrent=max_sub,
                    session_id=ctx.session_id,
                    eval_mode=ctx.eval_mode,
                )
                ctx.sub_agent_results = results

                # ADR-0088 D3: FRE-501's per-loop sub-agent cost rollup is removed — each
                # sub-agent model call already publishes turn.model_call_completed from the
                # cost boundary, so the live meter climbs without accumulation here. Report
                # progress so the tool/context fields refresh after the fan-out.
                await _report_turn_progress(ctx)

                # Build synthesis context and append to messages
                synthesis_parts = ["Sub-agent results:\n"]
                for r in results:
                    status = "OK" if r.success else f"FAILED: {r.error}"
                    synthesis_parts.append(f"- {r.spec_task}: [{status}] {r.summary}\n")
                synthesis_context = "".join(synthesis_parts)

                synthesis_msg = {
                    "role": "user",
                    "content": (
                        f"{synthesis_context}\n"
                        "The sub-tasks above have been completed. "
                        "Synthesize the results into a coherent response "
                        "for the user's original question."
                    ),
                }
                hybrid_assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response_content,
                }
                # Preserve thinking trace for templates that support
                # `preserve_thinking` (Qwen3.6 unsloth template reads
                # `message.reasoning_content` first, falls back to <think> tags
                # in content). Cloud paths and sub-agents with disable_thinking
                # emit reasoning_trace=None, so this is a no-op for them.
                hybrid_reasoning = response.get("reasoning_trace")
                if hybrid_reasoning:
                    hybrid_assistant_msg["reasoning_content"] = hybrid_reasoning
                ctx.messages.append(hybrid_assistant_msg)
                ctx.messages.append(synthesis_msg)

                log.info(
                    "expansion_phase1_complete",
                    sub_agent_count=len(results),
                    successful=sum(1 for r in results if r.success),
                    trace_id=ctx.trace_id,
                )

                # Re-enter LLM_CALL for synthesis (phase 2)
                return TaskState.LLM_CALL

            # No parseable specs — fall through to normal response path
            log.warning(
                "expansion_no_specs_parsed",
                strategy=ctx.expansion_strategy,
                trace_id=ctx.trace_id,
            )
        # --- End HYBRID expansion hook ---

        # Add assistant message to history (with tool calls if present).
        # Tool_call ids are rewritten with a turn prefix so ids do not collide
        # across rounds — see _build_assistant_tool_calls for why this matters.
        assistant_message: dict[str, Any] = {"role": "assistant", "content": response_content}
        # Preserve thinking trace for templates that support `preserve_thinking`
        # (Qwen3.6 unsloth template reads `message.reasoning_content` first,
        # falls back to <think> tags in content). Cloud paths and sub-agents
        # with disable_thinking emit reasoning_trace=None, so this is a no-op
        # for them. Until the slm_server flag flips, the template ignores it.
        reasoning_trace = response.get("reasoning_trace")
        if reasoning_trace:
            assistant_message["reasoning_content"] = reasoning_trace
        if response_tool_calls:
            assistant_message["tool_calls"] = _build_assistant_tool_calls(
                response_tool_calls,
                turn_id=ctx.tool_iteration_count,
            )
        ctx.messages.append(assistant_message)

        # ADR-0074 §I3: emit STEP_PLANNING_COMPLETED on every success exit so
        # the planning event pairs cleanly. Status indicates branch taken.
        step_planning_duration_ms = int((time.time() - step_start_time) * 1000)
        log.info(
            STEP_PLANNING_COMPLETED,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            span_id=span_id,
            parent_span_id=trace_ctx.parent_span_id,
            model_role=model_role.value,
            channel=ctx.channel.value,
            duration_ms=step_planning_duration_ms,
            status="success",
            next_state="tool_execution" if response_tool_calls else "synthesis",
        )

        # If tool calls present, transition to tool execution
        if response_tool_calls:
            return TaskState.TOOL_EXECUTION
        else:
            # No tools, set final reply and synthesize. FRE-734 Defect 2: when a
            # thinking model (Qwen3.6) emits the answer in the reasoning channel with
            # empty content — as on vision turns (ADR-0101) — surface the reasoning
            # trace rather than collapsing to a generic "Task completed".
            ctx.final_reply = _select_no_tool_final_reply(ctx, response_content, reasoning_trace)
            return TaskState.SYNTHESIS

    except Exception as e:
        duration_ms = int((time.time() - step_start_time) * 1000)
        if timer and llm_span_name:
            timer.end_span(
                llm_span_name,
                model_role=model_role.value,
                error=str(e),
                error_type=type(e).__name__,
            )
        log.error(
            MODEL_CALL_ERROR,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            span_id=span_id,
            duration_ms=duration_ms,
            error=str(e),
            error_type=type(e).__name__,
        )
        ctx.error = e

        # FRE-398: classify the error and salvage any gathered tool results.
        from personal_agent.error_classification import classify_error, with_partial

        classified = classify_error(e)

        # FRE-399 Layer 3: enrich the classified error reason with the last
        # known SLM health state when the SLM is degraded or down — converts
        # "an error occurred" into "GPU pinned (98%)" / "model not loaded" etc.
        # Best-effort: any exception here is swallowed silently.
        try:
            from personal_agent.config import settings as _s
            from personal_agent.llm_client.types import LLMClientError, LLMRateLimit
            from personal_agent.observability.slm_health import get_cached_snapshot

            # Only enrich for transient local failures (not rate-limit, not cloud).
            if (
                isinstance(e, LLMClientError)
                and not isinstance(e, LLMRateLimit)
                and classified.category not in ("budget_denied",)
            ):
                _snap = get_cached_snapshot(ttl=_s.slm_health_cache_ttl_seconds)
                if _snap is not None and _snap.status != "up":
                    _reason = _snap.degrade_reason()
                    if _reason:
                        classified = classified.__class__(
                            category=classified.category,
                            reason=f"{classified.reason} [{_reason}]",
                            next_step=classified.next_step,
                            actions=classified.actions,
                            partial=classified.partial,
                        )
                        log.info(
                            "slm_health_reason_injected",
                            slm_status=_snap.status,
                            degrade_reason=_reason,
                            trace_id=ctx.trace_id,
                            session_id=ctx.session_id,
                            component="executor",
                        )
        except Exception:  # noqa: BLE001
            pass  # health hint is best-effort — never impair the error path

        if ctx.tool_results:
            ctx.final_reply = (
                _fallback_reply_from_tool_results(
                    ctx,
                    lead="The model call failed before I could finish, but here's what I gathered:",
                )
                + f"\n\n---\n_{classified.reason} {classified.next_step}_"
            )
            classified = with_partial(classified)
        ctx.classified_error = classified

        error_step: OrchestratorStep = {
            "type": "warning",
            "description": f"LLM call failed: {classified.reason}",
            "metadata": {
                "error": classified.reason,
                "error_type": type(e).__name__,
                "error_category": classified.category,
                "span_id": span_id,
            },
        }
        ctx.steps.append(error_step)
        # ADR-0074 §I3: emit STEP_PLANNING_COMPLETED on error path so traces
        # have a matching completion for every started event.
        log.info(
            STEP_PLANNING_COMPLETED,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            span_id=span_id,
            parent_span_id=trace_ctx.parent_span_id,
            model_role=model_role.value,
            channel=ctx.channel.value,
            duration_ms=duration_ms,
            status="error",
            error_type=type(e).__name__,
            error_category=classified.category,
        )
        return TaskState.FAILED


async def step_tool_execution(
    ctx: ExecutionContext, session_manager: SessionManager, trace_ctx: TraceContext
) -> TaskState:
    """Execute tool calls, append results to context.

    This step:
    1. Extracts tool calls from the last assistant message
    2. Executes each tool via ToolExecutionLayer
    3. Appends tool results to ctx.messages as tool role messages
    4. Adds tool execution steps to ctx.steps
    5. Transitions back to LLM_CALL for synthesis

    Args:
        ctx: Execution context.
        session_manager: Session manager.
        trace_ctx: Trace context.

    Returns:
        Next state (LLM_CALL for synthesis, or FAILED on error).
    """
    timer = ctx.request_timer
    step_start_time = time.time()

    # ADR-0076: Stop button checkpoint — if the user cancelled mid-turn,
    # synthesize from results gathered so far instead of running more tools.
    if ctx.session_id and _is_turn_cancelled(ctx.session_id):
        await _emit_turn_cancelled(session_id=ctx.session_id, trace_id=ctx.trace_id)
        ctx.force_synthesis_from_limit = True
        return TaskState.LLM_CALL

    tool_span_name: str | None = None
    if timer:
        tool_span_name = f"tool_execution:{ctx.tool_iteration_count + 1}"
        timer.start_span(tool_span_name)

    # Loop governance: prevent infinite tool execution cycles
    ctx.tool_iteration_count += 1
    # ADR-0076: push the freshly-incremented tool count to the status bar.
    await _report_turn_progress(ctx)
    _max_iters = _resolve_max_iterations(ctx)
    if ctx.tool_iteration_count > _max_iters:
        log.warning(
            "tool_iteration_limit_reached",
            trace_id=ctx.trace_id,
            iteration=ctx.tool_iteration_count,
            max_iterations=_max_iters,
        )
        # ADR-0076: ask the user whether to continue past the limit or finish
        # now, instead of silently forcing synthesis. Stored preferences and
        # the no-WS fallback are handled inside the helper.
        action_id = await _maybe_pause_for_constraint(
            session_id=ctx.session_id,
            trace_id=ctx.trace_id,
            user_id=ctx.user_id,
            constraint="tool_iteration_limit",
            context=f"Reached {ctx.tool_iteration_count} tool calls on this turn.",
        )
        if action_id == "continue_10":
            ctx.tool_iteration_bonus += 10
            log.info(
                "tool_iteration_limit_extended",
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                iteration=ctx.tool_iteration_count,
                new_max=_resolve_max_iterations(ctx),
            )
            # Fall through to execute the pending tool calls under the raised limit.
        else:
            if timer and tool_span_name:
                timer.end_span(
                    tool_span_name,
                    reason="iteration_limit",
                    iteration=ctx.tool_iteration_count,
                )
            ctx.steps.append(
                {
                    "type": "warning",
                    "description": "Tool loop limit reached; forcing LLM synthesis pass",
                    "metadata": {
                        "iteration": ctx.tool_iteration_count,
                        "max_iterations": _max_iters,
                    },
                }
            )
            # Route back to LLM_CALL with tools disabled so the model synthesizes
            # from all gathered results rather than returning a useless fallback.
            ctx.force_synthesis_from_limit = True
            return TaskState.LLM_CALL

    # Get tool execution layer
    try:
        tool_layer = _get_tool_execution_layer()
    except Exception as e:
        if timer and tool_span_name:
            timer.end_span(tool_span_name, error=str(e), error_type=type(e).__name__)
        raise

    # Extract tool calls from the last assistant message
    if not ctx.messages:
        if timer and tool_span_name:
            timer.end_span(tool_span_name, error="no_messages_for_tool_execution")
        log.error(
            "no_messages_for_tool_execution",
            trace_id=ctx.trace_id,
            error="No messages in context to extract tool calls from",
        )
        ctx.error = ValueError("No messages in context to extract tool calls from")
        return TaskState.FAILED

    last_message = ctx.messages[-1]
    if last_message.get("role") != "assistant":
        if timer and tool_span_name:
            timer.end_span(tool_span_name, error="last_message_not_assistant")
        log.error(
            "last_message_not_assistant",
            trace_id=ctx.trace_id,
            error="Last message is not from assistant",
        )
        ctx.error = ValueError("Last message is not from assistant")
        return TaskState.FAILED

    # Extract tool calls (OpenAI format)
    tool_calls = last_message.get("tool_calls", [])
    if not tool_calls:
        if timer and tool_span_name:
            timer.end_span(tool_span_name, reason="no_tool_calls_in_message")
        log.warning(
            "no_tool_calls_in_message",
            trace_id=ctx.trace_id,
            message="No tool calls found in assistant message, transitioning to synthesis",
        )
        return TaskState.SYNTHESIS

    log.info(
        STEP_EXECUTED,
        trace_id=ctx.trace_id,
        tool_count=len(tool_calls),
    )

    # ── Phase 1: Sequential gate check ────────────────────────────────────────
    # Gate FSM mutations must be sequential so call-count and consecutive-count
    # thresholds are correct before any I/O is dispatched (ADR-0062).
    # Mark the start of a new turn so that within-turn parallel dispatch (e.g.
    # 14 bash calls in one assistant message) does not inflate consecutive_count
    # — only cross-turn repeats of the same tool advance the counter.
    ctx.loop_gate.begin_turn()

    tool_results: list[dict[str, Any]] = []  # blocked + error results (immediate)
    allowed_plans: list[dict[str, Any]] = []  # tool calls cleared for async dispatch
    # ADR-0085 / FRE-475: per-result {tool_name, success, arguments} keyed by
    # tool_call_id — the success bit + path arg the transcript message does not
    # carry, needed by the intra-turn digest pass for read→write pinning.
    digest_sidecar: dict[str, dict[str, Any]] = {}

    for tool_call in tool_calls:
        tool_call_id = tool_call.get("id", "")
        function_info = tool_call.get("function", {})
        tool_name = function_info.get("name", "")
        arguments_str = function_info.get("arguments", "{}")

        if not tool_name:
            log.warning("tool_call_missing_name", trace_id=ctx.trace_id, tool_call_id=tool_call_id)
            continue

        # Parse arguments JSON
        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError as e:
            log.error(
                "tool_call_invalid_arguments",
                trace_id=ctx.trace_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                error=str(e),
            )
            # Concise, neutral error — avoids poisoning the model's confidence
            # in tool use on subsequent turns (ADR-0032 §3.1).
            tool_results.append(
                {
                    "tool_call_id": tool_call_id,
                    "role": "tool",
                    "name": tool_name,
                    "content": json.dumps(
                        {
                            "status": "retry",
                            "hint": f"Arguments for {tool_name} were malformed JSON. Retry with valid JSON.",
                        }
                    ),
                }
            )
            continue

        # Gate pre-check (sequential — FSM state mutations happen here)
        args_hash = stable_hash(arguments)
        loop_policy = _get_tool_loop_policy(tool_name)
        gate_result = ctx.loop_gate.check_before(tool_name, args_hash, loop_policy)
        log.info(
            "tool_loop_gate",
            trace_id=ctx.trace_id,
            decision=gate_result.decision.value,
            tool_name=gate_result.tool_name,
            state_before=gate_result.state_before.value,
            state_after=gate_result.state_after.value,
            reason=gate_result.reason,
            consecutive_count=gate_result.consecutive_count,
            total_calls=gate_result.total_calls,
        )
        if gate_result.decision in (
            GateDecision.BLOCK_IDENTITY,
            GateDecision.BLOCK_OUTPUT,
            GateDecision.BLOCK_CONSECUTIVE,
        ):
            tool_results.append(_gate_blocked_result(tool_call_id, tool_name, gate_result))
            continue

        allowed_plans.append(
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "args_hash": args_hash,
                "loop_policy": loop_policy,
                "gate_result": gate_result,
            }
        )

    # ── Phase 2: Parallel async dispatch ──────────────────────────────────────
    # I/O-bound tool executions (network, ES, Neo4j) run concurrently; the gate
    # FSM has already been updated sequentially in Phase 1.
    _phase2_start = time.time()
    raw_dispatch: list[Any] = []
    if allowed_plans:
        raw_dispatch = list(
            await asyncio.gather(
                *[
                    dispatch_tool_call(
                        tool_call_id=p["tool_call_id"],
                        tool_name=p["tool_name"],
                        arguments=p["arguments"],
                        tool_layer=tool_layer,
                        trace_ctx=trace_ctx,
                        trace_id=ctx.trace_id,
                        session_id=ctx.session_id,
                        loaded_skills=ctx.loaded_skills,
                        args_hash=p["args_hash"],
                        gate_result=p["gate_result"],
                        loop_policy=p["loop_policy"],
                    )
                    for p in allowed_plans
                ],
                return_exceptions=True,
            )
        )

    # ── Phase 3: Sequential record + result assembly ───────────────────────────
    # gate.record_output and ctx mutations are sequential to preserve gate-FSM
    # invariants and ordering guarantees. Results are appended in allowed_plans order.
    _total_serial_ms = 0
    _max_dispatch_ms = 0
    # FRE-402: first dispatched result that declared a non-recoverable failure.
    terminal_failure: dict[str, Any] | None = None
    for i, raw in enumerate(raw_dispatch):
        plan = allowed_plans[i]

        if isinstance(raw, BaseException):
            # Unexpected exception escaped _dispatch_tool_call's internal handler
            log.error(
                "tool_dispatch_unexpected_exception",
                trace_id=ctx.trace_id,
                tool_name=plan["tool_name"],
                error=str(raw),
            )
            tool_results.append(
                {
                    "tool_call_id": plan["tool_call_id"],
                    "role": "tool",
                    "name": plan["tool_name"],
                    "content": json.dumps(
                        {
                            "status": "error",
                            "hint": f"{plan['tool_name']} failed to execute. Try a different approach or tool.",
                        }
                    ),
                }
            )
            continue

        dr: dict[str, Any] = raw

        # FRE-402: capture the first terminal (non-recoverable) tool failure so we
        # can short-circuit after assembly instead of looping back to the model.
        if terminal_failure is None and dr.get("terminal"):
            terminal_failure = dr

        # Gate: record output for output-identity detection (success only)
        if dr["success"] and dr["output_hash"] is not None:
            ctx.loop_gate.record_output(
                dr["tool_name"], dr["args_hash"], dr["output_hash"], dr["loop_policy"]
            )

        content: str = dr["content"]

        # Inject gate advisory hint into content for advisory decisions
        _ADVISORY_DECISIONS = frozenset(
            {GateDecision.WARN_CONSECUTIVE, GateDecision.ADVISE_IDENTITY}
        )
        if dr["gate_result"].decision in _ADVISORY_DECISIONS:
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    if dr["gate_result"].decision == GateDecision.WARN_CONSECUTIVE:
                        parsed["_gate_warning"] = (
                            f"{dr['tool_name']} called {dr['gate_result'].consecutive_count} times "
                            "consecutively. Consider synthesizing from gathered results."
                        )
                    else:  # ADVISE_IDENTITY
                        parsed["_gate_warning"] = (
                            f"{dr['tool_name']} called with the same args "
                            f"{dr['gate_result'].total_calls}x. "
                            "Consider whether the result is stable or use prior output."
                        )
                    content = json.dumps(parsed)
            except (json.JSONDecodeError, TypeError):
                pass

        # Persist in ctx.tool_results and ctx.steps (sequential — shared state)
        ctx.tool_results.append(
            {
                "tool_name": dr["tool_name"],
                "success": dr["success"],
                "output": dr["tool_layer_output"],
                "error": dr["tool_layer_error"],
                "latency_ms": dr["latency_ms"],
            }
        )
        ctx.steps.append(
            {
                "type": "tool_call",
                "description": f"Executed tool: {dr['tool_name']}",
                "metadata": {
                    "tool_name": dr["tool_name"],
                    "tool_call_id": dr["tool_call_id"],
                    "success": dr["success"],
                    "latency_ms": dr["latency_ms"],
                },
            }
        )

        _total_serial_ms += dr["latency_ms"]
        _max_dispatch_ms = max(_max_dispatch_ms, dr["latency_ms"])

        tool_results.append(
            {
                "tool_call_id": dr["tool_call_id"],
                "role": "tool",
                "name": dr["tool_name"],
                "content": content,
            }
        )
        digest_sidecar[dr["tool_call_id"]] = {
            "tool_name": dr["tool_name"],
            "success": dr["success"],
            "arguments": plan["arguments"],
        }

    # Emit parallel-dispatch telemetry for Kibana efficiency tracking
    if allowed_plans:
        _actual_wall_ms = int((time.time() - _phase2_start) * 1000)
        log.info(
            "tools_dispatched_parallel",
            trace_id=ctx.trace_id,
            count=len(allowed_plans),
            blocked_count=len(tool_calls) - len(allowed_plans),
            max_latency_ms=_max_dispatch_ms,
            total_serial_equivalent_ms=_total_serial_ms,
            actual_wall_ms=_actual_wall_ms,
        )

    # ADR-0085 / FRE-475: intra-turn tool-result digest pass — BIRTH-TIME (case-a).
    # Runs on the fresh `tool_results` batch BEFORE the extend below, so the verbatim
    # bytes of a digested result never enter ctx.messages (no cached-prefix
    # invalidation). Flag-off (default) ⇒ skipped entirely ⇒ zero behaviour change.
    if settings.tool_result_compression_enabled:
        from personal_agent.orchestrator.tool_result_digest import (  # noqa: PLC0415
            apply_intra_turn_digest,
        )
        from personal_agent.storage.artifact_store import get_artifact_store  # noqa: PLC0415

        _digest_store = get_artifact_store()
        if _digest_store is not None:
            await apply_intra_turn_digest(
                ctx,
                tool_results,
                digest_sidecar,
                trace_ctx=trace_ctx,
                store=_digest_store,
                bus=None,
            )

    # Append all tool results to messages (digested in place above when enabled).
    ctx.messages.extend(tool_results)

    duration_ms = int((time.time() - step_start_time) * 1000)

    tool_names = [tc.get("function", {}).get("name", "unknown") for tc in tool_calls]
    if timer and tool_span_name:
        timer.end_span(
            tool_span_name,
            tool_count=len(tool_calls),
            tool_names=tool_names,
        )

    log.info(
        "tool_execution_completed",
        trace_id=ctx.trace_id,
        tool_count=len(tool_calls),
        duration_ms=duration_ms,
    )

    # FRE-402: a tool declared a non-recoverable (terminal) failure — short-circuit
    # the reasoning loop instead of routing the error back through the model (which
    # would spend a full primary-model call to produce a "sorry, it failed" reply).
    # Mirrors the step_llm_call failure path: set ctx.error + ctx.classified_error +
    # ctx.final_reply and return FAILED; the shipped execute_task_safe then emits the
    # RUN_ERROR event and surfaces the deterministic reply (FRE-398 machinery).
    if terminal_failure is not None:
        from personal_agent.error_classification import ClassifiedError
        from personal_agent.tools.executor import ToolExecutionError

        classified = ClassifiedError(
            category="tool_failure",
            reason=terminal_failure["terminal_reason"],
            next_step=terminal_failure["terminal_next_step"],
            actions=("retry", "stop"),
        )
        ctx.classified_error = classified
        ctx.final_reply = f"{classified.reason} {classified.next_step}"
        ctx.error = ToolExecutionError(
            terminal_failure.get("tool_layer_error") or "terminal tool failure"
        )
        log.warning(
            "tool_terminal_short_circuit",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            tool_name=terminal_failure["tool_name"],
            error_category="tool_failure",
        )
        return TaskState.FAILED

    # Transition back to LLM_CALL for synthesis using the same model that made the tool call.
    last_llm_role: ModelRole | None = None
    for step in reversed(ctx.steps):
        if step.get("type") == "llm_call":
            role_str = (step.get("metadata") or {}).get("model_role")
            if isinstance(role_str, str):
                last_llm_role = ModelRole.from_str(role_str)
            break
    ctx.selected_model_role = last_llm_role or ModelRole.PRIMARY
    return TaskState.LLM_CALL


async def step_synthesis(
    ctx: ExecutionContext, session_manager: SessionManager, trace_ctx: TraceContext
) -> TaskState:
    """Finalize response.

    This step ensures the final reply is set and completes the task.

    Args:
        ctx: Execution context.
        session_manager: Session manager.
        trace_ctx: Trace context.

    Returns:
        Terminal state (COMPLETED).
    """
    timer = ctx.request_timer
    if timer:
        timer.start_span("synthesis")

    try:
        # Ensure final reply is set (should already be set from LLM call)
        if not ctx.final_reply:
            ctx.final_reply = "Task completed"  # Fallback

        # ADR-0101 §6 / FRE-690: guardrail alterations (downscale/drop) are disclosed
        # in the response, deterministically — never left to the model to relay.
        # FRE-928 AC-3 extends the same rule to a constraint default applied without a
        # user decision: silence is what made the first occurrence invisible.
        from personal_agent.orchestrator.constraint_options import (  # noqa: PLC0415
            get_decision_disclosures,
        )

        all_disclosures = list(ctx.attachment_disclosures) + get_decision_disclosures()
        if all_disclosures:
            disclosure_text = "\n\n".join(f"Note: {d}" for d in all_disclosures)
            ctx.final_reply = f"{ctx.final_reply}\n\n{disclosure_text}"

        # Update session with new messages
        if timer:
            timer.start_span("session_update")
        try:
            session_manager.update_session(ctx.session_id, messages=ctx.messages)
        finally:
            if timer:
                timer.end_span("session_update")
    finally:
        if timer:
            reply = ctx.final_reply or ""
            timer.end_span("synthesis", reply_length=len(reply))

    return TaskState.COMPLETED


async def execute_task_safe(
    ctx: ExecutionContext, session_manager: SessionManager
) -> OrchestratorResult:
    """Wrapper with top-level error handling.

    This is the public entry point that ensures the orchestrator never
    raises exceptions. All errors are captured and returned as part of
    the OrchestratorResult.

    Args:
        ctx: Execution context.
        session_manager: Session manager.

    Returns:
        OrchestratorResult with reply, steps, and trace_id.
    """
    try:
        # Note: MCP initialization moved to CLI startup for singleton pattern
        ctx = await execute_task(ctx, session_manager)

        # Build result
        result: OrchestratorResult = {
            "reply": ctx.final_reply or "Task completed",
            "steps": ctx.steps,
            "trace_id": ctx.trace_id,
        }

        if ctx.error:
            # FRE-398: use pre-classified error when available (set by step_llm_call);
            # fall back to classifying here for errors that bypass that path.
            from personal_agent.error_classification import classify_error

            classified = ctx.classified_error or classify_error(ctx.error)
            log.warning(
                TASK_FAILED,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                error=classified.reason,
                error_type=type(ctx.error).__name__,
                error_category=classified.category,
            )
            if not ctx.final_reply:
                # No partial work was salvaged — surface the classified message.
                result["reply"] = f"{classified.reason} {classified.next_step}"
            # else: result["reply"] already holds the salvaged partial reply (set above).
            result["steps"].append(
                {
                    "type": "error",
                    "description": classified.reason,
                    "metadata": {
                        "error_type": type(ctx.error).__name__,
                        "error_category": classified.category,
                    },
                }
            )
            await _emit_classified_error(ctx, classified)

        # Trigger async context compression if threshold crossed (ADR-0038
        # + ADR-0061 §D1 soft trigger).  Bus threaded through so the
        # within-session compression event lands on
        # ``stream:context.within_session_compressed``.
        # ADR-0081 §D3 Decision 3: under the frozen layout the reactive 0.65 soft
        # trigger is removed — the cache-aware scheduler (step_init) subsumes it;
        # firing reactive compaction here would rewrite history off-schedule and
        # break the forward-extension.
        if ctx.session_id and not settings.cache_frozen_layout_enabled:
            try:
                from personal_agent.events.bus import get_event_bus

                _soft_bus = get_event_bus()
            except Exception:
                _soft_bus = None
            compression_manager.maybe_trigger_compression(
                session_id=ctx.session_id,
                messages=ctx.messages,
                trace_id=ctx.trace_id,
                bus=_soft_bus,
            )

        log.info(
            REPLY_READY,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            reply_length=len(result["reply"]),
        )
        return result

    except Exception as e:
        log.critical(
            ORCHESTRATOR_FATAL_ERROR,
            trace_id=ctx.trace_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        # FRE-398: classify and surface a structured, actionable reply.
        from personal_agent.error_classification import classify_error

        classified = classify_error(e)
        log.error(
            TASK_FAILED,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            error=classified.reason,
            error_type=type(e).__name__,
            error_category=classified.category,
        )
        log.info(
            REPLY_READY,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            reply_length=0,
            fatal_error=True,
        )
        await _emit_classified_error(ctx, classified)
        return {
            "reply": f"{classified.reason} {classified.next_step}",
            "steps": [
                {
                    "type": "error",
                    "description": classified.reason,
                    "metadata": {
                        "error_type": type(e).__name__,
                        "error_category": classified.category,
                    },
                }
            ],
            "trace_id": ctx.trace_id,
        }
