"""Tests for event models (ADR-0041, ADR-0054)."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from personal_agent.events.models import (
    CG_CAPTAIN_LOG,
    CG_CONSOLIDATOR,
    CG_ES_INDEXER,
    CG_FEEDBACK,
    CG_FRESHNESS,
    CG_INSIGHTS,
    CG_PROMOTION,
    CG_SESSION_WRITER,
    STREAM_CONSOLIDATION_COMPLETED,
    STREAM_FEEDBACK_RECEIVED,
    STREAM_INSIGHTS_COST_ANOMALY,
    STREAM_INSIGHTS_PATTERN_DETECTED,
    STREAM_MEMORY_ACCESSED,
    STREAM_PROMOTION_ISSUE_CREATED,
    STREAM_REQUEST_CAPTURED,
    STREAM_REQUEST_COMPLETED,
    STREAM_SYSTEM_IDLE,
    AccessContext,
    ConsolidationCompletedEvent,
    FeedbackReceivedEvent,
    InsightsCostAnomalyEvent,
    InsightsPatternDetectedEvent,
    MemoryAccessedEvent,
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
        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        with pytest.raises(Exception):  # ValidationError for frozen models
            event.trace_id = "t2"  # type: ignore[misc]

    def test_auto_event_id(self) -> None:
        """Each event gets a unique event_id by default."""
        e1 = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        e2 = RequestCapturedEvent(trace_id="t2", session_id="s2", source_component="test")
        assert e1.event_id != e2.event_id
        assert len(e1.event_id) == 32  # uuid4 hex

    def test_auto_created_at(self) -> None:
        """created_at defaults to UTC now."""
        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        assert event.created_at.tzinfo is not None
        assert (datetime.now(timezone.utc) - event.created_at).total_seconds() < 2


class TestEventBaseFlattenedFields:
    """ADR-0054: feedback-stream contract fields live on EventBase itself."""

    def test_source_component_is_required(self) -> None:
        """EventBase subclasses raise ValidationError when source_component is missing."""
        with pytest.raises(ValidationError, match="source_component"):
            RequestCapturedEvent(trace_id="t", session_id="s")  # type: ignore[call-arg]

    def test_schema_version_defaults_to_one(self) -> None:
        """schema_version defaults to 1 and is carried in serialised payload."""
        event = RequestCapturedEvent(trace_id="t", session_id="s", source_component="test")
        assert event.schema_version == 1
        assert event.model_dump(mode="json")["schema_version"] == 1

    def test_scheduled_event_leaves_trace_id_none(self) -> None:
        """Scheduled / system events inherit nullable trace_id and leave it None."""
        event = ConsolidationCompletedEvent(
            captures_processed=0,
            entities_created=0,
            entities_promoted=0,
            source_component="test",
        )
        assert event.trace_id is None
        assert event.session_id is None

    def test_forward_compat_ignores_unknown_fields(self) -> None:
        """Consumer compiled at schema_version=1 parses a schema_version=2 payload.

        Producer upgrades must not pause consumer fleets — extra fields are
        silently dropped (ADR-0054 §D5 Rule 3).
        """
        payload = {
            "event_type": "consolidation.completed",
            "captures_processed": 0,
            "entities_created": 0,
            "entities_promoted": 0,
            "source_component": "brainstem.scheduler",
            "schema_version": 2,
            # Hypothetical future field:
            "new_v2_only_field": "ignored-by-old-consumer",
        }
        parsed = parse_stream_event(payload)
        assert isinstance(parsed, ConsolidationCompletedEvent)
        assert parsed.schema_version == 2

    def test_source_component_round_trips_through_json(self) -> None:
        """source_component survives JSON serialisation round-trip."""
        event = RequestCapturedEvent(
            trace_id="t", session_id="s", source_component="orchestrator.executor"
        )
        restored = RequestCapturedEvent.model_validate_json(event.model_dump_json())
        assert restored.source_component == "orchestrator.executor"


class TestRequestCapturedEvent:
    """RequestCapturedEvent model tests."""

    def test_event_type_discriminator(self) -> None:
        """event_type is always 'request.captured'."""
        event = RequestCapturedEvent(trace_id="abc", session_id="def", source_component="test")
        assert event.event_type == "request.captured"

    def test_serialization_roundtrip(self) -> None:
        """Model can serialize to dict and back."""
        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        data = event.model_dump(mode="json")
        restored = RequestCapturedEvent.model_validate(data)
        assert restored.trace_id == event.trace_id
        assert restored.session_id == event.session_id
        assert restored.event_type == "request.captured"
        assert restored.event_id == event.event_id

    def test_json_roundtrip(self) -> None:
        """Model can serialize to JSON string and back."""
        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
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
            source_component="test",
        )
        assert event.event_type == "request.completed"

    def test_serialization_roundtrip(self) -> None:
        event = RequestCompletedEvent(
            trace_id="t1",
            session_id="s1",
            assistant_response="reply",
            trace_summary={"total_duration_ms": 2.5, "total_steps": 1, "phases_summary": {"a": 1}},
            trace_breakdown=[{"phase": "setup", "name": "n", "sequence": 1}],
            source_component="test",
        )
        data = event.model_dump(mode="json")
        restored = RequestCompletedEvent.model_validate(data)
        assert restored == event


class TestParseStreamEvent:
    """parse_stream_event dispatches on event_type."""

    def test_request_captured_restores_subclass_fields(self) -> None:
        event = RequestCapturedEvent(trace_id="tx", session_id="sx", source_component="test")
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
            source_component="test",
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
            captures_processed=5,
            entities_created=10,
            entities_promoted=2,
            source_component="test",
        )
        assert event.event_type == "consolidation.completed"

    def test_consolidation_completed_roundtrip(self) -> None:
        event = ConsolidationCompletedEvent(
            captures_processed=3,
            entities_created=7,
            entities_promoted=1,
            source_component="test",
        )
        payload = event.model_dump(mode="json")
        parsed = parse_stream_event(payload)
        assert isinstance(parsed, ConsolidationCompletedEvent)
        assert parsed.captures_processed == 3
        assert parsed.entities_created == 7
        assert parsed.entities_promoted == 1

    def test_promotion_issue_created_event_type(self) -> None:
        event = PromotionIssueCreatedEvent(
            entry_id="CL-001",
            linear_issue_id="FRE-99",
            fingerprint="abc123",
            source_component="test",
        )
        assert event.event_type == "promotion.issue_created"

    def test_promotion_issue_created_roundtrip(self) -> None:
        event = PromotionIssueCreatedEvent(
            entry_id="CL-001",
            linear_issue_id="FRE-99",
            fingerprint=None,
            source_component="test",
        )
        payload = event.model_dump(mode="json")
        parsed = parse_stream_event(payload)
        assert isinstance(parsed, PromotionIssueCreatedEvent)
        assert parsed.entry_id == "CL-001"
        assert parsed.linear_issue_id == "FRE-99"
        assert parsed.fingerprint is None

    def test_feedback_received_event_type(self) -> None:
        event = FeedbackReceivedEvent(
            issue_id="uuid-1",
            issue_identifier="FRE-10",
            label="Rejected",
            source_component="test",
        )
        assert event.event_type == "feedback.received"

    def test_feedback_received_roundtrip(self) -> None:
        event = FeedbackReceivedEvent(
            issue_id="uuid-2",
            issue_identifier="FRE-11",
            label="Approved",
            fingerprint="fp42",
            source_component="test",
        )
        payload = event.model_dump(mode="json")
        parsed = parse_stream_event(payload)
        assert isinstance(parsed, FeedbackReceivedEvent)
        assert parsed.label == "Approved"
        assert parsed.fingerprint == "fp42"

    def test_system_idle_event_type(self) -> None:
        event = SystemIdleEvent(idle_seconds=300.0, source_component="test")
        assert event.event_type == "system.idle"
        assert event.trigger == "monitoring_loop"

    def test_system_idle_roundtrip(self) -> None:
        event = SystemIdleEvent(
            idle_seconds=120.5,
            trigger="lifecycle_loop",
            source_component="test",
        )
        payload = event.model_dump(mode="json")
        parsed = parse_stream_event(payload)
        assert isinstance(parsed, SystemIdleEvent)
        assert parsed.idle_seconds == 120.5
        assert parsed.trigger == "lifecycle_loop"


class TestAccessContext:
    """AccessContext enum (ADR-0042)."""

    def test_all_five_contexts_defined(self) -> None:
        members = {m.value for m in AccessContext}
        assert members == {
            "search",
            "context_assembly",
            "consolidation",
            "suggest_relevant",
            "tool_call",
        }

    def test_is_str_subclass(self) -> None:
        """AccessContext values are plain strings for JSON serialisation."""
        assert isinstance(AccessContext.SEARCH, str)
        assert AccessContext.SEARCH == "search"

    def test_roundtrip_via_value(self) -> None:
        for ctx in AccessContext:
            assert AccessContext(ctx.value) is ctx


class TestMemoryAccessedEvent:
    """MemoryAccessedEvent model (ADR-0042)."""

    def _make(self, **kwargs: object) -> MemoryAccessedEvent:
        defaults: dict[str, object] = dict(
            entity_ids=["e1", "e2"],
            relationship_ids=["r1"],
            access_context=AccessContext.SEARCH,
            query_type="query_memory",
            trace_id="trace-abc",
            source_component="test",
        )
        defaults.update(kwargs)
        return MemoryAccessedEvent(**defaults)  # type: ignore[arg-type]

    def test_event_type_discriminator(self) -> None:
        event = self._make()
        assert event.event_type == "memory.accessed"

    def test_frozen(self) -> None:
        event = self._make()
        with pytest.raises(Exception):
            event.trace_id = "other"  # type: ignore[misc]

    def test_access_context_typed(self) -> None:
        event = self._make(access_context=AccessContext.CONTEXT_ASSEMBLY)
        assert event.access_context is AccessContext.CONTEXT_ASSEMBLY

    def test_session_id_optional(self) -> None:
        assert self._make().session_id is None
        assert self._make(session_id="s1").session_id == "s1"

    def test_serialization_roundtrip_dict(self) -> None:
        event = self._make(
            entity_ids=["e1"],
            relationship_ids=["r1"],
            access_context=AccessContext.TOOL_CALL,
            query_type="memory_search",
            trace_id="t-1",
            session_id="s-1",
        )
        data = event.model_dump(mode="json")
        restored = MemoryAccessedEvent.model_validate(data)
        assert restored.entity_ids == ["e1"]
        assert restored.relationship_ids == ["r1"]
        assert restored.access_context is AccessContext.TOOL_CALL
        assert restored.query_type == "memory_search"
        assert restored.trace_id == "t-1"
        assert restored.session_id == "s-1"
        assert restored.event_type == "memory.accessed"

    def test_json_roundtrip(self) -> None:
        event = self._make()
        restored = MemoryAccessedEvent.model_validate_json(event.model_dump_json())
        assert restored == event

    def test_parse_stream_event_dispatches(self) -> None:
        event = self._make(access_context=AccessContext.CONSOLIDATION)
        payload = event.model_dump(mode="json")
        parsed = parse_stream_event(payload)
        assert isinstance(parsed, MemoryAccessedEvent)
        assert parsed.access_context is AccessContext.CONSOLIDATION
        assert parsed.relationship_ids == event.relationship_ids

    def test_access_context_serialises_as_string(self) -> None:
        """JSON payload carries the string value, not the enum member name."""
        event = self._make(access_context=AccessContext.SUGGEST_RELEVANT)
        data = event.model_dump(mode="json")
        assert data["access_context"] == "suggest_relevant"


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

    def test_phase4_constants(self) -> None:
        assert STREAM_MEMORY_ACCESSED == "stream:memory.accessed"
        assert CG_FRESHNESS == "cg:freshness"

    def test_wave2_streams(self) -> None:
        """Wave 2 (ADR-0057) stream constants are correctly defined."""
        assert STREAM_INSIGHTS_PATTERN_DETECTED == "stream:insights.pattern_detected"
        assert STREAM_INSIGHTS_COST_ANOMALY == "stream:insights.cost_anomaly"


class TestInsightsPatternDetectedEvent:
    """InsightsPatternDetectedEvent (ADR-0057)."""

    def test_event_type(self) -> None:
        """event_type is always 'insights.pattern_detected'."""
        event = InsightsPatternDetectedEvent(
            source_component="insights.engine",
            insight_type="correlation",
            pattern_kind="",
            title="Higher failure risk when memory is elevated",
            summary="Task success rate is 70% while memory p90 is 78%",
            confidence=0.68,
            actionable=True,
            evidence={"task_success_rate": 0.7, "memory_p90_percent": 78.0},
            fingerprint="a1b2c3d4e5f60718",
            analysis_window_days=7,
        )
        assert event.event_type == "insights.pattern_detected"
        assert event.trace_id is None
        assert event.session_id is None

    def test_parse_stream_event_dispatch(self) -> None:
        """parse_stream_event correctly dispatches insights.pattern_detected payloads."""
        event = InsightsPatternDetectedEvent(
            source_component="insights.engine",
            insight_type="anomaly",
            pattern_kind="",
            title="Cost spike detected",
            summary="Cost spike: $4.12 vs $1.28 avg.",
            confidence=0.75,
            actionable=True,
            evidence={"ratio": 3.22},
            fingerprint="c1d2e3f4a5b6c7d8",
            analysis_window_days=14,
        )
        parsed = parse_stream_event(event.model_dump(mode="json"))
        assert isinstance(parsed, InsightsPatternDetectedEvent)
        assert parsed.pattern_kind == ""


class TestInsightsCostAnomalyEvent:
    """InsightsCostAnomalyEvent (ADR-0057)."""

    def test_event_type(self) -> None:
        """event_type is always 'insights.cost_anomaly'."""
        event = InsightsCostAnomalyEvent(
            source_component="insights.engine",
            anomaly_type="daily_cost_spike",
            message="Cost spike: $4.12 on 2026-04-19 vs $1.28 avg.",
            observed_cost_usd=4.12,
            baseline_cost_usd=1.28,
            ratio=3.22,
            confidence=0.75,
            severity="medium",
            fingerprint="c1d2e3f4a5b6c7d8",
            observation_date="2026-04-19",
        )
        assert event.event_type == "insights.cost_anomaly"
        assert event.trace_id is None
        assert event.session_id is None

    def test_parse_stream_event_dispatch(self) -> None:
        """parse_stream_event correctly dispatches insights.cost_anomaly payloads."""
        event = InsightsCostAnomalyEvent(
            source_component="insights.engine",
            anomaly_type="daily_cost_spike",
            message="m",
            observed_cost_usd=5.0,
            baseline_cost_usd=1.0,
            ratio=5.0,
            confidence=0.85,
            severity="high",
            fingerprint="ff00ee11dd22cc33",
            observation_date="2026-04-20",
        )
        parsed = parse_stream_event(event.model_dump(mode="json"))
        assert isinstance(parsed, InsightsCostAnomalyEvent)
        assert parsed.severity == "high"


class TestCompactionQualityIncidentEvent:
    """Tests for the FRE-249 / ADR-0059 compaction quality event type."""

    def test_event_type_literal(self) -> None:
        from personal_agent.events.models import CompactionQualityIncidentEvent

        event = CompactionQualityIncidentEvent(
            trace_id="t",
            session_id="s",
            fingerprint="fp01234567890abc",
            noun_phrase="cache",
            dropped_entity="redis",
            recall_cue="what was our cache again",
            tier_affected="episodic",
            tokens_removed=42,
            detected_at=datetime.now(timezone.utc),
        )
        assert event.event_type == "context.compaction_quality_poor"
        assert event.source_component == "telemetry.context_quality"

    def test_parse_stream_event_dispatch(self) -> None:
        from personal_agent.events.models import CompactionQualityIncidentEvent

        event = CompactionQualityIncidentEvent(
            trace_id="t",
            session_id="s",
            fingerprint="fp01234567890abc",
            noun_phrase="cache",
            dropped_entity="redis",
            recall_cue="what was our cache again",
            tier_affected="episodic",
            tokens_removed=42,
            detected_at=datetime.now(timezone.utc),
        )
        parsed = parse_stream_event(event.model_dump(mode="json"))
        assert isinstance(parsed, CompactionQualityIncidentEvent)
        assert parsed.fingerprint == event.fingerprint
        assert parsed.noun_phrase == "cache"


class TestWithinSessionCompressionEvent:
    """Wave 4 — ADR-0061 within-session compression event."""

    def test_event_type_literal(self) -> None:
        from personal_agent.events.models import WithinSessionCompressionEvent

        event = WithinSessionCompressionEvent(
            trace_id="t",
            session_id="s",
            trigger="hard",
            head_tokens=100,
            middle_tokens_in=5000,
            middle_tokens_out=300,
            tail_tokens=400,
            pre_pass_replacements=2,
            summariser_called=True,
            summariser_duration_ms=900,
            tokens_saved=4700,
        )
        assert event.event_type == "context.within_session_compressed"
        assert event.source_component == "orchestrator.within_session_compression"
        assert event.trigger == "hard"

    def test_parse_stream_event_dispatch(self) -> None:
        from personal_agent.events.models import WithinSessionCompressionEvent

        event = WithinSessionCompressionEvent(
            trace_id="t",
            session_id="s",
            trigger="soft",
            head_tokens=10,
            middle_tokens_in=4000,
            middle_tokens_out=250,
            tail_tokens=200,
            pre_pass_replacements=1,
            summariser_called=True,
            summariser_duration_ms=750,
            tokens_saved=3750,
        )
        parsed = parse_stream_event(event.model_dump(mode="json"))
        assert isinstance(parsed, WithinSessionCompressionEvent)
        assert parsed.trigger == "soft"
        assert parsed.tokens_saved == 3750

    def test_required_fields_enforced(self) -> None:
        from pydantic import ValidationError

        from personal_agent.events.models import WithinSessionCompressionEvent

        with pytest.raises(ValidationError):
            # Missing trigger / token fields should fail validation.
            WithinSessionCompressionEvent(  # type: ignore[call-arg]
                trace_id="t",
                session_id="s",
            )

    def test_invalid_trigger_value_rejected(self) -> None:
        from pydantic import ValidationError

        from personal_agent.events.models import WithinSessionCompressionEvent

        with pytest.raises(ValidationError):
            WithinSessionCompressionEvent(
                trace_id="t",
                session_id="s",
                trigger="medium",  # type: ignore[arg-type]
                head_tokens=10,
                middle_tokens_in=100,
                middle_tokens_out=10,
                tail_tokens=10,
                pre_pass_replacements=0,
                summariser_called=False,
                summariser_duration_ms=0,
                tokens_saved=90,
            )
