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
from unittest.mock import patch

import pytest

from personal_agent.tools.primitives.bash import (
    _check_segment_allowlist,
    _is_hard_denied,
    _split_command_segments,
    bash_executor,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.get_event_loop().run_until_complete(coro)


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
# Executor integration tests — real /bin/bash
# ---------------------------------------------------------------------------


class TestBashShellContractExecution:
    """These tests verify that the executor actually runs a real shell."""

    def test_pipe_works(self) -> None:
        result = run(bash_executor("echo hello | tr 'a-z' 'A-Z'"))
        assert result["success"] is True
        assert "HELLO" in result["stdout"]

    def test_pipe_with_wc(self) -> None:
        result = run(bash_executor("printf 'a\\nb\\nc\\n' | wc -l"))
        assert result["success"] is True
        assert result["stdout"].strip() == "3"

    def test_logical_and(self) -> None:
        result = run(bash_executor("true && echo ok"))
        assert result["success"] is True
        assert "ok" in result["stdout"]

    def test_redirect_to_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "out.txt"
            result = run(bash_executor(f"echo written > {target}"))
            assert result["success"] is True
            assert target.exists()
            assert "written" in target.read_text()

    def test_glob_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "a.yaml").write_text("a")
            (Path(tmpdir) / "b.yaml").write_text("b")
            (Path(tmpdir) / "c.txt").write_text("c")
            result = run(bash_executor(f"ls {tmpdir}/*.yaml | wc -l"))
            assert result["success"] is True
            assert result["stdout"].strip() == "2"

    def test_pipefail_propagates_exit(self) -> None:
        # With pipefail, 'false | true' should exit 1, not 0.
        result = run(bash_executor("false | true"))
        assert result["success"] is False
        assert result["exit_code"] == 1

    def test_hard_deny_still_fires_in_pipeline(self) -> None:
        # rm -rf should be hard-denied even inside a pipeline.
        result = run(bash_executor("ls /tmp && rm -rf /tmp/nonexistent-test-target"))
        assert result["success"] is False
        assert result["error"] == "hard_denied"

    def test_empty_command_guard(self) -> None:
        result = run(bash_executor("   "))
        assert result["success"] is False
        assert result["error"] == "empty_command"

    def test_exit_code_propagated(self) -> None:
        result = run(bash_executor("exit 42"))
        assert result["exit_code"] == 42
        assert result["success"] is False
