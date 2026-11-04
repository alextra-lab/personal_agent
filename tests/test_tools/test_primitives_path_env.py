"""Environment variables in a model's path stay literal text (FRE-1729).

Each call runs through the real tool execution layer, the real permission rules and the real
path rules, in a new directory under ``/tmp``, which those rules allow.

AC-1: ``read``, ``write`` and ``edit`` treat ``$NAME`` and ``${NAME}`` as literal text, and the
variable's value is in no tool result and no log line. The seeded negative shows that the same
detector finds the value when the old expansion is back.
AC-2: an operator's path pattern that names a variable still expands.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Iterator, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import GovernanceConfig, Mode, ToolPolicy
from personal_agent.telemetry import TraceContext
from personal_agent.tools.executor import (
    ToolExecutionLayer,
    _validate_path_against_patterns,
)
from personal_agent.tools.primitives._governance import PathRefusalError, _matches_any
from personal_agent.tools.primitives.edit import edit_executor, edit_tool
from personal_agent.tools.primitives.read import read_executor, read_tool
from personal_agent.tools.primitives.write import _is_unattended_path, write_executor, write_tool
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolResult
from tests._helpers.log_capture import capture_log_records

_NAME = "FRE1729_CANARY"
_VALUE = "fre1729-canary-value"
_FORMS = (f"${_NAME}", "${" + _NAME + "}")
_LITERAL = "literal text\n"
_DECOY = "decoy text\n"
_ARGUMENTS: dict[str, dict[str, str]] = {
    "read": {},
    "write": {"content": "written\n"},
    "edit": {"old_string": "literal", "new_string": "edited"},
}
_EXPECTED_AFTER: dict[str, str] = {
    "read": _LITERAL,
    "write": "written\n",
    "edit": "edited text\n",
}


def _old_expand_path(path: str) -> str:
    """The expansion that FRE-1729 removed, for the seeded negative."""
    return os.path.expanduser(os.path.expandvars(path))


@pytest.fixture(autouse=True)
def _no_phase_events() -> Iterator[None]:
    """Replace ``phase_span`` with a no-op so unit tests do not touch Postgres."""

    @asynccontextmanager
    async def _noop(**_kw: Any) -> AsyncIterator[None]:
        yield None

    with patch("personal_agent.transport.agui.transport.phase_span", _noop):
        yield


@pytest.fixture(autouse=True)
def _canary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_NAME, _VALUE)


@pytest.fixture
def workdir() -> Iterator[Path]:
    """A directory under ``/tmp``: the real rules allow it, and pytest's tmp_path can differ."""
    with TemporaryDirectory(dir="/tmp", prefix="fre1729-") as path:
        yield Path(path)


@pytest.fixture
def old_expansion() -> Iterator[None]:
    """Put the removed expansion back in every place that resolves a model's path."""
    with (
        patch("personal_agent.tools.primitives.read._expand_path", _old_expand_path),
        patch("personal_agent.tools.primitives.write._expand_path", _old_expand_path),
        patch("personal_agent.tools.primitives.edit._expand_path", _old_expand_path),
        patch("personal_agent.tools.executor._expand_path", _old_expand_path),
    ):
        yield


def _layer(governance: GovernanceConfig | None = None) -> ToolExecutionLayer:
    registry = ToolRegistry()
    registry.register(read_tool, read_executor)
    registry.register(write_tool, write_executor)
    registry.register(edit_tool, edit_executor)
    mode_manager = MagicMock()
    mode_manager.get_current_mode.return_value = Mode.NORMAL
    return ToolExecutionLayer(
        registry=registry,
        governance_config=governance or load_governance_config(),
        mode_manager=mode_manager,
    )


@pytest.fixture(autouse=True)
def _debug_lines_in_scope() -> Iterator[None]:
    """The ``*_executor_called`` lines log the resolved path at debug level."""
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.DEBUG)
    yield
    root.setLevel(level)


async def _run(
    tool: str, path: str, *, layer: ToolExecutionLayer | None = None
) -> tuple[ToolResult, list[MutableMapping[str, Any]]]:
    """Run one call and return the result with every captured log entry.

    ``capture_log_records``, not ``capture_logs``: a logger that is cached earlier in a full
    suite run does not reach ``capture_logs``, and an empty list passes ``_value_in_logs``.
    """
    with capture_log_records() as logs:
        result = await (layer or _layer()).execute_tool(
            tool,
            {"path": path, **_ARGUMENTS[tool]},
            TraceContext.new_trace(),
            session_id="fre1729",
        )
    return result, logs


def _value_in_result(result: ToolResult) -> bool:
    return _VALUE in json.dumps(result.model_dump(mode="json"))


def _value_in_logs(logs: list[MutableMapping[str, Any]]) -> bool:
    return any(_VALUE in str(entry) for entry in logs)


# ---------------------------------------------------------------------------
# AC-1: the variable is literal text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("form", _FORMS)
@pytest.mark.parametrize("tool", ["read", "write", "edit"])
async def test_the_call_acts_on_the_file_with_the_literal_name(
    tool: str, form: str, workdir: Path
) -> None:
    """The file named ``$NAME`` is the target, and a file named with the value is untouched."""
    literal = workdir / form
    literal.write_text(_LITERAL)
    decoy = workdir / _VALUE
    decoy.write_text(_DECOY)

    result, logs = await _run(tool, f"{workdir}/{form}")

    assert result.success, result.error
    assert literal.read_text() == _EXPECTED_AFTER[tool]
    assert decoy.read_text() == _DECOY
    if tool == "read":
        assert isinstance(result.output, dict)
        assert result.output["content"] == _LITERAL
    assert not _value_in_result(result)
    assert not _value_in_logs(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("form", _FORMS)
@pytest.mark.parametrize(("tool", "code"), [("read", "not_a_file"), ("edit", "file_not_found")])
async def test_the_error_for_a_missing_file_holds_no_value(
    tool: str, code: str, form: str, workdir: Path
) -> None:
    """The route in the ticket: the error names the literal path, not the value."""
    result, logs = await _run(tool, f"{workdir}/{form}")

    assert not result.success
    assert result.error_detail is not None
    assert result.error_detail.code == code
    assert form in (result.error or "")
    assert not _value_in_result(result)
    assert not _value_in_logs(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["read", "write", "edit"])
async def test_a_variable_cannot_supply_an_allowed_root(
    tool: str, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that only reaches an allowed root through a variable is refused by the gate."""
    monkeypatch.setenv("FRE1729_ROOT", str(workdir))
    target = workdir / "target.txt"
    target.write_text(_LITERAL)

    result, _ = await _run(tool, "$FRE1729_ROOT/target.txt")

    assert not result.success
    assert result.error_detail is not None
    assert result.error_detail.code == "path_refused"
    assert target.read_text() == _LITERAL


@pytest.mark.asyncio
async def test_a_variable_after_a_home_mark_holds_no_value(workdir: Path) -> None:
    """``~$NAME`` is a user name lookup for literal text, never for the value."""
    result, logs = await _run("read", f"~${_NAME}/{workdir.name}")

    assert not result.success
    assert not _value_in_result(result)
    assert not _value_in_logs(logs)


def test_the_gate_copy_compares_the_literal_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The copy in ``tools/executor.py`` does not expand the path that it compares."""
    monkeypatch.setenv("FRE1729_ROOT", "/allowed")

    assert not _validate_path_against_patterns("$FRE1729_ROOT/file", ["/allowed/**"])
    assert _validate_path_against_patterns("/allowed/$FRE1729_ROOT", ["/allowed/**"])


# ---------------------------------------------------------------------------
# AC-1: seeded negative. The detector must fire when the defect is back.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("old_expansion")
@pytest.mark.parametrize("form", _FORMS)
@pytest.mark.parametrize("tool", ["read", "edit"])
async def test_seeded_the_detector_finds_the_value_in_result_and_logs(
    tool: str, form: str, workdir: Path
) -> None:
    """With the old expansion, the same call exposes the value in both places."""
    result, logs = await _run(tool, f"{workdir}/{form}")

    assert not result.success
    assert _value_in_result(result)
    assert _value_in_logs(logs)
    called = [entry for entry in logs if f"{tool}_executor_called" in str(entry)]
    assert called, "the debug line that holds the resolved path was not captured"
    assert _VALUE in str(called[0])


@pytest.mark.asyncio
@pytest.mark.usefixtures("old_expansion")
async def test_seeded_the_old_expansion_opens_the_decoy(workdir: Path) -> None:
    """With the old expansion, ``read`` returns the file that is named with the value."""
    (workdir / _VALUE).write_text(_DECOY)

    result, _ = await _run("read", f"{workdir}/${_NAME}")

    assert result.success, result.error
    assert isinstance(result.output, dict)
    assert result.output["content"] == _DECOY


@pytest.mark.asyncio
@pytest.mark.usefixtures("old_expansion")
async def test_seeded_the_old_expansion_passes_the_gate(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the old expansion, a variable supplies an allowed root and the call runs."""
    monkeypatch.setenv("FRE1729_ROOT", str(workdir))
    (workdir / "target.txt").write_text(_LITERAL)

    result, _ = await _run("read", "$FRE1729_ROOT/target.txt")

    assert result.success, result.error


# ---------------------------------------------------------------------------
# AC-2: an operator's pattern still expands
# ---------------------------------------------------------------------------


def _governance(tool: str, **policy: list[str]) -> GovernanceConfig:
    config = load_governance_config().model_copy(deep=True)
    config.tools[tool] = ToolPolicy(allowed_in_modes=["NORMAL"], **policy)
    return config


@pytest.mark.parametrize("match", [_matches_any, _validate_path_against_patterns])
def test_a_pattern_with_a_variable_matches_only_when_the_variable_is_set(
    match: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both gates expand the pattern. Unset, the pattern stays literal and does not match."""
    monkeypatch.setenv("FRE1729_ROOT", "/operator")
    assert match("/operator/file", ["$FRE1729_ROOT/**"])
    assert match("/operator/file", ["${FRE1729_ROOT}/**"])

    monkeypatch.delenv("FRE1729_ROOT")
    assert not match("/operator/file", ["$FRE1729_ROOT/**"])


@pytest.mark.asyncio
async def test_an_operator_allowed_pattern_lets_the_call_run(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real ``read`` under ``$ROOT/**``, through both gates."""
    monkeypatch.setenv("FRE1729_ROOT", str(workdir))
    (workdir / "file.txt").write_text(_LITERAL)
    governance = _governance("read", allowed_paths=["$FRE1729_ROOT/**"])

    with patch(
        "personal_agent.tools.primitives._governance.load_governance_config",
        return_value=governance,
    ):
        result, _ = await _run("read", f"{workdir}/file.txt", layer=_layer(governance))

    assert result.success, result.error


@pytest.mark.asyncio
async def test_an_operator_forbidden_pattern_refuses_the_call_in_the_tool(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool's own check expands the forbidden pattern too."""
    monkeypatch.setenv("FRE1729_ROOT", str(workdir))
    (workdir / "file.txt").write_text(_LITERAL)
    governance = _governance("read", forbidden_paths=["$FRE1729_ROOT/**"])

    with (
        patch(
            "personal_agent.tools.primitives._governance.load_governance_config",
            return_value=governance,
        ),
        pytest.raises(PathRefusalError) as refused,
    ):
        await read_executor(f"{workdir}/file.txt", ctx=TraceContext.new_trace())

    assert refused.value.code == "forbidden_path"


def test_an_operator_unattended_pattern_expands(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``unattended_paths`` shares the helper, so it keeps the expansion."""
    monkeypatch.setenv("FRE1729_ROOT", str(workdir))
    governance = _governance("write", unattended_paths=["$FRE1729_ROOT/**"])

    with patch(
        "personal_agent.tools.primitives.write.load_governance_config",
        return_value=governance,
    ):
        assert _is_unattended_path(workdir / "file.txt", tool_name="write")


# ---------------------------------------------------------------------------
# Kept: the home directory still expands, under the real rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_home_path_still_reads(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``~`` expands in a model's path, and the real ``$HOME`` pattern allows the result."""
    monkeypatch.setenv("HOME", str(workdir))
    target = workdir / "project" / "personal_agent" / "file.txt"
    target.parent.mkdir(parents=True)
    target.write_text(_LITERAL)

    result, _ = await _run("read", "~/project/personal_agent/file.txt")

    assert result.success, result.error
    assert isinstance(result.output, dict)
    assert result.output["content"] == _LITERAL


@pytest.mark.asyncio
async def test_a_home_path_to_a_forbidden_directory_stays_refused(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``$HOME/.ssh/**`` pattern still refuses ``~/.ssh/id_rsa``."""
    monkeypatch.setenv("HOME", str(workdir))
    target = workdir / ".ssh" / "id_rsa"
    target.parent.mkdir()
    target.write_text("not a real key\n")

    result, _ = await _run("read", "~/.ssh/id_rsa")

    assert not result.success
    assert result.error_detail is not None
    assert result.error_detail.code == "path_refused"
    assert "is in forbidden paths" in (result.error or "")
