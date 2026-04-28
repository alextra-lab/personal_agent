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
    assert p.loop_max_consecutive == 2  # tightened from 3 to align with ≤6 step budget (FRE-254)
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


def test_second_call_same_args_is_advisory_when_max_is_one():
    """Gate issues ADVISE_IDENTITY (advisory) on the 2nd call with max_per_signature=1."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=1)
    gate.check_before("web_search", "hash_abc", policy)
    result = gate.check_before("web_search", "hash_abc", policy)
    assert result.decision == GateDecision.ADVISE_IDENTITY
    # Advisory: FSM state stays ACTIVE, tool is still dispatched
    assert result.state_after == ToolCallState.ACTIVE


def test_identity_advisory_persists_until_terminal_threshold():
    """Gate keeps issuing ADVISE_IDENTITY until max+2 is exceeded (ADR-0063 §D5)."""
    gate = ToolLoopGate()
    # max=1 → advisory at count 2,3 (> 1 and <= 1+2=3); terminal at count 4 (> 3)
    policy = ToolLoopPolicy(loop_max_per_signature=1, loop_max_consecutive=10)
    gate.check_before("web_search", "hash_abc", policy)  # count=1, ALLOW
    r2 = gate.check_before("web_search", "hash_abc", policy)  # count=2, ADVISE
    assert r2.decision == GateDecision.ADVISE_IDENTITY
    r3 = gate.check_before("web_search", "hash_abc", policy)  # count=3, ADVISE
    assert r3.decision == GateDecision.ADVISE_IDENTITY


def test_identity_terminal_after_threshold_plus_two():
    """Gate blocks terminally once same args exceed max_per_signature + 2 (ADR-0063 §D5)."""
    gate = ToolLoopGate()
    # max=1, OFFSET=2 → terminal at count > 3, i.e. count=4+
    policy = ToolLoopPolicy(loop_max_per_signature=1, loop_max_consecutive=10)
    gate.check_before("web_search", "hash_abc", policy)  # 1 → ALLOW
    gate.check_before("web_search", "hash_abc", policy)  # 2 → ADVISE
    gate.check_before("web_search", "hash_abc", policy)  # 3 → ADVISE
    result = gate.check_before("web_search", "hash_abc", policy)  # 4 → BLOCK_IDENTITY
    assert result.decision == GateDecision.BLOCK_IDENTITY
    assert result.state_after == ToolCallState.BLOCKED


def test_blocked_tool_stays_blocked():
    """Once terminally blocked, tool returns BLOCK_IDENTITY on every subsequent call."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=1, loop_max_consecutive=10)
    for _ in range(4):  # reach terminal at count=4
        gate.check_before("web_search", "hash_abc", policy)
    result = gate.check_before("web_search", "hash_abc", policy)  # count=5, still BLOCKED
    assert result.decision == GateDecision.BLOCK_IDENTITY
    assert result.state_after == ToolCallState.BLOCKED


def test_advise_identity_does_not_set_blocked_state():
    """ADVISE_IDENTITY decisions must not transition the FSM to BLOCKED."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=1, loop_max_consecutive=10)
    gate.check_before("fetch_url", "hash_x", policy)  # ALLOW
    result = gate.check_before("fetch_url", "hash_x", policy)  # ADVISE
    assert result.decision == GateDecision.ADVISE_IDENTITY
    fsm = gate._fsms["fetch_url"]
    assert fsm.state != ToolCallState.BLOCKED  # advisory must not block the FSM


def test_different_args_not_blocked_by_identity():
    """Gate allows different args for the same tool."""
    gate = ToolLoopGate()
    # Use high consecutive limit so only identity blocking is tested here.
    policy = ToolLoopPolicy(loop_max_per_signature=1, loop_max_consecutive=10)
    gate.check_before("web_search", "hash_abc", policy)
    result = gate.check_before("web_search", "hash_xyz", policy)
    assert result.decision == GateDecision.ALLOW


def test_identity_respects_per_tool_max():
    """Gate respects loop_max_per_signature > 1: advisory fires only above max."""
    gate = ToolLoopGate()
    # Use high consecutive limit so only identity blocking is tested here.
    # max=2 → calls 1,2 ALLOW; call 3 ADVISE_IDENTITY (count=3 > 2)
    policy = ToolLoopPolicy(loop_max_per_signature=2, loop_max_consecutive=10)
    gate.check_before("run_sysdiag", "hash_same", policy)
    result2 = gate.check_before("run_sysdiag", "hash_same", policy)
    assert result2.decision == GateDecision.ALLOW  # second call within limit
    result3 = gate.check_before("run_sysdiag", "hash_same", policy)
    assert result3.decision == GateDecision.ADVISE_IDENTITY  # third call: advisory (not terminal)


# ── Consecutive signal tests ───────────────────────────────────────────────


def test_consecutive_warn_at_threshold():
    """Gate issues WARN_CONSECUTIVE when same tool reaches loop_max_consecutive calls."""
    gate = ToolLoopGate()
    # max_consecutive=2: WARN fires on the 2nd consecutive call
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2)
    gate.check_before("run_sysdiag", "hash_a", policy)  # consecutive=1, ALLOW
    result = gate.check_before("run_sysdiag", "hash_b", policy)  # consecutive=2, WARN
    assert result.decision == GateDecision.WARN_CONSECUTIVE
    # ADR-0063 §D5: consecutive is advisory only — FSM stays ACTIVE, tool is dispatched
    assert result.state_after == ToolCallState.ACTIVE


def test_consecutive_warn_repeated_above_threshold():
    """Gate keeps issuing WARN_CONSECUTIVE on calls above threshold — never blocks."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2)
    gate.check_before("run_sysdiag", "hash_a", policy)
    gate.check_before("run_sysdiag", "hash_b", policy)  # → WARN
    result = gate.check_before("run_sysdiag", "hash_c", policy)  # → WARN again, not BLOCK
    assert result.decision == GateDecision.WARN_CONSECUTIVE
    assert result.state_after == ToolCallState.ACTIVE


def test_consecutive_never_terminally_blocks_when_flag_false():
    """Repeated consecutive calls (even 20) never produce a terminal BLOCK when flag is off."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=100, loop_max_consecutive=2, loop_consecutive_terminal=False)
    for i in range(20):
        result = gate.check_before("read_file", f"hash_{i}", policy)
        # Each call past threshold is advisory, never terminal
        assert result.decision in (GateDecision.ALLOW, GateDecision.WARN_CONSECUTIVE)
        assert result.state_after != ToolCallState.BLOCKED


def test_consecutive_counter_resets_when_different_tool_runs():
    """Calling a different tool resets the consecutive counter."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2)
    gate.check_before("run_sysdiag", "hash_a", policy)
    gate.check_before("run_sysdiag", "hash_b", policy)  # → WARN_CONSECUTIVE
    gate.check_before("web_search", "hash_q", ToolLoopPolicy())  # different tool
    result = gate.check_before("run_sysdiag", "hash_c", policy)  # consecutive=1, ALLOW
    assert result.decision == GateDecision.ALLOW
    assert result.state_after == ToolCallState.ACTIVE


def test_two_tools_alternating_do_not_trigger_consecutive():
    """Alternating between two tools never triggers consecutive warning."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2)
    for i in range(5):
        r1 = gate.check_before("tool_a", f"hash_{i}a", policy)
        r2 = gate.check_before("tool_b", f"hash_{i}b", policy)
        assert r1.decision == GateDecision.ALLOW
        assert r2.decision == GateDecision.ALLOW


def test_consecutive_terminal_when_flag_set():
    """BLOCK_CONSECUTIVE fires at threshold when loop_consecutive_terminal=True (different args each call)."""
    gate = ToolLoopGate()
    # With terminal=True, threshold hit on call #2 (consecutive_count=2 >= loop_max_consecutive=2)
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2, loop_consecutive_terminal=True)
    r1 = gate.check_before("query_elasticsearch", "hash_a", policy)  # consecutive=1, ALLOW
    assert r1.decision == GateDecision.ALLOW
    r2 = gate.check_before("query_elasticsearch", "hash_b", policy)  # consecutive=2, BLOCK_CONSECUTIVE
    assert r2.decision == GateDecision.BLOCK_CONSECUTIVE
    assert r2.state_after == ToolCallState.BLOCKED


def test_consecutive_terminal_default_false_preserves_warn():
    """Default flag=False (omitting the field) keeps advisory WARN_CONSECUTIVE behavior unchanged."""
    gate = ToolLoopGate()
    # No loop_consecutive_terminal kwarg → defaults to False
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2)
    gate.check_before("query_elasticsearch", "hash_a", policy)
    r2 = gate.check_before("query_elasticsearch", "hash_b", policy)
    # Must remain advisory (ADR-0063 §D5 default preserved)
    assert r2.decision == GateDecision.WARN_CONSECUTIVE
    assert r2.state_after != ToolCallState.BLOCKED


def test_consecutive_terminal_blocked_tool_stays_blocked():
    """Once BLOCK_CONSECUTIVE fires, every subsequent call for that tool is also terminal."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2, loop_consecutive_terminal=True)
    gate.check_before("run_python", "hash_a", policy)  # ALLOW
    gate.check_before("run_python", "hash_b", policy)  # BLOCK_CONSECUTIVE (consecutive=2)
    # All further calls (different args) also terminate — consecutive_count stays >= threshold
    r3 = gate.check_before("run_python", "hash_c", policy)
    assert r3.decision == GateDecision.BLOCK_CONSECUTIVE
    assert r3.state_after == ToolCallState.BLOCKED
    r4 = gate.check_before("run_python", "hash_d", policy)
    assert r4.decision == GateDecision.BLOCK_CONSECUTIVE


def test_gate_result_includes_consecutive_count():
    """GateResult.consecutive_count reflects the current call's consecutive depth."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=5)
    for i in range(3):
        gate.check_before("run_sysdiag", f"hash_{i}", policy)
    result = gate.check_before("run_sysdiag", "hash_3", policy)
    assert result.consecutive_count == 4


# ── Output identity signal tests ───────────────────────────────────────────


def test_record_output_does_not_raise():
    """record_output completes without error after a check_before call."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=5)
    gate.check_before("self_telemetry_query", "hash_args", policy)
    gate.record_output("self_telemetry_query", "hash_args", "hash_out_1", policy)  # no error


def test_block_output_identity_after_two_identical_outputs():
    """Gate blocks on third call when prior two calls produced identical output."""
    gate = ToolLoopGate()
    # max_per_signature=5 so identity won't block; output_sensitive=False (default)
    policy = ToolLoopPolicy(loop_max_per_signature=5, loop_max_consecutive=10)
    # Call 1
    gate.check_before("query_es", "hash_args", policy)
    gate.record_output("query_es", "hash_args", "out_same", policy)
    # Call 2
    gate.check_before("query_es", "hash_args", policy)
    gate.record_output("query_es", "hash_args", "out_same", policy)  # identical!
    # Call 3 — output-identity should block
    result = gate.check_before("query_es", "hash_args", policy)
    assert result.decision == GateDecision.BLOCK_OUTPUT
    assert result.state_after == ToolCallState.BLOCKED


def test_different_outputs_do_not_trigger_output_block():
    """Gate does not block when outputs differ between calls."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=5, loop_max_consecutive=10)
    gate.check_before("query_es", "hash_args", policy)
    gate.record_output("query_es", "hash_args", "out_1", policy)
    gate.check_before("query_es", "hash_args", policy)
    gate.record_output("query_es", "hash_args", "out_2", policy)  # different!
    result = gate.check_before("query_es", "hash_args", policy)
    assert result.decision != GateDecision.BLOCK_OUTPUT


def test_output_sensitive_bypasses_output_identity_block():
    """loop_output_sensitive=True skips output-identity blocking."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(
        loop_max_per_signature=5,
        loop_max_consecutive=10,
        loop_output_sensitive=True,
    )
    gate.check_before("run_sysdiag", "hash_args", policy)
    gate.record_output("run_sysdiag", "hash_args", "out_same", policy)
    gate.check_before("run_sysdiag", "hash_args", policy)
    gate.record_output("run_sysdiag", "hash_args", "out_same", policy)
    result = gate.check_before("run_sysdiag", "hash_args", policy)
    # Should NOT be BLOCK_OUTPUT — output_sensitive=True bypasses this check
    assert result.decision != GateDecision.BLOCK_OUTPUT


def test_output_sensitive_still_records_for_telemetry():
    """record_output always stores hashes even for output_sensitive=True tools."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=5, loop_output_sensitive=True)
    gate.check_before("run_sysdiag", "hash_args", policy)
    gate.record_output("run_sysdiag", "hash_args", "out_hash", policy)
    fsm = gate._fsms["run_sysdiag"]
    assert "hash_args" in fsm.output_history
    assert fsm.output_history["hash_args"] == ["out_hash"]


# ── Integration-style tests ────────────────────────────────────────────────


def test_full_request_scenario_self_telemetry():
    """Simulates self_telemetry_query with max=2: 3rd same-args call is advisory."""
    gate = ToolLoopGate()
    # Use loop_max_consecutive=10 so consecutive blocking doesn't interfere.
    # max=2 → advisory at count 3,4; terminal at count 5+
    policy = ToolLoopPolicy(loop_max_per_signature=2, loop_output_sensitive=True, loop_max_consecutive=10)
    args_hash = stable_hash({"query_type": "health"})

    r1 = gate.check_before("self_telemetry_query", args_hash, policy)
    assert r1.decision == GateDecision.ALLOW
    gate.record_output("self_telemetry_query", args_hash, stable_hash({"status": "ok"}), policy)

    r2 = gate.check_before("self_telemetry_query", args_hash, policy)
    assert r2.decision == GateDecision.ALLOW
    gate.record_output("self_telemetry_query", args_hash, stable_hash({"status": "ok"}), policy)

    r3 = gate.check_before("self_telemetry_query", args_hash, policy)
    # ADR-0063 §D5: 3rd call (count=3 > max=2) is advisory, not terminal
    assert r3.decision == GateDecision.ADVISE_IDENTITY


def test_consecutive_warn_then_synthesis_via_different_tool():
    """After WARN, calling a different tool resets the FSM to ACTIVE."""
    gate = ToolLoopGate()
    diag_policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2)
    search_policy = ToolLoopPolicy()

    gate.check_before("run_sysdiag", "h1", diag_policy)
    r_warn = gate.check_before("run_sysdiag", "h2", diag_policy)
    assert r_warn.decision == GateDecision.WARN_CONSECUTIVE

    gate.check_before("web_search", "hq", search_policy)  # different tool

    r_resume = gate.check_before("run_sysdiag", "h3", diag_policy)
    assert r_resume.decision == GateDecision.ALLOW  # consecutive reset


def test_gate_result_fields_are_complete():
    """GateResult always has all fields populated with meaningful values."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy()
    result = gate.check_before("web_search", stable_hash({"q": "test"}), policy)
    assert result.tool_name == "web_search"
    assert result.consecutive_count >= 1
    assert result.total_calls >= 1
    assert result.reason != ""
