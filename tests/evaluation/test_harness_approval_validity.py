"""FRE-1539 — the A/B harnesses keep a denied-approval turn out of their rates.

fre433 (cache A/B), fre475 (compression A/B) and fre481 (decomposition A/B) default to the
production gateway, where FRE-1535 denies every approval-capable tool call. Pure report logic only:
nothing here reaches a gateway or Elasticsearch.
"""

from __future__ import annotations

from scripts.eval.approval_denial import UNASSESSED, in_rates
from scripts.eval.fre433_cache_ab.harness import TurnMetric
from scripts.eval.fre433_cache_ab.harness import render_markdown as render_433
from scripts.eval.fre475_compression_ab.run_ab import BASELINE_FRESH, verdict_lines
from scripts.eval.fre481_decomposition_ab.harness import TurnReport
from scripts.eval.fre481_decomposition_ab.harness import render_markdown as render_481


def test_only_valid_and_unassessed_turns_are_in_a_rate() -> None:
    assert in_rates("valid") is True
    assert in_rates(UNASSESSED) is True
    assert in_rates("invalid") is False
    assert in_rates("unverified") is False


def _metric(turn: int, cache_read: int, validity: str = "valid") -> TurnMetric:
    return TurnMetric(
        session_label="s1",
        turn_index=turn,
        trace_id=f"t-{turn}",
        session_id="sess",
        endpoint="ep",
        model="m",
        input_tokens=5000,
        cache_read_tokens=cache_read,
        cache_creation_input_tokens=0,
        latency_ms=10,
        static_prefix_hash="a" * 12,
        dynamic_hash="b" * 12,
        primary_call_count=1,
        validity=validity,
        approval_summary="invalid: bash(approval_connection_lost)" if validity == "invalid" else "",
    )


_META_433 = {"run_id": "r", "arm": "tail", "profile": "cloud", "timestamp": "t"}


def test_fre433_reuse_rate_leaves_a_denied_turn_out_and_prints_the_invalid_count() -> None:
    """AC-3 for fre433: the denied turn would lift the rate to 2/3. It must not."""
    metrics = [
        _metric(1, 0),
        _metric(2, 4000),
        _metric(3, 0, validity="valid"),
        _metric(4, 4000, validity="invalid"),
    ]
    md = render_433(_META_433, metrics)
    assert "Cross-turn reuse (turn>=2): 1/2 turns had cache_read > 0." in md
    assert "invalid 1" in md
    assert "bash(approval_connection_lost)" in md


def test_fre433_a_reread_pass_with_no_verdicts_keeps_its_rate() -> None:
    metrics = [_metric(1, 0, UNASSESSED), _metric(2, 4000, UNASSESSED)]
    md = render_433(_META_433, metrics)
    assert "Cross-turn reuse (turn>=2): 1/1 turns had cache_read > 0." in md


def test_fre475_a_passing_number_on_an_invalid_turn_is_not_a_pass() -> None:
    """The reduction percentage is a rate. A denied turn must not report one."""
    fresh = int(BASELINE_FRESH * 0.5)
    reduction, verdict = verdict_lines(fresh, artifact=1, failed=0, validity="valid")
    assert verdict == "PASS"
    assert "50.0%" in reduction
    for validity in ("invalid", "unverified"):
        reduction, verdict = verdict_lines(fresh, artifact=1, failed=0, validity=validity)
        assert verdict == validity.upper()
        assert reduction.startswith("n/a")
        assert "%" not in reduction


def _report(label: str, validity: str) -> TurnReport:
    return TurnReport(
        label=label,
        trace_id="t",
        session_id="s",
        strategy="single",
        reason="r",
        intent_signals=[],
        rounds=[],
        round_count=3,
        max_parent_fresh_in=71000,
        total_input_tokens=100,
        total_cache_read_tokens=0,
        total_output_tokens=10,
        wall_time_s=1.0,
        subagent_iterations=0,
        subagent_completes=0,
        artifact_response_chars=5,
        artifact_id=None,
        validity=validity,
        approval_summary="invalid: bash(approval_connection_lost)",
    )


def test_fre481_denied_turn_shows_no_metrics_and_the_invalid_count_prints() -> None:
    meta = {"run_id": "r", "arm": "baseline", "profile": "cloud", "timestamp": "t"}
    md = render_481(meta, [_report("ok", "valid"), _report("denied", "invalid")])
    assert "valid 1 · invalid 1 · unverified 0" in md
    assert "bash(approval_connection_lost)" in md
    # the denied row carries no max_fresh_in, so it cannot be diffed across arms
    assert md.count("**71000**") == 1
