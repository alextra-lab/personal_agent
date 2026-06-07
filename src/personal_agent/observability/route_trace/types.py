"""Route-trace ledger data types (FRE-452).

Defines the orchestration-event vocabulary (taxonomy §3) and :class:`RouteTraceRow`,
the frozen, seam-neutral DTO the ledger persists. The DTO is deliberately free of any
``ExecutionContext`` dependency: :func:`assemble_route_trace` (the interim primary-turn
adapter) builds it from ``ctx`` today, and the future ADR-0088 ``observe_topology`` seam
will build the same DTO per topology — only the adapter changes, never this row or the
schema.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

# The five orchestration events (RESULT_TYPE_TAXONOMY_SPEC §3 / ADR-0084 §D4). Membership
# is fixed by the ADR; this Literal must mirror it exactly. The programmatic classifier
# (classifier.py) only emits the reliably-detectable subset; ``delegate_result_used`` /
# ``delegate_result_discarded`` are hybrid (taxonomy §6) and refined later by rubric.
OrchestrationEvent = Literal[
    "primary_handled",
    "delegate_called",
    "delegate_result_used",
    "delegate_result_discarded",
    "fallback_triggered",
]


@dataclass(frozen=True)
class RouteTraceRow:
    """One per-turn route-trace ledger row — the ADR-0088 D6 direct durable record.

    All fields are read from a single turn's terminal state. Path-dependent fields are
    ``None`` when their producer did not run (e.g. ``task_type`` is ``None`` if the
    gateway never produced output; ``model_role`` is ``None`` before model selection),
    so the row is always constructible regardless of where a turn ended.

    Attributes:
        trace_id: Turn trace identifier (join key to ``api_costs``); ADR-0074 identity.
        session_id: Owning session identifier; ADR-0074 identity.
        task_id: Forward slot for the future per-topology ``(trace_id, task_id)`` key
            (ADR-0088 seam); ``None`` for the current turn-level write.
        created_at: Row creation time (UTC); ``None`` lets the DB default ``NOW()``.
        schema_version: Ledger row schema version (bump on field changes).
        user_message_chars: Length of the user stimulus in characters.
        message_count: Number of messages in the turn's context.
        user_message_sha256: 16-hex prefix of the stimulus SHA-256 (PII-safe pointer).
        user_message_preview: Bounded stimulus preview — populated **only** when the
            ``route_trace_store_preview`` PII gate is enabled; ``None`` otherwise.
        task_type: Gateway-classified task type value (e.g. ``"memory_recall"``).
        complexity: Gateway complexity estimate value (e.g. ``"simple"``).
        intent_confidence: Gateway intent-classification confidence.
        decomposition_strategy: Gateway decomposition strategy value (e.g. ``"single"``).
        decomposition_reason: Human-readable decomposition rationale.
        degraded_stages: Gateway stages that degraded gracefully (observability).
        mode: Governance/brainstem operational mode value.
        channel: Communication channel value (e.g. ``"chat"``).
        gateway_label: The deterministic-shell label ``"<task_type>/<strategy>"`` — the
            "what the gateway decided" half of the ADR-0088 critical boundary field.
        model_role: Selected model tier value (``"primary"``/``"sub_agent"``); ``None``
            if no explicit role was selected.
        thinking_enabled: Whether extended thinking was enabled, when known; ``None``
            when no reliable turn-level signal exists.
        routing_history: Per-turn routing decisions (stored as JSONB).
        tool_iteration_count: Number of tool-execution iterations.
        tools_used: Distinct tool names invoked during the turn.
        skills_loaded: Skill names loaded for the turn.
        sub_agent_count: Number of sub-agents invoked.
        sub_agents: Per-sub-agent disposition signals (JSONB): model/tokens/cost/success,
            ``summary_chars``/``output_chars``, and the FRE-515 ``reply_overlap``
            candidate signal — inputs to the hybrid used/discarded rubric.
        expansion_strategy: Expansion strategy label, when expansion ran.
        delegate_result_passed_to_synthesis: Structural fact (not a hybrid judgement):
            at least one sub-agent result reached the primary synthesis step.
        orchestration_event: Programmatic orchestration event (taxonomy §3).
        pedagogical_outcomes: Nullable slot for the pedagogical-outcome layer — left
            uncomputed here (human-rubric/hybrid until the M3 layer emits; taxonomy §6).
        final_reply_chars: Length of the final user-facing reply in characters.
        latency_total_ms: Total turn wall-clock latency in ms, when timed.
        latency_breakdown: Phase-bucketed latency summary (JSONB), when timed.
        cost_live_usd: Live per-loop cost accumulator value at turn end.
        cost_authoritative_usd: ``SUM(api_costs.cost_usd WHERE trace_id)`` — source of truth.
        cost_reconciled: Whether live and authoritative cost agree within tolerance.
        input_tokens: ``SUM(api_costs.input_tokens)`` for the turn.
        output_tokens: ``SUM(api_costs.output_tokens)`` for the turn.
        fallback_triggered: Whether a sub-agent/phase failure escalated to the primary.
        error_type: Exception class name if the turn errored, else ``None``.
        error_class: Classified-error category if available, else ``None``.
    """

    trace_id: UUID
    session_id: UUID | None
    task_id: UUID | None = None
    created_at: datetime | None = None
    schema_version: int = 1

    # Stimulus (PII-gated)
    user_message_chars: int = 0
    message_count: int = 0
    user_message_sha256: str | None = None
    user_message_preview: str | None = None

    # Gateway classification (deterministic shell)
    task_type: str | None = None
    complexity: str | None = None
    intent_confidence: float | None = None
    decomposition_strategy: str | None = None
    decomposition_reason: str | None = None
    degraded_stages: Sequence[str] = field(default_factory=tuple)
    mode: str | None = None
    channel: str | None = None
    gateway_label: str = "unknown/unknown"

    # Model path
    model_role: str | None = None
    thinking_enabled: bool | None = None
    routing_history: Sequence[Mapping[str, object]] = field(default_factory=tuple)

    # Tools / skills
    tool_iteration_count: int = 0
    tools_used: Sequence[str] = field(default_factory=tuple)
    skills_loaded: Sequence[str] = field(default_factory=tuple)

    # Delegation
    sub_agent_count: int = 0
    sub_agents: Sequence[Mapping[str, object]] = field(default_factory=tuple)
    expansion_strategy: str | None = None
    delegate_result_passed_to_synthesis: bool = False

    # Result type
    orchestration_event: OrchestrationEvent = "primary_handled"
    pedagogical_outcomes: Sequence[str] | None = None

    # Synthesis
    final_reply_chars: int = 0

    # Latency
    latency_total_ms: float | None = None
    latency_breakdown: Mapping[str, object] | None = None

    # Cost (ADR-0088 D3)
    cost_live_usd: float = 0.0
    cost_authoritative_usd: float = 0.0
    cost_reconciled: bool = False
    input_tokens: int = 0
    output_tokens: int = 0

    # Fallback / error
    fallback_triggered: bool = False
    error_type: str | None = None
    error_class: str | None = None
