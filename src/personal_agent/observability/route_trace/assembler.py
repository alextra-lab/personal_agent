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


def _sub_agent_records(subs: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    """Project sub-agent results into JSONB disposition records (hybrid-rubric inputs)."""
    records: list[dict[str, Any]] = []
    for s in subs:
        records.append(
            {
                "task_id": getattr(s, "task_id", None),
                "success": getattr(s, "success", None),
                "tools_used": list(getattr(s, "tools_used", []) or []),
                "token_count": getattr(s, "token_count", None),
                "cost_usd": getattr(s, "cost_usd", None),
                "summary_chars": len(getattr(s, "summary", "") or ""),
                "output_chars": len(getattr(s, "full_output", "") or ""),
                "error": getattr(s, "error", None),
            }
        )
    return tuple(records)


def assemble_route_trace(
    ctx: ExecutionContext,
    *,
    authoritative_cost_usd: float,
    input_tokens: int,
    output_tokens: int,
    store_preview: bool,
    preview_chars: int,
    created_at: datetime | None = None,
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

    cost_live = float(getattr(ctx, "turn_cost_usd", 0.0) or 0.0)
    orchestration_event = classify_orchestration_event(ctx)

    return RouteTraceRow(
        trace_id=_to_uuid(getattr(ctx, "trace_id", None)),  # type: ignore[arg-type]
        session_id=_to_uuid(getattr(ctx, "session_id", None)),
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
        sub_agents=_sub_agent_records(subs),
        expansion_strategy=getattr(ctx, "expansion_strategy", None),
        delegate_result_passed_to_synthesis=passed_to_synthesis,
        # Result type
        orchestration_event=orchestration_event,
        pedagogical_outcomes=None,
        # Synthesis
        final_reply_chars=len(getattr(ctx, "final_reply", "") or ""),
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
