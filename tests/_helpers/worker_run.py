"""Run one worker against real tools with a scripted model (FRE-1580, ADR-0156 D9).

The worker loop, ``dispatch_tool_call``, ``ToolExecutionLayer``, governance, the permission
rules and the tool executors are all real. Only the model is scripted: its first reply makes
the given tool calls, and every later reply is ``DONE``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from personal_agent.governance.models import Mode
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec
from personal_agent.tools.executor import ToolExecutionLayer
from personal_agent.tools.registry import ToolRegistry


def worker_layer() -> ToolExecutionLayer:
    """Build a tool layer with the real registry, the real governance and no approval channel.

    Returns:
        The layer. Its mode is NORMAL.
    """
    from personal_agent.config import settings  # noqa: PLC0415
    from personal_agent.tools import read_skill, register_mvp_tools  # noqa: PLC0415

    registry = ToolRegistry()
    # read_skill's name enum is built at import. Rebuild it so a test's seeded skills count.
    with (
        patch.object(settings, "primitive_tools_enabled", True),
        patch.object(read_skill, "read_skill_tool", read_skill.build_read_skill_tool()),
    ):
        register_mvp_tools(registry)
    mode_manager = MagicMock()
    mode_manager.get_current_mode.return_value = Mode.NORMAL
    return ToolExecutionLayer(registry=registry, mode_manager=mode_manager)


@contextmanager
def shared_layer(layer: ToolExecutionLayer) -> Iterator[ToolExecutionLayer]:
    """Make the worker loop use ``layer`` as its shared tool layer.

    Args:
        layer: The layer to install.

    Yields:
        The same layer.
    """
    with patch(
        "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
        return_value=layer,
    ):
        yield layer


@dataclass(frozen=True)
class WorkerRun:
    """What a scripted worker run produced.

    Attributes:
        result: The worker's result.
        calls: The keyword arguments of every model call, with ``messages`` copied at call time.
        tool_messages: The tool-role messages of the run, by tool call id.
    """

    result: SubAgentResult
    calls: list[dict[str, Any]]
    tool_messages: dict[str, dict[str, Any]]

    def payload(self, call_id: str) -> dict[str, Any]:
        """Return the parsed content of one tool message.

        Args:
            call_id: The tool call id.

        Returns:
            The parsed JSON content.
        """
        parsed: dict[str, Any] = json.loads(self.tool_messages[call_id]["content"])
        return parsed


async def run_worker(
    spec: SubAgentSpec,
    tool_calls: Sequence[tuple[str, dict[str, Any]]],
    *,
    user_id: UUID | None = None,
) -> WorkerRun:
    """Run one worker whose model makes ``tool_calls`` in its first round, then stops.

    Args:
        spec: The worker spec.
        tool_calls: ``(tool_name, arguments)`` pairs. Call ids are ``c0``, ``c1`` and so on.
        user_id: The turn's owning user, for a tool that is scoped to the user (FRE-1467).

    Returns:
        The run.
    """
    calls: list[dict[str, Any]] = []
    first = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": f"c{i}", "name": name, "arguments": json.dumps(args)}
            for i, (name, args) in enumerate(tool_calls)
        ],
        "usage": {},
        "response_id": None,
        "raw": {},
    }

    async def _respond(**kwargs: Any) -> dict[str, Any]:
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if len(calls) == 1 and tool_calls:
            return first
        return {
            "role": "assistant",
            "content": "DONE",
            "tool_calls": [],
            "usage": {},
            "response_id": None,
            "raw": {},
        }

    client = AsyncMock()
    client.respond = AsyncMock(side_effect=_respond)
    result = await run_sub_agent(
        spec=spec, llm_client=client, trace_id="t-1580", user_id=user_id
    )
    # The landing call can trim old tool results, so collect them from every call. The loop
    # renames each call id to ``call_r<round>_<index>_<id>``; key by the original ``c<i>``.
    tool_messages = {
        m["tool_call_id"].rsplit("_", 1)[-1]: m
        for call in calls
        for m in call["messages"]
        if m.get("role") == "tool"
    }
    return WorkerRun(result=result, calls=calls, tool_messages=tool_messages)
