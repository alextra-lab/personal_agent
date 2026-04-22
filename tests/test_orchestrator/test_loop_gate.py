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
    """stable_hash returns the same value regardless of dict key ordering."""
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})


def test_stable_hash_length():
    """stable_hash always returns a 16-character hex string."""
    h = stable_hash("hello")
    assert len(h) == 16


def test_tool_fsm_initial_state():
    """A freshly created ToolFSM starts in IDLE with zero call counts."""
    fsm = ToolFSM()
    assert fsm.state == ToolCallState.IDLE
    assert fsm.total_calls == 0
    assert fsm.consecutive_count == 0


def test_tool_loop_policy_defaults():
    """ToolLoopPolicy default thresholds match the documented spec values."""
    p = ToolLoopPolicy()
    assert p.loop_max_per_signature == 1
    assert p.loop_max_consecutive == 3
    assert p.loop_output_sensitive is False


def test_gate_result_is_frozen():
    """GateResult is immutable — assigning to any field raises an error."""
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


def test_tool_loop_gate_instantiation():
    """ToolLoopGate starts with no FSMs registered and no last tool name."""
    gate = ToolLoopGate()
    assert len(gate._fsms) == 0
    assert gate._last_tool_name is None


# ── Identity signal tests ──────────────────────────────────────────────────


def test_first_call_is_allowed():
    """Gate allows the first call to any tool."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=1)
    result = gate.check_before("web_search", "hash_abc", policy)
    assert result.decision == GateDecision.ALLOW
    assert result.state_before == ToolCallState.IDLE
    assert result.state_after == ToolCallState.ACTIVE


def test_second_call_same_args_blocked_when_max_is_one():
    """Gate blocks second call with same args when max_per_signature=1."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=1)
    gate.check_before("web_search", "hash_abc", policy)
    result = gate.check_before("web_search", "hash_abc", policy)
    assert result.decision == GateDecision.BLOCK_IDENTITY
    assert result.state_after == ToolCallState.BLOCKED


def test_different_args_not_blocked_by_identity():
    """Gate allows different args for the same tool."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=1)
    gate.check_before("web_search", "hash_abc", policy)
    result = gate.check_before("web_search", "hash_xyz", policy)
    assert result.decision == GateDecision.ALLOW


def test_identity_respects_per_tool_max():
    """Gate respects loop_max_per_signature > 1."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=2)
    gate.check_before("run_sysdiag", "hash_same", policy)
    result2 = gate.check_before("run_sysdiag", "hash_same", policy)
    assert result2.decision == GateDecision.ALLOW  # second call within limit
    result3 = gate.check_before("run_sysdiag", "hash_same", policy)
    assert result3.decision == GateDecision.BLOCK_IDENTITY  # third call exceeds limit


def test_blocked_tool_stays_blocked():
    """Once blocked, tool stays blocked on subsequent calls."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=1)
    gate.check_before("web_search", "hash_abc", policy)
    gate.check_before("web_search", "hash_abc", policy)  # → BLOCKED
    result = gate.check_before("web_search", "hash_abc", policy)
    assert result.decision == GateDecision.BLOCK_IDENTITY
    assert result.state_after == ToolCallState.BLOCKED
