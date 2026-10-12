"""Stage 5: Decomposition Assessment.

Deterministic decision matrix replacing the Slice 1 always-SINGLE stub.
Maps (task_type, complexity, expansion_budget, planner gate) → DecompositionStrategy.

ADR-0154 D2: when the session's primary deployment declares a ``planner`` mode, the four
register types route ``HYBRID`` with reason ``planner_asked``, and the planner decides
whether to expand. Without that mode they keep the matrix below.

No LLM call — pure function driven by intent classification output
and governance context from prior pipeline stages.
"""

from __future__ import annotations

import structlog

from personal_agent.request_gateway.types import (
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GovernanceContext,
    IntentResult,
    PlannerGateReason,
    TaskType,
)

logger = structlog.get_logger(__name__)

# ADR-0154 D2: the task types the planner decides for, behind the planner-mode gate.
REGISTER_TASK_TYPES = frozenset(
    {TaskType.CONVERSATIONAL, TaskType.TOOL_USE, TaskType.ANALYSIS, TaskType.PLANNING}
)


def assess_decomposition(
    intent: IntentResult,
    governance: GovernanceContext,
    delegation_enabled: bool = False,
    expansion_enabled: bool = True,
    planner_mode_declared: bool = False,
) -> DecompositionResult:
    """Assess how to handle this request: single, hybrid, decompose, or delegate.

    Pure function — no LLM call, no side effects. Applies the decision matrix
    from COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3.5.

    Resource pressure (expansion not permitted or budget exhausted) always
    forces SINGLE regardless of task type or complexity.

    Args:
        intent: Classified intent from Stage 4.
        governance: Governance context from Stage 3.
        delegation_enabled: Whether a delegation adapter is actually wired
            (``settings.delegation_enabled``). When False, TaskType.DELEGATION
            falls back to a complexity-based strategy instead of DELEGATE —
            DELEGATE would otherwise compose a DelegationPackage nothing can
            receive (FRE-1376).
        expansion_enabled: Whether worker expansion is allowed at all
            (``settings.expansion_enabled``). A study switch (FRE-1520), not a
            governance mode — brainstem modes that disable expansion also
            change other behaviour, which would confound an A/B measurement.
            When False, every request forces SINGLE with reason
            ``expansion_disabled``, checked after the resource-pressure
            forcings so ``expansion_denied`` and ``zero_budget`` keep their
            own reasons.
        planner_mode_declared: Whether the session's primary deployment declares a
            ``planner`` mode (ADR-0154 D2, from
            ``config.model_loader.primary_declares_planner_mode``). It opens the
            ``planner_asked`` route for the four register types. ``False`` (the default)
            keeps today's routing and records ``planner_mode_absent`` on those types.

    Returns:
        DecompositionResult with strategy and human-readable reason.
    """
    # ADR-0154 D6: recorded on every turn of the four register types whose primary declares
    # no planner mode, forced or not, so a reader can tell the gate from the forcings.
    planner_gate_reason: PlannerGateReason | None = (
        "planner_mode_absent"
        if not planner_mode_declared and intent.task_type in REGISTER_TASK_TYPES
        else None
    )

    # Resource pressure: force SINGLE
    if not governance.expansion_permitted:
        logger.debug("decomposition_forced_single", reason="expansion_denied")
        return DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="expansion_denied",
            planner_gate_reason=planner_gate_reason,
        )

    if governance.expansion_budget <= 0:
        logger.debug("decomposition_forced_single", reason="zero_budget")
        return DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="zero_budget",
            planner_gate_reason=planner_gate_reason,
        )

    if not expansion_enabled:
        logger.debug("decomposition_forced_single", reason="expansion_disabled")
        return DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="expansion_disabled",
            planner_gate_reason=planner_gate_reason,
        )

    strategy, reason = _apply_matrix(
        intent.task_type,
        intent.complexity,
        delegation_enabled,
        planner_mode_declared,
    )
    logger.info(
        "decomposition_assessed",
        task_type=intent.task_type.value,
        complexity=intent.complexity.value,
        strategy=strategy.value,
        reason=reason,
        planner_gate_reason=planner_gate_reason,
    )

    return DecompositionResult(
        strategy=strategy,
        reason=reason,
        planner_gate_reason=planner_gate_reason,
    )


def _apply_matrix(
    task_type: TaskType,
    complexity: Complexity,
    delegation_enabled: bool,
    planner_mode_declared: bool,
) -> tuple[DecompositionStrategy, str]:
    """Apply the decomposition decision matrix.

    Args:
        task_type: Classified task type.
        complexity: Estimated complexity.
        delegation_enabled: Whether a delegation adapter is wired (FRE-1376).
        planner_mode_declared: Whether the session's primary deployment declares a
            ``planner`` mode (ADR-0154 D2).

    Returns:
        Tuple of (strategy, reason).
    """
    if planner_mode_declared and task_type in REGISTER_TASK_TYPES:
        # ADR-0154 D2: the planner decides, and it may decline (planner_may_decline).
        return DecompositionStrategy.HYBRID, "planner_asked"

    match task_type:
        case TaskType.CONVERSATIONAL:
            return DecompositionStrategy.SINGLE, "conversational_always_single"

        case TaskType.MEMORY_RECALL:
            return DecompositionStrategy.SINGLE, "memory_recall_always_single"

        case TaskType.SELF_IMPROVE:
            return DecompositionStrategy.SINGLE, "self_improve_always_single"

        case TaskType.DELEGATION:
            if delegation_enabled:
                return DecompositionStrategy.DELEGATE, "delegation_route_external"
            match complexity:
                case Complexity.SIMPLE:
                    return (
                        DecompositionStrategy.SINGLE,
                        "delegation_no_target_fallback_single",
                    )
                case Complexity.MODERATE:
                    return (
                        DecompositionStrategy.HYBRID,
                        "delegation_no_target_fallback_hybrid",
                    )
                case _:
                    return (
                        DecompositionStrategy.DECOMPOSE,
                        "delegation_no_target_fallback_decompose",
                    )

        case TaskType.TOOL_USE:
            return DecompositionStrategy.SINGLE, "tool_use_single"

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
