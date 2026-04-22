"""Unit tests for ToolLoopGate FSM-based loop detection."""

import pytest
from personal_agent.orchestrator.loop_gate import (
    GateDecision,
    GateResult,
    ToolCallState,
    ToolFSM,
    ToolLoopGate,
    ToolLoopPolicy,
    stable_hash,
)


def test_stable_hash_is_deterministic():
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})


def test_stable_hash_length():
    h = stable_hash("hello")
    assert len(h) == 16


def test_tool_fsm_initial_state():
    fsm = ToolFSM()
    assert fsm.state == ToolCallState.IDLE
    assert fsm.total_calls == 0
    assert fsm.consecutive_count == 0


def test_tool_loop_policy_defaults():
    p = ToolLoopPolicy()
    assert p.loop_max_per_signature == 1
    assert p.loop_max_consecutive == 3
    assert p.loop_output_sensitive is False


def test_gate_result_is_frozen():
    result = GateResult(
        decision=GateDecision.ALLOW,
        tool_name="test",
        state_before=ToolCallState.IDLE,
        state_after=ToolCallState.ACTIVE,
        reason="ok",
        consecutive_count=1,
        total_calls=1,
    )
    with pytest.raises((AttributeError, TypeError)):
        result.decision = GateDecision.BLOCK_IDENTITY  # type: ignore[misc]
