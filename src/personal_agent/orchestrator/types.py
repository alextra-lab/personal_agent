"""Core types for the orchestrator.

This module defines the data structures used throughout the orchestrator:
- TaskState: State machine states
- ExecutionContext: Mutable state container passed through execution steps
- OrchestratorStep: Individual step metadata
- OrchestratorResult: Final result returned to UI
- RoutingDecision: Router decision types (HANDLE, DELEGATE)
- RoutingResult: Router decision output with format detection and parameters
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, TypedDict

from personal_agent.governance.models import Mode
from personal_agent.llm_client import ModelRole

if TYPE_CHECKING:
    from personal_agent.orchestrator.channels import Channel


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

    The router model can either handle a query directly or delegate to
    a specialized model (REASONING, CODING).
    """

    HANDLE = "HANDLE"  # Router answers directly (simple queries)
    DELEGATE = "DELEGATE"  # Delegate to specialized model (complex queries)


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
    tool_call_signatures: list[str] = field(default_factory=list)

    # Memory enrichment (Phase 2.2)
    memory_context: list[dict[str, Any]] | None = None  # Retrieved conversations for context


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
