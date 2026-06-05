"""Stage 5: Decomposition Assessment.

Deterministic decision matrix replacing the Slice 1 always-SINGLE stub.
Maps (task_type, complexity, expansion_budget) → DecompositionStrategy.

No LLM call — pure function driven by intent classification output
and governance context from prior pipeline stages.
"""

from __future__ import annotations

import structlog

from personal_agent.config import settings
from personal_agent.request_gateway.types import (
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GovernanceContext,
    IntentResult,
    TaskType,
)

logger = structlog.get_logger(__name__)


def assess_decomposition(
    intent: IntentResult,
    governance: GovernanceContext,
) -> DecompositionResult:
    """Assess how to handle this request: single, hybrid, decompose, or delegate.

    Pure function — no LLM call, no side effects. Applies the decision matrix
    from COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3.5.

    Resource pressure (expansion not permitted or budget exhausted) always
    forces SINGLE regardless of task type or complexity.

    Args:
        intent: Classified intent from Stage 4.
        governance: Governance context from Stage 3.

    Returns:
        DecompositionResult with strategy and human-readable reason.
    """
    # Resource pressure: force SINGLE
    if not governance.expansion_permitted:
        logger.debug("decomposition_forced_single", reason="expansion_denied")
        return DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="expansion_denied",
        )

    if governance.expansion_budget <= 0:
        logger.debug("decomposition_forced_single", reason="zero_budget")
        return DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="zero_budget",
        )

    strategy, reason = _apply_matrix(
        intent.task_type,
        intent.complexity,
        artifact_decomposition_enabled=settings.artifact_decomposition_enabled,
    )

    logger.info(
        "decomposition_assessed",
        task_type=intent.task_type.value,
        complexity=intent.complexity.value,
        strategy=strategy.value,
        reason=reason,
    )

    return DecompositionResult(
        strategy=strategy,
        reason=reason,
    )


def _apply_matrix(
    task_type: TaskType,
    complexity: Complexity,
    *,
    artifact_decomposition_enabled: bool,
) -> tuple[DecompositionStrategy, str]:
    """Apply the decomposition decision matrix.

    Args:
        task_type: Classified task type.
        complexity: Estimated complexity.
        artifact_decomposition_enabled: ADR-0086 rollout flag. When False, TOOL_USE
            turns keep the legacy unconditional SINGLE path; when True they branch
            on complexity (MODERATE/COMPLEX → HYBRID for discovery decomposition).

    Returns:
        Tuple of (strategy, reason).
    """
    match task_type:
        case TaskType.CONVERSATIONAL:
            return DecompositionStrategy.SINGLE, "conversational_always_single"

        case TaskType.MEMORY_RECALL:
            return DecompositionStrategy.SINGLE, "memory_recall_always_single"

        case TaskType.SELF_IMPROVE:
            return DecompositionStrategy.SINGLE, "self_improve_always_single"

        case TaskType.DELEGATION:
            return DecompositionStrategy.DELEGATE, "delegation_route_external"

        case TaskType.TOOL_USE:
            # ADR-0086 D2: gated branch. Flag off (default) preserves the legacy
            # SINGLE path so Phase 1 is inert in prod until FRE-480 (tool-using
            # sub-agent loop) + FRE-481 (telemetry/A/B) land. HYBRID (not
            # DECOMPOSE) for both moderate and complex: artifact-build discovery
            # slices parallelize with no inter-slice ordering.
            if not artifact_decomposition_enabled:
                return DecompositionStrategy.SINGLE, "tool_use_single"
            match complexity:
                case Complexity.SIMPLE:
                    return DecompositionStrategy.SINGLE, "tool_use_simple_single"
                case Complexity.MODERATE:
                    return DecompositionStrategy.HYBRID, "tool_use_moderate_hybrid"
                case _:
                    return DecompositionStrategy.HYBRID, "tool_use_complex_hybrid"

        case TaskType.ANALYSIS:
            match complexity:
                case Complexity.SIMPLE:
                    return DecompositionStrategy.SINGLE, "analysis_simple"
                case Complexity.MODERATE:
                    return DecompositionStrategy.HYBRID, "analysis_moderate_hybrid"
                case _:
                    return DecompositionStrategy.DECOMPOSE, "analysis_complex_decompose"

        case _:  # TaskType.PLANNING (and any future task types)
            match complexity:
                case Complexity.SIMPLE:
                    return DecompositionStrategy.SINGLE, "planning_simple"
                case _:
                    return DecompositionStrategy.HYBRID, "planning_moderate_hybrid"
