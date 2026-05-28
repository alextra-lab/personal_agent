"""Tests for AG-UI adapter seq field (ADR-0075 / FRE-388)."""

from __future__ import annotations

from personal_agent.transport.agui.adapter import serialize_event, to_agui_event
from personal_agent.transport.events import (
    ClassifiedErrorEvent,
    InterruptEvent,
    StateUpdateEvent,
    TextDeltaEvent,
    ToolApprovalRequestEvent,
    ToolEndEvent,
    ToolStartEvent,
)


class TestToAguiEventSeq:
    """Verify to_agui_event includes seq when provided."""

    def test_text_delta_with_seq(self) -> None:
        event = TextDeltaEvent(text="hello", session_id="s1")
        result = to_agui_event(event, seq=42)
        assert result["seq"] == 42
        assert result["type"] == "TEXT_DELTA"

    def test_text_delta_without_seq(self) -> None:
        event = TextDeltaEvent(text="hello", session_id="s1")
        result = to_agui_event(event)
        assert result["seq"] is None

    def test_tool_start_with_seq(self) -> None:
        event = ToolStartEvent(tool_name="bash", args={"cmd": "ls"}, session_id="s1")
        result = to_agui_event(event, seq=10)
        assert result["seq"] == 10
        assert result["type"] == "TOOL_CALL_START"

    def test_tool_end_with_seq(self) -> None:
        event = ToolEndEvent(tool_name="bash", result_summary="ok", session_id="s1")
        result = to_agui_event(event, seq=11)
        assert result["seq"] == 11

    def test_state_delta_with_seq(self) -> None:
        event = StateUpdateEvent(key="mode", value="NORMAL", session_id="s1")
        result = to_agui_event(event, seq=5)
        assert result["seq"] == 5

    def test_interrupt_with_seq(self) -> None:
        event = InterruptEvent(context="approve?", options=["yes", "no"], session_id="s1")
        result = to_agui_event(event, seq=99)
        assert result["seq"] == 99

    def test_tool_approval_with_seq(self) -> None:
        event = ToolApprovalRequestEvent(
            request_id="req-1",
            trace_id="tr-1",
            session_id="s1",
            tool="bash",
            args={"cmd": "rm -rf /"},
            risk_level="high",
            reason="dangerous",
            expires_at="2026-01-01T00:00:00Z",
        )
        result = to_agui_event(event, seq=77)
        assert result["seq"] == 77
        assert result["type"] == "tool_approval_request"


class TestSerializeEventSeq:
    """Verify serialize_event passes seq through."""

    def test_serialize_with_seq(self) -> None:
        import json

        event = TextDeltaEvent(text="hi", session_id="s1")
        raw = serialize_event(event, seq=5)
        parsed = json.loads(raw)
        assert parsed["seq"] == 5

    def test_serialize_without_seq(self) -> None:
        import json

        event = TextDeltaEvent(text="hi", session_id="s1")
        raw = serialize_event(event)
        parsed = json.loads(raw)
        assert parsed["seq"] is None


class TestClassifiedErrorEventSeq:
    """RUN_ERROR envelope passes seq through correctly (FRE-398)."""

    def _event(self) -> ClassifiedErrorEvent:
        return ClassifiedErrorEvent(
            session_id="s1",
            trace_id="t1",
            category="timeout",
            reason="timed out",
            next_step="retry",
            actions=["retry", "stop"],
            partial=False,
        )

    def test_with_seq(self) -> None:
        result = to_agui_event(self._event(), seq=55)
        assert result["seq"] == 55
        assert result["type"] == "RUN_ERROR"

    def test_without_seq(self) -> None:
        result = to_agui_event(self._event())
        assert result["seq"] is None
