# ruff: noqa: D103
"""Unit tests for the RC server-mode launch wrapper (FRE-788, ADR-0110 T4).

The wrapper resolves a dispatch stream to its worktree and builds the
``claude remote-control`` server-mode argv the systemd unit execs. Pure
topology + argv are the meaningful contract (the systemd unit's ExecStart);
these tests assert that, not the CLI's incidental text output.
"""

from __future__ import annotations

import pytest
from scripts.dispatch.rc_server import main, rc_server_plan


def test_rc_server_plan_build1() -> None:
    worktree, argv = rc_server_plan("build1")
    assert worktree == ".claude/worktrees/build"
    assert argv == (
        "claude",
        "remote-control",
        "--spawn",
        "session",
        "--name",
        "seshat-build1",
    )


def test_rc_server_plan_build2() -> None:
    worktree, argv = rc_server_plan("build2")
    assert worktree == ".claude/worktrees/build2"
    assert argv[-1] == "seshat-build2"


def test_rc_server_plan_adr() -> None:
    worktree, argv = rc_server_plan("adr")
    assert worktree == ".claude/worktrees/adrs"
    assert argv[-1] == "seshat-adr"


def test_rc_server_plan_unknown_stream_raises() -> None:
    with pytest.raises(ValueError, match="unknown dispatch stream"):
        rc_server_plan("nope")


def test_main_dry_run_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["build1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert ".claude/worktrees/build" in out
    assert "remote-control" in out


def test_main_dry_run_unknown_stream_exits_two() -> None:
    assert main(["nope", "--dry-run"]) == 2
