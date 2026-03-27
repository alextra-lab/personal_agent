"""Sub-agent contract types for Cognitive Architecture Redesign v2.

Sub-agents are task-scoped inference calls — NOT separate services or processes.
Each represents one focused LLM call with a well-defined input/output contract.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.6
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from personal_agent.llm_client import ModelRole


@dataclass(frozen=True)
class SubAgentSpec:
    """Specification for a single sub-agent inference call.

    Defines everything the sub-agent executor needs to run one focused
    LLM call. All fields are immutable once created.

    Attributes:
        task: Human-readable description of the sub-task to perform.
        context: Subset of context relevant to this sub-task
            (messages, retrieved docs, tool results, etc.).
        output_format: Expected output shape — e.g. "text", "json",
            "bullet_list", "code". Used by synthesiser to interpret results.
        max_tokens: Token ceiling for this sub-agent's response.
        timeout_seconds: Execution timeout for this sub-agent call.
        tools: Tool names the sub-agent is allowed to invoke (empty = none).
        background: Background context injected into the sub-agent's system
            prompt (parent task summary, constraints, etc.).
        model_role: Model role to use for inference. Defaults to SUB_AGENT (ADR-0033).
    """

    task: str
    context: list[dict[str, Any]]
    output_format: str = "text"
    max_tokens: int = 4096
    timeout_seconds: float = 120.0
    tools: list[str] = field(default_factory=list)
    background: str = ""
    model_role: ModelRole = ModelRole.SUB_AGENT


@dataclass(frozen=True)
class SubAgentResult:
    """Result of a single sub-agent inference call.

    Separates compact summary (used in synthesis context) from full output
    (stored in Elasticsearch for observability).

    Attributes:
        task_id: Unique identifier for this sub-agent invocation.
        spec_task: Original task string from the SubAgentSpec.
        summary: Compact distillation for the parent agent's synthesis context.
            Should be ≤ 500 tokens to keep synthesis context manageable.
        full_output: Complete sub-agent response, stored to ES for observability.
        tools_used: Names of tools actually invoked during this call.
        token_count: Total tokens consumed (prompt + completion).
        duration_ms: Wall-clock execution time in milliseconds.
        success: True if the call completed without error.
        error: Error message if success=False, None otherwise.
    """

    task_id: str
    spec_task: str
    summary: str
    full_output: str
    tools_used: list[str]
    token_count: int
    duration_ms: float
    success: bool
    error: str | None = None
