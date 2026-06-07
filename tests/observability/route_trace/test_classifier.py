"""Unit tests for the programmatic orchestration-event classifier (FRE-452).

Asserts the honest programmatic floor (taxonomy §3 / §6): the classifier emits only
``primary_handled`` / ``delegate_called`` / ``fallback_triggered`` and never fabricates
the hybrid ``delegate_result_used`` / ``delegate_result_discarded`` labels.

Also covers the FRE-515 ``delegate_disposition_candidate`` read-time heuristic — the
candidate-grade triage signal for the hybrid used/discarded rubric (taxonomy §3.3/§3.4),
including frozen fixtures of the two ``fre453-baseline-02`` delegate rows.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from personal_agent.observability.route_trace.classifier import (
    classify_orchestration_event,
    delegate_disposition_candidate,
)
from personal_agent.observability.route_trace.types import RouteTraceRow
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


# ---------------------------------------------------------------------------
# FRE-515 — delegate_disposition_candidate (read-time, candidate-grade)
# ---------------------------------------------------------------------------


def _row(**overrides: object) -> RouteTraceRow:
    """Build a RouteTraceRow with delegate-shaped defaults; override per test."""
    defaults: dict[str, object] = dict(
        trace_id=uuid4(),
        session_id=uuid4(),
        orchestration_event="delegate_called",
        sub_agent_count=2,
        delegate_result_passed_to_synthesis=True,
        final_reply_chars=1200,
        error_type=None,
    )
    defaults.update(overrides)
    return RouteTraceRow(**defaults)  # type: ignore[arg-type]


def test_disposition_none_for_primary_handled() -> None:
    row = _row(orchestration_event="primary_handled", sub_agent_count=0)
    assert delegate_disposition_candidate(row) is None


def test_disposition_none_for_fallback_triggered() -> None:
    # fallback rows carry subs too, but fallback is its own terminal event (§3.5) —
    # disposition refinement applies only to the delegate_called floor.
    row = _row(orchestration_event="fallback_triggered", fallback_triggered=True)
    assert delegate_disposition_candidate(row) is None


def test_disposition_used_candidate_on_clean_synthesis() -> None:
    assert delegate_disposition_candidate(_row()) == "used_candidate"


def test_disposition_discarded_when_not_passed_to_synthesis() -> None:
    row = _row(delegate_result_passed_to_synthesis=False)
    assert delegate_disposition_candidate(row) == "discarded_candidate"


def test_disposition_discarded_on_turn_error() -> None:
    row = _row(error_type="LLMServerError")
    assert delegate_disposition_candidate(row) == "discarded_candidate"


def test_disposition_discarded_on_empty_reply() -> None:
    row = _row(final_reply_chars=0)
    assert delegate_disposition_candidate(row) == "discarded_candidate"


def test_baseline_tool_heavy_research_row_is_used_candidate() -> None:
    """Frozen fre453-baseline-02 fixture: 4/4 subs ok, 7403-char synthesis → used."""
    row = _row(
        sub_agent_count=4,
        decomposition_strategy="hybrid",
        delegate_result_passed_to_synthesis=True,
        final_reply_chars=7403,
        error_type=None,
        tool_iteration_count=14,
    )
    assert delegate_disposition_candidate(row) == "used_candidate"


def test_baseline_artifact_study_guide_row_is_discarded_candidate() -> None:
    """Frozen fre453-baseline-02 fixture: 3/4 subs ok but the turn died on a 524
    (error_type=LLMServerError) and the 501-char reply is an error apology — the
    §3.4 implicit-non-use shape.
    """
    row = _row(
        sub_agent_count=4,
        decomposition_strategy="hybrid",
        delegate_result_passed_to_synthesis=True,
        final_reply_chars=501,
        error_type="LLMServerError",
        error_class="model_server",
        tool_iteration_count=1,
    )
    assert delegate_disposition_candidate(row) == "discarded_candidate"
