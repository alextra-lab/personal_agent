"""Interim primary-turn adapter: ``ExecutionContext`` → :class:`RouteTraceRow` (FRE-452).

This is a **pure** function (no I/O). It is the *interim* adapter that builds the
seam-neutral row from a completed primary-turn ``ExecutionContext``. When the ADR-0088
``observe_topology`` seam lands, it will build the same :class:`RouteTraceRow` DTO per
topology and hand it to the same :class:`RouteTraceLedger.write` — only this adapter and
its call site are replaced, never the row or the schema.

Every read is defensive: path-dependent producers (gateway, model selection, request
timer, expansion) may not have run, so missing values become explicit ``None`` /
``"unknown"`` rather than raising.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from personal_agent.observability.route_trace.classifier import (
    classify_orchestration_event,
)
from personal_agent.observability.route_trace.types import RouteTraceRow

if TYPE_CHECKING:
    from personal_agent.orchestrator.types import ExecutionContext

# Live vs authoritative cost are "reconciled" when they agree within this USD tolerance
# (sub-tenth-of-a-cent); a mismatch flags a per-loop-accumulation drift (ADR-0088 D3).
_COST_RECONCILE_TOLERANCE_USD = 0.0005


def _enum_value(value: object) -> Any:
    """Return ``value.value`` for an Enum, else ``value`` unchanged (recursively safe)."""
    if isinstance(value, Enum):
        return value.value
    return value


def _jsonify(value: object) -> Any:
    """Coerce a value into a JSON-serialisable form (enums → values, recurse).

    Args:
        value: Arbitrary value drawn from routing history or sub-agent records.

    Returns:
        A JSON-serialisable equivalent: enums become their ``.value``, mappings and
        sequences are converted element-wise, everything else passes through.
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    return value


def _to_uuid(value: object) -> UUID | None:
    """Parse a value into a UUID, returning ``None`` when it is absent or malformed."""
    if isinstance(value, UUID):
        return value
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _tools_from_steps(steps: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Extract the distinct tool names invoked, read from orchestrator steps."""
    seen: list[str] = []
    for step in steps:
        if step.get("type") != "tool_call":
            continue
        name = (step.get("metadata") or {}).get("tool_name")
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


# FRE-515: content tokens for the reply_overlap signal — lowercase alnum runs of length
# >= 4 (drops stopword-sized noise and punctuation without a stopword list).
_CONTENT_TOKEN_RE = re.compile(r"[a-z0-9_]{4,}")


def _content_tokens(text: str) -> frozenset[str]:
    """Return the distinct content tokens of ``text`` (lowercased, length >= 4)."""
    return frozenset(_CONTENT_TOKEN_RE.findall(text.lower()))


def _reply_overlap(summary: str, reply_tokens: frozenset[str]) -> float | None:
    """Containment of a sub-agent summary's content tokens in the final reply (FRE-515).

    A *weak, candidate-grade* disposition signal (taxonomy §3.3/§3.4): markdown noise and
    boilerplate inflate it, paraphrased incorporation deflates it. It informs the hybrid
    used/discarded rubric — it never decides it.

    Args:
        summary: The sub-agent summary text.
        reply_tokens: Pre-computed content tokens of the final user-facing reply.

    Returns:
        ``|tokens(summary) ∩ tokens(reply)| / |tokens(summary)|`` rounded to 3 decimals,
        or ``None`` when the summary has no content tokens.
    """
    summary_tokens = _content_tokens(summary)
    if not summary_tokens:
        return None
    return round(len(summary_tokens & reply_tokens) / len(summary_tokens), 3)


def _sub_agent_records(subs: Sequence[Any], final_reply: str) -> tuple[dict[str, Any], ...]:
    """Project sub-agent results into JSONB disposition records (hybrid-rubric inputs)."""
    reply_tokens = _content_tokens(final_reply)
    records: list[dict[str, Any]] = []
    for s in subs:
        sub_task_id = getattr(s, "task_id", None)
        records.append(
            {
                # FRE-517: task_id is a UUID; stringify for JSONB (UUID isn't JSON-serialisable).
                "task_id": str(sub_task_id) if sub_task_id is not None else None,
                "success": getattr(s, "success", None),
                "tools_used": list(getattr(s, "tools_used", []) or []),
                "token_count": getattr(s, "token_count", None),
                "cost_usd": getattr(s, "cost_usd", None),
                "summary_chars": len(getattr(s, "summary", "") or ""),
                "output_chars": len(getattr(s, "full_output", "") or ""),
                "reply_overlap": _reply_overlap(getattr(s, "summary", "") or "", reply_tokens),
                "error": getattr(s, "error", None),
            }
        )
    return tuple(records)


def assemble_sub_agent_route_trace(
    ctx: ExecutionContext,
    sub: Any,
    *,
    created_at: datetime | None = None,
) -> RouteTraceRow:
    """Build a per-segment :class:`RouteTraceRow` for one sub-agent (ADR-0088, FRE-517).

    A *segment* row is the per-topology counterpart to the turn-level row: it shares the
    turn's ``trace_id`` but carries the sub-agent's own ``task_id`` (a ``UUID``), so a trace
    fans out to one turn-level row (``task_id`` NULL) plus one segment row per sub-agent. The
    discriminator between the two is ``task_id IS NOT NULL`` — segment rows deliberately leave
    the gateway-classification fields unset (a sub-agent has no gateway decision of its own).

    Attribution (ADR-0088 D3 / FRE-517): ``api_costs`` has no ``task_id``, so per-segment cost
    comes from the sub-agent's own ``cost_usd`` (live == authoritative == self-sourced, hence
    ``cost_reconciled=True``). Per-sub token split is unavailable — ``SubAgentResult.token_count``
    is a word-split estimate, not a billed input/output split — so ``input_tokens`` /
    ``output_tokens`` are ``0`` and the estimate is preserved with an explicit availability flag
    in ``sub_agents`` (which, for a segment row, describes the segment itself, not children).

    Args:
        ctx: The completed turn's execution context (read only for identity).
        sub: The ``SubAgentResult`` for this segment.
        created_at: Row timestamp; defaults to ``datetime.now(UTC)``.

    Returns:
        A frozen, sparse :class:`RouteTraceRow` keyed by ``(trace_id, sub.task_id)``.
    """
    cost = float(getattr(sub, "cost_usd", 0.0) or 0.0)
    success = bool(getattr(sub, "success", False))
    full_output = getattr(sub, "full_output", "") or ""
    self_record = {
        "task_id": str(getattr(sub, "task_id", "")),
        "token_count": getattr(sub, "token_count", None),
        "token_count_is_estimate": True,
        "token_split_available": False,
    }
    return RouteTraceRow(
        trace_id=_to_uuid(getattr(ctx, "trace_id", None)),  # type: ignore[arg-type]
        session_id=_to_uuid(getattr(ctx, "session_id", None)),
        task_id=getattr(sub, "task_id", None),
        created_at=created_at or datetime.now(timezone.utc),
        model_role="sub_agent",
        tools_used=tuple(getattr(sub, "tools_used", None) or ()),
        sub_agents=(self_record,),
        # Discriminator is task_id IS NOT NULL; orchestration_event stays the neutral default.
        orchestration_event="primary_handled",
        final_reply_chars=len(full_output),
        cost_live_usd=cost,
        cost_authoritative_usd=cost,
        cost_reconciled=True,
        input_tokens=0,
        output_tokens=0,
        fallback_triggered=False,
        error_type=None if success else "sub_agent_failed",
    )


def assemble_route_trace(
    ctx: ExecutionContext,
    *,
    authoritative_cost_usd: float,
    input_tokens: int,
    output_tokens: int,
    store_preview: bool,
    preview_chars: int,
    created_at: datetime | None = None,
    topology: str | None = None,
    task_id: UUID | None = None,
) -> RouteTraceRow:
    """Build a :class:`RouteTraceRow` from a completed turn's execution context.

    Args:
        ctx: The turn's execution context at completion.
        authoritative_cost_usd: ``SUM(api_costs.cost_usd WHERE trace_id)`` — source of truth.
        input_tokens: ``SUM(api_costs.input_tokens)`` for the turn.
        output_tokens: ``SUM(api_costs.output_tokens)`` for the turn.
        store_preview: PII gate — when ``True``, a bounded stimulus preview is stored;
            when ``False`` only a SHA-256 pointer and counts are kept.
        preview_chars: Maximum preview length when ``store_preview`` is enabled.
        created_at: Row timestamp; defaults to ``datetime.now(UTC)``.
        topology: Resolved ADR-0088 execution-topology label (``primary`` /
            ``hybrid_fanout`` / ``decompose`` / ``delegate``). Drives the ``cost_live_usd``
            source (see below); ``None`` is treated as ``primary``.
        task_id: Per-topology task identifier for the ``(trace_id, task_id)`` key; ``None``
            for the turn-level write.

    Returns:
        A fully-populated, frozen :class:`RouteTraceRow`. Missing producers yield
        explicit ``None`` / ``"unknown"`` fields rather than raising.
    """
    user_message = getattr(ctx, "user_message", "") or ""
    sha = hashlib.sha256(user_message.encode("utf-8")).hexdigest()[:16] if user_message else None
    preview = user_message[:preview_chars] if (store_preview and user_message) else None

    gateway_output = getattr(ctx, "gateway_output", None)
    task_type: str | None = None
    complexity: str | None = None
    intent_confidence: float | None = None
    strategy: str | None = None
    decomposition_reason: str | None = None
    degraded_stages: tuple[str, ...] = ()
    mode: str | None = None
    if gateway_output is not None:
        task_type = _enum_value(gateway_output.intent.task_type)
        complexity = _enum_value(gateway_output.intent.complexity)
        intent_confidence = gateway_output.intent.confidence
        strategy = _enum_value(gateway_output.decomposition.strategy)
        decomposition_reason = gateway_output.decomposition.reason
        degraded_stages = tuple(gateway_output.degraded_stages or ())
        mode = _enum_value(gateway_output.governance.mode)

    gateway_label = f"{task_type or 'unknown'}/{strategy or 'unknown'}"

    subs = list(getattr(ctx, "sub_agent_results", None) or [])
    final_reply = getattr(ctx, "final_reply", "") or ""
    # Structural fact (not a hybrid judgement): a successful sub-agent result with a
    # non-empty summary reached the primary synthesis step.
    passed_to_synthesis = any(
        getattr(s, "success", False) and (getattr(s, "summary", "") or "") for s in subs
    )

    request_timer = getattr(ctx, "request_timer", None)
    latency_total_ms: float | None = None
    latency_breakdown: dict[str, Any] | None = None
    if request_timer is not None:
        latency_total_ms = request_timer.get_total_ms()
        latency_breakdown = request_timer.to_trace_summary()

    error = getattr(ctx, "error", None)
    classified = getattr(ctx, "classified_error", None)
    channel = getattr(ctx, "channel", None)
    model_role = getattr(ctx, "selected_model_role", None)

    # ADR-0088 D3 cost cadence: for the primary topology the live meter is the per-loop
    # accumulator (``ctx.turn_cost_usd``); for non-primary topologies that accumulator is
    # not the live source (FRE-501 rollup removed), so the row records the authoritative
    # SUM as the live value — the projector is the live surface for those turns and
    # reconciles to authoritative. Keeps ``cost_reconciled`` a primary-accumulator drift
    # signal rather than flagging every expansion turn.
    if topology in (None, "primary"):
        cost_live = float(getattr(ctx, "turn_cost_usd", 0.0) or 0.0)
    else:
        cost_live = float(authoritative_cost_usd)
    orchestration_event = classify_orchestration_event(ctx)

    return RouteTraceRow(
        trace_id=_to_uuid(getattr(ctx, "trace_id", None)),  # type: ignore[arg-type]
        session_id=_to_uuid(getattr(ctx, "session_id", None)),
        task_id=task_id,
        created_at=created_at or datetime.now(timezone.utc),
        # Stimulus (PII-gated)
        user_message_chars=len(user_message),
        message_count=len(getattr(ctx, "messages", None) or []),
        user_message_sha256=sha,
        user_message_preview=preview,
        # Gateway classification
        task_type=task_type,
        complexity=complexity,
        intent_confidence=intent_confidence,
        decomposition_strategy=strategy,
        decomposition_reason=decomposition_reason,
        degraded_stages=degraded_stages,
        mode=mode,
        channel=_enum_value(channel) if channel is not None else None,
        gateway_label=gateway_label,
        # Model path
        model_role=_enum_value(model_role) if model_role is not None else None,
        thinking_enabled=None,
        routing_history=tuple(_jsonify(getattr(ctx, "routing_history", None) or [])),
        # Tools / skills
        tool_iteration_count=int(getattr(ctx, "tool_iteration_count", 0) or 0),
        tools_used=_tools_from_steps(getattr(ctx, "steps", None) or []),
        skills_loaded=tuple(sorted(getattr(ctx, "loaded_skills", None) or set())),
        # Delegation
        sub_agent_count=len(subs),
        sub_agents=_sub_agent_records(subs, final_reply),
        expansion_strategy=getattr(ctx, "expansion_strategy", None),
        delegate_result_passed_to_synthesis=passed_to_synthesis,
        # Result type
        orchestration_event=orchestration_event,
        pedagogical_outcomes=None,
        # Synthesis
        final_reply_chars=len(final_reply),
        # Latency
        latency_total_ms=latency_total_ms,
        latency_breakdown=latency_breakdown,
        # Cost (ADR-0088 D3)
        cost_live_usd=cost_live,
        cost_authoritative_usd=float(authoritative_cost_usd),
        cost_reconciled=abs(cost_live - float(authoritative_cost_usd))
        <= _COST_RECONCILE_TOLERANCE_USD,
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        # Fallback / error
        fallback_triggered=orchestration_event == "fallback_triggered",
        error_type=type(error).__name__ if error is not None else None,
        error_class=getattr(classified, "category", None),
    )
