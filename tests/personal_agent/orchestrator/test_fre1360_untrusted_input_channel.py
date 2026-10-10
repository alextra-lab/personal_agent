"""FRE-1360 — untrusted input reaches the model only through a tool-result channel.

ADR-0140 T2 declares four input classes untrusted: knowledge-graph recall, tool results,
fetched web content and MCP server responses. Its AC-4 asks for one probe per class, each
carrying a unique marker, asserting the marker appears in no system block and no user text
block of the emitted request. A single-class probe can pass while another assembly path
violates T2, so the HYBRID worker reports and the HYBRID planner's memory digest are probed
too.

Every probe reads the wire form :func:`build_wire_messages` produces — the same
provider-neutral list both LLM clients serialize — never the executor's own list.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.orchestrator.untrusted_channel import (
    MEMORY_RECALL_TOOL,
    WORKER_REPORTS_TOOL,
    harness_call_id,
    harness_tool_exchange,
)
from tests.personal_agent.orchestrator.test_fre1489_volatile_duplication import (
    _assert_forward_extension,
    _drive_loop,
    _episode,
    _make_ctx,
)

_MEMORY_MARKER = "FRE1360-MEMORY-MARKER-Q7"
_WORKER_MARKER = "FRE1360-WORKER-MARKER-K2"
_NATIVE_MARKER = "FRE1360-NATIVE-TOOL-MARKER-B8"
_WEB_MARKER = "FRE1360-FETCHED-PAGE-MARKER-H4"
_MCP_MARKER = "FRE1360-MCP-RESPONSE-MARKER-V1"
_DIRECTIVE = "Synthesize from these results only. Where a sub-task did not complete, say so."

# AC-3's baseline, recorded on unchanged main (85ea515a) with the same three episodes and
# the same extraction (marker position across every non-system message, in wire order):
# ['EPI-ALPHA-7Q', 'EPI-BRAVO-3K', 'EPI-CHARLIE-9Z'], all inside the one user message.
_ORDER_MARKERS = ["EPI-ALPHA-7Q", "EPI-BRAVO-3K", "EPI-CHARLIE-9Z"]
_BASELINE_ORDER = ["EPI-ALPHA-7Q", "EPI-BRAVO-3K", "EPI-CHARLIE-9Z"]


# ── Wire helpers ────────────────────────────────────────────────────────────────


def _message_text(message: Mapping[str, Any]) -> str:
    """Every byte of a message the model can read: content (any shape) and tool calls."""
    return json.dumps(
        {"content": message.get("content"), "tool_calls": message.get("tool_calls")},
        ensure_ascii=False,
    )


def _assert_marker_only_in_tool_results(
    wire: Sequence[Mapping[str, Any]], marker: str, *, tool_name: str | None = None
) -> list[Mapping[str, Any]]:
    """The marker sits in tool results only, each answering a call earlier in the wire.

    Returns:
        The tool messages that hold the marker.
    """
    holders = [m for m in wire if marker in _message_text(m)]
    assert holders, f"marker {marker!r} never reached the wire"
    for message in holders:
        assert message.get("role") == "tool", (
            f"marker {marker!r} reached the model in a {message.get('role')!r} message: "
            f"{_message_text(message)[:300]}"
        )
        if tool_name is not None:
            assert message.get("name") == tool_name
        call_id = message.get("tool_call_id")
        index = list(wire).index(message)
        issued = [
            tc.get("id")
            for m in list(wire)[:index]
            if m.get("role") == "assistant"
            for tc in m.get("tool_calls") or []
        ]
        assert call_id in issued, f"tool result {call_id!r} answers no earlier call"
    return holders


def _memory_ctx(memory: list[dict[str, Any]] | None, *, hybrid: bool = False) -> Any:
    ctx = _make_ctx(hybrid=False, memory=memory)
    if hybrid:
        from personal_agent.orchestrator.executor import _append_synthesis_exchange

        _append_synthesis_exchange(
            ctx, f"## Sub-agent results\n- worker 1: found it. {_WORKER_MARKER}\n", _DIRECTIVE
        )
    return ctx


# ── AC-1: recalled memory ───────────────────────────────────────────────────────


class TestAc1RecalledMemory:
    @pytest.mark.asyncio
    async def test_ac1_recalled_memory_marker_reaches_only_a_tool_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory = [_episode("conv-1", f"the owner said {_MEMORY_MARKER}")]
        wires = await _drive_loop(_memory_ctx(memory), 1, monkeypatch)
        holders = _assert_marker_only_in_tool_results(
            wires[0], _MEMORY_MARKER, tool_name=MEMORY_RECALL_TOOL
        )
        assert len(holders) == 1

    @pytest.mark.asyncio
    async def test_non_populated_memory_state_line_rides_the_tool_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0148 D2: the section is always present; with no recall it is the state line."""
        from personal_agent.request_gateway.memory_status import MEMORY_STATE_LINES

        ctx = _memory_ctx(None)
        wires = await _drive_loop(ctx, 1, monkeypatch)
        memory_results = [m for m in wires[0] if m.get("name") == MEMORY_RECALL_TOOL]
        assert len(memory_results) == 1
        assert memory_results[0]["content"] == MEMORY_STATE_LINES[ctx.memory_status.status]
        for message in wires[0]:
            if message.get("role") in ("system", "user"):
                assert MEMORY_STATE_LINES[ctx.memory_status.status] not in _message_text(message)


# ── AC-2: the other declared classes, and the HYBRID paths ──────────────────────


def _registry_with(name: str, executor: Any, *, category: str = "read_only") -> Any:
    from personal_agent.tools.registry import ToolRegistry
    from personal_agent.tools.types import ToolDefinition

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=name,
            description="FRE-1360 probe tool",
            category=category,
            parameters=[],
            risk_level="low",
            allowed_modes=["NORMAL"],
        ),
        executor,
    )
    return registry


async def _run_turn_calling(tool_name: str, registry: Any) -> list[list[dict[str, Any]]]:
    """One real orchestrator turn: the model calls ``tool_name`` once, then answers.

    Returns:
        The wire form of every model call, in order.
    """
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator import Channel, Orchestrator
    from personal_agent.orchestrator.executor import build_wire_messages
    from personal_agent.tools.executor import ToolExecutionLayer
    from tests.test_orchestrator.conftest import configure_mock_llm_client_model_configs

    wires: list[list[dict[str, Any]]] = []

    async def _respond(**kwargs: Any) -> dict[str, Any]:
        wires.append(
            build_wire_messages(list(kwargs["messages"]), kwargs.get("system_prompt"), "t")
        )
        if len(wires) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "probe_1",
                        "name": tool_name,
                        "arguments": json.dumps({"url": "https://example.com/p"}),
                    }
                ],
                "reasoning_trace": None,
                "usage": {"total_tokens": 10},
                "raw": {},
            }
        return {
            "role": "assistant",
            "content": "Done.",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 10},
            "raw": {},
        }

    client = AsyncMock()
    client.respond = AsyncMock(side_effect=_respond)
    configure_mock_llm_client_model_configs(client)
    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=client),
        patch(
            "personal_agent.orchestrator.executor._get_tool_execution_layer",
            return_value=ToolExecutionLayer(registry),
        ),
    ):
        await Orchestrator().handle_user_request(
            session_id="fre-1360-probe",
            user_message="Run the probe tool",
            mode=Mode.NORMAL,
            channel=Channel.SYSTEM_HEALTH,
        )
    assert len(wires) >= 2, "the turn never sent the tool result back to the model"
    return wires


class TestAc2DeclaredClasses:
    @pytest.mark.asyncio
    async def test_ac2_native_tool_result_marker_reaches_only_a_tool_result(self) -> None:
        async def _probe(**_kwargs: object) -> dict[str, str]:
            return {"text": f"lookup result {_NATIVE_MARKER}"}

        wires = await _run_turn_calling("fre1360_probe", _registry_with("fre1360_probe", _probe))
        _assert_marker_only_in_tool_results(wires[-1], _NATIVE_MARKER, tool_name="fre1360_probe")

    @pytest.mark.asyncio
    async def test_ac2_fetched_web_content_marker_reaches_only_a_tool_result(self) -> None:
        """The real ``fetch_url`` executor runs; only its HTTP transport is a stub."""
        import personal_agent.tools.fetch as fetch_mod

        page = f"<html><body><p>Page body {_WEB_MARKER}</p></body></html>"

        def _client(**kwargs: Any) -> httpx.AsyncClient:
            kwargs.pop("event_hooks", None)  # the private-target DNS guard needs a network
            return httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _req: httpx.Response(
                        200, headers={"content-type": "text/html"}, text=page
                    )
                ),
                **kwargs,
            )

        tracker = MagicMock()
        tracker.check_and_record = AsyncMock(return_value=MagicMock(novel=False))
        from personal_agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(fetch_mod.fetch_url_tool, fetch_mod.fetch_url_executor)
        with (
            patch.object(fetch_mod, "create_guarded_http_client", side_effect=_client),
            patch.object(fetch_mod, "get_novelty_tracker", return_value=tracker),
        ):
            wires = await _run_turn_calling("fetch_url", registry)
        _assert_marker_only_in_tool_results(wires[-1], _WEB_MARKER, tool_name="fetch_url")

    @pytest.mark.asyncio
    async def test_ac2_mcp_response_marker_reaches_only_a_tool_result(self) -> None:
        """A real ``MCPGatewayAdapter`` registers the tool; only the MCP session is a stub."""
        from personal_agent.mcp.gateway import MCPGatewayAdapter
        from personal_agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        adapter = MCPGatewayAdapter(registry)
        adapter.client = AsyncMock()
        adapter.client.list_tools = AsyncMock(
            return_value=[
                {
                    "name": "fre1360_lookup",
                    "description": "Probe MCP tool",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        )
        adapter.client.call_tool = AsyncMock(return_value={"result": f"mcp says {_MCP_MARKER}"})
        governance = MagicMock()
        governance.get_description_override.return_value = None
        with patch("personal_agent.mcp.gateway.MCPGovernanceManager", return_value=governance):
            await adapter._discover_and_register_tools()
        tool_name = "mcp_fre1360_lookup"
        assert registry.get_tool(tool_name) is not None

        wires = await _run_turn_calling(tool_name, registry)
        _assert_marker_only_in_tool_results(wires[-1], _MCP_MARKER, tool_name=tool_name)

    @pytest.mark.asyncio
    async def test_ac2_worker_report_marker_reaches_only_a_tool_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory = [_episode("conv-1", f"the owner said {_MEMORY_MARKER}")]
        wires = await _drive_loop(_memory_ctx(memory, hybrid=True), 1, monkeypatch)
        _assert_marker_only_in_tool_results(wires[0], _WORKER_MARKER, tool_name=WORKER_REPORTS_TOOL)
        _assert_marker_only_in_tool_results(wires[0], _MEMORY_MARKER, tool_name=MEMORY_RECALL_TOOL)

    @pytest.mark.asyncio
    async def test_harness_synthesis_directives_stay_trusted_user_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Code-review fold-in: the harness's own instruction must not ride the tool result.

        The model reads instructions inside a tool result with scepticism, so the
        ADR-0149 D4 closing instruction stays in the synthesis user message.
        """
        wires = await _drive_loop(_memory_ctx(None, hybrid=True), 1, monkeypatch)
        holders = [m for m in wires[0] if _DIRECTIVE in _message_text(m)]
        assert [m.get("role") for m in holders] == ["user"]
        (reports,) = [m for m in wires[0] if m.get("name") == WORKER_REPORTS_TOOL]
        assert _DIRECTIVE not in reports["content"]

    def test_ac2_planner_memory_digest_reaches_only_a_tool_result(self) -> None:
        from personal_agent.orchestrator.expansion_controller import (
            build_planner_user_message,
            planner_request_messages,
        )

        built = build_planner_user_message(
            "Plan my trip",
            "HYBRID",
            [{"role": "user", "content": "Plan my trip"}],
            digest_text=f"- the owner prefers trains {_MEMORY_MARKER}",
            history_max_chars=2000,
            input_max_chars=10_000,
        )
        messages = planner_request_messages("PLANNER SYSTEM", built, trace_id="a" * 32)
        _assert_marker_only_in_tool_results(messages, _MEMORY_MARKER, tool_name=MEMORY_RECALL_TOOL)
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "Plan my trip" in messages[1]["content"]

    def test_ac2_planner_with_no_digest_sends_no_exchange(self) -> None:
        from personal_agent.orchestrator.expansion_controller import (
            build_planner_user_message,
            planner_request_messages,
        )

        built = build_planner_user_message(
            "Plan", "HYBRID", None, history_max_chars=100, input_max_chars=1000
        )
        messages = planner_request_messages("SYS", built, trace_id="a" * 32)
        assert [m["role"] for m in messages] == ["system", "user"]

    def test_ac2_planner_history_never_renders_tool_messages(self) -> None:
        """A reused session's history carries tool exchanges; the planner gets them as text."""
        from personal_agent.orchestrator.expansion_controller import planner_history_text

        call, result = harness_tool_exchange(
            call_id=harness_call_id("mem", "b" * 32),
            tool_name=MEMORY_RECALL_TOOL,
            content=f"old recall {_MEMORY_MARKER}",
        )
        real_call = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_t0_0_x", "function": {"name": "web_search"}}],
        }
        real_result = {"role": "tool", "tool_call_id": "call_t0_0_x", "content": _WEB_MARKER}
        messages = [
            {"role": "user", "content": "earlier question"},
            call,
            result,
            real_call,
            real_result,
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "now"},
        ]
        text = planner_history_text(messages, "now", 10_000)
        assert _MEMORY_MARKER not in text
        assert _WEB_MARKER not in text
        assert "earlier question" in text
        assert "earlier answer" in text

    def test_ac2_every_declared_class_is_probed(self) -> None:
        """ADR-0140 AC-4 fails if fewer than all four classes are probed.

        Each T2 class maps to a probe that exists AND asserts its own marker's placement
        through :func:`_assert_marker_only_in_tool_results` — a probe that only exists,
        or that checks another class's marker, does not count.
        """
        import inspect

        probes = {
            "knowledge_graph_recall": (
                TestAc1RecalledMemory.test_ac1_recalled_memory_marker_reaches_only_a_tool_result,
                "_MEMORY_MARKER",
            ),
            "tool_results": (
                TestAc2DeclaredClasses.test_ac2_native_tool_result_marker_reaches_only_a_tool_result,
                "_NATIVE_MARKER",
            ),
            "fetched_web_content": (
                TestAc2DeclaredClasses.test_ac2_fetched_web_content_marker_reaches_only_a_tool_result,
                "_WEB_MARKER",
            ),
            "mcp_server_responses": (
                TestAc2DeclaredClasses.test_ac2_mcp_response_marker_reaches_only_a_tool_result,
                "_MCP_MARKER",
            ),
        }
        markers = {marker for _, marker in probes.values()}
        assert len(markers) == 4, "each class needs its own marker"
        for cls_name, (probe, marker) in probes.items():
            source = inspect.getsource(probe)
            assert "_assert_marker_only_in_tool_results(" in source, f"{cls_name}: no assertion"
            assert marker in source, f"{cls_name}: probe does not use its own marker"


# ── AC-3: recall still works, and still ranks the same ──────────────────────────


class TestAc3RecallPreserved:
    @pytest.mark.asyncio
    async def test_ac3_recall_items_same_set_same_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.orchestrator.executor import _render_memory_section_with_ids
        from personal_agent.request_gateway.memory_status import (
            MEMORY_STATE_LINES,
            MemoryStatus,
        )

        memory = [_episode(f"conv-{i}", f"summary {m}") for i, m in enumerate(_ORDER_MARKERS)]
        ctx = _memory_ctx(memory)
        wires = await _drive_loop(ctx, 1, monkeypatch)

        # The same extraction the baseline used on main.
        text = "\n".join(
            json.dumps(m.get("content"), ensure_ascii=False)
            for m in wires[0]
            if m.get("role") != "system"
        )
        assert all(text.find(m) >= 0 for m in _ORDER_MARKERS), "a recall item was lost"
        assert sorted(_ORDER_MARKERS, key=text.find) == _BASELINE_ORDER

        # Byte for byte the renderer's output (plus the ADR-0148 state line, if any).
        rendered, rendered_ids, _ = _render_memory_section_with_ids(memory, ctx.source_registry)
        status = ctx.memory_status.status
        expected = (
            rendered
            if status is MemoryStatus.POPULATED
            else f"{rendered}\n\n{MEMORY_STATE_LINES[status]}"
        )
        (result,) = [m for m in wires[0] if m.get("name") == MEMORY_RECALL_TOOL]
        assert result["content"] == expected

        # The evidence record admits the same identities, in the same order.
        assert ctx.turn_evidence is not None
        assert ctx.turn_evidence.assembled_context.memory_identities == list(rendered_ids)
        assert "memory_section" in ctx.turn_evidence.assembled_context.prompt_component_ids

    @pytest.mark.asyncio
    async def test_ac3_memory_is_the_context_nearest_the_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory = [_episode("conv-1", f"summary {_MEMORY_MARKER}")]
        ctx = _memory_ctx(memory)
        wires = await _drive_loop(ctx, 1, monkeypatch)
        wire = wires[0]
        carrier = max(i for i, m in enumerate(wire) if m.get("role") == "user")
        assert "<turn_context>" in _message_text(wire[carrier])
        call, result = wire[carrier + 1], wire[carrier + 2]
        assert call["role"] == "assistant"
        assert call["tool_calls"][0]["id"] == harness_call_id("mem", ctx.trace_id)
        assert result["role"] == "tool" and result["name"] == MEMORY_RECALL_TOOL
        assert len(wire) == carrier + 3, "memory is no longer the last context before generation"

    @pytest.mark.asyncio
    async def test_ac3_wire_stays_a_forward_extension_across_rounds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory = [_episode("conv-1", f"summary {_MEMORY_MARKER}")]
        for hybrid in (False, True):
            wires = await _drive_loop(_memory_ctx(memory, hybrid=hybrid), 3, monkeypatch)
            _assert_forward_extension(wires)
            assert sum(m.get("name") == MEMORY_RECALL_TOOL for m in wires[-1]) == 1

    @pytest.mark.asyncio
    async def test_next_turn_over_persisted_history_is_a_forward_extension(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reused SessionManager replays the exchange; turn 2 only appends."""
        from datetime import UTC, datetime

        from personal_agent.governance.models import Mode
        from personal_agent.orchestrator.channels import Channel
        from personal_agent.orchestrator.types import ExecutionContext

        first = _memory_ctx([_episode("conv-1", f"summary {_MEMORY_MARKER}")])
        turn1 = await _drive_loop(first, 1, monkeypatch)
        history = [*first.messages, {"role": "assistant", "content": "turn one answer"}]
        second = ExecutionContext(
            session_id="test-session",
            trace_id="second-trace",
            user_message="and then?",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            messages=[*history, {"role": "user", "content": "and then?"}],
            memory_context=[_episode("conv-2", "a second recall")],
        )
        second.turn_started_at = datetime(2026, 9, 10, 15, 20, tzinfo=UTC)
        turn2 = await _drive_loop(second, 1, monkeypatch)
        prior = turn1[0]
        assert [json.dumps(m, sort_keys=True) for m in turn2[0][1 : len(prior)]] == [
            json.dumps(m, sort_keys=True) for m in prior[1:]
        ]
        ids = [m.get("tool_call_id") for m in turn2[0] if m.get("name") == MEMORY_RECALL_TOOL]
        assert ids == [harness_call_id("mem", "test-trace"), harness_call_id("mem", "second-trace")]


# ── Supporting changes ──────────────────────────────────────────────────────────


class TestContextRetryTrim:
    def test_harness_results_are_never_stubbed(self) -> None:
        from personal_agent.orchestrator.executor import _trim_messages_for_context_retry

        mem_call, mem_result = harness_tool_exchange(
            call_id=harness_call_id("mem", "c" * 32), tool_name=MEMORY_RECALL_TOOL, content="M" * 50
        )
        wrk_call, wrk_result = harness_tool_exchange(
            call_id=harness_call_id("wrk", "c" * 32),
            tool_name=WORKER_REPORTS_TOOL,
            content="W" * 50,
        )
        real = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"r{i}"}]}
            if j == 0
            else {
                "role": "tool",
                "tool_call_id": f"r{i}",
                "name": "web_search",
                "content": "R" * 50,
            }
            for i in range(2)
            for j in range(2)
        ]
        messages = [
            {"role": "user", "content": "q"},
            wrk_call,
            wrk_result,
            mem_call,
            mem_result,
            *real,
        ]
        trimmed, dropped, _ = _trim_messages_for_context_retry(messages)
        assert dropped == 1
        assert trimmed[2] == wrk_result
        assert trimmed[4] == mem_result
        assert trimmed[6]["content"] != "R" * 50  # the older real result is stubbed
        assert trimmed[8]["content"] == "R" * 50  # the newest is kept


class TestContextWindowEviction:
    def test_a_harness_result_quoting_error_keywords_is_not_evicted(self) -> None:
        """Recalled text may quote '"error"'; that never makes the memory result an error."""
        from personal_agent.orchestrator.context_window import _is_tool_error_message

        _, mem_result = harness_tool_exchange(
            call_id=harness_call_id("mem", "f" * 32),
            tool_name=MEMORY_RECALL_TOOL,
            content='the owner once saw {"error": "timeout"} in a log',
        )
        real = {"role": "tool", "tool_call_id": "call_t0_0_x", "content": '{"error": "x"}'}
        assert not _is_tool_error_message(mem_result)
        assert _is_tool_error_message(real)


class TestTurnEvidenceAdmission:
    @pytest.mark.asyncio
    async def test_memory_not_admitted_when_its_result_misses_the_wire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wire without this turn's memory result admits nothing, even with the fence."""
        from personal_agent.captains_log.turn_evidence import InlineOutcome, build_turn_evidence

        memory = [_episode("conv-1", f"summary {_MEMORY_MARKER}")]
        ctx = _memory_ctx(memory)
        wires = await _drive_loop(ctx, 1, monkeypatch)
        without = [
            m
            for m in wires[0]
            if m.get("name") != MEMORY_RECALL_TOOL
            and not (m.get("role") == "assistant" and m.get("tool_calls"))
        ]
        kwargs: dict[str, Any] = {
            "candidates": ctx.recall_candidates,
            "memory_context_present": True,
            "rendered_identities": list(ctx.turn_evidence.assembled_context.memory_identities),
            "inline_outcome": InlineOutcome.INLINED,
            "system_prompt": "",
            "user_message": ctx.user_message,
            "skill_bodies": [],
            "call_index": 0,
            "prompt_component_ids": ["memory_section"],
            "memory_result_call_id": harness_call_id("mem", ctx.trace_id),
        }
        dropped = build_turn_evidence(wire_messages=without, **kwargs)
        landed = build_turn_evidence(wire_messages=wires[0], **kwargs)
        assert dropped.assembled_context.memory_identities == []
        assert "memory_section" not in dropped.assembled_context.prompt_component_ids
        assert landed.assembled_context.memory_identities
        assert "memory_section" in landed.assembled_context.prompt_component_ids

    @pytest.mark.asyncio
    async def test_a_prior_turns_memory_result_does_not_stand_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.captains_log.turn_evidence import InlineOutcome, build_turn_evidence

        ctx = _memory_ctx([_episode("conv-1", "summary")])
        wires = await _drive_loop(ctx, 1, monkeypatch)
        evidence = build_turn_evidence(
            candidates=ctx.recall_candidates,
            memory_context_present=True,
            rendered_identities=list(ctx.turn_evidence.assembled_context.memory_identities),
            inline_outcome=InlineOutcome.INLINED,
            wire_messages=wires[0],
            system_prompt="",
            user_message=ctx.user_message,
            skill_bodies=[],
            call_index=0,
            memory_result_call_id=harness_call_id("mem", "a-later-turn"),
        )
        assert evidence.assembled_context.memory_identities == []


class TestProviderShapes:
    @pytest.mark.asyncio
    async def test_hybrid_sequence_survives_role_fixer_and_sanitiser(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory = [_episode("conv-1", "summary")]
        wires = await _drive_loop(_memory_ctx(memory, hybrid=True), 1, monkeypatch)
        assert [m.get("role") for m in wires[0]] == [
            "system",
            "user",  # query + synthesis instruction, merged by the role fixer as before
            "assistant",
            "tool",
            "assistant",
            "tool",
        ]
        assert [m.get("name") for m in wires[0] if m.get("role") == "tool"] == [
            WORKER_REPORTS_TOOL,
            MEMORY_RECALL_TOOL,
        ]

    def test_anthropic_transform_accepts_the_exchange_without_tools(self) -> None:
        """Litellm injects a dummy tool when ``tools`` is absent and history has tool blocks."""
        from litellm.llms.anthropic.chat.transformation import AnthropicConfig

        call, result = harness_tool_exchange(
            call_id=harness_call_id("mem", "d" * 32),
            tool_name=MEMORY_RECALL_TOOL,
            content=f"recall {_MEMORY_MARKER}",
        )
        request = AnthropicConfig().transform_request(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "q"}, call, result],
            optional_params={},
            litellm_params={},
            headers={},
        )
        assert request.get("tools"), "Anthropic requires tools when the history has tool blocks"
        last = request["messages"][-1]
        assert last["role"] == "user"
        blocks = [b for b in last["content"] if b.get("type") == "tool_result"]
        assert blocks and _MEMORY_MARKER in json.dumps(blocks)

    def test_anthropic_history_end_breakpoint_stays_before_the_carrier(self) -> None:
        from personal_agent.llm_client.litellm_client import _decorated_anthropic_copy

        call, result = harness_tool_exchange(
            call_id=harness_call_id("mem", "e" * 32), tool_name=MEMORY_RECALL_TOOL, content="m"
        )
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "turn one"},
            {"role": "assistant", "content": "answer one"},
            {"role": "user", "content": "<turn_context>x</turn_context>\n\nturn two"},
            call,
            result,
        ]
        wire, _ = _decorated_anthropic_copy(messages, None, frozen_layout=True)
        marked = [
            i
            for i, m in enumerate(wire)
            if isinstance(m.get("content"), list)
            and any(isinstance(b, dict) and "cache_control" in b for b in m["content"])
        ]
        assert marked == [0, 2]
