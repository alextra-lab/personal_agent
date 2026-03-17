"""Request Gateway Pipeline -- orchestrates all stages.

Runs the deterministic pre-LLM pipeline:
  Stage 1: Security (stub in Slice 1)
  Stage 2: Session (handled externally -- messages passed in)
  Stage 3: Governance
  Stage 4: Intent Classification
  Stage 5: Decomposition Assessment (always SINGLE in Slice 1)
  Stage 6+7: Context Assembly + Budget
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from personal_agent.governance.models import Mode
from personal_agent.memory.protocol import MemoryProtocol
from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.governance import evaluate_governance
from personal_agent.request_gateway.intent import classify_intent
from personal_agent.request_gateway.types import (
    DecompositionResult,
    DecompositionStrategy,
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

    Returns:
        GatewayOutput with intent, governance, decomposition, and context.
    """
    degraded_stages: list[str] = []

    # Stage 1: Security (stub -- pass-through in Slice 1)
    # Future: rate limiting, input sanitization, PII detection

    # Stage 2: Session (handled by caller -- messages passed in as session_messages)

    # Stage 3: Governance
    governance = evaluate_governance(mode=mode)

    # Stage 4: Intent Classification
    intent = classify_intent(user_message)

    # Stage 5: Decomposition Assessment (always SINGLE in Slice 1)
    decomposition = DecompositionResult(
        strategy=DecompositionStrategy.SINGLE,
        reason="slice_1_always_single",
    )

    # Stage 6+7: Context Assembly + Budget
    context = await assemble_context(
        user_message=user_message,
        session_messages=session_messages,
        intent=intent,
        memory_adapter=memory_adapter,
        trace_id=trace_id,
    )

    # Track degraded memory.
    # Slice 1: only MEMORY_RECALL triggers a real memory query in
    # _query_memory_for_intent(). Other intents return None by design,
    # so we only flag degradation for MEMORY_RECALL.  Extend this guard
    # in Slice 2 when more intents use memory enrichment.
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

    # Telemetry event
    logger.info(
        "gateway_pipeline_complete",
        task_type=intent.task_type.value,
        complexity=intent.complexity.value,
        confidence=intent.confidence,
        signals=intent.signals,
        mode=governance.mode.value,
        expansion_permitted=governance.expansion_permitted,
        strategy=decomposition.strategy.value,
        message_count=len(context.messages),
        token_count=context.token_count,
        has_memory=context.memory_context is not None,
        degraded_stages=degraded_stages,
        trace_id=trace_id,
    )

    return output
