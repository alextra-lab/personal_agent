"""The harness tool exchange — the carrier for untrusted input (FRE-1360, ADR-0140 T2)."""

from __future__ import annotations

import pytest

from personal_agent.orchestrator.untrusted_channel import (
    MEMORY_RECALL_TOOL,
    WORKER_REPORTS_TOOL,
    harness_call_id,
    harness_tool_exchange,
    is_harness_tool_result,
)

_TRACE = "0123456789abcdef0123456789abcdef"


class TestHarnessCallId:
    def test_ids_differ_by_kind_and_are_deterministic(self) -> None:
        assert harness_call_id("mem", _TRACE) == harness_call_id("mem", _TRACE)
        assert harness_call_id("mem", _TRACE) != harness_call_id("wrk", _TRACE)

    def test_ids_differ_by_turn(self) -> None:
        assert harness_call_id("mem", _TRACE) != harness_call_id("mem", "f" * 32)

    def test_ids_keep_enough_of_the_trace_to_stay_unique(self) -> None:
        """Two trace ids that share a 16-character prefix still get different ids."""
        assert harness_call_id("mem", "a" * 16 + "1" * 16) != harness_call_id(
            "mem", "a" * 16 + "2" * 16
        )

    @pytest.mark.parametrize("kind", ["mem", "wrk"])
    def test_ids_fit_the_strictest_provider_id_rules(self, kind: str) -> None:
        """OpenAI caps tool-call ids at 40 chars; Anthropic allows only [A-Za-z0-9_-]."""
        call_id = harness_call_id(kind, _TRACE)  # type: ignore[arg-type]
        assert len(call_id) <= 40
        assert all(c.isalnum() or c in "_-" for c in call_id)


class TestHarnessToolExchange:
    def test_exchange_is_a_matched_call_and_result(self) -> None:
        call_id = harness_call_id("mem", _TRACE)
        call, result = harness_tool_exchange(
            call_id=call_id, tool_name=MEMORY_RECALL_TOOL, content="recalled bytes"
        )
        assert call["role"] == "assistant"
        assert call["tool_calls"] == [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": MEMORY_RECALL_TOOL, "arguments": "{}"},
            }
        ]
        assert result == {
            "tool_call_id": call_id,
            "role": "tool",
            "name": MEMORY_RECALL_TOOL,
            "content": "recalled bytes",
        }

    def test_content_is_carried_unchanged(self) -> None:
        content = "## Your Memory Graph\n- item  with  spacing\n"
        _, result = harness_tool_exchange(
            call_id="call_mem_x", tool_name=MEMORY_RECALL_TOOL, content=content
        )
        assert result["content"] == content

    def test_sanitiser_leaves_the_exchange_untouched(self) -> None:
        from personal_agent.llm_client.history_sanitiser import sanitise_messages

        call, result = harness_tool_exchange(
            call_id=harness_call_id("mem", _TRACE), tool_name=MEMORY_RECALL_TOOL, content="m"
        )
        messages = [{"role": "user", "content": "q"}, call, result]
        cleaned, report = sanitise_messages(messages, trace_id="t", emit_telemetry=False)
        assert cleaned == messages
        assert not report.was_dirty


class TestIsHarnessToolResult:
    @pytest.mark.parametrize("kind", ["mem", "wrk"])
    def test_harness_results_are_recognised(self, kind: str) -> None:
        tool = MEMORY_RECALL_TOOL if kind == "mem" else WORKER_REPORTS_TOOL
        _, result = harness_tool_exchange(
            call_id=harness_call_id(kind, _TRACE),  # type: ignore[arg-type]
            tool_name=tool,
            content="x",
        )
        assert is_harness_tool_result(result)

    def test_a_real_tool_result_is_not(self) -> None:
        real = {"tool_call_id": "call_t0_0_abc", "role": "tool", "name": "web_search"}
        assert not is_harness_tool_result(real)

    def test_a_non_tool_message_is_not(self) -> None:
        assert not is_harness_tool_result({"role": "user", "content": "call_mem_x"})
