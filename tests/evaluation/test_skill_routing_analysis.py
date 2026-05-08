"""Unit tests for skill_routing_analysis metric correctness (FRE-329).

All tests pass synthetic es_hits so no ES connection is required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ is not a package — add it to sys.path so the module is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "eval"))
from skill_routing_analysis import analyse_trace  # noqa: E402


def _trace(bash_command: str) -> dict:
    """Build minimal es_hits for a trace with one bash_started event."""
    return analyse_trace(
        "test-trace",
        es_hits=[{"event_type": "bash_started", "command": bash_command}],
    )


def _trace_no_bash() -> dict:
    """Build trace with no bash commands."""
    return analyse_trace("test-trace", es_hits=[])


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
