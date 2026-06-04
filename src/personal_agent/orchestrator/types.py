"""Core types for the orchestrator.

This module defines the data structures used throughout the orchestrator:
- TaskState: State machine states
- ExecutionContext: Mutable state container passed through execution steps
- OrchestratorStep: Individual step metadata
- OrchestratorResult: Final result returned to UI
- RoutingDecision: Router decision types (HANDLE, DELEGATE)
- RoutingResult: Router decision output with format detection and parameters
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID

from personal_agent.governance.models import Mode
from personal_agent.llm_client import ModelRole
from personal_agent.orchestrator.loop_gate import ToolLoopGate
from personal_agent.request_gateway.types import GatewayOutput

if TYPE_CHECKING:
    from personal_agent.error_classification import ClassifiedError
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.expansion_types import ExpansionPlan, PhaseResult
    from personal_agent.orchestrator.sub_agent_types import SubAgentResult
    from personal_agent.telemetry.request_timer import RequestTimer


class TaskState(str, Enum):
    """State machine states for task execution."""

    INIT = "init"
    PLANNING = "planning"
    LLM_CALL = "llm_call"
    TOOL_EXECUTION = "tool_execution"
    SYNTHESIS = "synthesis"
    COMPLETED = "completed"
    FAILED = "failed"


class RoutingDecision(str, Enum):
    """Router decision types.

    Router is delegate-only; DELEGATE sends to STANDARD/REASONING/CODING.
    HANDLE retained for backward compatibility in RoutingResult.
    """

    HANDLE = "HANDLE"  # Legacy; router no longer answers directly
    DELEGATE = "DELEGATE"  # Delegate to specialized model


class HeuristicRoutingPlan(TypedDict):
    """Result of deterministic pre-router heuristic gate.

    Used to skip LLM router when confidence is high.
    """

    target_model: ModelRole
    confidence: float
    reason: str
    used_heuristics: bool


class RecommendedParams(TypedDict, total=False):
    """Recommended parameters from router for downstream model calls.

    Phase 2 enhancement: Router can recommend parameters based on
    detected output format and query complexity.

    Fields:
        max_tokens: Recommended maximum tokens for response.
        temperature: Recommended sampling temperature.
        timeout_multiplier: Multiplier for base timeout (1.0 = default).
    """

    max_tokens: int
    temperature: float
    timeout_multiplier: float


class RoutingResult(TypedDict, total=False):
    """Router decision output with format detection and parameters.

    This TypedDict contains the router model's decision about how to
    handle a query, including model selection and parameter recommendations.

    Fields:
        decision: HANDLE (router answers) or DELEGATE (use specialized model).
        target_model: If DELEGATE, which model to use (REASONING, CODING).
        confidence: Router's confidence in decision (0.0-1.0).
        reasoning_depth: Estimated complexity on 1-10 scale.

        # Output format detection (Phase 2)
        detected_format: Detected output format (summary, detailed, etc.).
        format_confidence: Confidence in format detection (0.0-1.0).
        format_keywords_matched: Keywords that triggered format detection.

        # Parameter recommendations (Phase 2)
        recommended_params: Recommended parameters for downstream call.

        # Direct response (if HANDLE)
        response: Router's direct response text (if decision=HANDLE).

        # Explanation
        reason: Brief explanation of routing decision.
    """

    # Required fields
    decision: RoutingDecision
    confidence: float
    reasoning_depth: int
    reason: str

    # Delegation fields (if decision=DELEGATE)
    target_model: ModelRole | None

    # Format detection (Phase 2)
    detected_format: str | None
    format_confidence: float | None
    format_keywords_matched: list[str] | None

    # Parameter recommendations (Phase 2)
    recommended_params: RecommendedParams | None

    # Direct response (if decision=HANDLE)
    response: str | None


@dataclass(frozen=True)
class ToolResultPin:
    """A verbatim ``read`` result held back from digestion (ADR-0085 §D4).

    The most-recent ``read`` of a file path is kept verbatim while a dependent
    ``write`` against that path may still be issued (the read→write hazard). The
    pin is released on a successful ``write`` to the path or after
    ``tool_result_digest_pin_ttl_turns`` rounds (abandonment).

    Attributes:
        path: The file path the pinned read targeted.
        round_pinned: ``tool_iteration_count`` when the pin was recorded.
    """

    path: str
    round_pinned: int


@dataclass
class ExecutionContext:
    """Mutable state container passed through execution steps.

    This dataclass holds all state that flows through the orchestrator's
    state machine. It is intentionally mutable to allow step functions to
    update state as execution progresses.

    Attributes:
        session_id: Unique identifier for the session.
        trace_id: Unique identifier for this task's trace (for telemetry).
        user_message: The user's input message.
        mode: Current operational mode from governance.
        channel: Communication channel (CHAT, CODE_TASK, SYSTEM_HEALTH).
        messages: OpenAI-style chat history (system, user, assistant, tool).
        current_plan: Optional execution plan (for future planning features).
        tool_results: List of tool execution results.
        final_reply: The final response text to return to user.
        error: Exception if task failed, None otherwise.
        steps: List of OrchestratorStep records for observability.
        state: Current state in the state machine.

    # Routing state (Day 11.5)
    selected_model_role: Model role selected by router (if delegated).
    routing_history: History of routing decisions for this task.

    # Request monitoring (ADR-0012)
    metrics_summary: Aggregated system metrics summary from RequestMonitor.
    """

    session_id: str
    trace_id: str
    user_message: str
    mode: Mode
    channel: "Channel"  # Forward reference, defined in channels.py
    messages: list[dict[str, Any]] = field(default_factory=list)
    current_plan: dict[str, Any] | None = None
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    final_reply: str | None = None
    error: Exception | None = None
    classified_error: ClassifiedError | None = None
    steps: list["OrchestratorStep"] = field(default_factory=list)
    state: TaskState = TaskState.INIT
    metrics_summary: dict[str, Any] | None = None  # ADR-0012: Request-scoped metrics

    # Routing state (Day 11.5)
    selected_model_role: ModelRole | None = None
    routing_history: list[RoutingResult] = field(default_factory=list)

    # LLM response tracking (for stateful /v1/responses API)
    last_response_id: str | None = None

    # Tool loop governance (per-request)
    tool_iteration_count: int = 0
    loop_gate: ToolLoopGate = field(default_factory=ToolLoopGate)
    # Set True when the iteration limit fires so step_llm_call performs a no-tool synthesis pass
    force_synthesis_from_limit: bool = False
    # ADR-0076: extra iterations granted when the user picks "Continue" at a
    # tool_iteration_limit constraint pause. Added on top of the resolved max.
    tool_iteration_bonus: int = 0
    # ADR-0076: accumulated LLM spend for this turn (USD), surfaced live via the
    # turn_status STATE_DELTA so the user sees cost as it accrues.
    turn_cost_usd: float = 0.0

    # Memory enrichment (Phase 2.2)
    memory_context: list[dict[str, Any]] | None = None  # Retrieved conversations for context

    # Request timing (FRE-37): inline span-based instrumentation
    request_timer: "RequestTimer | None" = None

    # Gateway output (Cognitive Architecture Redesign v2)
    gateway_output: GatewayOutput | None = None  # From request_gateway pipeline

    eval_mode: bool = False  # True when request came from an eval/benchmark harness channel

    # FRE-229: owning user UUID — passed from the authenticated request for TaskCapture
    user_id: UUID | None = None
    # FRE-213: user email + display name for the operator stanza (ADR-0052)
    user_email: str | None = None
    user_display_name: str | None = None
    # Rendered operator stanza populated in step_init; injected into system prompt in step_llm_call.
    operator_stanza: str = ""

    # --- Expansion controller state (Slice 3, ADR-0036) ---
    expansion_strategy: str | None = None
    expansion_constraints: dict[str, Any] | None = None
    sub_agent_results: list["SubAgentResult"] | None = None
    expansion_plan: "ExpansionPlan | None" = None
    expansion_phase_results: list["PhaseResult"] = field(default_factory=list)

    # --- Phase B skill routing (FRE-skill-routing) ---
    # Tracks which skill bodies have been read_skill'd this conversation for dedup.
    loaded_skills: set[str] = field(default_factory=set)

    # --- Phase C skill routing (FRE-skill-routing) ---
    # Set True after the routing model has been queried for this request.
    # Prevents the routing call from re-firing on every step_llm_call iteration.
    skill_routing_done: bool = False
    # Model ID returned by the routing call (for telemetry breakdown across cells).
    skill_routing_model_id: str = ""

    # --- ADR-0081 §D3 cache-aware compaction (FRE-434) ---
    # Bounded salient highlights produced by the most recent frozen reset; ride
    # the current turn's volatile block (regenerated on reset, never frozen).
    salient_highlights: str = ""

    # --- ADR-0085 §D4 intra-turn tool-result digest pinning (FRE-475) ---
    # Reads held verbatim pending a dependent write, keyed by tool_call_id.
    tool_result_pins: dict[str, ToolResultPin] = field(default_factory=dict)


class OrchestratorStep(TypedDict):
    """Step metadata for observability.

    This TypedDict records information about each step in the orchestrator's
    execution. Used for trace reconstruction and debugging.

    Fields:
        type: Step type ("llm_call", "tool_call", "plan", "summary", "warning").
        description: Human-readable description of what this step did.
        metadata: Additional structured data (model_role, tool_name, span_ids, etc.).
    """

    type: str
    description: str
    metadata: dict[str, Any]


class OrchestratorResult(TypedDict, total=False):
    """Final result returned to UI from orchestrator.

    This TypedDict contains the orchestrator's response to a user request.

    Fields:
        reply: Final user-facing text response.
        steps: List of OrchestratorStep records for transparency.
        trace_id: Trace ID for telemetry correlation.
    """

    reply: str
    steps: list[OrchestratorStep]
    trace_id: str | None
