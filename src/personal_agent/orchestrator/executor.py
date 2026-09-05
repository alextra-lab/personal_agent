"""Orchestrator execution loop and state machine.

This module implements the core orchestrator state machine with step functions.
The executor coordinates task execution through explicit state transitions.
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import Token
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4

from opentelemetry.context.context import Context
from opentelemetry.trace import Span

from personal_agent.captains_log.turn_evidence import (
    CandidatePopulation,
    GroundingRecord,
    InlineOutcome,
    MemoryItemKind,
    build_recall_candidates,
    build_turn_evidence,
    derive_evidence_presence,
    mark_truncated,
    memory_item_identity,
)
from personal_agent.config import settings
from personal_agent.config.env_loader import Environment
from personal_agent.grounding.citations import (
    count_near_miss_markers,
    parse_citations,
    strip_citation_markers,
)
from personal_agent.grounding.enforcement import (
    TurnDecision,
    build_no_source_statement,
    build_retry_directive,
    decide,
)
from personal_agent.grounding.enforcement_selection import (
    EnforcementBand,
    EnforcementLevel,
    EnforcementSelection,
    SelectionReason,
)
from personal_agent.grounding.source_registry import SourceRegistry
from personal_agent.grounding.verification import (
    CheckOutcome,
    TurnEvidenceClass,
    TurnVerification,
    apply_entailment,
    build_grounding_record,
    classify_turn_evidence,
    unavailable,
    verify_turn,
)
from personal_agent.llm_client import ModelRole
from personal_agent.llm_client.message_content import (
    MessageContent,
    get_text_content,
    merge_content,
)
from personal_agent.llm_client.models import Placement, ToolCallingStrategy
from personal_agent.observability.topology import observe_topology
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
    ConstraintResolutionRecord,
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
from personal_agent.telemetry.spans import close_step_span, open_step_span
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


GROUNDING_RETRY_TOOL_GRANT = 2
"""Tool iterations reserved for each ADR-0138 D4 forced-retrieval retry (FRE-1282).

Two: one to search, one to fetch what the search found — the shortest path from "you have
no source" to "here is one". More would let a retry spend the turn's remaining wall clock
on a claim the model was never going to source; fewer would not reach a page.
"""


def _resolve_max_iterations(ctx: "ExecutionContext") -> int:
    """Return the effective max-tool-iterations ceiling for this request.

    Uses the per-TaskType limit from settings when the gateway classified a
    task type, falling back to the global orchestrator_max_tool_iterations.
    The global value is the hard upper bound for the *base* ceiling; any
    ``tool_iteration_bonus`` granted by a user "Continue" decision at a
    constraint pause (ADR-0076) is added on top, since the user explicitly
    opted to proceed past the original limit.

    ADR-0138 D4's forced-retrieval retry gets its own grant on the same footing
    (``grounding_retrieval_grant``, FRE-1282). Without it the retry is "forced" in name
    only: a turn that spent its tool budget legitimately would be told to retrieve and
    then have no iteration left to retrieve with, so the bound would be reached without
    retrieval ever having been possible — a refusal caused by our accounting rather than
    by the absence of a source.
    """
    global_max = settings.orchestrator_max_tool_iterations
    base = global_max
    if ctx.gateway_output is not None:
        task_type_val = ctx.gateway_output.intent.task_type.value
        by_type = settings.orchestrator_max_tool_iterations_by_task_type
        if task_type_val in by_type:
            base = min(by_type[task_type_val], global_max)
    resolved = base + ctx.tool_iteration_bonus + ctx.grounding_retrieval_grant
    # ADR-0142 (FRE-1391): stamp the post-grant value actually used, so the route-trace
    # ledger records what ran rather than what was configured (AC-1). Re-stamped on
    # every call — including the reflection-cadence check on a tool-free turn — so the
    # last call before turn end reflects any bonus granted mid-turn.
    ctx.effective_tool_iteration_ceiling = resolved
    return resolved


def _turn_deadline_remaining(ctx: "ExecutionContext") -> float:
    """Seconds left in this turn's work budget (FRE-973, credited per ADR-0142 D4a).

    May be negative once the budget is exhausted. Checked at the LLM-call seam
    in step_llm_call (bounds an in-flight call to whatever remains) and in
    step_tool_execution's iteration-limit gate (skips the interactive
    "continue?" pause once there is no time left to spend asking).

    ``credited_pause_seconds`` is added back so a human's wait on a pause card
    does not itself consume the work budget — otherwise
    ``orchestrator_task_timeout_seconds`` would be spent partly on generation
    and partly on however long the user took to answer a prompt. Bounded
    separately by :func:`_turn_lifetime_remaining`, which this credit can
    never push past.
    """
    return (
        ctx.turn_started_monotonic
        + settings.orchestrator_task_timeout_seconds
        + ctx.credited_pause_seconds
    ) - time.monotonic()


def _turn_lifetime_remaining(ctx: "ExecutionContext") -> float:
    """Seconds left in this turn's absolute lifetime cap (ADR-0142 D4a).

    Unlike :func:`_turn_deadline_remaining`, never extended by anything —
    not a credited pause, not an iteration-limit grant. Bounds the clock;
    ``_turn_deadline_remaining`` bounds the work. A pause already waiting
    when this reaches zero is preempted to its safe default
    (``_maybe_pause_for_constraint``); an in-flight LLM call is bounded by
    the lesser of the two (step_llm_call).
    """
    return (
        ctx.turn_started_monotonic + settings.orchestrator_turn_lifetime_seconds
    ) - time.monotonic()


# ── Constraint governance (ADR-0076 / FRE-389) ─────────────────────────────


def _is_turn_cancelled(session_id: str) -> bool:
    """Return whether the user requested cancellation for this session (ADR-0076)."""
    from personal_agent.transport.agui.ws_endpoint import is_cancel_requested

    return is_cancel_requested(session_id)


def _get_cancel_event(session_id: str) -> asyncio.Event | None:
    """Return the session's cancel event, or None if it has never connected (FRE-1375).

    ``get_cancel_event`` is deliberately non-creating: only a session that has
    connected over WebSocket at least once (a fact that is stable once true — it
    does not flip back on a later disconnect) has any possible source of a
    ``USER_CANCEL``. Racing against a freshly-created Event that can never be set
    would be a no-op in production, but pytest-asyncio's per-test event loops make
    a *reused* ``asyncio.Event`` object from a prior test's closed loop resolve as
    spuriously "done" the instant it's raced again — surfacing as a phantom cancel
    on a completely unrelated test that happens to reuse the same session_id.
    """
    from personal_agent.transport.agui.ws_endpoint import get_cancel_event

    return get_cancel_event(session_id)


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
    """Return the active primary model's context window.

    Resolves the profile-active primary model's ``context_length`` (e.g. 200K
    for cloud Sonnet, 131K for local Qwen) so both the PWA status meter
    (FRE-414) and the in-turn compaction/consent gate (FRE-972) measure
    pressure against the real window instead of the static local budget.
    Falls back to the configured budget when the model config can't be
    resolved. Delegates to the shared resolver (FRE-978) also used by the
    pre-LLM gateway's Stage 7 budget trim.

    Returns:
        The active model's context length, or ``settings.context_window_max_tokens``.
    """
    from personal_agent.config.model_loader import resolve_active_context_length  # noqa: PLC0415

    return resolve_active_context_length("primary", fallback=settings.context_window_max_tokens)


async def _report_turn_progress(ctx: "ExecutionContext") -> None:
    """Report live turn progress to the ADR-0088 spine (FRE-513).

    Publishes a best-effort ``turn.progress`` event carrying the executor-side live fields
    (tool iteration, context-window occupancy) the cost boundary cannot see. The live
    projector relays these onto ``turn_status`` (ADR-0076 sink); topologies never emit
    ``turn_status`` directly (ADR-0088 D4). Live cost is carried separately on
    ``turn.model_call_completed`` from the cost boundary (D3).

    FRE-1326: ``context_tokens`` prefers ``ctx.last_prompt_tokens`` — the real,
    provider-reported input-token count of the latest completed primary model call —
    over the pre-call ``estimate_messages_tokens`` heuristic, which excludes the
    assembled system prompt and so under-counts by an order of magnitude. The estimate
    is used only before the first primary model call of the turn resolves — including
    the reports emitted around HYBRID/DECOMPOSE expansion, whose planner/sub-agent
    calls never touch ``last_prompt_tokens``, so the estimate still applies there until
    the primary's own synthesis call resolves.

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
                context_tokens=ctx.last_prompt_tokens or estimate_messages_tokens(ctx.messages),
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
    ctx: "ExecutionContext | None" = None,
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
        ctx: The turn's execution context, for ADR-0142 pause accounting (FRE-1391)
            and the D4a lifetime cap (FRE-1392) — ``pause_count``,
            ``credited_pause_seconds`` and ``constraint_resolutions`` are updated
            for a genuine pause only, never for a preference bypass (which never
            waits); the pause's own ``timeout_seconds`` is also capped to the
            turn's remaining lifetime, and the turn is stopped early
            (``ctx.turn_stopped_early``) if the cap binds before or during the
            wait. ``None`` skips both — a caller with no ctx in scope.

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
        phase_span,
        register_and_push_constraint,
    )
    from personal_agent.transport.agui.ws_endpoint import WaiterMetadata
    from personal_agent.transport.events import ConstraintPauseEvent, Phase

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

    # ADR-0142 D4a (FRE-1392): a pause is bounded by the lesser of its own
    # timeout and the turn's remaining lifetime — the lifetime cap is never
    # extended, so it must win when it is the tighter bound. If the cap is
    # already reached, don't open a pause with nothing left to spend (same
    # precedent as FRE-973's existing deadline auto-decline).
    _lifetime_remaining = _turn_lifetime_remaining(ctx) if ctx is not None else None
    _effective_timeout = (
        timeout_seconds
        if _lifetime_remaining is None
        else min(timeout_seconds, max(_lifetime_remaining, 0.0))
    )
    if _effective_timeout <= 0:
        log.info(
            "constraint_lifetime_cap_already_exceeded",
            constraint=constraint,
            default_option=default_id,
            trace_id=trace_id,
            session_id=session_id,
        )
        if ctx is not None:
            _stop_turn_for_lifetime_cap(ctx)
        return ConstraintDecision(default_id, "lifetime_cap_exceeded")

    request_id = str(uuid4())
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=_effective_timeout)).isoformat()
    log.info(
        "constraint_pause_emitted",
        constraint=constraint,
        request_id=request_id,
        options=opts,
        trace_id=trace_id,
        session_id=session_id,
    )

    # ADR-0123 §1 (FRE-934): the turn is blocked on the user here, not working —
    # surface an explicit WAITING_FOR_CHOICE phase so the wait is honest and its
    # interval is excludable from the AC-2 silence clock. Only the actual pause is
    # wrapped; a stored-preference bypass returns above and never reaches this.
    _pause_started_monotonic = time.monotonic()
    async with phase_span(session_id=session_id, phase=Phase.WAITING_FOR_CHOICE, detail=constraint):
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
            timeout_seconds=_effective_timeout,
        )
    _pause_duration_seconds = time.monotonic() - _pause_started_monotonic

    action_id = str(payload.get("decision", default_id))
    resolution = str(payload.get("resolution", "user_choice"))
    remember = bool(payload.get("remember", False))

    # Defensive: since FRE-928 the constraint waiter no longer returns connection_lost
    # (a disconnect leaves the waiter pending to ride its timeout), so this path is
    # unreachable via the pause transport. Kept because the resolution Literal still
    # admits it — but it is no longer "the no-WS path", which now times out instead.
    # ADR-0142 (FRE-1391): checked before pause accounting — this branch stands for an
    # immediate no-client default, not a resolved wait, so it must not be credited as
    # one if it is ever reached.
    if resolution == "connection_lost":
        log.info(
            "constraint_no_ws_default_applied",
            constraint=constraint,
            default_option=default_id,
            trace_id=trace_id,
            session_id=session_id,
        )
        return ConstraintDecision(default_id, "connection_lost")

    # ADR-0142 D4a (FRE-1392): detect a lifetime-cap preemption two ways.
    # (a) The logical signal — this wait's timeout was capped below its own
    #     timeout_seconds AND it resolved via that (shortened) timeout, rather
    #     than a genuine answer. (b) A direct clock recheck — the waiter's
    #     timeout and a landing user answer race independently
    #     (ws_endpoint.py), so a decision can arrive as "user_choice"
    #     microseconds after the cap; (a) alone would miss that. The wait was
    #     already bounded to at most _effective_timeout, so (b) only fires at
    #     (or a hair past) the cap.
    if ctx is not None and (
        (_effective_timeout < timeout_seconds and resolution == "timeout_default")
        or _turn_lifetime_remaining(ctx) <= 0
    ):
        _stop_turn_for_lifetime_cap(ctx)

    # ADR-0142 (FRE-1391): a pause happened here, whatever it resolves to — record it
    # before any further early return so accounting cannot be short-circuited. A
    # preference bypass never reaches this line (it returns above), so this is
    # pause-only by construction.
    if ctx is not None:
        # ADR-0142 D4a (FRE-1392): only the first orchestrator_creditable_pause_limit
        # pauses this turn credit their wait back to the work budget — checked
        # before incrementing so the limit is turn-wide (every constraint draws on
        # the same counter), not per-constraint. Pauses beyond it still function
        # and are still offered; their wait is simply not credited.
        if ctx.pause_count < settings.orchestrator_creditable_pause_limit:
            ctx.credited_pause_seconds += _pause_duration_seconds
        ctx.pause_count += 1
        ctx.constraint_resolutions.append(
            ConstraintResolutionRecord(constraint=constraint, action_id=action_id)
        )

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


def _record_undispatched_invocation(
    ctx: ExecutionContext,
    *,
    tool_name: str,
    arguments: dict[str, Any] | str,
    error: str,
) -> None:
    """Record a tool invocation that never reached dispatch (ADR-0124 AC-8, FRE-947).

    Three paths abandon a tool call before it is dispatched — malformed argument
    JSON, a loop-gate block, and an exception escaping the dispatcher. Each already
    appends a hint to the *transcript* so the model can recover, but before FRE-947
    none of them touched ``ctx.tool_results``, which is what
    ``TaskCapture.tool_results`` is built from. A blocked or malformed invocation
    was therefore invisible to the capture, and to everything reading it.

    ADR-0124 AC-8 requires the summariser's prompt to carry **every** tool
    invocation with its name, arguments, status and error. An invocation the
    capture never recorded cannot be recovered later, so it is recorded here.

    The one behavioural consumer of ``ctx.tool_results`` is
    :func:`_fallback_reply_from_tool_results`, which renders these as
    ``- <tool>: failed (<error>)`` — correct under its existing contract, and
    strictly better than the previous silence.

    Args:
        ctx: Execution context whose ``tool_results`` list is appended to.
        tool_name: The tool that was invoked.
        arguments: Parsed arguments, or the raw argument string when parsing is
            what failed.
        error: Why the invocation never ran.
    """
    ctx.tool_results.append(
        {
            "tool_name": tool_name,
            "success": False,
            "output": None,
            "error": error,
            "latency_ms": 0.0,
            "arguments": arguments,
        }
    )


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
    from personal_agent.grounding.entailment import ModelEntailmentJudge
    from personal_agent.mcp.gateway import MCPGatewayAdapter
    from personal_agent.memory.service import MemoryService
    from personal_agent.orchestrator.cache_reset_scheduler import ResetDecision
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


def build_wire_messages(
    messages: list[dict[str, Any]],
    system_prompt: str | None,
    trace_id: str,
) -> list[dict[str, Any]]:
    """Return the provider-neutral message list the LLM clients will dispatch.

    Both clients perform this identical pre-flight before serializing a request —
    prepend the system prompt, then sanitise (``llm_client/client.py`` and
    ``llm_client/litellm_client.py``). It is not cosmetic: ``sanitise_messages`` strips
    ``<tool_code>`` blocks, drops orphaned tool messages, can truncate the history, and
    can append a synthetic continuation. A turn-evidence record built from the
    pre-client pair could therefore describe messages that never reached the provider.

    Provider-specific decoration downstream of this (the Anthropic ``cache_control``
    copy) is additive metadata and never removes content, so this form is the correct
    basis for the evidence record and the record does not vary by provider.

    Args:
        messages: The message list the executor hands to ``respond()``.
        system_prompt: The system prompt for the same call.
        trace_id: Trace identifier, for the sanitiser's logging.

    Returns:
        The wire-form message list, system message included.
    """
    from personal_agent.llm_client.history_sanitiser import sanitise_messages  # noqa: PLC0415

    wire = list(messages)
    if system_prompt:
        wire.insert(0, {"role": "system", "content": system_prompt})
    # emit_telemetry=False: this is an observation of the wire form, not a dispatch.
    # The real call sanitises again inside the client, and ``history_sanitised`` is
    # documented as counting real-world occurrence rates — double-emitting would
    # inflate that series on exactly the turns that reach the admission point.
    return sanitise_messages(wire, trace_id=trace_id, emit_telemetry=False)[0]


def _record_turn_evidence(
    ctx: ExecutionContext,
    *,
    system_prompt: str,
    request_messages: list[dict[str, Any]],
    rendered_memory_ids: tuple[str, ...],
    inline_outcome: InlineOutcome,
    skill_body_names: tuple[str, ...],
    prompt_component_ids: tuple[str, ...] = (),
) -> None:
    """Build and store this turn's evidence record (ADR-0125 D3 items 5 and 6).

    Best-effort: a failure here must never break the turn, but it is logged rather than
    swallowed, because a silently missing record is exactly the ambiguity the evidence
    contract exists to remove — and ``TaskCapture`` will mark it ``not_recorded``.

    Args:
        ctx: Execution context. ``ctx.turn_evidence`` is set on success.
        system_prompt: System prompt for this call.
        request_messages: Message list handed to the client for this call.
        rendered_memory_ids: Identities the memory renderer actually emitted.
        inline_outcome: What the volatile-block inliner did.
        skill_body_names: Names of the skill bodies loaded into the prompt.
        prompt_component_ids: Components spliced into this call, in assembly order.
    """
    try:
        gw_context = ctx.gateway_output.context if ctx.gateway_output is not None else None
        ctx.turn_evidence = build_turn_evidence(
            candidates=ctx.recall_candidates,
            memory_context_present=bool(ctx.memory_context),
            rendered_identities=rendered_memory_ids,
            inline_outcome=inline_outcome,
            wire_messages=build_wire_messages(request_messages, system_prompt, ctx.trace_id),
            system_prompt=system_prompt,
            user_message=ctx.user_message,
            skill_bodies=skill_body_names,
            call_index=0,
            # FRE-1060: read from the assembler, never assumed here. The gateway path
            # reports its producers' discards, so its records name the population; the
            # legacy in-executor recall paths below do not, and their records must keep
            # saying so rather than inheriting a claim they cannot support.
            candidate_population=(
                gw_context.candidate_population
                if gw_context is not None
                else CandidatePopulation.POST_SELECTION
            ),
            # FRE-1150: what the prompt actually carried, and the identity it asserted.
            # Both read from the same values the model was given, never re-derived.
            prompt_component_ids=prompt_component_ids,
            operator_identity=ctx.operator_name or None,
            operator_assertion=ctx.operator_assertion or None,
        )
    except Exception:
        log.exception(
            "turn_evidence_build_failed",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
        )

    # ADR-0138 D2 item 1 (FRE-1280): the memory half of the source registry, resolved
    # from the record just built so admission is decided once for both surfaces.
    _register_admitted_memory_sources(ctx)


def _register_admitted_memory_sources(ctx: ExecutionContext) -> None:
    """Register the memory items this turn actually admitted (ADR-0138 D2 item 1).

    Reads admission from ``ctx.turn_evidence`` — the ADR-0125 record built moments earlier
    at the same admission point — rather than forming a second opinion about what reached
    the model. An item the evidence record says was dropped is not a source the turn can
    cite, and two surfaces disagreeing about that is exactly the ambiguity ADR-0125 exists
    to remove.

    Best-effort: a registry that fails to populate must never break the turn.

    Args:
        ctx: Execution context. Requires ``ctx.source_registry`` and ``ctx.turn_evidence``.
    """
    registry = ctx.source_registry
    if registry is None or ctx.turn_evidence is None or not ctx.memory_context:
        return

    admitted = set(ctx.turn_evidence.assembled_context.memory_identities)
    if not admitted:
        return

    try:
        for item in ctx.memory_context:
            if not isinstance(item, Mapping):
                continue
            _, identity = memory_item_identity(item)
            if identity in admitted:
                registry.register_memory_item(item)
    except Exception:
        log.exception(
            "source_registry_memory_registration_failed",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
        )


def _register_tool_source(
    ctx: ExecutionContext,
    *,
    tool_name: str,
    arguments: Mapping[str, object],
    content: str,
    success: bool,
) -> str | None:
    """Offer one tool result to this turn's source registry (ADR-0138 D2).

    The registry decides admissibility; this only carries the call across. The arguments
    matter as much as the content — D2 admits a tool result only to the extent it is not
    the model's own arguments returning.

    Best-effort, for the same reason as the memory half: an unregistered source costs a
    citation, an exception here would cost the turn.

    Args:
        ctx: Execution context.
        tool_name: The tool that ran.
        arguments: The model's own arguments to the call.
        content: The result as recorded for the model.
        success: Whether the call succeeded.

    Returns:
        The registered source's identifier (ADR-0138 D3(a), FRE-1296), so the caller
        can splice it into the content the model reads. None when nothing was
        admissible or no registry is attached to this turn.
    """
    registry = ctx.source_registry
    if registry is None:
        return None

    # ADR-0138 D4 (FRE-1282): what this turn searched, recorded whether or not the result
    # proved admissible. The terminal no-source statement names it, and a search that
    # returned nothing citable is exactly the kind the user needs to hear about.
    _describe_retrieval(ctx, tool_name, arguments)

    try:
        registration = registry.register_tool_result(
            tool_name=tool_name,
            arguments=arguments,
            content=content,
            success=success,
        )
    except Exception:
        log.exception(
            "source_registry_tool_registration_failed",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            tool_name=tool_name,
        )
        return None

    if registration.source is None:
        log.debug(
            "source_registry_tool_inadmissible",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            tool_name=tool_name,
            admissibility=registration.admissibility.value,
            reason=registration.reason,
        )
        return None

    return registration.source.identifier


def _with_citation_marker(content: str, identifier: str) -> str:
    """Splice a citation marker into tool content the model is about to read.

    JSON object content gets the marker as a top-level field — the same shape the
    ``_gate_warning`` advisory injection already uses a few lines above this
    function's call site — so a structured result stays valid JSON rather than
    gaining trailing prose after its closing brace. Anything else (plain text, a
    JSON array, an unparseable string) gets the marker appended as text.

    Args:
        content: The tool result as the executor recorded it.
        identifier: This result's registered source identifier (ADR-0138 D3(a)).

    Returns:
        ``content`` with the ``[S{n}@{digest}]`` marker spliced in.
    """
    marker = f"[{identifier}]"
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        parsed["_citation"] = marker
        return json.dumps(parsed)
    return f"{content}\n\n{marker}" if content else marker


_MAX_DESCRIBED_RETRIEVAL_CHARS = 120
"""Bound on one recorded retrieval descriptor.

The terminal statement is read by a person, and a model-authored query can be arbitrarily
long. Bounded rather than dropped: naming a truncated search still names the search.
"""


def _describe_retrieval(
    ctx: ExecutionContext, tool_name: str, arguments: Mapping[str, object]
) -> None:
    """Record one retrieval attempt for D4's terminal statement (FRE-1282).

    The descriptor names the tool and its most salient argument. That the model *chose*
    that argument is exactly why D2 refuses it as evidence — but a statement about what
    this turn searched is a claim about the turn record, not about the world, so D1's
    system-record exemption covers it and it is safe to say.

    Args:
        ctx: Execution context.
        tool_name: The tool that ran.
        arguments: The model's arguments to it.
    """
    salient = next(
        (
            str(arguments[key])
            for key in ("query", "url", "path", "question", "search")
            if isinstance(arguments.get(key), str) and str(arguments[key]).strip()
        ),
        "",
    )
    descriptor = f"{tool_name}({salient})" if salient else tool_name
    if len(descriptor) > _MAX_DESCRIBED_RETRIEVAL_CHARS:
        descriptor = f"{descriptor[: _MAX_DESCRIBED_RETRIEVAL_CHARS - 1]}…"
    if descriptor not in ctx.retrieval_attempts:
        ctx.retrieval_attempts.append(descriptor)


def _entailment_judge() -> "ModelEntailmentJudge":
    """Build the D3(d) judge, bound to its own role and its own timeout.

    Returns:
        The judge. The timeout is the latency budget: it is the actual bound on what this
        can add to a turn, since the measurement taken afterwards cannot shorten a call
        that already ran long.
    """
    from personal_agent.grounding.entailment import ModelEntailmentJudge  # noqa: PLC0415
    from personal_agent.llm_client.factory import get_llm_client  # noqa: PLC0415

    return ModelEntailmentJudge(
        get_llm_client(role_name=ModelRole.ENTAILMENT.value),
        timeout_s=settings.grounding_entailment_latency_budget_ms / 1000,
        max_excerpt_chars=settings.grounding_entailment_max_excerpt_chars,
    )


async def _apply_inline_entailment(
    ctx: ExecutionContext, verification: TurnVerification, trace_ctx: TraceContext
) -> TurnVerification:
    """Settle D3(d)'s escalated class, if this turn has one (FRE-1286).

    Args:
        ctx: Execution context, carrying the cumulative check count.
        verification: What the deterministic gates decided.
        trace_ctx: The turn's trace context.

    Returns:
        The verification, unchanged when nothing escalated — which is the common turn, and
        which is why the judge is built lazily rather than per call.
    """
    registry = ctx.source_registry
    if registry is None or not any(
        span.outcome is CheckOutcome.ENTAILMENT_REQUIRED for span in verification.spans
    ):
        # A None registry cannot reach here — ``_verify_grounding`` returns ``unavailable``
        # before verifying — so this narrows rather than handles. Written as a guard rather
        # than an ``assert`` or a type: ignore because the escalated spans would otherwise
        # resolve their sources against nothing, and silently failing them closed is a
        # refusal built on our own bug.
        return verification

    settled = await apply_entailment(
        verification,
        registry,
        _entailment_judge(),
        max_checks=settings.grounding_entailment_max_inline_checks,
        budget_ms=settings.grounding_entailment_latency_budget_ms,
        checks_already_used=ctx.grounding_entailment_checks,
        trace_ctx=trace_ctx,
    )
    ctx.grounding_entailment_checks += settled.entailment_checks
    return settled


def _schedule_offline_entailment(
    ctx: ExecutionContext, verification: TurnVerification, trace_ctx: TraceContext
) -> None:
    """Hand this turn's sampled spans to the offline arm (ADR-0087, FRE-1286).

    Called on the **delivery** branch only. D4's retry branch returns to ``LLM_CALL``
    before this point, so a turn that retried is sampled once, against its final reply,
    rather than once per generation.

    Args:
        ctx: Execution context, for the registry and the answering model.
        verification: What the inline checks decided.
        trace_ctx: The turn's trace context.
    """
    from personal_agent.captains_log.background import run_in_background  # noqa: PLC0415
    from personal_agent.config.model_loader import resolve_role_target  # noqa: PLC0415
    from personal_agent.config.selection import get_current_selection  # noqa: PLC0415
    from personal_agent.grounding.entailment_sampling import (  # noqa: PLC0415
        score_offline_samples,
        select_offline_samples,
    )

    registry = ctx.source_registry
    if registry is None:
        return
    samples = select_offline_samples(verification, rate=settings.grounding_entailment_sample_rate)
    if not samples:
        return

    answering_role = (ctx.selected_model_role or ModelRole.PRIMARY).value
    answering_model, _ = resolve_role_target(
        answering_role, model_key=get_current_selection(answering_role)
    )
    judge_model, _ = resolve_role_target(ModelRole.ENTAILMENT.value)

    run_in_background(
        score_offline_samples(
            samples,
            registry,
            _entailment_judge(),
            answering_model=answering_model,
            judge_model=judge_model,
            max_excerpt_chars=settings.grounding_entailment_max_excerpt_chars,
            trace_ctx=trace_ctx,
        )
    )


async def _verify_grounding(ctx: ExecutionContext, trace_ctx: TraceContext) -> TurnVerification:
    """Run ADR-0138 D3's inline checks over this turn's reply (FRE-1282).

    Span extraction is a model call, so it is the one part of the pass that can fail for
    reasons having nothing to do with the claim — a denied budget reservation, a provider
    error. Those return :func:`~personal_agent.grounding.verification.unavailable` rather
    than a verdict, and D4 delivers such a turn while recording it as unverified. A budget
    denial is a fact about Seshat's accounting; refusing the user's turn over it would
    punish them for our bookkeeping, and passing it silently would hide the malfunction.

    Args:
        ctx: Execution context, carrying the reply and this turn's source registry.
        trace_ctx: The turn's trace context, threaded into the extractor call.

    Returns:
        The verification result, or an unavailable verdict naming what stopped it.
    """
    from personal_agent.grounding.extractor import ModelSpanExtractor  # noqa: PLC0415
    from personal_agent.llm_client.factory import get_llm_client  # noqa: PLC0415

    registry = ctx.source_registry
    if registry is None or not ctx.final_reply:
        return unavailable("no source registry or no reply on this turn")

    try:
        extractor = ModelSpanExtractor(get_llm_client(role_name=ModelRole.SPAN_EXTRACTION.value))
        extraction = await extractor.extract(
            ctx.final_reply, user_message=ctx.user_message, trace_ctx=trace_ctx
        )
    except Exception as exc:
        log.warning(
            "grounding_span_extraction_failed",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return unavailable(f"span extraction failed: {type(exc).__name__}")

    try:
        verification = verify_turn(extraction, parse_citations(ctx.final_reply), registry)
        return await _apply_inline_entailment(ctx, verification, trace_ctx)
    except Exception as exc:
        # The checks themselves parse attacker-influenced content — a fetched page's
        # numeric tokens, its Unicode. A defect there must degrade to "unverified", never
        # to a failed turn: this runs on the turn path for every reply, and the user's
        # answer is not ours to lose over our own bug (security review).
        log.exception(
            "grounding_verification_failed",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            error_type=type(exc).__name__,
        )
        return unavailable(f"verification failed: {type(exc).__name__}")


def _append_heavy_directive(
    request_messages: list[dict[str, Any]], ctx: ExecutionContext
) -> list[dict[str, Any]]:
    """Append heavy enforcement's retrieval directive to one request (ADR-0138 D5).

    **Returns a new list and never touches ``ctx.messages``**, which is the whole point
    of the function existing rather than the append happening at selection time. Two
    defects follow from putting it in ``ctx.messages``, and the first is unbounded:

    - ``ctx.messages`` is persisted at end of turn and reloaded on the next one, while
      heavy applies to *every* turn rather than to a limit being approached. Turn N would
      therefore carry N-1 stale pseudo-user directives — growth linear in session length,
      unlike every other injector in this module (D4's retry, the tool-budget warning,
      forced synthesis), each of which fires only on a condition.
    - Selection runs at the top of ``step_llm_call``, well before
      ``_inline_volatile_with_outcome``, which targets the **last user message**. A
      directive sitting there would capture ADR-0081's volatile tail — recalled memory,
      skill bodies, salient highlights — inverting the rule that the volatile block rides
      the current user turn, closest to the query. ``_append_no_think_to_last_user_message``
      retargets identically. FRE-1137 fixed a sibling of exactly this on attachment turns.

    Called after both of those have run, so the volatile block and the ``/no_think``
    suffix land on the user's real query and the directive follows them.

    Args:
        request_messages: This request's message list.
        ctx: Execution context, for the selected enforcement level.

    Returns:
        The list to send. The input list unchanged when this turn is not heavy.
    """
    enforcement = ctx.grounding_enforcement
    if enforcement is None or enforcement.applied is not EnforcementLevel.HEAVY:
        return request_messages

    from personal_agent.grounding.enforcement_selection import (  # noqa: PLC0415
        build_forced_retrieval_directive,
    )

    return [*request_messages, {"role": "user", "content": build_forced_retrieval_directive()}]


def _resolve_heavy_gate(
    ctx: ExecutionContext,
    *,
    tools: list[dict[str, Any]] | None,
    tool_strategy: "ToolCallingStrategy",
    is_synthesizing: bool,
    model_key: str,
) -> str | None:
    """Return heavy enforcement's ``tool_choice`` pin, or None (ADR-0138 D5, FRE-1285).

    **This is what makes heavy more than advice.** Without a gate the executor receives a
    generation *before* it executes any tool, so a model that ignored the directive would
    compose its assertion with an empty source registry. Pinning ``"required"`` makes the
    first thing the model may emit a tool call rather than prose.

    **What it does not do, stated because the ADR's phrasing invites the stronger read.**
    D5 describes heavy as leaving the model unable to "compose an assertion without a
    source set already in hand". This mechanism does not deliver that, and the claim
    should not be made for it: ``"required"`` forces *a* tool call, not a *retrieval* one,
    and nothing here puts anything into the ``SourceRegistry``. A model can satisfy the
    pin with ``run_python``, which the registry classifies as inadmissible by
    construction. What heavy actually buys is that the turn cannot go straight from the
    prompt to prose — it must take a tool step first, and the directive says what that
    step is for. Correctness still rests where it always did, on D3's inline checks and
    D4's block-and-retry, which are identical at both levels; and the metric direction is
    safe, since a heavy turn is excluded from measurement whether or not the tool it
    called retrieved anything.

    Applied only to the turn's **first** generation. Once the loop is running the model
    has already been through the gate, and re-pinning every pass would forbid the turn
    from ever answering.

    The availability conditions are exactly those under which ``tool_choice`` reaches a
    backend at all — ``client.py`` nulls it when the strategy is not NATIVE. When they
    fail, heavy degrades to directive-only and **says so at WARNING**: a deployment where
    the gate never reaches the model is a silent downgrade to the design this replaced,
    and the log line is what makes it visible.

    Args:
        ctx: Execution context, for the selected level and the pass counters.
        tools: The resolved tool list for this call.
        tool_strategy: The model's tool-calling strategy.
        is_synthesizing: Whether this call is the forced-synthesis pass, which pins its
            own ``tool_choice`` and must not be overridden.
        model_key: The deployment key serving this generation, for telemetry.

    Returns:
        ``"required"`` when the gate applies, otherwise ``None`` — leaving whatever
        ``tool_choice`` the caller had already resolved untouched.
    """
    enforcement = ctx.grounding_enforcement
    if enforcement is None or enforcement.applied is not EnforcementLevel.HEAVY:
        return None
    if ctx.tool_iteration_count != 0 or ctx.grounding_attempts:
        return None

    if tools and tool_strategy == ToolCallingStrategy.NATIVE and not is_synthesizing:
        log.info(
            "grounding_heavy_gate_applied",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            model_key=model_key,
            probation=enforcement.probation,
        )
        return "required"

    log.warning(
        "grounding_heavy_gate_unavailable",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        model_key=model_key,
        tool_strategy=tool_strategy.value,
        has_tools=bool(tools),
        is_synthesizing=is_synthesizing,
        reason=(
            "heavy enforcement degraded to directive-only: tool_choice cannot reach "
            "this model, so retrieval is requested but not gated"
        ),
    )
    return None


async def _select_enforcement(ctx: ExecutionContext) -> None:
    """Choose this turn's enforcement level, before generation (ADR-0138 D5, FRE-1285).

    Runs once per turn and then holds: ``ctx.grounding_enforcement`` is both the result
    and the guard. Placed immediately after the answering deployment key is stamped
    because that is the first moment the model is known and the last moment before the
    turn generates — D5's forcing is *pre*-generation or it is nothing.

    **Heavy is applied here in two parts.** The ``tool_choice`` gate lives at the request
    site (it needs the resolved tool list); this attaches the directive that says what to
    retrieve for, and the iteration grant that means a turn which already spent its tool
    budget still has an iteration to retrieve with — FRE-1282's reasoning, unchanged.

    **Everything fails to heavy.** A missing key, an unreadable window, a misconfigured
    band: all resolve to heavy and log. Unmeasured means heavy is D5's bootstrap, and a
    broken instrument is at most as trustworthy as no instrument.

    One known imprecision, recorded rather than papered over: selection reads the key
    stamped by the *first* pass, while the compliance observation is credited to the key
    stamped by the last (FRE-1284's existing behaviour — the last generation is the one
    whose reply is verified). The two differ only when a turn re-routes mid-flight, which
    today means vision escalation. The selected key is on the log line so the divergence
    is visible rather than silent.

    Args:
        ctx: Execution context.
    """
    if ctx.grounding_enforcement is not None:
        return
    if settings.grounding_verification_mode != "enforce":
        # In `observe` nothing blocks and nothing is forced, so every turn is
        # unconfounded — which is the bootstrap the mode exists to provide. Forcing
        # retrieval in a mode that promises not to change behaviour would be a lie
        # about the mode.
        return

    from personal_agent.grounding.enforcement_selection import (  # noqa: PLC0415
        configured_band,
        initial_state,
    )

    model_key = ctx.answering_model_key
    selection = None
    try:
        band = configured_band()
        if not model_key:
            raise ValueError("no answering model key to select enforcement for")
        selection = await _resolve_enforcement(ctx, model_key, band=band)
    except Exception:
        log.exception(
            "grounding_enforcement_selection_failed",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            model_key=model_key,
        )

    if selection is None:
        # The fail-safe, built here rather than by re-entering the selection that just
        # failed: heavy, standing unchanged on disk, and never persisted — a reading we
        # could not take must not overwrite the one we have.
        selection = EnforcementSelection(
            applied=EnforcementLevel.HEAVY,
            standing=initial_state(),
            reason=SelectionReason.UNMEASURED,
        )

    ctx.grounding_enforcement = selection
    log.info(
        "grounding_enforcement_selected",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        model_key=model_key,
        applied=selection.applied.value,
        standing=selection.standing.level.value,
        reason=selection.reason.value,
        probation=selection.probation,
        changed=selection.changed,
        demoted_at=selection.standing.demoted_at.isoformat()
        if selection.standing.demoted_at
        else None,
    )

    if selection.applied is not EnforcementLevel.HEAVY:
        return

    # The directive itself is NOT attached here — it is appended per request, at the
    # call site, by _append_heavy_directive. See that function for why it must never
    # touch ctx.messages.
    #
    # The grant is the same two iterations D4's retry reserves, for the same reason: one
    # to search, one to fetch what the search found. A turn told to retrieve with nothing
    # left to retrieve with is forced in name only. It belongs here rather than at the
    # call site because it is per-turn state, and this function runs once per turn.
    ctx.grounding_retrieval_grant += GROUNDING_RETRY_TOOL_GRANT


async def _resolve_enforcement(
    ctx: ExecutionContext, model_key: str, *, band: EnforcementBand
) -> EnforcementSelection:
    """Read the window and the standing state, select, and persist any transition.

    The write is **awaited**, unlike the compliance observation write. A transition
    happens on the order of once per hundreds of turns, so the cost is negligible — and a
    lost demotion is the one loss no later turn repairs, because the next turn re-demotes
    with a *later* stamp and hands the model a cooldown it has already partly served.

    **``now`` is taken after the read, not before it.** The repository's guard orders
    concurrent writers by this value, so it has to mean "the state I saw" and not "the
    moment my turn began". Acquiring the pool can block (``pool_size=5``,
    ``max_overflow=10``, ``pool_timeout=30``), and a turn that waited on a connection
    holds a pre-read timestamp older than a turn that read *later* — so it would win the
    guard while carrying the staler view, and a demotion's cooldown stamp would be
    erased by a write that never saw it. Taking the instant after the read collapses
    that window to the gap between reading and stamping.

    Args:
        ctx: Execution context, for telemetry identity on the rejected-write path.
        model_key: The catalog deployment key that will answer.
        band: The pre-registered thresholds.

    Returns:
        The selection.
    """
    from personal_agent.grounding.compliance import classify, configured_window  # noqa: PLC0415
    from personal_agent.grounding.enforcement_selection import (  # noqa: PLC0415
        initial_state,
        select_enforcement,
    )
    from personal_agent.service.database import AsyncSessionLocal  # noqa: PLC0415
    from personal_agent.service.repositories.grounding_compliance_repository import (  # noqa: PLC0415
        GroundingComplianceRepository,
    )
    from personal_agent.service.repositories.grounding_enforcement_repository import (  # noqa: PLC0415
        GroundingEnforcementRepository,
    )

    window = configured_window()
    async with AsyncSessionLocal() as db:
        observations = await GroundingComplianceRepository(db).recent(model_key, limit=window.size)
        stored = await GroundingEnforcementRepository(db).get(model_key)

    # After the read — see the docstring. This instant both ages the window and orders
    # this writer against concurrent ones, so it must date the state actually observed.
    now = datetime.now(timezone.utc)

    reading = classify(model_key, observations, window=window, now=now)
    selection = select_enforcement(
        rate=reading.rate, standing=stored or initial_state(), band=band, now=now
    )

    if selection.changed:
        async with AsyncSessionLocal() as db:
            applied = await GroundingEnforcementRepository(db).upsert(
                model_key, selection.standing, updated_at=now
            )
        if not applied:
            # A concurrent turn wrote a newer transition, so the guard correctly kept
            # theirs. Logged because the alternative is telemetry that lies: this turn is
            # about to report `changed=True` on a transition that is not in the store, and
            # on a demotion that means a cooldown stamp nobody can see went missing.
            log.warning(
                "grounding_enforcement_transition_not_persisted",
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                model_key=model_key,
                level=selection.standing.level.value,
                reason=selection.reason.value,
                demoted_at=selection.standing.demoted_at.isoformat()
                if selection.standing.demoted_at
                else None,
                detail=(
                    "a concurrent turn stored a newer transition; this selection still "
                    "governs the current turn but was not persisted"
                ),
            )
    return selection


def _record_grounding(ctx: ExecutionContext, verification: TurnVerification, mode: str) -> None:
    """Attach and emit the output side of the evidence contract (AC-6).

    The two failure families are counted apart on the record and on the log line, because
    ADR-0138 D3 requires a normalizer limit and an honest no-source outcome never to blur:
    a blended counter would let a wave of false refusals read as the model becoming candid.

    Args:
        ctx: Execution context.
        verification: What the checks decided.
        mode: The verification mode this turn ran under.
    """
    # ADR-0138 D5 (FRE-1285) widens this field, exactly as compliance.py's docstring
    # anticipated. It meant "this generation followed a D4 retry"; it now also covers
    # heavy enforcement's pre-generation forcing. Both are confounded for the same
    # reason — sources were supplied rather than sought — and a heavy turn scored as
    # unforced is how a model that only complies when spoon-fed earns promotion, fails
    # under light, is demoted, recovers under heavy, and oscillates forever.
    #
    # A PROBATION turn reports FALSE and is measured: it ran the light path, which is
    # the whole point of probation. `retrieval_forced` reads the APPLIED level, never
    # the standing one.
    _enforcement = ctx.grounding_enforcement
    record = build_grounding_record(
        verification,
        mode=mode,
        attempts=max(1, ctx.grounding_attempts),
        retrieval_forced=(
            ctx.grounding_attempts > 1
            or (_enforcement is not None and _enforcement.retrieval_forced)
        ),
    )
    ctx.grounding_record = record

    # ADR-0139 D1 (FRE-1332): the compliance metric's denominator. Putting these on this
    # event rather than a second one removes the join `passed_count: 0` used to require —
    # `source_registry_tool_inadmissible` (DEBUG) against this line (INFO) by `trace_id` —
    # a join nobody ran. Gated on `verification.available`: a turn verification did not
    # run on has no span list these fields could trust, and the ADR scopes the whole
    # table to "on every turn where verification ran".
    registry = ctx.source_registry
    tool_results_offered = registry.tool_results_offered if registry is not None else 0
    tool_results_admitted = registry.tool_results_admitted if registry is not None else 0
    turn_evidence_class: TurnEvidenceClass | None = None
    near_miss_markers: dict[str, int] | None = None
    observed_span_outcomes: dict[str, int] | None = None
    invocation_checked_span_outcomes: dict[str, int] | None = None
    if verification.available:
        turn_evidence_class = classify_turn_evidence(
            verification,
            tool_results_offered=tool_results_offered,
            tool_results_admitted=tool_results_admitted,
        )
        near_miss_markers = {"unresolved": count_near_miss_markers(ctx.final_reply or "")}
        # Neither `Entitlement.OBSERVED` nor `RegisteredSource.invocation_check_required`
        # exists yet (ADR-0139 D2/D3, FRE-1334), so no span can qualify for either map.
        # Emitted empty rather than omitted so a consumer built against this ticket does
        # not have to special-case a missing key once D2/D3 populate them by construction.
        observed_span_outcomes = {}
        invocation_checked_span_outcomes = {}

    observation = _record_compliance_observation(ctx, record, turn_evidence_class)
    log.info(
        "grounding_verification_completed",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        mode=mode,
        available=record.available,
        unavailable_reason=record.unavailable_reason,
        non_exempt_spans=record.non_exempt_count,
        passed_spans=record.passed_count,
        unverifiable_by_containment=record.unverifiable_count,
        no_source=record.no_source_count,
        degraded_extraction=record.degraded_extraction,
        entailment_checks=record.entailment_checks,
        entailment_latency_ms=record.entailment_latency_ms,
        entailment_budget_exceeded=record.entailment_budget_exceeded,
        attempts=record.attempts,
        first_generation_compliant=record.first_generation_compliant,
        outcomes=[span.outcome for span in record.spans],
        # ADR-0138 D5 (FRE-1284). Carried on this line rather than a second one so the
        # per-model exclusion rate is derivable from the record every turn already writes.
        # It has to stay derivable: excluding turns verification could not run on is not
        # obviously missing-at-random — a model whose output breaks the extractor may also
        # be a model that cites poorly — and an exclusion nobody can see is one nobody can
        # audit.
        compliance_observation=observation,
        answering_model_key=ctx.answering_model_key,
        # ADR-0139 D1 (FRE-1332).
        turn_evidence_class=turn_evidence_class.value if turn_evidence_class else None,
        tool_results_offered=tool_results_offered,
        tool_results_admitted=tool_results_admitted,
        observed_span_outcomes=observed_span_outcomes,
        invocation_checked_span_outcomes=invocation_checked_span_outcomes,
        near_miss_markers=near_miss_markers,
    )


def _record_compliance_observation(
    ctx: ExecutionContext, record: GroundingRecord, turn_evidence_class: TurnEvidenceClass | None
) -> str:
    """Append this turn to D5's compliance window, when it is an unconfounded observation.

    **Why here and not at capture time.** ``ctx.grounding_record`` is replaced on every D4
    attempt, so a turn that retried has already lost attempt 1's record by the time the
    capture is written. Attempt 1 is the only eligible attempt, so the observation has to
    be taken as it happens — which is what this call site is.

    A failed write is logged at ERROR and never raised into the turn. Not fail-closed: a
    database error is uncorrelated with whether the turn complied, so a dropped observation
    is missing-at-random and cannot bias the rate, while failing a user's turn over our own
    bookkeeping is the reasoning ADR-0138 D4 already rejects for verification itself. A
    *wave* of these is a malfunction, and the ERROR line is what makes it visible.

    Args:
        ctx: Execution context.
        record: This attempt's grounding record.
        turn_evidence_class: ADR-0139 D1's classification of this turn, or ``None`` when
            verification did not run. An ``uncitable`` turn is excluded on the same
            footing as a pre-forced one (AC-5): the system offered nothing to cite from,
            which is not evidence about the model.

    Returns:
        What happened, for the caller's log line: ``recorded``, ``confounded`` (pre-forced
        retrieval, no non-exempt span, uncitable, or verification unavailable), or
        ``unattributable``.
    """
    from personal_agent.captains_log.background import run_in_background  # noqa: PLC0415
    from personal_agent.grounding.compliance import is_unconfounded_observation  # noqa: PLC0415

    if not is_unconfounded_observation(
        record, citable=(turn_evidence_class is TurnEvidenceClass.CITABLE)
    ):
        return "confounded"

    model_key = ctx.answering_model_key
    if not model_key or not ctx.trace_id:
        # No key means no observation, rather than an observation on a guessed one:
        # crediting a compliant turn to a default model buys a promotion nothing earned.
        log.warning(
            "grounding_compliance_observation_unattributable",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            model_key=model_key,
        )
        return "unattributable"

    run_in_background(
        _write_compliance_observation(
            model_key=model_key,
            compliant=record.first_generation_compliant,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
        )
    )
    return "recorded"


async def _write_compliance_observation(
    *, model_key: str, compliant: bool, trace_id: str, session_id: str
) -> None:
    """Write one compliance observation, off the turn's critical path.

    Args:
        model_key: The deployment key that answered.
        compliant: Whether every non-exempt span passed on first generation.
        trace_id: The turn's trace identifier, and the store's idempotency key.
        session_id: For telemetry only.
    """
    from personal_agent.service.database import AsyncSessionLocal  # noqa: PLC0415
    from personal_agent.service.repositories.grounding_compliance_repository import (  # noqa: PLC0415
        GroundingComplianceRepository,
    )

    try:
        async with AsyncSessionLocal() as db:
            inserted = await GroundingComplianceRepository(db).record(
                model_key=model_key,
                compliant=compliant,
                trace_id=trace_id,
                observed_at=datetime.now(timezone.utc),
            )
    except Exception:
        log.exception(
            "grounding_compliance_observation_write_failed",
            trace_id=trace_id,
            session_id=session_id,
            model_key=model_key,
        )
        return

    log.info(
        "grounding_compliance_observation_recorded",
        trace_id=trace_id,
        session_id=session_id,
        model_key=model_key,
        compliant=compliant,
        inserted=inserted,
    )


def _strip_markers_from_turn(ctx: ExecutionContext) -> None:
    """Remove citation markers from everything this turn will hand on (ADR-0138, FRE-1282).

    Markers are protocol, not content: verification has consumed them by the time this
    runs, and every path they survive into is a leak. There are **two**, and closing only
    the obvious one leaves the worse one open:

    - ``ctx.final_reply`` is what the user reads and what ``capture.py`` persists as
      ``assistant_response``.
    - ``ctx.messages`` is the session history, appended by ``step_llm_call`` *before*
      ``final_reply`` is set and persisted by ``step_synthesis``. A marker left here comes
      back on the next turn as conversation context, where — identifiers being turn-scoped
      by construction (D3(a)) — it resolves to nothing and would manufacture a refusal on
      a turn that did nothing wrong.

    Runs in every verification mode, including ``off``: the leak exists because FRE-1283
    instructs the model to emit markers and FRE-1296 gives it real ones to copy, which is
    true whether or not anything verifies them.

    Args:
        ctx: Execution context.
    """
    if ctx.final_reply:
        ctx.final_reply = strip_citation_markers(ctx.final_reply)

    for message in ctx.messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            message["content"] = strip_citation_markers(content)


def _log_source_registry_snapshot(ctx: ExecutionContext) -> None:
    """Emit what this turn could have cited, once, at the end of the turn.

    Identifiers, kinds and labels only — never content, which would put retrieved text and
    any PII it carries into a text-indexed store. This is the surface that makes the
    registry observable before anything consumes it (FRE-1280 AC-1).

    Best-effort like its two siblings, and more load-bearing than either: this runs from
    the turn-scoped ``finally`` in :func:`execute_task`, so an exception escaping here
    would propagate past ``return ctx`` and be caught by ``execute_task_safe``, reporting a
    turn that actually *succeeded* as failed. An unwritten observability line is worth
    nothing next to that.

    Args:
        ctx: Execution context.
    """
    registry = ctx.source_registry
    if registry is None:
        return

    try:
        sources = registry.sources()
        log.info(
            "source_registry_snapshot",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            source_count=len(sources),
            sources=[
                {
                    "identifier": s.identifier,
                    "kind": s.kind.value,
                    "label": s.label,
                    "origin": s.origin,
                }
                for s in sources
            ],
        )
    except Exception:
        log.exception(
            "source_registry_snapshot_failed",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
        )


async def _populate_operator_identity(
    ctx: ExecutionContext, memory_service: "MemoryService | None"
) -> None:
    """Resolve the connected user's identity onto ``ctx`` (FRE-213 / ADR-0052).

    Sets ``ctx.operator_stanza`` and ``ctx.operator_name`` from a single resolution, so
    the prompt and the turn's capture record cannot name the user differently.

    Every path that produces no stanza is logged (FRE-1150). Before this, the guard's
    false branches were silent, so nothing distinguished "never ran" from "ran and
    produced nothing" — which is what let a wrong diagnosis of a *present* stanza stand.
    Severity follows how anomalous each case is: an unidentified request is the supported
    CLI/unauthenticated path and would be per-turn noise as a warning.

    Args:
        ctx: Execution context; both operator fields are set on success.
        memory_service: Connected MemoryService, or None when unavailable.
    """
    if not (ctx.user_id and ctx.user_email):
        log.info(
            "operator_stanza_skipped",
            reason="unidentified_request",
            has_user_id=bool(ctx.user_id),
            has_user_email=bool(ctx.user_email),
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
        )
        return

    if not (memory_service and memory_service.connected):
        log.warning(
            "operator_stanza_skipped",
            reason="memory_service_unavailable",
            memory_service_present=bool(memory_service),
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
        )
        return

    try:
        from personal_agent.orchestrator.prompts import get_owner_identity  # noqa: PLC0415

        identity = await get_owner_identity(
            memory_service=memory_service,
            user_id=ctx.user_id,
            email=ctx.user_email,
            display_name=ctx.user_display_name,
        )
    except Exception as stanza_err:
        log.warning(
            "operator_stanza_failed",
            error=str(stanza_err),
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
        )
        return

    if not identity.name:
        # The call succeeded and yielded nothing — no :Person facts, or a node with no
        # name. Previously indistinguishable from the stanza never being attempted.
        log.warning(
            "operator_stanza_skipped",
            reason="identity_unresolved",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
        )
        return

    ctx.operator_stanza = identity.stanza
    ctx.operator_name = identity.name
    ctx.operator_assertion = identity.assertion


def _inline_volatile_with_outcome(
    messages: list[dict[str, Any]], volatile_block: str
) -> tuple[list[dict[str, Any]], InlineOutcome]:
    """Inline the volatile block and report *what happened* (FRE-1004).

    Same behaviour as :func:`_inline_volatile_into_last_user_message`, which delegates
    here. The outcome is what makes recall admission decidable structurally: the block
    can be rendered and still never reach the model input, and ADR-0125 D3 item 5
    requires that case to be recorded as a drop rather than assumed to be an admission.

    Attachment turns widen ``content`` to block form (a list of typed blocks —
    ADR-0101/0102); the block is prepended as a leading ``{"type": "text", ...}``
    element there rather than skipped, otherwise every image/PDF turn silently drops
    the whole volatile tail — memory, skill bodies, highlights (FRE-1137). The
    string-form branch is byte-identical to before this fix (ADR-0081 §D2).

    Args:
        messages: Working message list. Not mutated.
        volatile_block: Pre-joined volatile content.

    Returns:
        Tuple of (message list, outcome). The list is the input object unchanged
        whenever the outcome is not :attr:`InlineOutcome.INLINED`.
    """
    block = volatile_block.strip() if volatile_block else ""
    if not block:
        return messages, InlineOutcome.EMPTY_BLOCK
    fence = f"{_TURN_CONTEXT_OPEN}\n{block}\n{_TURN_CONTEXT_CLOSE}"
    out = deepcopy(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") != "user":
            continue
        content = out[i].get("content")
        if isinstance(content, str):
            if content.lstrip().startswith(_TURN_CONTEXT_OPEN):
                # Already wrapped this turn — never double-wrap (byte stability).
                return out, InlineOutcome.ALREADY_WRAPPED
            out[i]["content"] = f"{fence}\n\n{content}"
            return out, InlineOutcome.INLINED
        if isinstance(content, list):
            first = content[0] if content else None
            if (
                isinstance(first, dict)
                and first.get("type") == "text"
                and str(first.get("text", "")).lstrip().startswith(_TURN_CONTEXT_OPEN)
            ):
                return out, InlineOutcome.ALREADY_WRAPPED
            out[i]["content"] = [{"type": "text", "text": fence}, *content]
            return out, InlineOutcome.INLINED
        return messages, InlineOutcome.NO_TARGET
    return messages, InlineOutcome.NO_TARGET


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
        A new message list with the block inlined into the last user message
        (as a prepended text block when the content is an attachment turn's
        block list — FRE-1137), or the input list unchanged when there is
        nothing to inline or no user message exists.
    """
    return _inline_volatile_with_outcome(messages, volatile_block)[0]


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

    Writes to ``agent-monitors-cache-reset-cadence-YYYY-MM`` (monthly, FRE-1036) so
    Kibana can aggregate
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
    index_name = f"agent-monitors-cache-reset-cadence-{ts.strftime('%Y-%m')}"
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


def _emit_cache_reset_decision(
    ctx: ExecutionContext,
) -> "tuple[ResetDecision, dict[str, Any], str] | None":
    """Evaluate the cache-reset scheduler and log the decision. Never acts.

    ADR-0081 §D3 makes this per-turn evaluation the observability surface for
    compaction: it is emitted on *every* evaluation, reset or not, so quality-slope
    inertness, ``L*`` and the headroom to the token ceiling stay readable even while
    the scheduler holds. ``accumulated_tokens`` and ``accum_max_tokens`` travel on the
    event so headroom is answerable from one document with no join (FRE-944).

    Split out from :func:`_maybe_frozen_reset` so the live gateway-driven turn path can
    evaluate and log without performing a reset — the emit was structurally unreachable
    there, since the gateway branch of :func:`step_init` returns before the reset call
    site (FRE-944).

    Args:
        ctx: Execution context for the turn.

    Returns:
        ``(decision, scheduler_inputs, backend)``, or ``None`` when there is no session
        to evaluate against.
    """
    if not ctx.session_id:
        return None

    import math  # noqa: PLC0415

    from personal_agent.orchestrator.cache_reset_scheduler import (  # noqa: PLC0415
        marginal_hold_cost,
        should_reset,
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
        # FRE-944: the headroom pair — how much the run has accumulated, and the
        # ceiling it is measured against. Without both, the emit reports "holding"
        # every turn and never says how near the edge we were.
        accumulated_tokens=inputs["accumulated_tokens"],
        accum_max_tokens=inputs["accum_max_tokens"],
    )
    return decision, inputs, backend


async def _maybe_frozen_reset(ctx: ExecutionContext) -> None:
    """Fire a scheduled frozen-prefix reset when the scheduler decides to.

    ADR-0081 §D3: when the run reaches the cost/quality optimum (or the token
    ceiling), compact ``ctx.messages`` into ``[first user][assistant recap][K
    verbatim turns]`` and stash the volatile salient highlights for this turn.

    Args:
        ctx: Execution context (``ctx.messages`` and ``ctx.salient_highlights``
            are updated in place on a reset).
    """
    evaluated = _emit_cache_reset_decision(ctx)
    if evaluated is None:
        return
    decision, inputs, backend = evaluated

    if not decision.should_reset:
        return

    from personal_agent.orchestrator.within_session_compression import (  # noqa: PLC0415
        build_frozen_reset,
    )

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


def _emit_conversation_context_loaded(
    ctx: ExecutionContext,
    *,
    total_messages_in_db: int,
    messages_loaded: int,
    messages_truncated: int,
    estimated_tokens: int,
) -> None:
    """Log the per-turn record of what history ``step_init`` loaded (FRE-945).

    Centralizes the emit's schema so the legacy and gateway-driven call sites cannot drift
    apart on field names — unlike :func:`_emit_cache_reset_decision`, this helper evaluates
    nothing; it only logs the values its callers already computed.

    Args:
        ctx: Execution context for the turn.
        total_messages_in_db: Message count from the persisted session, before this turn's
            message is appended (``0`` when there is no persisted session, e.g. the
            gateway-driven path with no prior session load).
        messages_loaded: ``len(ctx.messages)`` at the point of the call — includes this
            turn's just-appended user message, so it is one more than
            ``total_messages_in_db`` for a persisted session with no truncation.
        messages_truncated: Messages dropped by ``step_init``'s own ``apply_context_window``
            call specifically — not a measure of any trimming performed elsewhere in the
            pipeline (e.g. gateway Stage 7 ``apply_budget``, which runs on a separate copy
            of history before ``step_init`` ever executes). Callers on a path where
            ``apply_context_window`` never runs report this as ``0`` — a real structural
            fact (nothing to truncate), not a placeholder standing in for missing data.
        estimated_tokens: Estimated token count of ``ctx.messages`` at the point of the call.
    """
    log.info(
        "conversation_context_loaded",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        total_messages_in_db=total_messages_in_db,
        messages_loaded=messages_loaded,
        messages_truncated=messages_truncated,
        estimated_tokens=estimated_tokens,
    )


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


def _salvage_partial_reply(
    ctx: ExecutionContext, classified: "ClassifiedError", *, lead: str
) -> "ClassifiedError":
    """Populate ``ctx.final_reply`` from gathered ``tool_results``, if any (FRE-398/FRE-973).

    No-op if ``ctx.tool_results`` is empty (nothing to salvage) or if
    ``ctx.final_reply`` is already set (never overwrite an existing reply —
    idempotent to call more than once for the same failure).

    Args:
        ctx: Execution context whose ``tool_results`` / ``final_reply`` are inspected.
        classified: The error classification for this failure.
        lead: Opening line for the salvaged summary (context-appropriate framing).

    Returns:
        ``classified``, marked ``partial=True`` when a reply was actually salvaged.
    """
    if ctx.tool_results and not ctx.final_reply:
        from personal_agent.error_classification import with_partial

        ctx.final_reply = (
            _fallback_reply_from_tool_results(ctx, lead=lead)
            + f"\n\n---\n_{classified.reason} {classified.next_step}_"
        )
        classified = with_partial(classified)
    return classified


def _stop_turn_for_deadline(ctx: ExecutionContext) -> None:
    """Populate ``ctx.final_reply`` for a graceful turn-deadline stop (FRE-973).

    Called from step_llm_call when the turn's wall-clock budget
    (``settings.orchestrator_task_timeout_seconds``) is exhausted, either
    before a call is attempted or mid-call via ``asyncio.wait_for``. This is a
    deliberate, successful early stop — not an error — so unlike
    :func:`_salvage_partial_reply` it always sets a reply (even with no
    ``tool_results`` gathered yet) and appends a ``warning`` step rather than
    an ``error`` one.
    """
    budget = settings.orchestrator_task_timeout_seconds
    if ctx.tool_results:
        ctx.final_reply = _fallback_reply_from_tool_results(
            ctx,
            lead=(
                f"This turn was stopped early — it exceeded its {budget}s time "
                "budget. Here's what was gathered so far:"
            ),
        )
    else:
        ctx.final_reply = (
            f"This turn was stopped early — it exceeded its {budget}s time budget "
            "before gathering any results."
        )
    ctx.steps.append(
        {
            "type": "warning",
            "description": "Turn wall-clock budget exceeded; stopping early",
            "metadata": {"budget_seconds": budget},
        }
    )
    ctx.turn_stopped_early = True


def _stop_turn_for_lifetime_cap(ctx: ExecutionContext) -> None:
    """Populate ``ctx.final_reply`` for a graceful lifetime-cap stop (ADR-0142 D4a).

    Called when ``settings.orchestrator_turn_lifetime_seconds`` — the absolute,
    unextendable wall-clock cap — is reached, whether between rounds or while a
    constraint pause is in flight (in which case the pause has already resolved
    to its safe default; this only supplies the turn's final reply). Distinct
    from :func:`_stop_turn_for_deadline` so telemetry can tell a lifetime-cap
    stop apart from a work-budget stop — the two consumed different budgets.
    """
    budget = settings.orchestrator_turn_lifetime_seconds
    if ctx.tool_results:
        ctx.final_reply = _fallback_reply_from_tool_results(
            ctx,
            lead=(
                f"This turn was stopped early — it exceeded its {budget}s lifetime "
                "cap. Here's what was gathered so far:"
            ),
        )
    else:
        ctx.final_reply = (
            f"This turn was stopped early — it exceeded its {budget}s lifetime cap "
            "before gathering any results."
        )
    ctx.steps.append(
        {
            "type": "warning",
            "description": "Turn lifetime cap exceeded; stopping early",
            "metadata": {"budget_seconds": budget},
        }
    )
    ctx.turn_stopped_early = True


def _stop_turn_for_cancel(ctx: ExecutionContext) -> None:
    """Populate ``ctx.final_reply`` for a user-initiated Stop (ADR-0076 / FRE-1375).

    Called from :func:`step_llm_call` when the cancel event fires while a primary
    call is in flight, and from :func:`step_tool_execution`'s between-rounds
    checkpoint. Deliberately never routes back through another ``LLM_CALL`` — AC-3
    requires that pressing Stop cannot itself schedule more model work, which is
    exactly what the old ``force_synthesis_from_limit`` / ``TaskState.LLM_CALL``
    path this replaces used to do.
    """
    if ctx.tool_results:
        ctx.final_reply = _fallback_reply_from_tool_results(
            ctx,
            lead="Stopped — here's what was gathered before the stop:",
        )
    else:
        ctx.final_reply = "Stopped before gathering any results."
    ctx.steps.append(
        {
            "type": "warning",
            "description": "Turn stopped by user request",
            "metadata": {"reason": "user_cancel"},
        }
    )
    ctx.turn_stopped_early = True


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
        ctx=ctx,
    )
    # ADR-0142 D4a (FRE-1392): the lifetime cap can bind while this pause is in
    # flight — stop immediately rather than logging/persisting pending-confirmation
    # state for a turn that is already ending.
    if ctx.turn_stopped_early:
        return False
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


_MAX_RENDERED_ENTITIES = 15
"""Rank cap on described entities (pre-existing, FRE-374 D1).

Bounds broad recall's ``limit=20`` (``request_gateway/context.py``). Retained
deliberately by FRE-1010 as an explicit non-goal: it was not the reported mechanism
(the observed turn had five candidates) and its drop is already recordable.
"""

_MAX_RENDERED_EPISODES = 5
"""Cardinality bound on rendered episodes (FRE-1010).

Restates, at render time, the bound every episode producer already enforces
upstream — gateway proactive ``proactive_memory_max_injected_items=5``, gateway
entity-match ``limit=5``, legacy entity-match ``limit=5``. It therefore cannot bite
on any current path; it exists so a future producer cannot quietly unbound the
volatile tail on the legacy path, which has no token budget.
"""

_MAX_RENDERED_STANCES = _MAX_RENDERED_ENTITIES
"""Rank cap on rendered stances (ADR-0126 T1).

Deliberately the *same* constant as _MAX_RENDERED_ENTITIES, not an independent value: a
stance is only ever fetched for an entity the recall path already selected, so its
rendered prefix must never exceed what the entity prefix already bounds — an independent
cap value could select a stance subset misaligned with which entities actually render,
which would make this an unstated second relevance decision (the one thing ADR-0126
forbids).
"""

_MAX_RENDERED_BEHAVIOURAL_STANCES = 12
"""Cap fixed by ADR-0126 AC-7 (T2): the curated behavioural set holds at most 12 stances.

Unlike _MAX_RENDERED_STANCES (T1), this is not inherited from another producer's bound
-- there is no recall selection this layer rides on (D2: always-present, not gated on
entity recall). It restates AC-7's own ceiling directly, so a curated-set edit that
quietly grew past 12 stays bounded by construction here even before a test assertion
catches it. Raising it requires amending ADR-0126.
"""

_MAX_ITEM_CHARS = 1000
"""Per-item character bound on rendered memory content (FRE-1010).

The volatile tail sits *outside* the prompt cache by construction (ADR-0081 §D2 —
every cache breakpoint precedes it), so each character here is a fresh input token
on **every** turn, never amortised. The legacy recall path has no token budget, and
its episode ``summary`` is a stored digest with no inherent length bound, so the
bound is applied here by construction rather than shipped-and-watched — ADR-0078's
cache-erosion instrument cannot detect the problem (FRE-1008: the static-prefix and
dynamic prompt hashes are computed from the same input).

Set **above** the largest upstream value so it never double-truncates: gateway
entity-match writes ``mark_truncated(..., 800)`` (``request_gateway/context.py``),
i.e. up to 800 chars plus a ~26-char marker. Also ≈ ADR-0124's ~250-token digest
target. Truncation that does bite is marked, never silent (ADR-0125 D5).
"""


_DEICTIC_USER_RE = re.compile(r"\bthe user\b", re.IGNORECASE)
"""Matches a stored description that refers to "the user" (FRE-1150).

Such a description is **deictic**: it only means something relative to the conversation
it was extracted from, but it is stored globally and rendered to every reader. The
incident entity — ``Susan: The user's stated name in the conversation.`` — is this class,
and read by a different user it asserts their name is Susan. Measured 2026-08-05: 202 of
6,220 described entities, of which only 26 retain the turn provenance a backfill would
need, so 176 cannot be repaired in the data and must be handled at render.
"""

_DEICTIC_DISAMBIGUATION = (
    ' (in this note "the user" means whoever that earlier conversation was with,'
    " not necessarily the person you are assisting now)"
)
"""Clarifier appended to a deictic description, at the point of the conflict.

Deliberately narrow. Applied to every entity line, it would tax the 96.8% of
descriptions that are not deictic — measured on the 27B, a blanket tag costs ~122s per
turn against ~68s without it — for no benefit, since a non-deictic description makes no
claim about the reader. It also does not remove or reorder the item: the claim stays
admitted, rendered and in position, which the ticket requires (cross-user scoping is
FRE-674's, not this ticket's).
"""


def _entity_line(item: dict[str, Any], identifier: str | None = None) -> str:
    """Render one described entity.

    A description that says "the user" is disambiguated in place (FRE-1150), so a fact
    about somebody else's conversation cannot read as a statement about the connected
    user. Applied after truncation so the clarifier can never itself be truncated away.

    The mention count is read as ``mention_count`` first and legacy ``mentions``
    second: three of the four producers write ``mention_count``
    (``memory/proactive.py``, ``request_gateway/context.py`` ×2) and only
    ``_format_broad_recall`` writes ``mentions``, so reading one key alone rendered a
    fabricated "(mentioned 1x)" for every gateway-sourced entity. When neither key is
    present the clause is omitted rather than defaulted — an absent count is absent,
    not one.

    Args:
        item: The entity item to render.
        identifier: This entity's citation identifier (ADR-0138 D3(a), FRE-1296), or
            None to render without one — the entity was never registered as a source.
    """
    description = mark_truncated((item.get("description") or "").strip(), _MAX_ITEM_CHARS)
    if _DEICTIC_USER_RE.search(description):
        description += _DEICTIC_DISAMBIGUATION
    line = f"- [{item.get('entity_type', '')}] {item.get('name', '')}: {description}"
    count = item.get("mention_count", item.get("mentions"))
    if count is not None:
        line += f" (mentioned {count}x)"
    if identifier:
        line += f" [{identifier}]"
    return line


def _episode_text(item: dict[str, Any]) -> str:
    """The renderable text of one episode, bounded and marked.

    ``or`` rather than ``dict.get`` defaults deliberately: a payload carrying
    ``summary=None`` must fall back to the user message, not render the string
    ``"None"``. Each candidate is stripped *before* the fallback, so a
    whitespace-only summary also falls through — otherwise ``" "`` is truthy and
    would suppress a perfectly good ``user_message``, losing content on a path whose
    whole purpose is not losing it.
    """
    summary = (item.get("summary") or "").strip()
    raw = summary or (item.get("user_message") or "").strip()
    return mark_truncated(raw, _MAX_ITEM_CHARS)


def _stance_line(item: dict[str, Any], identifier: str | None = None) -> str:
    """Render one current stance -- topic-scoped (ADR-0126 T1) or curated behavioural (T2).

    Both item shapes carry only ``target``/``affect``, so one renderer serves both
    producers. Mastery is not rendered: D1 decides affect alone is sufficient, and
    mastery is correctly null on every live topic-scoped stance (a pure
    preference/intention, not a stated skill level) -- the curated behavioural set is
    likewise preference-only.

    Args:
        item: The stance item to render.
        identifier: This stance's citation identifier (ADR-0138 D3(a), FRE-1296), or
            None to render without one — the stance was never registered as a source.
    """
    target = item.get("target", "")
    affect = mark_truncated((item.get("affect") or "").strip(), _MAX_ITEM_CHARS)
    line = f"- {target}: {affect}"
    if identifier:
        line += f" [{identifier}]"
    return line


def _render_memory_section_with_ids(
    items: list[dict[str, Any]],
    registry: "SourceRegistry | None" = None,
) -> tuple[str, tuple[str, ...]]:
    """Render recalled memory for the volatile tail, dispatching **per item kind**.

    Replaces a branch selected by the *first* item's type (FRE-1010). That selection
    was unsound by construction: a mixed set was rendered wholesale by whichever
    renderer its top-scoring item happened to pick, so on a set led by an episode
    every entity fell through the conversation renderer — which reads ``summary`` /
    ``user_message``, keys an entity payload does not carry — and rendered as a bare
    numbered bullet. Observed live on trace 94b70cd9.

    An item that yields no content contributes **no line and no id**. That is what
    keeps "admitted" honest in the evidence record (ADR-0125 D3 item 5): drops stay
    recordable as candidates − rendered, rather than being reported as admitted while
    contributing nothing.

    Session items are deliberately not rendered — an explicit FRE-1010 non-goal. Two
    incompatible session shapes exist and the legacy one (``_format_broad_recall``)
    carries no summary field at all, so rendering would fix one producer and leave the
    other silently dropped.

    Args:
        items: Memory-context items of any kind, in upstream relevance order.
        registry: This turn's source registry (ADR-0138 D2, FRE-1296). When given,
            every item that renders a line is also registered as a source, and its
            identifier is appended to that line so the model has something to copy
            (D1's citation binding). None renders every line without an identifier —
            the pre-FRE-1296 behaviour, still exercised where no registry is attached
            (e.g. sub-agent paths, ``ExecutionContext.source_registry`` defaults to
            None).

    Returns:
        Tuple of (section string, identities that actually rendered content). Both
        empty when nothing renders.
    """
    entities: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    stance_items: list[dict[str, Any]] = []
    behavioural_items: list[dict[str, Any]] = []
    for item in items:
        kind, _ = memory_item_identity(item)
        if kind is MemoryItemKind.ENTITY:
            entities.append(item)
        elif kind is MemoryItemKind.EPISODE:
            episodes.append(item)
        elif kind is MemoryItemKind.STANCE:
            stance_items.append(item)
        elif kind is MemoryItemKind.BEHAVIOURAL_STANCE:
            behavioural_items.append(item)

    # Filter for content BEFORE applying the bound, so the bound caps what is actually
    # rendered rather than what was merely considered. Cap-then-filter would let blank
    # items consume slots and silently exclude a later item that does have content —
    # the exact "recalled then discarded" failure this ticket exists to fix, and it
    # would undermine the bounds' own purpose (bounding the volatile tail's cost, which
    # only rendered content contributes to).
    described = [m for m in entities if (m.get("description") or "").strip()][
        :_MAX_RENDERED_ENTITIES
    ]
    recalled = [m for m in episodes if _episode_text(m)][:_MAX_RENDERED_EPISODES]
    # D6 (ADR-0126): an empty or whitespace-only affect is filtered before render, never
    # rendered blank — this is the same filter-then-cap shape as `described`/`recalled`
    # above, so a blank stance can never burn a rendered slot ahead of a real one.
    stances = [m for m in stance_items if (m.get("affect") or "").strip()][:_MAX_RENDERED_STANCES]
    # ADR-0126 T2: same filter-then-cap shape, own bound (AC-7's 12, not the entity cap).
    behavioural = [m for m in behavioural_items if (m.get("affect") or "").strip()][
        :_MAX_RENDERED_BEHAVIOURAL_STANCES
    ]

    sections: list[str] = []
    rendered_ids: list[str] = []

    def _identifier_for(item: dict[str, Any]) -> str | None:
        """This item's citation identifier, registering it as a source if needed."""
        return registry.register_memory_item(item).identifier if registry is not None else None

    if behavioural:
        rendered_ids.extend(memory_item_identity(m)[1] for m in behavioural)
        section = "\n\n## Standing Behavioural Preferences\n"
        section += "\n".join(_stance_line(m, _identifier_for(m)) for m in behavioural)
        sections.append(section)

    if described:
        rendered_ids.extend(memory_item_identity(m)[1] for m in described)
        section = "\n\n## Your Memory Graph — Known Entities\n"
        section += "\n".join(_entity_line(m, _identifier_for(m)) for m in described)
        # FRE-1150: the previous wording — "use this list to directly answer questions
        # about what the user has previously discussed" — licensed exactly the failure
        # that shipped: an entity describing a *third party* ("Susan: the user's stated
        # name in the conversation") was used to answer who the connected user is. The
        # list's subject is what was discussed, never who is being spoken to; the
        # authenticated identity in the cached head is the only source for that.
        section += (
            "\n\nThese are entities drawn from earlier conversations, including other "
            "people; they record what was discussed, not who you are speaking with. "
            "Use them to answer questions about what the user has previously discussed."
        )
        sections.append(section)

    if recalled:
        rendered_ids.extend(memory_item_identity(m)[1] for m in recalled)
        section = "\n\n## Relevant Past Conversations\n"
        section += "The following past conversations may be relevant to the current request:\n\n"
        for index, item in enumerate(recalled, 1):
            identifier = _identifier_for(item)
            section += f"{index}. {_episode_text(item)}"
            section += f" [{identifier}]\n" if identifier else "\n"
            if item.get("key_entities"):
                section += f"   Entities: {', '.join(item['key_entities'][:5])}\n"
        section += (
            "\nYou can reference these past conversations to provide more context-aware responses."
        )
        sections.append(section)

    if stances:
        rendered_ids.extend(memory_item_identity(m)[1] for m in stances)
        section = "\n\n## What The User Thinks About Related Topics\n"
        section += "\n".join(_stance_line(m, _identifier_for(m)) for m in stances)
        sections.append(section)

    return "".join(sections), tuple(rendered_ids)


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

    # ADR-0138 D2/D3(a) (FRE-1280): one registry per turn, created before any retrieval
    # so every source this turn gathers has somewhere to land. The user's own words are
    # D2's fourth admissible kind and are available right here, at turn start.
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = get_default_registry()
    ctx.source_registry = SourceRegistry(turn_id=ctx.trace_id, tool_registry=_tool_registry)
    ctx.source_registry.register_user_message(ctx.user_message)

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
    # ADR-0129 D3 (FRE-1067): the step span. Opened on entry into LLM_CALL and
    # stays open across the following TOOL_EXECUTION call, if any — model-call
    # and tool-call spans created inside those two step functions parent onto
    # it automatically (attaching it makes it the "current" OTel span). Closed
    # by the try/finally below on every exit that does not continue into
    # TOOL_EXECUTION, and backstopped by the turn-scoped finally further down
    # in case an exception escapes this loop entirely.
    current_step_span: Span | None = None
    current_step_span_token: Token[Context] | None = None
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

                if state == TaskState.LLM_CALL and current_step_span is None:
                    current_step_span, current_step_span_token = open_step_span(
                        iteration=ctx.tool_iteration_count
                    )
                    # A fresh step starts with zero tools dispatched — without this,
                    # a step_llm_call failure that never reaches step_tool_execution
                    # would close this step's span carrying a stale count left over
                    # from a previous round's successful dispatch.
                    ctx.last_tool_execution_count = 0

                # Execute step function
                try:
                    state = await step_func(ctx, session_manager, trace_ctx)
                finally:
                    if current_step_span is not None and state != TaskState.TOOL_EXECUTION:
                        assert current_step_span_token is not None  # set together with the span
                        close_step_span(
                            current_step_span,
                            current_step_span_token,
                            tool_count=ctx.last_tool_execution_count,
                        )
                        current_step_span, current_step_span_token = None, None

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
                    cap_llm_calls = 0
                    for step in ctx.steps:
                        if step.get("type") == "tool_call":
                            tool_name = (step.get("metadata") or {}).get("tool_name")
                            if tool_name:
                                tools_used.append(tool_name)
                        elif step.get("type") == "llm_call":
                            meta = step.get("metadata") or {}
                            cap_llm_calls += 1
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
                        # ADR-0125 D3 (FRE-1004). The turn evidence is stamped with the
                        # turn's real primary-call count here, at the only point it is
                        # known; the record itself still describes call 0 alone.
                        recall_admission=(ctx.turn_evidence.recall if ctx.turn_evidence else None),
                        assembled_context=(
                            ctx.turn_evidence.assembled_context.model_copy(
                                update={"primary_call_count": cap_llm_calls}
                            )
                            if ctx.turn_evidence
                            else None
                        ),
                        grounding=ctx.grounding_record,
                        evidence_presence=derive_evidence_presence(
                            user_message=ctx.user_message,
                            assistant_response=ctx.final_reply,
                            tool_results=ctx.tool_results,
                            llm_call_count=cap_llm_calls,
                            turn_evidence=ctx.turn_evidence,
                            trace_id=ctx.trace_id,
                            session_id=ctx.session_id,
                            user_id=ctx.user_id,
                        ),
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

            # FRE-973: an exception raised outside step_llm_call's own try (e.g. in
            # step_tool_execution, or the state-dispatch loop itself) used to reach
            # this handler and silently discard ctx.tool_results — this is the
            # confirmed gap: only step_llm_call's local except previously salvaged
            # gathered work. Close it here too so no exit path drops it.
            from personal_agent.error_classification import classify_error

            classified = ctx.classified_error or classify_error(e)
            classified = _salvage_partial_reply(
                ctx,
                classified,
                lead="The turn failed before I could finish, but here's what I gathered:",
            )
            ctx.classified_error = classified

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
            # ADR-0129 D3 (FRE-1067): backstop — guarantee no step span survives
            # the turn even if an exception escaped the per-iteration try/finally
            # above (e.g. raised while `state == TaskState.TOOL_EXECUTION`, which
            # that finally deliberately leaves open so the exception still
            # propagates to the outer handler above this block).
            if current_step_span is not None:
                assert current_step_span_token is not None  # set together with the span
                close_step_span(current_step_span, current_step_span_token, tool_count=0)

            # ADR-0122 §4 (FRE-930): drop the turn-scoped artifact-builder carrier so
            # no resolution outlives this turn into a later async context (AC-10c).
            reset_artifact_builder_resolution(_builder_carrier_token)
            reset_decision_disclosures(_disclosure_carrier_token)

            # ADR-0138 (FRE-1280): what this turn could have cited. In the `finally` so a
            # failed turn is observable too — a turn that retrieved nothing before it
            # broke is exactly the case worth being able to read back.
            _log_source_registry_snapshot(ctx)

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
        ctx=ctx,
    )
    # ADR-0142 D4a (FRE-1392): the lifetime cap can bind while this pause is in
    # flight — skip resolving a deployment/budget for a turn that is already
    # ending, and let step_init's own check route straight to synthesis.
    if ctx.turn_stopped_early:
        return
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
    # Load session and build message history
    session_message_count = 0
    session = session_manager.get_session(ctx.session_id)
    if session:
        ctx.messages = list(session.messages)
        session_message_count = len(ctx.messages)

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
    # ADR-0142 D4a (FRE-1392): the lifetime cap can bind during the pause above —
    # stop here rather than entering the gateway-driven/legacy routing below.
    if ctx.turn_stopped_early:
        return TaskState.SYNTHESIS

    # --- Gateway-driven path: skip inline routing and memory ---
    if ctx.gateway_output is not None:
        gw = ctx.gateway_output
        # FRE-944: evaluate the ADR-0081 §D3 cache-reset scheduler and emit its decision.
        # Every branch below this point ends in a return, so the reset call site further
        # down the function is unreachable on gateway-driven turns — which is all of them
        # (157/157 observed over 30 days) — leaving compaction entirely unobservable.
        # Placed at the TOP of the branch, not before one of the returns, so that every
        # gateway sub-path emits exactly once: the enforced-expansion path returns early
        # (mid-branch) and would otherwise stay silent, reproducing this very bug on a
        # subset of turns. Here ctx.messages already holds the loaded session history plus
        # this turn's user message, so the reading is the turn's real accumulation, taken
        # at the same point on every gateway sub-path.
        #
        # Note the two call sites do NOT measure the same thing: this one reads UNTRIMMED
        # history, whereas the legacy call site below runs after apply_context_window has
        # truncated. Consumers must not compare gateway- and legacy-sourced
        # accumulated_tokens as like-for-like. Untrimmed is the right reading here — the
        # §D3 accumulation ceiling (cache_frozen_accum_max_ratio, 0.50) exists to schedule
        # a reset BEFORE apply_context_window's 0.85 hard truncation backstop engages, so
        # measuring growth post-truncation would hide exactly the pressure it watches for.
        #
        # Deliberately evaluate-and-log ONLY: no reset is performed and ctx.messages is not
        # touched, keeping this to visibility with no compaction behaviour change. Whether
        # the reset itself should run on this path is the separate, larger review that this
        # emit exists to give ground truth to.
        _emit_cache_reset_decision(ctx)
        # FRE-945: the sibling conversation_context_loaded emit was dark for the identical
        # reason — its call site (below apply_context_window/_maybe_frozen_reset) sits below
        # this same branch's return. Placed here, at the top, for the same reason as the
        # call above: it covers every gateway sub-path, including the enforced-expansion
        # path that returns early.
        #
        # messages_truncated=0 here is a real structural fact, not a placeholder: this
        # function's own apply_context_window call (the only thing that truncates within
        # step_init) sits below the branch return and never runs on this path. It reports
        # only step_init's own truncation action — it says nothing about gateway Stage 7
        # (request_gateway/budget.py::apply_budget), which trims its own separate copy of
        # history (gw.context.messages) before step_init ever executes. Read at branch
        # entry, this describes ctx.messages as loaded-session-history-plus-this-turn's
        # message — not the final list some sub-paths (e.g. enforced expansion) go on to
        # append a synthesis message to afterward.
        #
        # estimated_tokens reads the same untrimmed ctx.messages, at the same point, as
        # _emit_cache_reset_decision's accumulated_tokens above — the two are expected to
        # read identically on gateway turns; see that call's comment for the untrimmed-vs-
        # trimmed measurement-point caveat, which applies here unchanged.
        _emit_conversation_context_loaded(
            ctx,
            total_messages_in_db=session_message_count,
            messages_loaded=len(ctx.messages),
            messages_truncated=0,
            estimated_tokens=estimate_messages_tokens(ctx.messages),
        )
        # Use pre-assembled memory context.
        # FRE-1004: candidates are taken unconditionally — when Stage 7 dropped the
        # memory context to fit budget, ``memory_context`` is None but the candidates
        # are exactly what has to be recorded as dropped.
        ctx.recall_candidates = gw.context.recall_candidates
        if gw.context.memory_context:
            ctx.memory_context = gw.context.memory_context
            log.info(
                "memory_enrichment_completed",
                trace_id=ctx.trace_id,
                conversations_found=len(gw.context.memory_context),
            )
        # Populate operator identity in gateway path (was only wired for legacy path).
        # This is the live site: 186 executions over 30 days, versus zero for the legacy
        # one below (FRE-1150 investigation).
        try:
            from personal_agent.service.app import memory_service as _ms

            await _populate_operator_identity(ctx, _ms)
        # Broad by intent: the import of service.app pulls the whole app module graph, and
        # step_init must not fail a turn because the operator stanza could not be built.
        # The helper already handles its own failures; this covers the import itself.
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
                        item.get("summary")
                        or item.get("description")
                        # A stance item carries no summary/description/name -- only
                        # target and affect -- so the affect alone would be an
                        # orphaned preference string with no attribution (ADR-0126 T1,
                        # T2: behavioural_stance carries the same shape).
                        or (
                            f"{item['target']}: {item['affect']}"
                            if item.get("type") in ("stance", "behavioural_stance")
                            else None
                        )
                        or item.get("name", "")
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

            from personal_agent.llm_client.factory import get_llm_client
            from personal_agent.orchestrator.expansion_controller import (
                ExpansionController,
            )

            # FRE-958: the DISPATCH client (every run_sub_agent call) must be
            # built for role=SUB_AGENT (ADR-0033 client isolation), never PRIMARY.
            # sub_agent may be bound to a different placement (e.g. a cloud
            # provider) than primary; building for the wrong role dials the wrong
            # client/endpoint for every sub-agent call.
            llm_client = get_llm_client(role_name=ModelRole.SUB_AGENT.value)
            # FRE-1390: the PLANNER client is a SEPARATE, explicitly-built
            # client for role=PRIMARY. Decomposition is a reasoning judgement
            # about work that has not happened yet, and SUB_AGENT binds to a
            # deployment with thinking hard-disabled (config/model_roles.yaml).
            # A single shared client cannot serve both roles: LiteLLMClient's
            # dispatched deployment is fixed at construction, not by the
            # ``role`` kwarg passed to ``.respond()`` (that kwarg is a
            # telemetry label only) — so passing role=PRIMARY into a
            # SUB_AGENT-built client would keep silently dispatching to the
            # SUB_AGENT deployment. This must be a second client, not a
            # request-time override.
            planner_llm_client = get_llm_client(role_name=ModelRole.PRIMARY.value)
            controller = ExpansionController()
            # ADR-0088 D4: report progress at dispatch start so tool/context fields are
            # live during the (potentially multi-minute) expansion window. Cost itself
            # climbs from turn.model_call_completed events, not a per-loop accumulator.
            await _report_turn_progress(ctx)
            expansion_result = await controller.execute(
                query=get_text_content(ctx.messages[-1].get("content", "")) if ctx.messages else "",
                strategy=gw.decomposition.strategy.value.upper(),
                llm_client=llm_client,
                planner_llm_client=planner_llm_client,
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
        return TaskState.LLM_CALL

    # Apply context window controls before LLM usage to prevent overflow.
    input_messages_count = len(ctx.messages)
    estimated_tokens = 0
    # ADR-0081 §D3 Decision 4: under the frozen append-only layout the transient
    # re-derivation (re-inserting a popped summary at a fixed index every turn)
    # is a cache-buster, so it is gone — compaction is the scheduled reset below
    # and apply_context_window keeps only its pure truncation role. (The legacy
    # pre-ADR-0081 compression_manager summary path was retired with the
    # cache_frozen_layout_enabled flag — FRE-941.)
    ctx.messages = apply_context_window(
        ctx.messages,
        # FRE-972: the session's selected-model window, not the static
        # local-Qwen budget — a larger-window cloud primary must not be
        # truncated as if it only had settings.context_window_max_tokens.
        max_tokens=_resolve_context_max(),
        strategy=settings.conversation_context_strategy,
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        compressed_summary=None,
    )

    # ADR-0081 §D3: cache-aware compaction scheduler. When the run reaches the
    # cost/quality optimum (or the token ceiling), compact to a frozen reset
    # that re-establishes a reusable prefix; otherwise hold (history stays a
    # strict forward extension). No-op when the flag is off.
    await _maybe_frozen_reset(ctx)

    estimated_tokens = estimate_messages_tokens(ctx.messages)

    _emit_conversation_context_loaded(
        ctx,
        total_messages_in_db=session_message_count,
        messages_loaded=len(ctx.messages),
        messages_truncated=max(0, input_messages_count - len(ctx.messages)),
        estimated_tokens=estimated_tokens,
    )

    # Query memory graph for relevant context (Phase 2.2)
    if settings.enable_memory_graph:
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
                        # ADR-0126 T1 (FRE-1015): a second, independent entity producer
                        # outside request_gateway/context.py -- push each recalled
                        # entity's current stance in before the candidate record below
                        # is built, so it reflects the enriched list. Fail-closed: a
                        # stance-layer fault omits enrichment, never fails the turn.
                        if ctx.memory_context and ctx.authenticated:
                            from personal_agent.request_gateway.context import (
                                _entity_names_from_memory_context,
                                _stance_context_items,
                            )

                            entity_names = _entity_names_from_memory_context(ctx.memory_context)
                            if entity_names:
                                try:
                                    stances = await memory_service.query_current_stances(
                                        entity_names,
                                        authenticated=ctx.authenticated,
                                        trace_id=ctx.trace_id,
                                    )
                                    ctx.memory_context.extend(
                                        _stance_context_items(entity_names, stances)
                                    )
                                except Exception as stance_err:
                                    log.warning(
                                        "stance_enrichment_failed",
                                        trace_id=ctx.trace_id,
                                        error=str(stance_err),
                                    )
                        # FRE-1004: legacy path — no gateway candidates to inherit,
                        # so the recalled set is its own candidate set.
                        ctx.recall_candidates = build_recall_candidates(ctx.memory_context, {})
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
                        # ADR-0126 T2 (FRE-1017, codex plan-review MAJOR finding): this
                        # sub-branch previously had no local guard, unlike the
                        # broad-recall sub-branch above -- an exception here jumped
                        # straight past the behavioural-injection hook below to the
                        # outer except at the bottom of this function, which would
                        # make T2's "always-present" guarantee silently disappear
                        # whenever entity-match recall failed for an unrelated reason.
                        try:
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
                                    "summary": conv.summary
                                    or mark_truncated(conv.user_message, 400),
                                    "key_entities": conv.key_entities,
                                }
                                for conv in result.conversations
                            ]
                            # FRE-1004: legacy path — see the broad-recall branch above.
                            ctx.recall_candidates = build_recall_candidates(ctx.memory_context, {})
                            conversations_found = len(ctx.memory_context)
                            log.info(
                                "memory_enrichment_completed",
                                trace_id=ctx.trace_id,
                                conversations_found=conversations_found,
                            )
                        except Exception as entity_match_err:
                            log.warning(
                                "memory_entity_match_query_failed",
                                trace_id=ctx.trace_id,
                                error=str(entity_match_err),
                            )

                # ADR-0126 T2 (FRE-1017): standing behavioural stances, independent of
                # what either recall sub-branch above selected -- present whenever
                # memory_service is connected, not gated on entity recall (D2).
                if ctx.authenticated:
                    from personal_agent.request_gateway.context import (
                        CURATED_BEHAVIOURAL_STANCE_TARGETS,
                        _behavioural_stance_context_items,
                    )

                    try:
                        behavioural_stances = await memory_service.query_current_stances(
                            list(CURATED_BEHAVIOURAL_STANCE_TARGETS),
                            authenticated=ctx.authenticated,
                            trace_id=ctx.trace_id,
                        )
                        behavioural_items = _behavioural_stance_context_items(behavioural_stances)
                        if behavioural_items:
                            if ctx.memory_context is None:
                                ctx.memory_context = []
                            ctx.memory_context.extend(behavioural_items)
                            ctx.recall_candidates = build_recall_candidates(ctx.memory_context, {})
                    except Exception as behavioural_err:
                        log.warning(
                            "behavioural_stance_injection_failed",
                            trace_id=ctx.trace_id,
                            error=str(behavioural_err),
                        )

                # Populate operator identity (FRE-213 / ADR-0052) while service is
                # connected. Unreachable in production — every live entrypoint passes a
                # gateway_output, so the branch above returns first (FRE-1150).
                await _populate_operator_identity(ctx, memory_service)

                if memory_service != global_memory_service:
                    await memory_service.disconnect()

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
            else:
                # Memory graph enabled but service not connected and not a recall-only path.
                pass
        except Exception as e:
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
    # ADR-0061 — within-session hard trigger.  Fires synchronously when the
    # working messages list crosses the hard threshold (default 0.85 of the
    # context window).  Layers above Stage 7 (which runs at request entry);
    # this catches in-flight overflow caused by large tool responses.
    from personal_agent.orchestrator.within_session_compression import (
        compress_in_place,
        needs_hard_compression,
    )

    # FRE-972: measure against the session's selected-model window (the same
    # resolver the turn-status meter uses), not the static local-Qwen budget —
    # else a larger-window cloud primary hits this gate and its consent popup
    # far below its real window.
    _effective_max_tokens = _resolve_context_max()
    if ctx.session_id and needs_hard_compression(ctx.messages, _effective_max_tokens):
        # ADR-0076: ask before silently summarising history. "Stop here"
        # produces a final answer from current context; "Compress and continue"
        # (the default) runs the existing within-session compression.
        _max_tokens = _effective_max_tokens
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
            ctx=ctx,
        )
        # ADR-0142 D4a (FRE-1392): the lifetime cap can bind while this pause is
        # in flight — stop straight to synthesis rather than compressing or
        # continuing into another LLM call for a turn that is already ending.
        if ctx.turn_stopped_early:
            return TaskState.SYNTHESIS
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
                max_tokens=_effective_max_tokens,
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

    # ADR-0138 D1/D2/D6 (FRE-1283): the grounding contract applies to any world-fact
    # claim, not only to tool-using turns, so it seeds system_prompt unconditionally
    # rather than living behind the "if tools" branch below with the tool-use rules.
    from personal_agent.orchestrator.prompts import (
        GROUNDING_CONTRACT_PROMPT,
        render_current_datetime_block,
    )

    system_prompt: str | None = GROUNDING_CONTRACT_PROMPT

    # Inject deployment context so the model doesn't try to access host-only paths.
    # Tool-name hints are appended later, only when tools are actually being passed
    # — otherwise the model sees named tools it can't call and hallucinates pseudo-code.
    if settings.environment == Environment.PRODUCTION:
        _deployment_context = (
            "## Deployment Context\n"
            "You are running inside a Docker container on a cloud VPS.\n"
            "- App code is at `/app` — the host's repo mount point is NOT accessible from here\n"
            "- Configuration is injected as environment variables at startup; there is no `.env` file inside the container\n"
            "- Do NOT search for files at host filesystem paths (the host's repo checkout or home directory) — they do not exist inside the container\n"
            # FRE-1165: embeddings/reranker deliberately absent — that substrate is the
            # managed profile now (an external endpoint, not a Docker DNS name), and the
            # local containers they'd have named are stopped. Naming either wrongly gave
            # the model a dialable-looking address for something it can't reach.
            "- All backend services are reachable via Docker internal DNS:\n"
            "    postgres:5432  |  neo4j:7687 (bolt) / neo4j:7474 (HTTP)  |  elasticsearch:9200\n"
            "    redis:6379"
        )
        system_prompt = (
            f"{system_prompt}\n\n{_deployment_context}" if system_prompt else _deployment_context
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
    tool_awareness = ""

    # ADR-0081 D4: the volatile skill-bodies block (selected bodies +
    # <skill_usage_directives>). Assembled in the skill-routing block below but
    # appended to the VOLATILE tail (after the static-prefix capture), alongside
    # memory_section. Declared here so it survives into the try block regardless
    # of whether the prefer_primitives_enabled path runs.
    _skill_bodies_tail = ""
    # FRE-1004: same reason — the turn evidence record reads the loaded body names at
    # the admission point, which is reached whether or not the skill path ran.
    _skill_body_names: tuple[str, ...] = ()

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
        get_skill_bodies,
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

                _routing_client = get_llm_client_for_key(
                    settings.skill_routing_model_key, budget_role="skill_routing"
                )
                _relevant = await route_skills(
                    user_message=_user_message,
                    routing_client=_routing_client,
                    cap_tokens=settings.skill_index_max_tokens,
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                )
                # Pre-load returned skill bodies — primary agent sees them already in scope
                from personal_agent.orchestrator.skills import get_all_skills  # noqa: PLC0415

                _all = get_all_skills()
                for _name in _relevant:
                    if _name in _all:
                        ctx.loaded_skills.add(_name)

                log.info(
                    "skill_routing_call_completed",
                    routing_model_key=settings.skill_routing_model_key,
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
        # FRE-1004: _skill_body_names is declared above this block — D3 item 6 asks
        # *which* bodies were loaded, and the admission point reads it whether or not
        # this path runs.

        _all_skills = get_all_skills()

        if _routing_mode == "model_decided":
            # Index (stable) + bodies of any pre-loaded (router-selected) skills.
            _skill_index_text = assemble_skill_index(cap_tokens=settings.skill_index_max_tokens)
            _preloaded_bodies: list[str] = []
            _preloaded_names: list[str] = []
            if ctx.loaded_skills:
                for _name in sorted(ctx.loaded_skills):
                    _doc = _all_skills.get(_name)
                    if _doc and _doc.body:
                        _preloaded_bodies.append(_doc.body)
                        _preloaded_names.append(_name)
            _skill_bodies_text = "\n\n".join(p for p in _preloaded_bodies if p)
            _skill_body_names = tuple(_preloaded_names)
        elif _routing_mode == "hybrid":
            _skill_index_text = assemble_skill_index(cap_tokens=settings.skill_index_max_tokens)
            _skill_bodies_text, _skill_body_names = get_skill_bodies(
                message=_user_message,
                loaded_skills=ctx.loaded_skills,
            )
        else:  # keyword (default / legacy) — bodies only, no index
            _skill_bodies_text, _skill_body_names = get_skill_bodies(message=_user_message)

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
        # Create LLM client — LiteLLMClient for every placement (ADR-0141 D1)
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
            respond_role = model_role
        else:
            from personal_agent.cost_gate import budget_role_for
            from personal_agent.llm_client.factory import get_llm_client_for_key
            from personal_agent.llm_client.litellm_client import LiteLLMClient

            llm_client = get_llm_client_for_key(
                effective_model_key, budget_role=budget_role_for(ModelRole.VISION.value)
            )
            # FRE-1037: relabel the call's telemetry role to VISION only when
            # it's provably safe — LiteLLMClient's model is fixed at
            # construction, so `role` is label-only there. A client that
            # re-resolves its deployment from role.value internally would risk
            # a second, divergent resolution if `vision` is ever rebound to a
            # local deployment (ADR-0141 D1 unified all placements onto
            # LiteLLMClient, but the guard stays — defensive against any
            # future client shape, not this specific historical split).
            respond_role = ModelRole.VISION if isinstance(llm_client, LiteLLMClient) else model_role

        # ADR-0138 D5 (FRE-1284): stamp the deployment key that actually serves this
        # generation, so the compliance metric attributes the turn to the model that made
        # the claims rather than to the role that nominally owns them. Assigned on every
        # pass, so a tool loop's last generation — the one whose reply is verified — is
        # the one recorded.
        ctx.answering_model_key = effective_model_key

        # ADR-0138 D5 (FRE-1285): choose light or heavy from the model's MEASURED
        # compliance, before this turn generates anything. Once per turn — the call
        # no-ops on every later pass — because the level describes how the turn was
        # generated, not how its most recent pass would have been.
        await _select_enforcement(ctx)

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

        # ADR-0138 D4 (FRE-1282): a forced-retrieval retry must be able to retrieve.
        # This clears any synthesis-forcing left over from the blocked generation — a
        # retry told "do NOT call any more tools" is forced in name only — and its
        # iteration grant was reserved when the retry was ordered.
        if ctx.grounding_retry_pending:
            ctx.grounding_retry_pending = False
            ctx.force_synthesis_from_limit = False
            log.info(
                "grounding_forced_retrieval_retry",
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                attempt=ctx.grounding_attempts,
                tool_iterations_remaining=_resolve_max_iterations(ctx) - ctx.tool_iteration_count,
            )

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

        # ADR-0138 D5 (FRE-1285): heavy enforcement's actual gate. Never overrides the
        # forced-synthesis pin above — _resolve_heavy_gate declines while synthesizing.
        _heavy_pin = _resolve_heavy_gate(
            ctx,
            tools=tools,
            tool_strategy=tool_strategy,
            is_synthesizing=is_synthesizing,
            model_key=effective_model_key,
        )
        if _heavy_pin is not None:
            tool_choice = _heavy_pin

        # ADR-0081 D1: Volatility-gradient layout — build memory_section locally
        # without injecting it yet; it will be appended last as the VOLATILE tail.
        # This ensures the KV-cache boundary sits between the stable prefix and
        # the per-turn dynamic content, fixing the cross-turn reuse ≈ 0 issue.
        memory_section: str | None = None
        # FRE-1004: the identities this render actually emitted, so a dropped candidate
        # stays distinguishable from a used one in the evidence record (ADR-0125 D3
        # item 5). FRE-1010: one renderer for every kind — the previous pair of branches
        # was selected by the FIRST item's type, so a mixed set was rendered wholesale
        # by whichever renderer its top item picked, and entities fell through the
        # conversation renderer as empty bullets.
        _rendered_memory_ids: tuple[str, ...] = ()
        if ctx.memory_context:
            _section_text, _rendered_memory_ids = _render_memory_section_with_ids(
                ctx.memory_context, ctx.source_registry
            )
            memory_section = _section_text or None
            if memory_section is None:
                _rendered_memory_ids = ()

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

        # Capture the cacheable prefix AFTER all STATIC/SEMI-STATIC assembly
        # (incl. the stable skill index) and BEFORE the VOLATILE tail — this is
        # what the static_prefix_hash covers (ADR-0081 D1 + D4).
        inner_system_before_memory = system_prompt or ""

        # FRE-1298: current date/time, rendered from the timestamp captured once at
        # request ingress (ctx.turn_started_at) so every model call in this turn
        # — this assembly reruns per tool-loop iteration — renders the identical
        # value. VOLATILE tail only; never spliced above this capture point.
        _current_datetime_block = render_current_datetime_block(ctx.turn_started_at)

        # ADR-0081 §D2 (FRE-434): frozen append-only layout — the sole layout since
        # the cache_frozen_layout_enabled A/B flag was retired (FRE-941; frozen won
        # decisively, quality flat). Per-turn volatile (selected skill bodies +
        # usage-directives + recalled memory + D3 salient highlights) rides the
        # CURRENT user turn, not the system head. message[0] stays exactly
        # inner_system_before_memory, so the wire prefix is byte-stable and prior
        # turns replay as a strict forward extension — the property local KV reuse
        # requires.
        #
        # The block is inlined into ctx.messages in place so the history persisted
        # at end of turn (update_session) equals the wire form sent now. _inline_…
        # is a no-op when the block is empty or the last message is not a user turn
        # (e.g. post-tool synthesis, where the current user query — already inlined
        # on the tool-request call — still carries the volatile earlier in the
        # sequence).
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
                _current_datetime_block,
            )
            if p
        )
        ctx.messages, _inline_outcome = _inline_volatile_with_outcome(ctx.messages, _volatile_block)

        # Call the unified client's respond()
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

        # ADR-0138 D5 (FRE-1285): heavy's retrieval directive, per request and never
        # persisted. Placed after the volatile inline and the /no_think suffix above so
        # both still land on the user's real query rather than on the directive.
        request_messages = _append_heavy_directive(request_messages, ctx)

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
        from personal_agent.llm_client.concurrency import InferencePriority
        from personal_agent.llm_client.prompt_identity import derive_orchestrator_prompt_identity

        # Build the orchestrator.primary PromptIdentity (ADR-0078 D1/D4, FRE-405).
        # After ADR-0081 D1, inner_system_before_memory IS the full cacheable prefix:
        # tool rules (STATIC) → tool awareness (SEMI-STATIC) → base system body → decomposition.
        # The volatile memory tail is appended after this capture point.
        _static_prefix = inner_system_before_memory
        _component_ids: list[str] = []
        # ADR-0138 (FRE-1283): unconditional, unlike every other entry below — recorded
        # anyway so the audit trail names every component actually spliced in, not only
        # the conditional ones.
        _component_ids.append("grounding_contract")
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
        if memory_section:
            # FRE-1150: keyed on what was actually spliced, not on ctx.memory_context
            # being non-empty. A turn whose candidates all rendered blank set the flag
            # while contributing no bytes — harmless while this list was log-only, but
            # this diff promotes it to the capture's audit surface, where a component
            # named but not present is the same class of false negative the ticket exists
            # to close. Safe: component_ids feeds neither hash in derive_prompt_identity.
            _component_ids.append("memory_section")
        if ctx.salient_highlights:
            # FRE-1008: contributes bytes to _volatile_block (line ~4679) same as the
            # other VOLATILE markers above; was missing from the audit trail entirely.
            _component_ids.append("salient_highlights")
        if ctx.artifact_builder_planning_note:
            # ADR-0122 §5/T6: distinct VOLATILE marker — turn-scoped, never enters
            # static_prefix_hash.
            _component_ids.append("artifact_builder_planning_note")
        # FRE-1298: unconditional, like grounding_contract — the current-date/time
        # block is always injected, every turn.
        _component_ids.append("current_datetime")
        if tool_awareness:
            _component_ids.append("tool_use_rules")
        # FRE-1008: dynamic_hash must cover what is actually sent — request_messages
        # (which carries the ADR-0081 volatile tail inlined into the current user
        # turn) and tools — not a precomputed candidate string, since the inline step
        # can no-op and diverge from it. Same request_messages/tools values passed to
        # llm_client.respond() below.
        _prompt_identity = derive_orchestrator_prompt_identity(
            static_prefix=_static_prefix,
            request_messages=request_messages,
            tools=tools,
            component_ids=tuple(_component_ids),
        )

        # ── The admission point (ADR-0125 D3 items 5 and 6, FRE-1004) ────────────
        # Recorded once per turn, on the first primary call — the one that serializes
        # context assembly's output after all trimming and compaction. Later calls in
        # the tool/hybrid loop are continuations, not fresh assemblies, so recording
        # them would produce a record describing a different model call than the one
        # the recall admission belongs to.
        if ctx.tool_iteration_count == 0 and ctx.turn_evidence is None:
            _record_turn_evidence(
                ctx,
                system_prompt=system_prompt or "",
                request_messages=request_messages,
                rendered_memory_ids=_rendered_memory_ids,
                inline_outcome=_inline_outcome,
                skill_body_names=_skill_body_names,
                prompt_component_ids=tuple(_component_ids),
            )

        # FRE-973: bound this call to the turn's remaining wall-clock budget so a
        # slow primary generation cannot run past the SLM/tunnel read-timeout with
        # no partial output salvaged (incident: a 1013s turn hard-failed on a
        # Cloudflare 524 on a single 251s call, with an iteration-count-only gate
        # that never tripped). If the budget is already gone, don't even attempt
        # the call — stop and salvage what's gathered so far.
        # ADR-0142 D4a (FRE-1392): also bound this call to the absolute lifetime
        # cap — never extended by a credited pause, unlike _deadline_remaining —
        # so the tighter of the two always wins.
        _deadline_remaining = _turn_deadline_remaining(ctx)
        _lifetime_remaining = _turn_lifetime_remaining(ctx)
        _remaining = min(_deadline_remaining, _lifetime_remaining)
        if _remaining <= 0:
            if _lifetime_remaining <= _deadline_remaining:
                log.warning(
                    "turn_lifetime_cap_exhausted",
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                    span_id=span_id,
                    budget_seconds=settings.orchestrator_turn_lifetime_seconds,
                )
                _stop_turn_for_lifetime_cap(ctx)
            else:
                log.warning(
                    "turn_wall_clock_budget_exhausted",
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                    span_id=span_id,
                    budget_seconds=settings.orchestrator_task_timeout_seconds,
                )
                _stop_turn_for_deadline(ctx)
            # ADR-0074 §I3: pair the STEP_PLANNING_STARTED emitted above this try
            # with a completion, matching the success/error exits below.
            log.info(
                STEP_PLANNING_COMPLETED,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                span_id=span_id,
                parent_span_id=trace_ctx.parent_span_id,
                model_role=model_role.value,
                channel=ctx.channel.value,
                status="deadline_exceeded",
                next_state="synthesis",
            )
            return TaskState.SYNTHESIS

        # ADR-0123 §2 (FRE-934): bracket the primary inference as a transport
        # phase so the client can name the wait instead of showing silence. The
        # first round (no tool has run yet) is the PLANNING inference; a post-tool
        # round is the "final synthesis" inference. Tight scope — the span closes
        # when respond() returns/raises, before tool processing or the expansion
        # hook — so it never overlaps the EXPANSION parent phase.
        from personal_agent.transport.agui.transport import phase_span  # noqa: PLC0415
        from personal_agent.transport.events import Phase  # noqa: PLC0415

        _inference_phase = Phase.PLANNING if ctx.tool_iteration_count == 0 else Phase.SYNTHESIS
        # FRE-937 (master PR #758 bounce, ADR-0123 owner comment 2026-07-28): a
        # multi-round tool loop re-enters SYNTHESIS once per pass, and every pass
        # was emitted with no `detail` — the client rendered the same generic
        # label N times with no way to tell them apart. `tool_iteration_count` is
        # already incremented per round by the time this fires (line ~4940 in the
        # prior pass), so it is a cheap, always-available round distinguisher.
        _inference_detail = (
            f"round {ctx.tool_iteration_count}" if _inference_phase is Phase.SYNTHESIS else None
        )
        try:
            async with phase_span(
                session_id=ctx.session_id, phase=_inference_phase, detail=_inference_detail
            ):
                _respond_coro = llm_client.respond(
                    role=respond_role,
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
                _cancel_event = _get_cancel_event(ctx.session_id) if ctx.session_id else None
                if _cancel_event is None:
                    response = await asyncio.wait_for(_respond_coro, timeout=_remaining)
                else:
                    # ADR-0076 / FRE-1375: race the call against the user's cancel
                    # event too, so Stop aborts an in-flight generation instead of
                    # only being read between tool rounds (where a turn almost
                    # never is). Two real tasks via asyncio.wait rather than a
                    # watcher cancelling the inner future out-of-band from
                    # asyncio.wait_for's own timeout — mixing two independent
                    # cancellation sources on one future is fragile across
                    # asyncio's cancel/uncancel bookkeeping (codex plan-review).
                    _respond_task = asyncio.ensure_future(_respond_coro)
                    _cancel_wait_task = asyncio.ensure_future(_cancel_event.wait())
                    _race_tasks = (_respond_task, _cancel_wait_task)
                    try:
                        done, _pending = await asyncio.wait(
                            _race_tasks,
                            timeout=_remaining,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        # Unconditional: runs even if this await is itself
                        # cancelled from outside (a turn-level cancellation), so
                        # _respond_task is never orphaned still generating and
                        # holding its concurrency slot (codex plan-review).
                        for _t in _race_tasks:
                            if not _t.done():
                                _t.cancel()
                        await asyncio.gather(*_race_tasks, return_exceptions=True)

                    # Cancel checked first: if both complete in the same
                    # asyncio.wait() call, Stop must win — a response landing in
                    # the same instant as a cancel must never be delivered (AC-3).
                    if _cancel_wait_task in done:
                        log.info(
                            "user_cancel_mid_generation",
                            trace_id=ctx.trace_id,
                            session_id=ctx.session_id,
                            span_id=span_id,
                        )
                        await _emit_turn_cancelled(session_id=ctx.session_id, trace_id=ctx.trace_id)
                        _stop_turn_for_cancel(ctx)
                        log.info(
                            STEP_PLANNING_COMPLETED,
                            trace_id=ctx.trace_id,
                            session_id=ctx.session_id,
                            span_id=span_id,
                            parent_span_id=trace_ctx.parent_span_id,
                            model_role=model_role.value,
                            channel=ctx.channel.value,
                            status="user_cancelled",
                            next_state="synthesis",
                        )
                        return TaskState.SYNTHESIS
                    if _respond_task in done:
                        response = _respond_task.result()
                    else:
                        raise TimeoutError
        except TimeoutError:
            # ADR-0142 D4a (FRE-1392): the same tighter-bound reasoning as the
            # pre-call gate above — the call was bounded by _remaining, so a
            # timeout here means whichever of the two was the binding one fired.
            if _lifetime_remaining <= _deadline_remaining:
                log.warning(
                    "turn_lifetime_cap_exceeded_mid_call",
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                    span_id=span_id,
                    budget_seconds=settings.orchestrator_turn_lifetime_seconds,
                )
                _stop_turn_for_lifetime_cap(ctx)
            else:
                log.warning(
                    "turn_wall_clock_budget_exceeded_mid_call",
                    trace_id=ctx.trace_id,
                    session_id=ctx.session_id,
                    span_id=span_id,
                    budget_seconds=settings.orchestrator_task_timeout_seconds,
                )
                _stop_turn_for_deadline(ctx)
            # ADR-0074 §I3: pair the STEP_PLANNING_STARTED emitted above this try
            # with a completion, matching the success/error exits below.
            log.info(
                STEP_PLANNING_COMPLETED,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                span_id=span_id,
                parent_span_id=trace_ctx.parent_span_id,
                model_role=model_role.value,
                channel=ctx.channel.value,
                status="deadline_exceeded",
                next_state="synthesis",
            )
            return TaskState.SYNTHESIS

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
        # FRE-1326: capture the real, provider-reported input-token count for the
        # status-bar context meter. Guarded on truthy — a response that genuinely omits
        # usage must not clobber a good prior reading with 0.
        if prompt_tokens:
            ctx.last_prompt_tokens = prompt_tokens
        await _report_turn_progress(ctx)

        log.info(
            LLM_STEP_COMPLETED,
            trace_id=ctx.trace_id,
            span_id=span_id,
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
        log.info(
            STEP_PLANNING_COMPLETED,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            span_id=span_id,
            parent_span_id=trace_ctx.parent_span_id,
            model_role=model_role.value,
            channel=ctx.channel.value,
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
        log.error(
            MODEL_CALL_ERROR,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            span_id=span_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        ctx.error = e

        # FRE-398: classify the error and salvage any gathered tool results.
        from personal_agent.error_classification import classify_error

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

        classified = _salvage_partial_reply(
            ctx,
            classified,
            lead="The model call failed before I could finish, but here's what I gathered:",
        )
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
    # Reset before any early-return path — the driver loop's step-span closure
    # reads this back once this function returns something other than
    # TOOL_EXECUTION, and a stale nonzero value from a *previous* round's
    # successful dispatch must not survive onto this round's (possibly
    # zero-tool) exit.
    ctx.last_tool_execution_count = 0

    # ADR-0076 / FRE-1375: Stop button checkpoint — if the user cancelled mid-turn,
    # synthesize from results gathered so far instead of running more tools. Goes
    # straight to SYNTHESIS, never back through another LLM_CALL (AC-3: Stop must
    # never itself schedule more model work).
    if ctx.session_id and _is_turn_cancelled(ctx.session_id):
        await _emit_turn_cancelled(session_id=ctx.session_id, trace_id=ctx.trace_id)
        _stop_turn_for_cancel(ctx)
        return TaskState.SYNTHESIS

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
        # FRE-973: unless the turn's wall-clock budget is already exhausted —
        # there's no time left to spend asking, so treat it as an automatic
        # decline rather than pausing for a decision the turn can't act on.
        # ADR-0142 D4a (FRE-1392): same treatment for the absolute lifetime cap.
        if min(_turn_deadline_remaining(ctx), _turn_lifetime_remaining(ctx)) <= 0:
            log.info(
                "tool_iteration_limit_pause_skipped_deadline_exceeded",
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                iteration=ctx.tool_iteration_count,
            )
            action_id = None
        else:
            action_id = await _maybe_pause_for_constraint(
                session_id=ctx.session_id,
                trace_id=ctx.trace_id,
                user_id=ctx.user_id,
                constraint="tool_iteration_limit",
                context=f"Reached {ctx.tool_iteration_count} tool calls on this turn.",
                ctx=ctx,
            )
        # ADR-0142 D4a (FRE-1392): the lifetime cap can bind while this pause is
        # in flight — stop straight to synthesis rather than granting a bonus or
        # routing back through LLM_CALL for a turn that is already ending.
        if ctx.turn_stopped_early:
            return TaskState.SYNTHESIS
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
    tool_layer = _get_tool_execution_layer()

    # Extract tool calls from the last assistant message
    if not ctx.messages:
        log.error(
            "no_messages_for_tool_execution",
            trace_id=ctx.trace_id,
            error="No messages in context to extract tool calls from",
        )
        ctx.error = ValueError("No messages in context to extract tool calls from")
        return TaskState.FAILED

    last_message = ctx.messages[-1]
    if last_message.get("role") != "assistant":
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
            # FRE-947: the raw string, since parsing it is what failed.
            _record_undispatched_invocation(
                ctx,
                tool_name=tool_name,
                arguments=arguments_str,
                error=f"malformed argument JSON: {e}",
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
            _record_undispatched_invocation(
                ctx,
                tool_name=tool_name,
                arguments=arguments,
                error=f"blocked by loop gate: {gate_result.decision.value}",
            )
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
            _record_undispatched_invocation(
                ctx,
                tool_name=plan["tool_name"],
                arguments=plan["arguments"],
                error=f"dispatch raised {type(raw).__name__}: {raw}",
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
                # FRE-947 (ADR-0124 AC-8): the summariser's prompt must carry each
                # invocation's arguments. They previously survived only in the
                # intra-turn digest sidecar and were dropped before the capture.
                "arguments": plan["arguments"],
            }
        )
        # ADR-0138 D2 item 2/3 (FRE-1280). The arguments travel with the content: a tool
        # result is a source only to the extent it is not the model's own arguments
        # returning, and `content` alone cannot answer that.
        _citation_identifier = _register_tool_source(
            ctx,
            tool_name=dr["tool_name"],
            arguments=plan["arguments"],
            content=content,
            success=dr["success"],
        )
        # ADR-0138 D3(a) (FRE-1296): the identifier is only citable if the model can
        # see it. FRE-1280 minted it and stopped there — nothing rendered it into the
        # content the model actually reads.
        if _citation_identifier:
            content = _with_citation_marker(content, _citation_identifier)
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

    # ADR-0129 D3 / AC-5: this step's tool count moves onto the step span as an
    # attribute (set by the driver loop's close_step_span call) instead of a
    # separate "tool_execution_completed" log record — the parent-with-no-span_id
    # duplication D3 collapses. See ExecutionContext.last_tool_execution_count.
    ctx.last_tool_execution_count = len(tool_calls)

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
    # Ensure final reply is set (should already be set from LLM call)
    if not ctx.final_reply:
        ctx.final_reply = "Task completed"  # Fallback

    # ADR-0138 D3/D4 (FRE-1282): the inline checks, then D4's decision. Placed here
    # because this is where the turn's reply is final and the registry is complete;
    # a retry returns to LLM_CALL, which the driver loop already allows.
    # FRE-1375: skipped entirely once turn_stopped_early — a salvaged reply from a
    # deadline or user Stop is not a generated claim to verify, and enforce mode's
    # retry path (back to TaskState.LLM_CALL) would otherwise issue exactly the
    # extra model call a Stop must never produce (AC-3).
    mode = settings.grounding_verification_mode
    if mode != "off" and not ctx.turn_stopped_early:
        ctx.grounding_attempts += 1
        verification = await _verify_grounding(ctx, trace_ctx)
        _record_grounding(ctx, verification, mode)

        if mode == "enforce":
            decision = decide(
                verification,
                attempt=ctx.grounding_attempts,
                max_attempts=settings.grounding_max_generation_attempts,
            )
            log.info(
                "grounding_enforcement_decision",
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                decision=decision.decision.value,
                attempt=decision.attempt,
                max_attempts=decision.max_attempts,
                blocking_outcomes=[o.value for o in decision.blocking_outcomes],
            )
            if decision.decision is TurnDecision.RETRY_WITH_FORCED_RETRIEVAL:
                ctx.messages.append(
                    {"role": "user", "content": build_retry_directive(verification)}
                )
                ctx.grounding_retry_pending = True
                ctx.grounding_retrieval_grant += GROUNDING_RETRY_TOOL_GRANT
                ctx.final_reply = None
                return TaskState.LLM_CALL
            if decision.decision is TurnDecision.TERMINAL_NO_SOURCE:
                # D4's terminal state: built from the turn record, never generated, so it
                # consists entirely of system-record spans (D1) and cannot recurse into
                # another verification failure. That is what guarantees the loop ends.
                ctx.final_reply = build_no_source_statement(verification, ctx.retrieval_attempts)

        # ADR-0138 D3(d)'s sampled offline arm (FRE-1286). Reached only on the delivery
        # branch — the retry above already returned to LLM_CALL — so a turn that retried
        # is sampled once, against its final reply. It runs in the background and never
        # touches this turn: it measures the residue containment cannot see, and its
        # miss rate is the evidence for any future decision to promote entailment inline
        # more generally.
        _schedule_offline_entailment(ctx, verification, trace_ctx)

    # Markers are protocol and verification has now consumed them — in every mode,
    # since the leak predates the checks that read them.
    _strip_markers_from_turn(ctx)

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
    session_manager.update_session(ctx.session_id, messages=ctx.messages)

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

        # ADR-0081 §D3 Decision 3: the reactive 0.65 soft compaction trigger was
        # removed with the cache_frozen_layout_enabled flag (FRE-941) — the
        # cache-aware scheduler (step_init `_maybe_frozen_reset`) subsumes it;
        # firing reactive compaction here would rewrite history off-schedule and
        # break the forward-extension.
        # CAVEAT (FRE-944, 2026-07-22): "subsumes it" does not hold in production
        # today. `_maybe_frozen_reset` sits below the gateway branch's unconditional
        # return in step_init, so its reset half is unreachable on gateway-driven
        # turns — which is all of them — and `frozen_reset_fired` is at zero. Nothing
        # currently performs the scheduled reset the removed trigger was traded for;
        # only the decision emit was restored (evaluate-and-log). Gateway Stage 7
        # `apply_budget` still trims, so context is bounded, but not by this. See
        # ADR-0092 open item 7.

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
        # FRE-398: classify and surface a structured, actionable reply. Prefer
        # ctx.classified_error when execute_task already classified this failure
        # (it may also have salvaged a partial reply — never reclassify over that).
        from personal_agent.error_classification import classify_error

        classified = ctx.classified_error or classify_error(e)
        # FRE-973: this is a last-resort net — execute_task's own except normally
        # salvages first, so this is idempotent (no-op) when ctx.final_reply is
        # already set. It only does real work if execute_task itself raised
        # before reaching its own handler.
        classified = _salvage_partial_reply(
            ctx,
            classified,
            lead="The turn failed before I could finish, but here's what I gathered:",
        )
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
            reply_length=len(ctx.final_reply or ""),
            fatal_error=True,
        )
        await _emit_classified_error(ctx, classified)
        return {
            # FRE-973: surface any salvaged partial reply instead of always the
            # bare classified message (this previously ignored ctx.final_reply
            # unconditionally).
            "reply": ctx.final_reply or f"{classified.reason} {classified.next_step}",
            # FRE-973: preserve steps recorded before the fatal exception instead
            # of discarding them (this previously replaced ctx.steps outright).
            "steps": [
                *ctx.steps,
                {
                    "type": "error",
                    "description": classified.reason,
                    "metadata": {
                        "error_type": type(e).__name__,
                        "error_category": classified.category,
                    },
                },
            ],
            "trace_id": ctx.trace_id,
        }
