"""Tests for scripts/check_tool_failure_coverage.py (ADR-0156 AC-1, FRE-1575)."""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from scripts.check_tool_failure_coverage import (
    conditional,
    executed_lines,
    failure_lines,
    uncovered,
)

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


#: Builder calls that coverage.py credits whenever their statement runs, built or not.
_CONDITIONAL_SOURCE = textwrap.dedent(
    """
    from personal_agent.tools.contract import dispatch_error


    def fallback(detail: object | None) -> object:
        return detail or dispatch_error("timeout", "four", retryable=True, next_action="again")


    def choose(flag: bool) -> object:
        return None if flag else dispatch_error("timeout", "five", retryable=True, next_action="a")


    def later() -> object:
        return lambda: dispatch_error("timeout", "six", retryable=True, next_action="again")


    def first(other: object) -> object:
        return dispatch_error("timeout", "seven", retryable=True, next_action="again") or other
    """
)


def _load(tmp_path: Path, name: str, source: str) -> Iterator[ModuleType]:
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules[name]


@pytest.fixture
def probe_module(tmp_path: Path) -> Iterator[ModuleType]:
    """Import a small tool module written for the test."""
    yield from _load(tmp_path, "probe_tool", _SOURCE)


@pytest.fixture
def conditional_module(tmp_path: Path) -> Iterator[ModuleType]:
    """Import a module whose builders sit in conditional expressions."""
    yield from _load(tmp_path, "probe_conditional", _CONDITIONAL_SOURCE)


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


def test_a_plain_module_has_no_conditional_builder(probe_module: ModuleType) -> None:
    """A builder in its own statement is not reported as conditional."""
    assert conditional(failure_lines(probe_module)) == []


def test_seeded_negative_a_conditional_builder_is_reported(
    conditional_module: ModuleType,
) -> None:
    """An ``or`` fallback, a conditional branch and a lambda body are each reported.

    Coverage.py records lines per statement, so it credits these builders whenever the
    statement runs. The first operand of ``or`` always runs, so it is not reported.
    """
    reported = {line.line for line in conditional(failure_lines(conditional_module))}
    assert reported == {
        _line_of(conditional_module, '"four"'),
        _line_of(conditional_module, '"five"'),
        _line_of(conditional_module, '"six"'),
    }
