"""Tool results the model reads carry their citation identifier (ADR-0138 D3(a), FRE-1296).

FRE-1280 registered admissible tool results into the turn's source registry, but nothing
spliced the minted identifier into the content the model actually reads — a citation
identifier the model was never shown can never be copied (FRE-1283's grounding
instruction was inert). This proves the identifier reaches ``ctx.messages`` on the wire
tool-result message and resolves against the same turn's registry, while an inadmissible
result (D2's independence rule) carries no marker at all.
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.governance.models import Mode
from personal_agent.grounding.source_registry import SourceRegistry
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.telemetry.trace import TraceContext

_MARKER_RE = re.compile(r"\[(S\d+@[0-9a-f]+)\]")


def _make_ctx(*, tool_name: str) -> ExecutionContext:
    ctx = ExecutionContext(  # type: ignore[arg-type]
        session_id="sess-1296",
        trace_id="trace-1296",
        user_message="Which tinned tuna should I buy?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    ctx.messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc-1", "function": {"name": tool_name, "arguments": "{}"}},
            ],
        }
    ]
    ctx.source_registry = SourceRegistry(turn_id=ctx.trace_id)
    return ctx


def _trace_ctx() -> TraceContext:
    return TraceContext(trace_id="trace-1296", session_id="sess-1296")


def _dispatch_returning(*, content: str, success: bool = True):
    async def _fake_dispatch(
        tool_call_id, tool_name, arguments, args_hash, gate_result, loop_policy, *a, **k
    ):
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "content": content,
            "success": success,
            "latency_ms": 1.0,
            "output_hash": "h" if success else None,
            "gate_result": gate_result,
            "args_hash": args_hash,
            "loop_policy": loop_policy,
            "tool_layer_output": content,
            "tool_layer_error": None,
            "terminal": False,
            "terminal_reason": None,
            "terminal_next_step": None,
        }

    return _fake_dispatch


@pytest.fixture(autouse=True)
def _patch_executor_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ex, "_get_tool_execution_layer", lambda: object())
    monkeypatch.setattr(ex, "_is_turn_cancelled", lambda _sid: False)

    async def _noop_status(_ctx) -> None:
        return None

    monkeypatch.setattr(ex, "_report_turn_progress", _noop_status)


class TestToolResultCitationSplice:
    @pytest.mark.asyncio
    async def test_admissible_tool_result_carries_a_resolvable_citation_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content = json.dumps({"results": [{"content": "Ortiz is sold in Biarritz."}]})
        monkeypatch.setattr(ex, "dispatch_tool_call", _dispatch_returning(content=content))
        ctx = _make_ctx(tool_name="web_search")

        next_state = await ex.step_tool_execution(ctx, MagicMock(), _trace_ctx())

        assert next_state == TaskState.LLM_CALL
        tool_message = next(m for m in ctx.messages if m.get("role") == "tool")
        rendered = str(tool_message["content"])
        match = _MARKER_RE.search(rendered)
        assert match is not None, rendered
        assert ctx.source_registry.resolve(match.group(1)) is not None  # type: ignore[union-attr]
        # The underlying content is preserved, not replaced by the marker.
        assert "Ortiz is sold in Biarritz." in rendered

    @pytest.mark.asyncio
    async def test_inadmissible_tool_result_carries_no_citation_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D2's independence rule: a shell echo of model-authored text is not a source."""
        content = "Paris has 9 million residents"
        monkeypatch.setattr(ex, "dispatch_tool_call", _dispatch_returning(content=content))
        ctx = _make_ctx(tool_name="bash")

        await ex.step_tool_execution(ctx, MagicMock(), _trace_ctx())

        tool_message = next(m for m in ctx.messages if m.get("role") == "tool")
        assert _MARKER_RE.search(str(tool_message["content"])) is None

    @pytest.mark.asyncio
    async def test_no_registry_leaves_tool_content_unmodified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sub-agent paths carry no registry — content must pass through byte-identical."""
        content = json.dumps({"results": [{"content": "Ortiz is sold in Biarritz."}]})
        monkeypatch.setattr(ex, "dispatch_tool_call", _dispatch_returning(content=content))
        ctx = _make_ctx(tool_name="web_search")
        ctx.source_registry = None

        await ex.step_tool_execution(ctx, MagicMock(), _trace_ctx())

        tool_message = next(m for m in ctx.messages if m.get("role") == "tool")
        assert tool_message["content"] == content
