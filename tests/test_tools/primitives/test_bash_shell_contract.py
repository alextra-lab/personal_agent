"""Tests for the FRE-283 bash primitive real-shell contract.

Verifies that the bash executor runs commands through /bin/bash so that
shell features — pipes, logical operators, redirects, globs, pipefail — all
work as documented in docs/skills/bash.md.

These tests run the actual executor (no subprocess mocking) and therefore
require /bin/bash to be available.  They do NOT require any infrastructure
services and carry no test marker — they are treated as fast unit tests.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.primitives.bash import (
    _check_segment_allowlist,
    _has_top_level_pipe,
    _split_command_segments,
    bash_executor,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


_CTX = TraceContext.new_trace()


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


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

    def test_empty_command_passes(self) -> None:
        assert _check_segment_allowlist("", self.ALLOWLIST) is None


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
        assert result["success"] is True
        assert "HELLO" in result["stdout"]

    def test_pipe_with_wc(self) -> None:
        result = run(bash_executor("printf 'a\\nb\\nc\\n' | wc -l", ctx=_CTX))
        assert result["success"] is True
        assert result["stdout"].strip() == "3"

    def test_logical_and(self) -> None:
        result = run(bash_executor("true && echo ok", ctx=_CTX))
        assert result["success"] is True
        assert "ok" in result["stdout"]

    def test_redirect_to_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "out.txt"
            result = run(bash_executor(f"echo written > {target}", ctx=_CTX))
            assert result["success"] is True
            assert target.exists()
            assert "written" in target.read_text()

    def test_glob_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "a.yaml").write_text("a")
            (Path(tmpdir) / "b.yaml").write_text("b")
            (Path(tmpdir) / "c.txt").write_text("c")
            result = run(bash_executor(f"ls {tmpdir}/*.yaml | wc -l", ctx=_CTX))
            assert result["success"] is True
            assert result["stdout"].strip() == "2"

    def test_pipefail_propagates_exit(self) -> None:
        # With pipefail, 'false | true' should exit 1, not 0.
        result = run(bash_executor("false | true", ctx=_CTX))
        assert result["success"] is False
        assert result["exit_code"] == 1

    def test_hard_deny_still_fires_in_pipeline(self) -> None:
        # rm -rf should be hard-denied even inside a pipeline.
        result = run(bash_executor("ls /tmp && rm -rf /tmp/nonexistent-test-target", ctx=_CTX))
        assert result["success"] is False
        assert result["error"] == "hard_denied"

    def test_empty_command_guard(self) -> None:
        result = run(bash_executor("   ", ctx=_CTX))
        assert result["success"] is False
        assert result["error"] == "empty_command"

    def test_exit_code_propagated(self) -> None:
        result = run(bash_executor("exit 42", ctx=_CTX))
        assert result["exit_code"] == 42
        assert result["success"] is False


class TestBashSigpipeHandling:
    """FRE-470: SIGPIPE (exit 141) from a closing downstream pipe is benign.

    ``yes`` writes its argument forever; when a downstream consumer (``head``,
    ``grep -q``) takes what it needs and closes the pipe, ``yes`` is killed by
    SIGPIPE (signal 13 -> exit 128 + 13 = 141).  Under ``-o pipefail`` the shell
    surfaces 141 as the pipeline's exit code.  This is normal early-exit
    behavior, not a failure, so the executor must report ``success: True``.
    """

    def test_sigpipe_head_reports_success(self) -> None:
        result = run(bash_executor("yes | head -n 1", ctx=_CTX))
        assert result["exit_code"] == 141
        assert result["success"] is True
        assert "y" in result["stdout"]
        assert result["note"] is not None
        assert "141" in result["note"]

    def test_sigpipe_grep_q_reports_success(self) -> None:
        # grep -q produces no stdout but exits 0 on first match, closing the
        # pipe early -> upstream `yes` dies of SIGPIPE -> pipefail surfaces 141.
        result = run(bash_executor("yes | grep -q y", ctx=_CTX))
        assert result["exit_code"] == 141
        assert result["success"] is True
        assert result["note"] is not None

    def test_standalone_exit_141_still_fails(self) -> None:
        # No pipeline: an explicit 141 is not a benign SIGPIPE and must remain
        # a failure so genuine errors are not masked.
        result = run(bash_executor("exit 141", ctx=_CTX))
        assert result["exit_code"] == 141
        assert result["success"] is False
        assert result["note"] is None

    def test_genuine_failure_still_fails(self) -> None:
        result = run(bash_executor("exit 1", ctx=_CTX))
        assert result["exit_code"] == 1
        assert result["success"] is False
        assert result["note"] is None

    def test_successful_pipeline_has_no_note(self) -> None:
        result = run(bash_executor("echo hello | tr 'a-z' 'A-Z'", ctx=_CTX))
        assert result["success"] is True
        assert result["note"] is None
