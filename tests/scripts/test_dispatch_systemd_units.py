# ruff: noqa: D103
"""Structural checks for the dispatch systemd units (FRE-788, ADR-0110 T4).

These assert the load-bearing directives the ADR acceptance criteria depend on
(AC-a: the RC service restarts on failure), not the whole unit — the live
`systemctl` behaviour is master's seam verification (ADR-0110 §345), which
requires the VPS + the owner's device.
"""

from __future__ import annotations

from pathlib import Path

_UNITS = Path("infrastructure/systemd")


def _read(name: str) -> str:
    return (_UNITS / name).read_text()


def test_rc_server_unit_restarts_on_failure() -> None:
    """AC-a: the Remote Control service restarts on failure."""
    unit = _read("claude-remote-control@.service")
    assert "Restart=always" in unit


def test_rc_server_unit_execs_the_wrapper_per_stream() -> None:
    unit = _read("claude-remote-control@.service")
    assert "scripts.dispatch.rc_server %i" in unit


def test_orchestrator_unit_runs_the_loop_and_restarts() -> None:
    unit = _read("seshat-dispatch-orchestrator.service")
    assert "--loop" in unit
    assert "Restart=always" in unit


def test_orchestrator_unit_preflights_before_starting() -> None:
    """The enable-once precondition gate runs before the loop (AC-b)."""
    unit = _read("seshat-dispatch-orchestrator.service")
    assert "--preflight" in unit
    assert "ExecStartPre" in unit
