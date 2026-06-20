"""Tests for EVAL-04b occupancy curve helpers (FRE-577).

Covers the synthetic message builders, token estimation shapes,
threshold-crossing detection, and gateway drop simulation — all without
a live agent or Elasticsearch.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make src/ importable in the test runner
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "src"))

# Import the helpers under test from the eval script.  The script lives at
# scripts/eval_04b_occupancy_curve.py — add scripts/ to sys.path too.
scripts_dir = project_root / "scripts"
sys.path.insert(0, str(scripts_dir))

import eval_04b_occupancy_curve as _eval  # noqa: E402

# ---------------------------------------------------------------------------
# Synthetic message builders
# ---------------------------------------------------------------------------


class TestSyntheticMessages:
    def test_system_msg_role(self) -> None:
        msg = _eval._make_system_msg()
        assert msg["role"] == "system"
        assert len(msg["content"]) > 20

    def test_user_msg_role_and_rotates(self) -> None:
        msgs = [_eval._make_user_msg(t) for t in range(1, 12)]
        assert all(m["role"] == "user" for m in msgs)
        # Turn 1 and turn 11 should differ (10-item rotation)
        assert msgs[0]["content"] != msgs[10]["content"]

    def test_tool_call_msg_structure(self) -> None:
        msg = _eval._make_tool_call_msg(1)
        assert msg["role"] == "assistant"
        assert msg["content"] is None
        calls = msg["tool_calls"]
        assert isinstance(calls, list) and len(calls) == 1
        assert calls[0]["type"] == "function"
        assert calls[0]["function"]["name"] == "read_file"

    def test_tool_result_msg_structure(self) -> None:
        msg = _eval._make_tool_result_msg(1)
        assert msg["role"] == "tool"
        assert "tool_call_id" in msg
        assert len(msg["content"]) > 200  # large output

    def test_tool_result_ids_match(self) -> None:
        for t in range(1, 5):
            call = _eval._make_tool_call_msg(t)
            result = _eval._make_tool_result_msg(t)
            assert call["tool_calls"][0]["id"] == result["tool_call_id"]

    def test_assistant_response_msg(self) -> None:
        msg = _eval._make_assistant_response_msg(3)
        assert msg["role"] == "assistant"
        assert isinstance(msg["content"], str)
        assert len(msg["content"]) > 50


# ---------------------------------------------------------------------------
# Synthetic memory / tool def constants
# ---------------------------------------------------------------------------


class TestSyntheticConstants:
    def test_memory_slab_is_list_of_dicts(self) -> None:
        assert isinstance(_eval._SYNTHETIC_MEMORY_SLAB, list)
        assert all(isinstance(item, dict) for item in _eval._SYNTHETIC_MEMORY_SLAB)

    def test_tool_defs_have_name_and_description(self) -> None:
        for td in _eval._SYNTHETIC_TOOL_DEFS:
            assert "name" in td
            assert "description" in td
            assert "parameters" in td


# ---------------------------------------------------------------------------
# build_occupancy_curve
# ---------------------------------------------------------------------------


class TestBuildOccupancyCurve:
    def test_returns_nonempty_list(self) -> None:
        snaps = _eval.build_occupancy_curve(n_turns=5)
        assert len(snaps) >= 1

    def test_turn_indices_are_sequential(self) -> None:
        snaps = _eval.build_occupancy_curve(n_turns=5)
        for i, s in enumerate(snaps):
            assert s.turn == i + 1

    def test_tokens_grow_monotonically(self) -> None:
        snaps = _eval.build_occupancy_curve(n_turns=10)
        tokens = [s.messages_tokens for s in snaps]
        assert tokens == sorted(tokens), "Token count should grow with each turn"

    def test_gateway_total_exceeds_messages_tokens(self) -> None:
        """Gateway view includes memory + tool defs on top of messages."""
        snaps = _eval.build_occupancy_curve(n_turns=5)
        for s in snaps:
            assert s.gateway_total > s.messages_tokens

    def test_memory_tokens_constant_across_turns(self) -> None:
        snaps = _eval.build_occupancy_curve(n_turns=5)
        mem_values = {s.memory_tokens for s in snaps}
        assert len(mem_values) == 1, "Memory slab size is fixed for the session"

    def test_soft_threshold_eventually_crossed(self) -> None:
        snaps = _eval.build_occupancy_curve(n_turns=60)
        crossed = [s for s in snaps if s.soft_crossed]
        assert len(crossed) == 1, "Soft threshold should be crossed exactly once"
        assert crossed[0].messages_tokens >= _eval.SOFT_THRESHOLD

    def test_hard_threshold_eventually_crossed(self) -> None:
        # Growth ~1 130 tok/turn → hard (81 600) reached around turn 72; need ≥ 80.
        snaps = _eval.build_occupancy_curve(n_turns=120)
        crossed = [s for s in snaps if s.hard_crossed]
        assert len(crossed) == 1, "Hard threshold should be crossed exactly once"
        assert crossed[0].messages_tokens >= _eval.HARD_THRESHOLD

    def test_soft_fires_before_hard(self) -> None:
        snaps = _eval.build_occupancy_curve(n_turns=120)
        soft_turn = next(s.turn for s in snaps if s.soft_crossed)
        hard_turn = next(s.turn for s in snaps if s.hard_crossed)
        assert soft_turn < hard_turn

    def test_wsc_pct_matches_ratio(self) -> None:
        snaps = _eval.build_occupancy_curve(n_turns=3)
        for s in snaps:
            expected = s.messages_tokens / _eval.CONTEXT_WINDOW_MAX * 100
            assert abs(s.wsc_pct - expected) < 0.01

    def test_gateway_pct_matches_ratio(self) -> None:
        snaps = _eval.build_occupancy_curve(n_turns=3)
        for s in snaps:
            expected = s.gateway_total / _eval.GATEWAY_CEILING * 100
            assert abs(s.gateway_pct - expected) < 0.01

    def test_early_stop_after_both_ceilings_crossed(self) -> None:
        """Simulation stops early once both hard and gateway are crossed."""
        snaps = _eval.build_occupancy_curve(n_turns=200)
        # Should stop well before 200 turns once both ceilings are crossed
        assert len(snaps) < 150


# ---------------------------------------------------------------------------
# TurnSnapshot properties
# ---------------------------------------------------------------------------


class TestTurnSnapshot:
    def _make_snap(
        self,
        *,
        messages_tokens: int = 10_000,
        gateway_total: int = 15_000,
    ) -> _eval.TurnSnapshot:
        return _eval.TurnSnapshot(
            turn=1,
            message_count=10,
            messages_tokens=messages_tokens,
            memory_tokens=2_000,
            tool_def_tokens=3_000,
            gateway_total=gateway_total,
        )

    def test_wsc_pct_zero_for_empty(self) -> None:
        s = self._make_snap(messages_tokens=0)
        assert s.wsc_pct == 0.0

    def test_wsc_pct_100_at_ceiling(self) -> None:
        s = self._make_snap(messages_tokens=_eval.CONTEXT_WINDOW_MAX)
        assert abs(s.wsc_pct - 100.0) < 0.01

    def test_gateway_pct_100_at_ceiling(self) -> None:
        s = self._make_snap(gateway_total=_eval.GATEWAY_CEILING)
        assert abs(s.gateway_pct - 100.0) < 0.01


# ---------------------------------------------------------------------------
# Threshold ordering invariant
# ---------------------------------------------------------------------------


class TestThresholds:
    def test_soft_below_hard(self) -> None:
        assert _eval.SOFT_THRESHOLD < _eval.HARD_THRESHOLD

    def test_hard_below_gateway(self) -> None:
        assert _eval.HARD_THRESHOLD < _eval.GATEWAY_CEILING

    def test_soft_ratio_is_65pct(self) -> None:
        expected = int(0.65 * _eval.CONTEXT_WINDOW_MAX)
        assert _eval.SOFT_THRESHOLD == expected

    def test_hard_ratio_is_85pct(self) -> None:
        expected = int(0.85 * _eval.CONTEXT_WINDOW_MAX)
        assert _eval.HARD_THRESHOLD == expected


# ---------------------------------------------------------------------------
# Gateway drop simulation
# ---------------------------------------------------------------------------


class TestSimulateGatewayDrop:
    def test_under_budget_no_trim(self) -> None:
        results = _eval.simulate_gateway_drop([0.80])
        r = results[0]
        assert not r.trimmed
        assert r.tokens_shed == 0
        assert r.overflow_action is None
        assert r.phases_fired == []

    def test_over_budget_trims(self) -> None:
        results = _eval.simulate_gateway_drop([1.30])
        r = results[0]
        assert r.trimmed
        assert r.tokens_shed > 0
        assert r.overflow_action is not None

    def test_tokens_after_le_before(self) -> None:
        results = _eval.simulate_gateway_drop([0.80, 1.00, 1.30])
        for r in results:
            assert r.tokens_after <= r.tokens_before

    def test_tokens_shed_matches_before_minus_after(self) -> None:
        results = _eval.simulate_gateway_drop([1.10])
        r = results[0]
        assert r.tokens_shed == r.tokens_before - r.tokens_after

    def test_multiple_fill_levels_returned_in_order(self) -> None:
        levels = [0.80, 1.00, 1.30]
        results = _eval.simulate_gateway_drop(levels)
        assert len(results) == len(levels)
        for r, lvl in zip(results, levels):
            assert r.fill_pct == lvl


# ---------------------------------------------------------------------------
# _build_synthetic_context
# ---------------------------------------------------------------------------


class TestBuildSyntheticContext:
    def test_context_has_required_fields(self) -> None:
        ctx = _eval._build_synthetic_context(target_total_tokens=10_000)
        assert ctx.messages is not None
        assert ctx.memory_context is not None
        assert ctx.tool_definitions is not None

    def test_context_approaches_target(self) -> None:
        target = 20_000
        ctx = _eval._build_synthetic_context(target_total_tokens=target)
        # Should be within ~10% of target (we stop once we meet or exceed it)
        assert ctx.token_count >= target * 0.8

    def test_messages_start_with_system(self) -> None:
        ctx = _eval._build_synthetic_context(target_total_tokens=5_000)
        assert ctx.messages[0]["role"] == "system"
