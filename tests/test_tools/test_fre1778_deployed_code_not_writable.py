"""FRE-1778 AC-3: ``write`` and ``edit`` refuse the deployed code under ``/app/src``.

Helpers and the primary may read the code the image runs, so nothing may change it. The rule
is in ``config/governance/tools.yaml``: ``/app/src/**`` is in write's forbidden paths, and edit
aliases write's list. These tests load the real rules file.

*Fails if* ``/app/src/**`` is dropped from write's forbidden paths, or edit's list stops being
write's: ``/app/**`` is allowed, so the path would pass the path rules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.primitives._governance import PathRefusalError, _check_path_governance
from personal_agent.tools.primitives.edit import edit_executor
from personal_agent.tools.primitives.write import write_executor

_PATHS = [
    "/app/src/personal_agent/service/app.py",
    "/app/src/personal_agent/orchestrator/prompts.py",
    "/app/src/new_module.py",
]


@pytest.mark.parametrize("tool", ["write", "edit"])
@pytest.mark.parametrize("path", _PATHS)
def test_the_path_rules_refuse_the_deployed_code(tool: str, path: str) -> None:
    with pytest.raises(PathRefusalError) as exc_info:
        _check_path_governance(Path(path), tool)
    assert exc_info.value.code == "forbidden_path"


def test_the_control_a_mounted_workspace_path_passes_the_path_rules() -> None:
    """The refusal is the /app/src rule, not a blanket refusal of /app."""
    _check_path_governance(Path("/app/agent_workspace/notes.md"), "write")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_the_write_tool_refuses_the_deployed_code(path: str) -> None:
    with pytest.raises(PathRefusalError) as exc_info:
        await write_executor(path, content="x", mode="overwrite", ctx=TraceContext.new_trace())
    assert exc_info.value.code == "forbidden_path"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_the_edit_tool_refuses_the_deployed_code(path: str) -> None:
    with pytest.raises(PathRefusalError) as exc_info:
        await edit_executor(path, old_string="a", new_string="b", ctx=TraceContext.new_trace())
    assert exc_info.value.code == "forbidden_path"
