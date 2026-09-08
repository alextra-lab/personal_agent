"""Tests for FRE-1473 — the primary's own dispatch call site names no principal.

``dispatch_tool_call`` defaults its ``principal`` parameter to ``"primary"``, which
never runs the sub-agent-only clamp. This pins that the primary's real call site
(``step_tool_execution``) relies on that default rather than passing one explicitly —
proof against the actual call path, not just a read of the code (AC-3).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext
from personal_agent.telemetry.trace import TraceContext


def _make_ctx() -> ExecutionContext:
    ctx = ExecutionContext(  # type: ignore[arg-type]
        session_id="sess-fre1473",
        trace_id="trace-fre1473",
        user_message="what did I say last week",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    ctx.messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tc-1",
                    "function": {
                        "name": "recall_personal_history",
                        "arguments": json.dumps({"days_ago": 365}),
                    },
                }
            ],
        }
    ]
    return ctx


def _trace_ctx() -> TraceContext:
    return TraceContext(trace_id="trace-fre1473", session_id="sess-fre1473")


@pytest.fixture(autouse=True)
def _patch_executor_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ex, "_get_tool_execution_layer", lambda: object())
    monkeypatch.setattr(ex, "_is_turn_cancelled", lambda _sid: False)

    async def _noop_status(_ctx: ExecutionContext) -> None:
        return None

    monkeypatch.setattr(ex, "_report_turn_progress", _noop_status)


class TestPrimaryDispatchNamesNoPrincipal:
    @pytest.mark.asyncio
    async def test_the_primarys_real_call_site_never_passes_a_principal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def _fake_dispatch(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "tool_call_id": kwargs["tool_call_id"],
                "tool_name": kwargs["tool_name"],
                "content": json.dumps({"status": "ok", "body": "..."}),
                "success": True,
                "latency_ms": 1.0,
                "output_hash": "h",
                "gate_result": kwargs.get("gate_result"),
                "args_hash": kwargs.get("args_hash"),
                "loop_policy": kwargs.get("loop_policy"),
                "tool_layer_output": {"turns": []},
                "tool_layer_error": None,
                "terminal": False,
                "terminal_reason": None,
                "terminal_next_step": None,
            }

        monkeypatch.setattr(ex, "dispatch_tool_call", _fake_dispatch)
        ctx = _make_ctx()

        await ex.step_tool_execution(ctx, MagicMock(), _trace_ctx())

        assert captured["tool_name"] == "recall_personal_history"
        assert "principal" not in captured
