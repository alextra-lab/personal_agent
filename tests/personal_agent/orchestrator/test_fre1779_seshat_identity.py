"""FRE-1779 AC-1: the primary and the helpers know that the agent is Seshat.

On 2026-11-20 the planner told the helpers "Seshat is described as a telemetry or reporting
system", and the answer called Seshat an unconfirmed outside service. The primary prompt named
the agent "Personal Local AI Collaborator", the planner prompt gave no identity, and the helper
task message did not say where the deployed code is.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from personal_agent.config import settings
from personal_agent.config.env_loader import Environment
from personal_agent.orchestrator.sub_agent_types import SubAgentSpec
from personal_agent.orchestrator.worker_types import WORKER_TYPES, WorkerType
from personal_agent.tools.types import ToolDefinition

_IDENTITY_LINE = (
    "You are Seshat v{version}, the user's self-hosted personal agent. A question about "
    "Seshat is a question about you and your own deployment."
)
_HELPER_LINE = "Seshat's deployed code is at /app/src and is readable with `read`."


def test_project_name_default_is_seshat() -> None:
    """The setting the identity line renders defaults to Seshat."""
    assert type(settings).model_fields["project_name"].default == "Seshat"


def test_primary_prompt_names_seshat() -> None:
    """The tool-awareness block, part of every primary prompt with tools, opens with the identity."""
    import personal_agent.orchestrator.prompts as prompts_module

    prompts_module._tool_awareness_cache = None
    prompts_module._tool_awareness_cache_time = 0.0
    registry = MagicMock()
    registry.list_tools.return_value = [
        ToolDefinition(
            name="read",
            description="d",
            category="files",
            risk_level="low",
            allowed_modes=["NORMAL"],
        )
    ]
    with (
        patch("personal_agent.tools.get_default_registry", return_value=registry),
        patch.object(settings, "project_name", "Seshat"),
    ):
        prompt = prompts_module.get_tool_awareness_prompt()
    prompts_module._tool_awareness_cache = None

    assert prompt.splitlines()[0] == _IDENTITY_LINE.format(version=settings.version)


def _spec(worker_type: WorkerType) -> SubAgentSpec:
    return SubAgentSpec(
        task="check the gateway",
        context=[],
        max_tokens=1024,
        timeout_seconds=30.0,
        tools=list(WORKER_TYPES[worker_type].tools),
        worker_type=worker_type,
        thoroughness="quick",
    )


def test_helper_task_message_points_at_app_src_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A production helper that holds `read` is told the deployed code is readable."""
    from personal_agent.orchestrator.sub_agent import _build_task_message

    monkeypatch.setattr(settings, "environment", Environment.PRODUCTION)
    spec = _spec(WorkerType.GENERAL)
    assert "read" in spec.tools

    assert _HELPER_LINE in _build_task_message(spec, "t", None).splitlines()


def test_helper_task_message_omits_the_line_without_read_or_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line is not shown where it would be false: no `read` tool, or no `/app/src`."""
    from personal_agent.orchestrator.sub_agent import _build_task_message

    no_read = next(t for t in WorkerType if "read" not in WORKER_TYPES[t].tools)
    monkeypatch.setattr(settings, "environment", Environment.PRODUCTION)
    assert _HELPER_LINE not in _build_task_message(_spec(no_read), "t", None)

    monkeypatch.setattr(settings, "environment", Environment.DEVELOPMENT)
    assert _HELPER_LINE not in _build_task_message(_spec(WorkerType.GENERAL), "t", None)
