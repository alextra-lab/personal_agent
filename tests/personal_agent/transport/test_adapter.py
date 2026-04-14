"""Tests for the AG-UI adapter (event → wire format conversion)."""
from __future__ import annotations

import json

import pytest

from personal_agent.transport.agui.adapter import serialize_event, to_agui_event
from personal_agent.transport.events import (
    InterruptEvent,
    StateUpdateEvent,
    TextDeltaEvent,
    ToolEndEvent,
    ToolStartEvent,
)


class TestToAguiEvent:
    def test_text_delta(self) -> None:
        event = TextDeltaEvent(text="hello", session_id="s1")
        result = to_agui_event(event)
        assert result["type"] == "TEXT_DELTA"
        assert result["data"] == {"text": "hello"}
        assert result["session_id"] == "s1"

    def test_tool_start(self) -> None:
        event = ToolStartEvent(tool_name="web_search", args={"q": "python"}, session_id="s2")
        result = to_agui_event(event)
        assert result["type"] == "TOOL_CALL_START"
        assert result["data"]["tool_name"] == "web_search"
        assert result["data"]["args"] == {"q": "python"}
        assert result["session_id"] == "s2"

    def test_tool_start_empty_args(self) -> None:
        event = ToolStartEvent(tool_name="ping", args={}, session_id="s")
        result = to_agui_event(event)
        assert result["data"]["args"] == {}

    def test_tool_end(self) -> None:
        event = ToolEndEvent(tool_name="web_search", result_summary="5 results", session_id="s3")
        result = to_agui_event(event)
        assert result["type"] == "TOOL_CALL_END"
        assert result["data"]["tool_name"] == "web_search"
        assert result["data"]["result"] == "5 results"
        assert result["session_id"] == "s3"

    def test_state_update(self) -> None:
        event = StateUpdateEvent(key="mode", value="HYBRID", session_id="s4")
        result = to_agui_event(event)
        assert result["type"] == "STATE_DELTA"
        assert result["data"] == {"key": "mode", "value": "HYBRID"}
        assert result["session_id"] == "s4"

    def test_state_update_numeric_value(self) -> None:
        event = StateUpdateEvent(key="budget", value=3, session_id="s")
        result = to_agui_event(event)
        assert result["data"]["value"] == 3

    def test_interrupt(self) -> None:
        event = InterruptEvent(
            context="Approve?", options=["approve", "reject"], session_id="s5"
        )
        result = to_agui_event(event)
        assert result["type"] == "INTERRUPT"
        assert result["data"]["context"] == "Approve?"
        assert result["data"]["options"] == ["approve", "reject"]
        assert result["session_id"] == "s5"

    def test_interrupt_options_as_list(self) -> None:
        """Options from a tuple should be converted to list in wire format."""
        event = InterruptEvent(context="c", options=("yes", "no"), session_id="s")  # type: ignore[arg-type]
        result = to_agui_event(event)
        assert isinstance(result["data"]["options"], list)

    def test_args_mapping_converted_to_dict(self) -> None:
        """Mapping args must be serializable as plain dict."""
        from collections.abc import Mapping

        class FrozenMapping(Mapping):  # type: ignore[type-arg]
            def __init__(self, d: dict) -> None:
                self._d = d

            def __getitem__(self, k: str) -> object:
                return self._d[k]

            def __iter__(self):  # type: ignore[override]
                return iter(self._d)

            def __len__(self) -> int:
                return len(self._d)

        event = ToolStartEvent(
            tool_name="t", args=FrozenMapping({"x": 1}), session_id="s"
        )
        result = to_agui_event(event)
        assert result["data"]["args"] == {"x": 1}
        assert isinstance(result["data"]["args"], dict)


class TestSerializeEvent:
    def test_returns_valid_json(self) -> None:
        event = TextDeltaEvent(text="hi", session_id="s1")
        raw = serialize_event(event)
        parsed = json.loads(raw)
        assert parsed["type"] == "TEXT_DELTA"

    def test_tool_start_json(self) -> None:
        event = ToolStartEvent(tool_name="t", args={"a": 1}, session_id="s")
        raw = serialize_event(event)
        parsed = json.loads(raw)
        assert parsed["type"] == "TOOL_CALL_START"
        assert parsed["data"]["args"] == {"a": 1}

    def test_tool_end_json(self) -> None:
        event = ToolEndEvent(tool_name="t", result_summary="ok", session_id="s")
        raw = serialize_event(event)
        parsed = json.loads(raw)
        assert parsed["type"] == "TOOL_CALL_END"

    def test_state_update_json(self) -> None:
        event = StateUpdateEvent(key="k", value=42, session_id="s")
        raw = serialize_event(event)
        parsed = json.loads(raw)
        assert parsed["type"] == "STATE_DELTA"
        assert parsed["data"]["value"] == 42

    def test_interrupt_json(self) -> None:
        event = InterruptEvent(context="c", options=["a", "b"], session_id="s")
        raw = serialize_event(event)
        parsed = json.loads(raw)
        assert parsed["type"] == "INTERRUPT"
        assert parsed["data"]["options"] == ["a", "b"]

    def test_output_is_string(self) -> None:
        event = TextDeltaEvent(text="x", session_id="s")
        assert isinstance(serialize_event(event), str)

    def test_all_event_types_serialize(self) -> None:
        """Smoke test: all event types produce parseable JSON."""
        from personal_agent.transport.events import InternalEvent

        events: list[InternalEvent] = [
            TextDeltaEvent(text="t", session_id="s"),
            ToolStartEvent(tool_name="t", args={}, session_id="s"),
            ToolEndEvent(tool_name="t", result_summary="r", session_id="s"),
            StateUpdateEvent(key="k", value="v", session_id="s"),
            InterruptEvent(context="c", options=["a"], session_id="s"),
        ]
        for event in events:
            raw = serialize_event(event)
            parsed = json.loads(raw)
            assert "type" in parsed
            assert "session_id" in parsed
