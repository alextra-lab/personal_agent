"""Tests for the FRE-283 bash primitive real-shell contract.

Verifies that the bash executor runs commands through /bin/bash so that
shell features — pipes, logical operators, redirects, globs, pipefail — all
work as the bash tool description documents (ADR-0156 D5.6 folded docs/skills/bash.md
into it).

These tests run the actual executor (no subprocess mocking) and therefore
require /bin/bash to be available.  They do NOT require any infrastructure
services and carry no test marker — they are treated as fast unit tests.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from personal_agent.governance.permissions import (
    PermissionRules,
    evaluate_permission,
    load_permission_rules,
    parse_rule,
)
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.primitives.bash import BashError, _has_top_level_pipe, bash_executor
from personal_agent.tools.primitives.bash_allowlist import scan_segments

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


_CTX = TraceContext.new_trace()


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _split_command_segments(command: str) -> list[str]:
    return [segment.text for segment in scan_segments(command)]


def _check_segment_allowlist(command: str, allowlist: list[str]) -> str | None:
    """The refused subcommand under ``bash(<entry> *)`` allow rules, or ``None`` (FRE-1574)."""
    rules = PermissionRules(allow=tuple(parse_rule(f"bash({entry} *)") for entry in allowlist))
    decision = evaluate_permission("bash", {"command": command}, "NORMAL", rules)
    return None if decision.decision == "allow" else decision.refused


# ---------------------------------------------------------------------------
# Unit tests for _split_command_segments
# ---------------------------------------------------------------------------


class TestSplitCommandSegments:
    def test_simple_pipe(self) -> None:
        assert _split_command_segments("echo hello | tr a-z A-Z") == [
            "echo hello",
            "tr a-z A-Z",
        ]

    def test_logical_and(self) -> None:
        assert _split_command_segments("true && echo ok") == ["true", "echo ok"]

    def test_logical_or(self) -> None:
        assert _split_command_segments("false || echo fallback") == [
            "false",
            "echo fallback",
        ]

    def test_semicolon(self) -> None:
        assert _split_command_segments("echo a; echo b") == ["echo a", "echo b"]

    def test_quoted_pipe_not_split(self) -> None:
        result = _split_command_segments("echo 'hello | world'")
        assert result == ["echo 'hello | world'"]

    def test_double_quoted_pipe_not_split(self) -> None:
        result = _split_command_segments('grep "foo|bar" file.txt')
        assert result == ['grep "foo|bar" file.txt']

    def test_empty_command(self) -> None:
        assert _split_command_segments("") == []

    def test_whitespace_only(self) -> None:
        assert _split_command_segments("   ") == []

    def test_multi_pipe(self) -> None:
        result = _split_command_segments("cat file | grep foo | wc -l")
        assert result == ["cat file", "grep foo", "wc -l"]


# ---------------------------------------------------------------------------
# Unit tests for _check_segment_allowlist
# ---------------------------------------------------------------------------


class TestCheckSegmentAllowlist:
    ALLOWLIST = ["curl", "grep", "ls", "wc", "echo", "tr", "find", "ps", "cat"]

    def test_simple_allowed(self) -> None:
        assert _check_segment_allowlist("curl https://example.com", self.ALLOWLIST) is None

    def test_pipe_all_allowed(self) -> None:
        assert _check_segment_allowlist("echo hello | tr a-z A-Z", self.ALLOWLIST) is None

    def test_first_segment_not_allowed(self) -> None:
        result = _check_segment_allowlist("rm file.txt | echo done", self.ALLOWLIST)
        assert result is not None
        assert "rm" in result

    def test_second_segment_not_allowed(self) -> None:
        result = _check_segment_allowlist("echo hello | rm file.txt", self.ALLOWLIST)
        assert result is not None
        assert "rm" in result

    def test_multi_word_prefix(self) -> None:
        allowlist = ["docker ps", "curl", "grep"]
        assert _check_segment_allowlist("docker ps -a", allowlist) is None

    def test_multi_word_prefix_partial_no_match(self) -> None:
        allowlist = ["docker ps", "curl"]
        result = _check_segment_allowlist("docker logs mycontainer", allowlist)
        assert result is not None  # "docker logs" not in allowlist

    def test_empty_command_is_not_allowed(self) -> None:
        # FRE-1574: an empty command has no subcommand that an allow rule could match.
        assert _check_segment_allowlist("", self.ALLOWLIST) is not None


# ---------------------------------------------------------------------------
# Unit tests for _has_top_level_pipe
# ---------------------------------------------------------------------------


class TestHasTopLevelPipe:
    def test_simple_pipe(self) -> None:
        assert _has_top_level_pipe("find . | head") is True

    def test_no_pipe(self) -> None:
        assert _has_top_level_pipe("echo hello") is False

    def test_logical_or_is_not_a_pipe(self) -> None:
        assert _has_top_level_pipe("false || echo fallback") is False

    def test_single_quoted_pipe_not_detected(self) -> None:
        assert _has_top_level_pipe("echo 'a | b'") is False

    def test_double_quoted_pipe_not_detected(self) -> None:
        assert _has_top_level_pipe('grep "foo|bar" file.txt') is False

    def test_pipe_after_logical_or(self) -> None:
        assert _has_top_level_pipe("false || cat x | head") is True

    def test_empty_command(self) -> None:
        assert _has_top_level_pipe("") is False


# ---------------------------------------------------------------------------
# Executor integration tests — real /bin/bash
# ---------------------------------------------------------------------------


class TestBashShellContractExecution:
    """These tests verify that the executor actually runs a real shell."""

    def test_pipe_works(self) -> None:
        result = run(bash_executor("echo hello | tr 'a-z' 'A-Z'", ctx=_CTX))
        assert result["exit_code"] == 0
        assert "HELLO" in result["stdout"]

    def test_pipe_with_wc(self) -> None:
        result = run(bash_executor("printf 'a\\nb\\nc\\n' | wc -l", ctx=_CTX))
        assert result["exit_code"] == 0
        assert result["stdout"].strip() == "3"

    def test_logical_and(self) -> None:
        result = run(bash_executor("true && echo ok", ctx=_CTX))
        assert result["exit_code"] == 0
        assert "ok" in result["stdout"]

    def test_redirect_to_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "out.txt"
            result = run(bash_executor(f"echo written > {target}", ctx=_CTX))
            assert result["exit_code"] == 0
            assert target.exists()
            assert "written" in target.read_text()

    def test_glob_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "a.yaml").write_text("a")
            (Path(tmpdir) / "b.yaml").write_text("b")
            (Path(tmpdir) / "c.txt").write_text("c")
            result = run(bash_executor(f"ls {tmpdir}/*.yaml | wc -l", ctx=_CTX))
            assert result["exit_code"] == 0
            assert result["stdout"].strip() == "2"

    def test_pipefail_propagates_exit(self) -> None:
        # With pipefail, 'false | true' should exit 1, not 0.
        result = run(bash_executor("false | true", ctx=_CTX))
        assert result["exit_code"] == 1

    def test_deny_rule_still_fires_in_pipeline(self) -> None:
        # rm -rf is refused even inside a list (FRE-1574: a deny rule, not a regex).
        decision = evaluate_permission(
            "bash",
            {"command": "ls /tmp && rm -rf /tmp/nonexistent-test-target"},
            "NORMAL",
            load_permission_rules(),
        )
        assert decision.decision == "deny"
        assert decision.refused == "rm -rf /tmp/nonexistent-test-target"

    def test_empty_command_guard(self) -> None:
        with pytest.raises(BashError) as exc_info:
            run(bash_executor("   ", ctx=_CTX))
        assert exc_info.value.code == "empty_command"

    def test_exit_code_propagated(self) -> None:
        result = run(bash_executor("exit 42", ctx=_CTX))
        assert result["exit_code"] == 42


class TestBashSigpipeHandling:
    """FRE-470: SIGPIPE (exit 141) from a closing downstream pipe is benign.

    ``yes`` writes its argument forever; when a downstream consumer (``head``,
    ``grep -q``) takes what it needs and closes the pipe, ``yes`` is killed by
    SIGPIPE (signal 13 -> exit 128 + 13 = 141).  Under ``-o pipefail`` the shell
    surfaces 141 as the pipeline's exit code.  This is normal early-exit
    behavior, not a failure, so the executor returns it as a normal result with a ``note``.
    """

    def test_sigpipe_head_is_noted(self) -> None:
        result = run(bash_executor("yes | head -n 1", ctx=_CTX))
        assert result["exit_code"] == 141
        assert "y" in result["stdout"]
        assert result["note"] is not None
        assert "141" in result["note"]

    def test_sigpipe_grep_q_is_noted(self) -> None:
        # grep -q produces no stdout but exits 0 on first match, closing the
        # pipe early -> upstream `yes` dies of SIGPIPE -> pipefail surfaces 141.
        result = run(bash_executor("yes | grep -q y", ctx=_CTX))
        assert result["exit_code"] == 141
        assert result["note"] is not None

    def test_standalone_exit_141_still_fails(self) -> None:
        # No pipeline: an explicit 141 is not a benign SIGPIPE and must remain
        # a failure so genuine errors are not masked.
        result = run(bash_executor("exit 141", ctx=_CTX))
        assert result["exit_code"] == 141
        assert result["note"] is None

    def test_genuine_failure_still_fails(self) -> None:
        result = run(bash_executor("exit 1", ctx=_CTX))
        assert result["exit_code"] == 1
        assert result["note"] is None

    def test_successful_pipeline_has_no_note(self) -> None:
        result = run(bash_executor("echo hello | tr 'a-z' 'A-Z'", ctx=_CTX))
        assert result["exit_code"] == 0
        assert result["note"] is None
