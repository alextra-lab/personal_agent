"""Tests for event models (ADR-0041)."""

from datetime import datetime, timezone

import pytest

from personal_agent.events.models import (
    CG_CONSOLIDATOR,
    STREAM_REQUEST_CAPTURED,
    EventBase,
    RequestCapturedEvent,
)


class TestEventBase:
    """EventBase model tests."""

    def test_frozen(self) -> None:
        """EventBase instances are immutable."""
        event = RequestCapturedEvent(trace_id="t1", session_id="s1")
        with pytest.raises(Exception):  # ValidationError for frozen models
            event.trace_id = "t2"  # type: ignore[misc]

    def test_auto_event_id(self) -> None:
        """Each event gets a unique event_id by default."""
        e1 = RequestCapturedEvent(trace_id="t1", session_id="s1")
        e2 = RequestCapturedEvent(trace_id="t2", session_id="s2")
        assert e1.event_id != e2.event_id
        assert len(e1.event_id) == 32  # uuid4 hex

    def test_auto_created_at(self) -> None:
        """created_at defaults to UTC now."""
        event = RequestCapturedEvent(trace_id="t1", session_id="s1")
        assert event.created_at.tzinfo is not None
        assert (datetime.now(timezone.utc) - event.created_at).total_seconds() < 2


class TestRequestCapturedEvent:
    """RequestCapturedEvent model tests."""

    def test_event_type_discriminator(self) -> None:
        """event_type is always 'request.captured'."""
        event = RequestCapturedEvent(trace_id="abc", session_id="def")
        assert event.event_type == "request.captured"

    def test_serialization_roundtrip(self) -> None:
        """Model can serialize to dict and back."""
        event = RequestCapturedEvent(trace_id="t1", session_id="s1")
        data = event.model_dump(mode="json")
        restored = RequestCapturedEvent.model_validate(data)
        assert restored.trace_id == event.trace_id
        assert restored.session_id == event.session_id
        assert restored.event_type == "request.captured"
        assert restored.event_id == event.event_id

    def test_json_roundtrip(self) -> None:
        """Model can serialize to JSON string and back."""
        event = RequestCapturedEvent(trace_id="t1", session_id="s1")
        json_str = event.model_dump_json()
        restored = RequestCapturedEvent.model_validate_json(json_str)
        assert restored == event


class TestConstants:
    """Stream and consumer group constants."""

    def test_stream_name(self) -> None:
        assert STREAM_REQUEST_CAPTURED == "stream:request.captured"

    def test_consumer_group_name(self) -> None:
        assert CG_CONSOLIDATOR == "cg:consolidator"
