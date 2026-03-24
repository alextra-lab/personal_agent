"""Request Gateway Pipeline -- orchestrates all stages.

Runs the deterministic pre-LLM pipeline:
  Stage 1: Security (stub in Slice 1)
  Stage 2: Session (handled externally -- messages passed in)
  Stage 3: Governance
  Stage 4: Intent Classification
  Stage 5: Decomposition Assessment
  Stage 6+7: Context Assembly + Budget
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from personal_agent.config import get_settings
from personal_agent.governance.models import Mode
from personal_agent.memory.protocol import MemoryProtocol
from personal_agent.request_gateway.budget import apply_budget
from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.decomposition import assess_decomposition
from personal_agent.request_gateway.governance import evaluate_governance
from personal_agent.request_gateway.intent import classify_intent
from personal_agent.request_gateway.types import (
    GatewayOutput,
    TaskType,
)

logger = structlog.get_logger(__name__)


async def run_gateway_pipeline(
    user_message: str,
    session_id: str,
    session_messages: Sequence[dict[str, Any]],
    trace_id: str,
    mode: Mode = Mode.NORMAL,
    memory_adapter: MemoryProtocol | None = None,
    expansion_budget: int | None = None,
    max_context_tokens: int | None = None,
) -> GatewayOutput:
    """Run the full request gateway pipeline.

    Executes all stages sequentially, producing a GatewayOutput
    that the executor receives as pre-assembled context.

    Args:
        user_message: The user's message text.
        session_id: Active session identifier.
        session_messages: Prior conversation messages (OpenAI format).
        trace_id: Request trace identifier.
        mode: Current brainstem operational mode.
        memory_adapter: Seshat protocol adapter (None if unavailable).
        expansion_budget: Remaining expansion slots (None = read from settings).
        max_context_tokens: Context token ceiling (None = read from settings).

    Returns:
        GatewayOutput with intent, governance, decomposition, and context.
    """
    settings = get_settings()

    if expansion_budget is None:
        expansion_budget = settings.expansion_budget_max

    if max_context_tokens is None:
        max_context_tokens = settings.context_budget_max_tokens

    degraded_stages: list[str] = []

    # Stage 1: Security (stub -- pass-through in Slice 1)
    # Future: rate limiting, input sanitization, PII detection

    # Stage 2: Session (handled by caller -- messages passed in as session_messages)

    # Stage 3: Governance
    governance = evaluate_governance(mode=mode, expansion_budget=expansion_budget)

    # Stage 4: Intent Classification
    intent = classify_intent(user_message)

    logger.info(
        "intent_classified",
        task_type=intent.task_type.value,
        complexity=intent.complexity.value,
        confidence=intent.confidence,
        signals=intent.signals,
        trace_id=trace_id,
    )

    # Stage 5: Decomposition Assessment
    decomposition = assess_decomposition(intent=intent, governance=governance)

    logger.info(
        "decomposition_assessed",
        task_type=intent.task_type.value,
        complexity=intent.complexity.value,
        strategy=decomposition.strategy.value,
        reason=decomposition.reason,
        trace_id=trace_id,
    )

    # Stage 6+7: Context Assembly + Budget
    context = await assemble_context(
        user_message=user_message,
        session_messages=session_messages,
        intent=intent,
        memory_adapter=memory_adapter,
        trace_id=trace_id,
    )
    context = apply_budget(
        context=context,
        max_tokens=max_context_tokens,
        trace_id=trace_id,
    )

    # Track degraded memory.
    # Only flag degradation for MEMORY_RECALL (other intents return None by design).
    if (
        memory_adapter is not None
        and context.memory_context is None
        and intent.task_type == TaskType.MEMORY_RECALL
    ):
        degraded_stages.append("context_assembly:memory_unavailable")

    output = GatewayOutput(
        intent=intent,
        governance=governance,
        decomposition=decomposition,
        context=context,
        session_id=session_id,
        trace_id=trace_id,
        degraded_stages=degraded_stages,
    )

    # Summary telemetry event
    logger.info(
        "gateway_output",
        task_type=intent.task_type.value,
        complexity=intent.complexity.value,
        confidence=intent.confidence,
        signals=intent.signals,
        mode=governance.mode.value,
        expansion_permitted=governance.expansion_permitted,
        expansion_budget=governance.expansion_budget,
        strategy=decomposition.strategy.value,
        message_count=len(context.messages),
        token_count=context.token_count,
        budget_trimmed=context.trimmed,
        overflow_action=context.overflow_action,
        has_memory=context.memory_context is not None,
        degraded_stages=degraded_stages,
        trace_id=trace_id,
    )

    return output
