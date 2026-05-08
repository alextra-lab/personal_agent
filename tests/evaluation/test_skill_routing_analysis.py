"""Unit tests for skill_routing_analysis metric correctness (FRE-329 + FRE-331).

All tests pass synthetic es_hits so no ES connection is required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ is not a package — add it to sys.path so the module is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "eval"))
from skill_routing_analysis import _classify_success, analyse_trace  # noqa: E402


def _trace(bash_command: str, *, ground_truth: dict | None = None) -> dict:
    """Build minimal es_hits for a trace with one bash_started event."""
    return analyse_trace(
        "test-trace",
        es_hits=[{"event_type": "bash_started", "command": bash_command}],
        ground_truth=ground_truth,
    )


def _trace_no_bash(*, ground_truth: dict | None = None) -> dict:
    """Build trace with no bash commands."""
    return analyse_trace("test-trace", es_hits=[], ground_truth=ground_truth)


def _trace_with_events(
    events: list[dict],
    *,
    ground_truth: dict | None = None,
) -> dict:
    """Build trace from arbitrary es_hits."""
    return analyse_trace("test-trace", es_hits=events, ground_truth=ground_truth)


# ---------------------------------------------------------------------------
# es_first_call_correct_rate metric (FRE-329 bug fix)
# ---------------------------------------------------------------------------


class TestFirstBashUsesCorrectIndex:
    """Verify the AND-NOT logic introduced in FRE-329.

    Old (buggy) logic: `"agent-logs-" in cmd or "/logs-*" not in cmd`
    New (correct) logic: `"agent-logs-" in cmd and "/logs-*" not in cmd`
    """

    def test_correct_query_scores_true(self) -> None:
        """Querying agent-logs-* with no bad pattern → True."""
        r = _trace("curl http://elasticsearch:9200/agent-logs-*/_search -d '{}'")
        assert r["first_bash_uses_correct_index"] is True

    def test_bad_pattern_scores_false(self) -> None:
        """Querying /logs-* (hallucinated index) → False."""
        r = _trace("curl http://elasticsearch:9200/logs-*/_search")
        assert r["first_bash_uses_correct_index"] is False

    def test_unrelated_command_scores_false(self) -> None:
        """Generic command with neither pattern → False.

        This is the regression case from FRE-329: the old OR logic returned
        True because '/logs-*' was absent, making any unrelated command look
        like a correct ES query.
        """
        r = _trace("curl localhost:9200/_cat/indices")
        assert r["first_bash_uses_correct_index"] is False

    def test_ls_scores_false(self) -> None:
        """Plain shell command unrelated to ES → False (was True under old logic)."""
        r = _trace("ls /var/log")
        assert r["first_bash_uses_correct_index"] is False

    def test_grep_scores_false(self) -> None:
        """Grep on a local file — not an ES query → False."""
        r = _trace("grep errors /var/log/app.log")
        assert r["first_bash_uses_correct_index"] is False

    def test_both_patterns_present_scores_false(self) -> None:
        """Command containing both agent-logs- AND /logs-* → False (bad pattern wins)."""
        r = _trace("curl http://es:9200/agent-logs-*/_search && curl http://es:9200/logs-*/_count")
        assert r["first_bash_uses_correct_index"] is False

    def test_no_bash_command_scores_none(self) -> None:
        """No bash events → None (excluded from rate denominator)."""
        r = _trace_no_bash()
        assert r["first_bash_uses_correct_index"] is None
        assert r["first_bash_command"] == ""


# ---------------------------------------------------------------------------
# Router-only metrics (FRE-331)
# ---------------------------------------------------------------------------


class TestRouterRecallPrecision:
    """Verify router_recall and router_precision against ground truth."""

    def _routing_event(self, skills: list[str]) -> dict:
        return {
            "event_type": "skill_routing_call_completed",
            "skills_returned": skills,
            "latency_ms": 100,
        }

    def test_perfect_recall_precision(self) -> None:
        """Router returns exactly the expected skill → recall=1, precision=1."""
        gt = {"expected_router_skills": ["query-elasticsearch"], "forbidden_router_skills": []}
        r = _trace_with_events(
            [self._routing_event(["query-elasticsearch"])],
            ground_truth=gt,
        )
        assert r["router_recall"] == pytest.approx(1.0)
        assert r["router_precision"] == pytest.approx(1.0)

    def test_partial_recall(self) -> None:
        """Router returns 1 of 2 expected skills → recall=0.5."""
        gt = {
            "expected_router_skills": ["bash", "list-directory"],
            "forbidden_router_skills": [],
        }
        r = _trace_with_events(
            [self._routing_event(["bash"])],
            ground_truth=gt,
        )
        assert r["router_recall"] == pytest.approx(0.5)
        assert r["router_precision"] == pytest.approx(1.0)

    def test_extra_returned_skill_lowers_precision(self) -> None:
        """Router returns expected + extra skill → precision < 1."""
        gt = {"expected_router_skills": ["query-elasticsearch"], "forbidden_router_skills": []}
        r = _trace_with_events(
            [self._routing_event(["query-elasticsearch", "run-python"])],
            ground_truth=gt,
        )
        assert r["router_recall"] == pytest.approx(1.0)
        assert r["router_precision"] == pytest.approx(0.5)

    def test_router_empty_when_expected(self) -> None:
        """Router returns [] when skill is expected → recall=0, precision=0.

        precision = |returned ∩ expected| / max(1, |returned|) = 0 / max(1,0) = 0/1 = 0.
        """
        gt = {"expected_router_skills": ["neo4j-direct"], "forbidden_router_skills": []}
        r = _trace_with_events([self._routing_event([])], ground_truth=gt)
        assert r["router_recall"] == pytest.approx(0.0)
        assert r["router_precision"] == pytest.approx(0.0)

    def test_no_expected_skills_empty_return_is_correct(self) -> None:
        """No expected skills + router returns [] → recall=None, precision=1.0."""
        gt = {"expected_router_skills": [], "forbidden_router_skills": []}
        r = _trace_with_events([self._routing_event([])], ground_truth=gt)
        assert r["router_recall"] is None
        assert r["router_precision"] == pytest.approx(1.0)

    def test_no_expected_skills_router_returns_something(self) -> None:
        """No expected skills + router returns a skill → precision=0.0 (false positive)."""
        gt = {"expected_router_skills": [], "forbidden_router_skills": []}
        r = _trace_with_events(
            [self._routing_event(["query-elasticsearch"])],
            ground_truth=gt,
        )
        assert r["router_recall"] is None
        assert r["router_precision"] == pytest.approx(0.0)

    def test_forbidden_skill_detected(self) -> None:
        """Router returns a forbidden skill → router_has_forbidden=True."""
        gt = {
            "expected_router_skills": [],
            "forbidden_router_skills": ["query-elasticsearch"],
        }
        r = _trace_with_events(
            [self._routing_event(["query-elasticsearch"])],
            ground_truth=gt,
        )
        assert r["router_has_forbidden"] is True

    def test_no_forbidden_skill(self) -> None:
        """Router returns a safe skill → router_has_forbidden=False."""
        gt = {
            "expected_router_skills": ["run-python"],
            "forbidden_router_skills": ["query-elasticsearch"],
        }
        r = _trace_with_events(
            [self._routing_event(["run-python"])],
            ground_truth=gt,
        )
        assert r["router_has_forbidden"] is False

    def test_no_ground_truth_returns_none(self) -> None:
        """Without ground truth, router metrics are all None."""
        r = _trace("ls /tmp")
        assert r["router_recall"] is None
        assert r["router_precision"] is None
        assert r["router_empty"] is None
        assert r["router_has_forbidden"] is None


# ---------------------------------------------------------------------------
# Success-class classification (FRE-331)
# ---------------------------------------------------------------------------


class TestSuccessClass:
    """Verify the 4-way success_class classification."""

    def test_iteration_limit_is_failed(self) -> None:
        """Iteration limit reached → failed regardless of anything else."""
        trace = {
            "tool_iteration_limit_reached": True,
            "guard_blocks": 0,
            "routing_skills_returned": ["query-elasticsearch"],
            "read_skill_names": [],
        }
        assert _classify_success(trace, None) == "failed"

    def test_guard_block_is_guard_saved(self) -> None:
        """Guard fired, no iteration limit → guard_saved."""
        trace = {
            "tool_iteration_limit_reached": False,
            "guard_blocks": 1,
            "routing_skills_returned": [],
            "read_skill_names": [],
        }
        gt = {"expected_router_skills": ["query-elasticsearch"], "forbidden_router_skills": []}
        assert _classify_success(trace, gt) == "guard_saved"

    def test_router_hit_is_clean_success(self) -> None:
        """Router returned expected skill, no limit → clean_success."""
        trace = {
            "tool_iteration_limit_reached": False,
            "guard_blocks": 0,
            "routing_skills_returned": ["query-elasticsearch"],
            "read_skill_names": [],
        }
        gt = {"expected_router_skills": ["query-elasticsearch"], "forbidden_router_skills": []}
        assert _classify_success(trace, gt) == "clean_success"

    def test_router_miss_with_read_skill_recovery_is_recovered(self) -> None:
        """Router returned [], primary fetched skill via read_skill → recovered_success."""
        trace = {
            "tool_iteration_limit_reached": False,
            "guard_blocks": 0,
            "routing_skills_returned": [],
            "read_skill_names": ["query-elasticsearch"],
        }
        gt = {"expected_router_skills": ["query-elasticsearch"], "forbidden_router_skills": []}
        assert _classify_success(trace, gt) == "recovered_success"

    def test_router_miss_no_recovery_is_failed(self) -> None:
        """Router missed AND primary never fetched the skill → failed."""
        trace = {
            "tool_iteration_limit_reached": False,
            "guard_blocks": 0,
            "routing_skills_returned": [],
            "read_skill_names": [],
        }
        gt = {"expected_router_skills": ["query-elasticsearch"], "forbidden_router_skills": []}
        assert _classify_success(trace, gt) == "failed"

    def test_no_expected_skills_no_limit_is_clean(self) -> None:
        """Baseline prompt (no skills needed), no limit → clean_success."""
        trace = {
            "tool_iteration_limit_reached": False,
            "guard_blocks": 0,
            "routing_skills_returned": [],
            "read_skill_names": [],
        }
        gt = {"expected_router_skills": [], "forbidden_router_skills": []}
        assert _classify_success(trace, gt) == "clean_success"

    def test_no_ground_truth_no_limit_is_clean(self) -> None:
        """No ground truth, no iteration limit → clean_success (default)."""
        trace = {
            "tool_iteration_limit_reached": False,
            "guard_blocks": 0,
            "routing_skills_returned": [],
            "read_skill_names": [],
        }
        assert _classify_success(trace, None) == "clean_success"

    def test_guard_takes_priority_over_router_miss(self) -> None:
        """Guard fired + router missed: guard_saved wins over failed."""
        trace = {
            "tool_iteration_limit_reached": False,
            "guard_blocks": 2,
            "routing_skills_returned": [],
            "read_skill_names": [],
        }
        gt = {"expected_router_skills": ["query-elasticsearch"], "forbidden_router_skills": []}
        assert _classify_success(trace, gt) == "guard_saved"

    def test_iteration_limit_beats_guard(self) -> None:
        """Iteration limit reached even when guard fired → failed (limit wins)."""
        trace = {
            "tool_iteration_limit_reached": True,
            "guard_blocks": 3,
            "routing_skills_returned": [],
            "read_skill_names": [],
        }
        assert _classify_success(trace, None) == "failed"


# ---------------------------------------------------------------------------
# read_skill 3-bucket metrics (FRE-331)
# ---------------------------------------------------------------------------


class TestReadSkill3Bucket:
    """Verify the three read_skill bucket flags."""

    def _events(self, read_skills: list[str], routing_skills: list[str] | None = None) -> list[dict]:
        events: list[dict] = [
            {"event_type": "read_skill_invoked", "skill_name": s} for s in read_skills
        ]
        if routing_skills is not None:
            events.append({
                "event_type": "skill_routing_call_completed",
                "skills_returned": routing_skills,
                "latency_ms": 50,
            })
        return events

    def test_needed_and_invoked(self) -> None:
        """Expected skill fetched via read_skill → needed_and_invoked=True."""
        gt = {"expected_router_skills": ["query-elasticsearch"], "forbidden_router_skills": []}
        r = _trace_with_events(self._events(["query-elasticsearch"]), ground_truth=gt)
        assert r["read_skill_needed_and_invoked"] is True
        assert r["read_skill_needed_but_not_invoked"] is False
        assert r["read_skill_not_needed_but_invoked"] is False

    def test_needed_but_not_invoked(self) -> None:
        """Expected skill never fetched, router also missed → needed_but_not_invoked=True."""
        gt = {"expected_router_skills": ["neo4j-direct"], "forbidden_router_skills": []}
        r = _trace_with_events(self._events([]), ground_truth=gt)
        assert r["read_skill_needed_and_invoked"] is False
        assert r["read_skill_needed_but_not_invoked"] is True

    def test_not_needed_but_invoked(self) -> None:
        """Primary fetches unexpected skill → not_needed_but_invoked=True."""
        gt = {"expected_router_skills": ["run-python"], "forbidden_router_skills": []}
        # router loaded run-python; primary also fetched neo4j-direct unexpectedly
        r = _trace_with_events(
            self._events(["neo4j-direct"], routing_skills=["run-python"]),
            ground_truth=gt,
        )
        assert r["read_skill_not_needed_but_invoked"] is True

    def test_router_loaded_expected_skill_no_read_needed(self) -> None:
        """Router pre-loaded expected skill → needed_but_not_invoked=False (router covered it)."""
        gt = {"expected_router_skills": ["query-elasticsearch"], "forbidden_router_skills": []}
        r = _trace_with_events(
            self._events([], routing_skills=["query-elasticsearch"]),
            ground_truth=gt,
        )
        # Router loaded it, so 'not invoked' flag should be False
        assert r["read_skill_needed_but_not_invoked"] is False

    def test_no_ground_truth_buckets_are_none(self) -> None:
        """Without ground truth all 3 bucket flags are None."""
        r = _trace_with_events(self._events(["query-elasticsearch"]))
        assert r["read_skill_needed_and_invoked"] is None
        assert r["read_skill_needed_but_not_invoked"] is None
        assert r["read_skill_not_needed_but_invoked"] is None
