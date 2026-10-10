"""Tests for scripts/check_tool_failure_coverage.py (ADR-0156 AC-1, FRE-1575)."""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest
from scripts.check_tool_failure_coverage import executed_lines, failure_lines, uncovered

_SOURCE = textwrap.dedent(
    '''
    from typing import Literal

    from personal_agent.tools.contract import ToolError, dispatch_error

    ProbeCode = Literal["bad"]


    class ProbeError(ToolError[ProbeCode]):
        """Probe."""


    def _refuse(reason: str) -> ProbeError:
        return ProbeError("bad", reason, retryable=False, next_action="none")


    def tool(flag: int) -> dict[str, int]:
        if flag == 1:
            raise ProbeError(
                "bad",
                "one",
                retryable=False,
                next_action="none",
            )
        if flag == 2:
            raise _refuse("two")
        if flag == 3:
            detail = dispatch_error("timeout", "three", retryable=True, next_action="again")
            return {"code": len(detail.code)}
        return {"value": flag}
    '''
)


@pytest.fixture
def probe_module(tmp_path: Path) -> ModuleType:
    """Import a small tool module written for the test."""
    path = tmp_path / "probe_tool.py"
    path.write_text(_SOURCE)
    spec = importlib.util.spec_from_file_location("probe_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_tool"] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules["probe_tool"]


def _line_of(module: ModuleType, text: str) -> int:
    source = Path(module.__file__ or "").read_text().splitlines()
    return next(i for i, line in enumerate(source, start=1) if text in line)


def test_it_finds_every_failure_building_statement(probe_module: ModuleType) -> None:
    """A typed raise, a helper returning a ToolError, and dispatch_error each count once."""
    found = {(line.line, line.builder) for line in failure_lines(probe_module)}
    assert found == {
        (_line_of(probe_module, 'return ProbeError("bad", reason'), "ProbeError"),
        (_line_of(probe_module, "raise ProbeError("), "ProbeError"),
        (_line_of(probe_module, 'raise _refuse("two")'), "_refuse"),
        (_line_of(probe_module, "detail = dispatch_error("), "dispatch_error"),
    }


def test_seeded_negative_an_unexecuted_failure_line_is_reported(probe_module: ModuleType) -> None:
    """A failure line missing from the report's executed lines is listed."""
    lines = failure_lines(probe_module)
    path = lines[0].path
    raise_line = _line_of(probe_module, "raise ProbeError(")
    every_line_but_one = frozenset(line.line for line in lines) - {raise_line}
    missing = uncovered(lines, {path: every_line_but_one})
    assert [line.line for line in missing] == [raise_line]


def test_a_fully_executed_module_passes(probe_module: ModuleType) -> None:
    """With every failure line executed, nothing is reported."""
    lines = failure_lines(probe_module)
    assert uncovered(lines, {lines[0].path: frozenset(line.line for line in lines)}) == []


def test_a_file_missing_from_the_report_counts_as_unexecuted(probe_module: ModuleType) -> None:
    """A module the test run never imported is not silently skipped."""
    lines = failure_lines(probe_module)
    assert uncovered(lines, {}) == lines


def test_it_reads_relative_paths_from_a_coverage_report() -> None:
    """coverage.py writes paths relative to the run directory; they resolve to the repo."""
    report = {"files": {"src/personal_agent/tools/fetch.py": {"executed_lines": [3, 5]}}}
    executed = executed_lines(report)
    path = (Path(__file__).parents[2] / "src/personal_agent/tools/fetch.py").resolve()
    assert executed == {path: frozenset({3, 5})}
