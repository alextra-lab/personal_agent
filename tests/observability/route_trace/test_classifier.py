"""Unit tests for the programmatic orchestration-event classifier (FRE-452).

Asserts the honest programmatic floor (taxonomy §3 / §6): the classifier emits only
``primary_handled`` / ``delegate_called`` / ``fallback_triggered`` and never fabricates
the hybrid ``delegate_result_used`` / ``delegate_result_discarded`` labels.
"""

from __future__ import annotations

from types import SimpleNamespace

from personal_agent.observability.route_trace.classifier import (
    classify_orchestration_event,
)

from personal_agent.orchestrator.expansion_types import ExpansionPhase, PhaseResult
from personal_agent.orchestrator.sub_agent_types import SubAgentResult


def _sub(success: bool = True) -> SubAgentResult:
    """Build a minimal SubAgentResult for classifier tests."""
    return SubAgentResult(
        task_id="t1",
        spec_task="do x",
        summary="s",
        full_output="full",
        tools_used=[],
        token_count=10,
        duration_ms=1.0,
        success=success,
    )


def _phase(success: bool = True) -> PhaseResult:
    """Build a minimal PhaseResult for classifier tests."""
    return PhaseResult(phase=ExpansionPhase.DISPATCH, duration_ms=1.0, success=success)


def test_no_subagents_is_primary_handled() -> None:
    ctx = SimpleNamespace(sub_agent_results=None, expansion_phase_results=[])
    assert classify_orchestration_event(ctx) == "primary_handled"


def test_empty_subagent_list_is_primary_handled() -> None:
    ctx = SimpleNamespace(sub_agent_results=[], expansion_phase_results=[])
    assert classify_orchestration_event(ctx) == "primary_handled"


def test_healthy_subagents_is_delegate_called() -> None:
    ctx = SimpleNamespace(
        sub_agent_results=[_sub(success=True), _sub(success=True)],
        expansion_phase_results=[_phase(success=True)],
    )
    assert classify_orchestration_event(ctx) == "delegate_called"


def test_all_subagents_failed_is_fallback() -> None:
    ctx = SimpleNamespace(
        sub_agent_results=[_sub(success=False), _sub(success=False)],
        expansion_phase_results=[],
    )
    assert classify_orchestration_event(ctx) == "fallback_triggered"


def test_phase_failure_is_fallback() -> None:
    ctx = SimpleNamespace(
        sub_agent_results=[_sub(success=True)],
        expansion_phase_results=[_phase(success=False)],
    )
    assert classify_orchestration_event(ctx) == "fallback_triggered"


def test_partial_subagent_failure_is_delegate_called() -> None:
    # Some succeeded → primary did not fully fall back → delegate_called (floor).
    ctx = SimpleNamespace(
        sub_agent_results=[_sub(success=True), _sub(success=False)],
        expansion_phase_results=[],
    )
    assert classify_orchestration_event(ctx) == "delegate_called"


def test_missing_expansion_attrs_tolerated() -> None:
    # Pre-expansion / minimal ctx: no attrs set → primary_handled, no exception.
    ctx = SimpleNamespace(sub_agent_results=None, expansion_phase_results=None)
    assert classify_orchestration_event(ctx) == "primary_handled"
