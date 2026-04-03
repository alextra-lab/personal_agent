"""Tests for event models (ADR-0041)."""

from datetime import datetime, timezone

import pytest

from personal_agent.events.models import (
    CG_CAPTAIN_LOG,
    CG_CONSOLIDATOR,
    CG_ES_INDEXER,
    CG_FEEDBACK,
    CG_INSIGHTS,
    CG_PROMOTION,
    CG_SESSION_WRITER,
    STREAM_CONSOLIDATION_COMPLETED,
    STREAM_FEEDBACK_RECEIVED,
    STREAM_PROMOTION_ISSUE_CREATED,
    STREAM_REQUEST_CAPTURED,
    STREAM_REQUEST_COMPLETED,
    STREAM_SYSTEM_IDLE,
    ConsolidationCompletedEvent,
    EventBase,
    FeedbackReceivedEvent,
    PromotionIssueCreatedEvent,
    RequestCapturedEvent,
    RequestCompletedEvent,
    SystemIdleEvent,
    parse_stream_event,
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


class TestRequestCompletedEvent:
    """RequestCompletedEvent model tests."""

    def test_event_type(self) -> None:
        event = RequestCompletedEvent(
            trace_id="t1",
            session_id="s1",
            assistant_response="hi",
            trace_summary={"total_duration_ms": 1.0, "total_steps": 0, "phases_summary": {}},
            trace_breakdown=[],
        )
        assert event.event_type == "request.completed"

    def test_serialization_roundtrip(self) -> None:
        event = RequestCompletedEvent(
            trace_id="t1",
            session_id="s1",
            assistant_response="reply",
            trace_summary={"total_duration_ms": 2.5, "total_steps": 1, "phases_summary": {"a": 1}},
            trace_breakdown=[{"phase": "setup", "name": "n", "sequence": 1}],
        )
        data = event.model_dump(mode="json")
        restored = RequestCompletedEvent.model_validate(data)
        assert restored == event


class TestParseStreamEvent:
    """parse_stream_event dispatches on event_type."""

    def test_request_captured_restores_subclass_fields(self) -> None:
        event = RequestCapturedEvent(trace_id="tx", session_id="sx")
        payload = event.model_dump(mode="json")
        parsed = parse_stream_event(payload)
        assert isinstance(parsed, RequestCapturedEvent)
        assert parsed.trace_id == "tx"
        assert parsed.session_id == "sx"

    def test_request_completed(self) -> None:
        event = RequestCompletedEvent(
            trace_id="t",
            session_id="s",
            assistant_response="x",
            trace_summary={},
            trace_breakdown=[],
        )
        parsed = parse_stream_event(event.model_dump(mode="json"))
        assert isinstance(parsed, RequestCompletedEvent)
        assert parsed.assistant_response == "x"

    def test_unknown_event_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown"):
            parse_stream_event({"event_type": "nope"})


class TestPhase3Events:
    """Phase 3 event models (ADR-0041)."""

    def test_consolidation_completed_event_type(self) -> None:
        event = ConsolidationCompletedEvent(
            captures_processed=5, entities_created=10, entities_promoted=2
        )
        assert event.event_type == "consolidation.completed"

    def test_consolidation_completed_roundtrip(self) -> None:
        event = ConsolidationCompletedEvent(
            captures_processed=3, entities_created=7, entities_promoted=1
        )
        payload = event.model_dump(mode="json")
        parsed = parse_stream_event(payload)
        assert isinstance(parsed, ConsolidationCompletedEvent)
        assert parsed.captures_processed == 3
        assert parsed.entities_created == 7
        assert parsed.entities_promoted == 1

    def test_promotion_issue_created_event_type(self) -> None:
        event = PromotionIssueCreatedEvent(
            entry_id="CL-001", linear_issue_id="FRE-99", fingerprint="abc123"
        )
        assert event.event_type == "promotion.issue_created"

    def test_promotion_issue_created_roundtrip(self) -> None:
        event = PromotionIssueCreatedEvent(
            entry_id="CL-001", linear_issue_id="FRE-99", fingerprint=None
        )
        payload = event.model_dump(mode="json")
        parsed = parse_stream_event(payload)
        assert isinstance(parsed, PromotionIssueCreatedEvent)
        assert parsed.entry_id == "CL-001"
        assert parsed.linear_issue_id == "FRE-99"
        assert parsed.fingerprint is None

    def test_feedback_received_event_type(self) -> None:
        event = FeedbackReceivedEvent(
            issue_id="uuid-1", issue_identifier="FRE-10", label="Rejected"
        )
        assert event.event_type == "feedback.received"

    def test_feedback_received_roundtrip(self) -> None:
        event = FeedbackReceivedEvent(
            issue_id="uuid-2",
            issue_identifier="FRE-11",
            label="Approved",
            fingerprint="fp42",
        )
        payload = event.model_dump(mode="json")
        parsed = parse_stream_event(payload)
        assert isinstance(parsed, FeedbackReceivedEvent)
        assert parsed.label == "Approved"
        assert parsed.fingerprint == "fp42"

    def test_system_idle_event_type(self) -> None:
        event = SystemIdleEvent(idle_seconds=300.0)
        assert event.event_type == "system.idle"
        assert event.trigger == "monitoring_loop"

    def test_system_idle_roundtrip(self) -> None:
        event = SystemIdleEvent(idle_seconds=120.5, trigger="lifecycle_loop")
        payload = event.model_dump(mode="json")
        parsed = parse_stream_event(payload)
        assert isinstance(parsed, SystemIdleEvent)
        assert parsed.idle_seconds == 120.5
        assert parsed.trigger == "lifecycle_loop"


class TestConstants:
    """Stream and consumer group constants."""

    def test_stream_name(self) -> None:
        assert STREAM_REQUEST_CAPTURED == "stream:request.captured"

    def test_stream_request_completed(self) -> None:
        assert STREAM_REQUEST_COMPLETED == "stream:request.completed"

    def test_consumer_group_name(self) -> None:
        assert CG_CONSOLIDATOR == "cg:consolidator"

    def test_phase2_consumer_groups(self) -> None:
        assert CG_ES_INDEXER == "cg:es-indexer"
        assert CG_SESSION_WRITER == "cg:session-writer"

    def test_phase3_streams(self) -> None:
        assert STREAM_CONSOLIDATION_COMPLETED == "stream:consolidation.completed"
        assert STREAM_PROMOTION_ISSUE_CREATED == "stream:promotion.issue_created"
        assert STREAM_FEEDBACK_RECEIVED == "stream:feedback.received"
        assert STREAM_SYSTEM_IDLE == "stream:system.idle"

    def test_phase3_consumer_groups(self) -> None:
        assert CG_INSIGHTS == "cg:insights"
        assert CG_PROMOTION == "cg:promotion"
        assert CG_CAPTAIN_LOG == "cg:captain-log"
        assert CG_FEEDBACK == "cg:feedback"
