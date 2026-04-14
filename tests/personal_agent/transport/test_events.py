"""Tests for transport layer internal event types."""
from __future__ import annotations

import pytest

from personal_agent.transport.events import (
    InterruptEvent,
    InternalEvent,
    StateUpdateEvent,
    TextDeltaEvent,
    ToolEndEvent,
    ToolStartEvent,
)


class TestTextDeltaEvent:
    def test_creation(self) -> None:
        event = TextDeltaEvent(text="hello world", session_id="s1")
        assert event.text == "hello world"
        assert event.session_id == "s1"

    def test_frozen(self) -> None:
        event = TextDeltaEvent(text="hi", session_id="s1")
        with pytest.raises(Exception):  # FrozenInstanceError
            event.text = "changed"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = TextDeltaEvent(text="x", session_id="s")
        b = TextDeltaEvent(text="x", session_id="s")
        assert a == b


class TestToolStartEvent:
    def test_creation(self) -> None:
        event = ToolStartEvent(tool_name="search", args={"q": "test"}, session_id="s2")
        assert event.tool_name == "search"
        assert event.args == {"q": "test"}
        assert event.session_id == "s2"

    def test_frozen(self) -> None:
        event = ToolStartEvent(tool_name="t", args={}, session_id="s")
        with pytest.raises(Exception):
            event.tool_name = "changed"  # type: ignore[misc]

    def test_empty_args(self) -> None:
        event = ToolStartEvent(tool_name="ping", args={}, session_id="s")
        assert event.args == {}


class TestToolEndEvent:
    def test_creation(self) -> None:
        event = ToolEndEvent(tool_name="search", result_summary="3 results", session_id="s3")
        assert event.tool_name == "search"
        assert event.result_summary == "3 results"
        assert event.session_id == "s3"

    def test_frozen(self) -> None:
        event = ToolEndEvent(tool_name="t", result_summary="r", session_id="s")
        with pytest.raises(Exception):
            event.result_summary = "changed"  # type: ignore[misc]


class TestStateUpdateEvent:
    def test_creation(self) -> None:
        event = StateUpdateEvent(key="mode", value="NORMAL", session_id="s4")
        assert event.key == "mode"
        assert event.value == "NORMAL"
        assert event.session_id == "s4"

    def test_any_value_type(self) -> None:
        event = StateUpdateEvent(key="budget", value=42, session_id="s")
        assert event.value == 42

        event2 = StateUpdateEvent(key="data", value={"nested": True}, session_id="s")
        assert event2.value == {"nested": True}

    def test_frozen(self) -> None:
        event = StateUpdateEvent(key="k", value="v", session_id="s")
        with pytest.raises(Exception):
            event.key = "changed"  # type: ignore[misc]


class TestInterruptEvent:
    def test_creation(self) -> None:
        event = InterruptEvent(
            context="Approve tool execution?",
            options=["approve", "reject"],
            session_id="s5",
        )
        assert event.context == "Approve tool execution?"
        assert list(event.options) == ["approve", "reject"]
        assert event.session_id == "s5"

    def test_frozen(self) -> None:
        event = InterruptEvent(context="c", options=["a"], session_id="s")
        with pytest.raises(Exception):
            event.context = "changed"  # type: ignore[misc]

    def test_multiple_options(self) -> None:
        event = InterruptEvent(
            context="Choose",
            options=["yes", "no", "maybe"],
            session_id="s",
        )
        assert len(list(event.options)) == 3


class TestInternalEventUnion:
    """Verify the discriminated union covers all event types."""

    def test_text_delta_is_internal_event(self) -> None:
        event: InternalEvent = TextDeltaEvent(text="x", session_id="s")
        assert isinstance(event, TextDeltaEvent)

    def test_tool_start_is_internal_event(self) -> None:
        event: InternalEvent = ToolStartEvent(tool_name="t", args={}, session_id="s")
        assert isinstance(event, ToolStartEvent)

    def test_tool_end_is_internal_event(self) -> None:
        event: InternalEvent = ToolEndEvent(tool_name="t", result_summary="r", session_id="s")
        assert isinstance(event, ToolEndEvent)

    def test_state_update_is_internal_event(self) -> None:
        event: InternalEvent = StateUpdateEvent(key="k", value="v", session_id="s")
        assert isinstance(event, StateUpdateEvent)

    def test_interrupt_is_internal_event(self) -> None:
        event: InternalEvent = InterruptEvent(context="c", options=["a"], session_id="s")
        assert isinstance(event, InterruptEvent)

    def test_pattern_matching(self) -> None:
        """Verify structural pattern matching works on all union members."""
        events: list[InternalEvent] = [
            TextDeltaEvent(text="hi", session_id="s"),
            ToolStartEvent(tool_name="t", args={}, session_id="s"),
            ToolEndEvent(tool_name="t", result_summary="r", session_id="s"),
            StateUpdateEvent(key="k", value=1, session_id="s"),
            InterruptEvent(context="c", options=[], session_id="s"),
        ]
        matched = []
        for event in events:
            match event:
                case TextDeltaEvent():
                    matched.append("text")
                case ToolStartEvent():
                    matched.append("tool_start")
                case ToolEndEvent():
                    matched.append("tool_end")
                case StateUpdateEvent():
                    matched.append("state")
                case InterruptEvent():
                    matched.append("interrupt")
        assert matched == ["text", "tool_start", "tool_end", "state", "interrupt"]
