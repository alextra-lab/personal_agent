"""Unit tests for evaluation harness components.

Tests the data model, telemetry checker (mocked ES), and runner (mocked HTTP).
These do NOT require a running agent — they verify harness correctness.
"""

from __future__ import annotations

import pytest

from tests.evaluation.harness.models import (
    AssertionResult,
    ConversationPath,
    ConversationTurn,
    EventPresenceAssertion,
    FieldAssertion,
    FieldComparisonAssertion,
    PathResult,
    TurnResult,
    absent,
    fld,
    gte,
    present,
)
from tests.evaluation.harness.telemetry import TelemetryChecker

# ---------------------------------------------------------------------------
# Data model tests
# ---------------------------------------------------------------------------

class TestModels:
    """Tests for frozen dataclasses and builder helpers."""

    def test_field_assertion_creation(self) -> None:
        """Verify fld() creates FieldAssertion with correct fields."""
        a = fld("intent_classified", "task_type", "analysis")
        assert isinstance(a, FieldAssertion)
        assert a.event_type == "intent_classified"
        assert a.field_name == "task_type"
        assert a.expected == "analysis"
        assert a.kind == "field"

    def test_presence_assertion_creation(self) -> None:
        """Verify present() creates EventPresenceAssertion with present=True."""
        a = present("hybrid_expansion_start")
        assert isinstance(a, EventPresenceAssertion)
        assert a.event_type == "hybrid_expansion_start"
        assert a.present is True
        assert a.kind == "presence"

    def test_absence_assertion_creation(self) -> None:
        """Verify absent() creates EventPresenceAssertion with present=False."""
        a = absent("tool_call_completed")
        assert isinstance(a, EventPresenceAssertion)
        assert a.event_type == "tool_call_completed"
        assert a.present is False

    def test_comparison_assertion_creation(self) -> None:
        """Verify gte() creates FieldComparisonAssertion with >= operator."""
        a = gte("hybrid_expansion_complete", "successes", 1)
        assert isinstance(a, FieldComparisonAssertion)
        assert a.operator == ">="
        assert a.threshold == 1

    def test_conversation_path_is_frozen(self) -> None:
        """Verify ConversationPath raises AttributeError on mutation attempt."""
        path = ConversationPath(
            path_id="CP-TEST",
            name="Test Path",
            category="Test",
            objective="Test objective",
            turns=(
                ConversationTurn(
                    user_message="Hello",
                    expected_behavior="Responds",
                    assertions=(fld("intent_classified", "task_type", "conversational"),),
                ),
            ),
        )
        with pytest.raises(AttributeError):
            path.name = "Changed"  # type: ignore[misc]

    def test_path_result_properties(self) -> None:
        """Verify PathResult computed properties with known assertion data."""
        result = PathResult(
            path_id="CP-01",
            path_name="Test",
            category="Test",
            session_id="abc-123",
        )
        # Add a turn with 2 assertions (1 pass, 1 fail)
        result.turns.append(
            TurnResult(
                turn_index=0,
                user_message="Hello",
                response_text="Hi",
                trace_id="trace-1",
                assertion_results=(
                    AssertionResult(
                        assertion=fld("x", "y", "z"),
                        passed=True,
                        actual_value="z",
                        message="ok",
                    ),
                    AssertionResult(
                        assertion=fld("x", "y", "z"),
                        passed=False,
                        actual_value="w",
                        message="fail",
                    ),
                ),
                response_time_ms=150.0,
            )
        )
        assert result.total_assertions == 2
        assert result.passed_assertions == 1
        assert result.failed_assertions == 1
        assert result.total_time_ms == 150.0


# ---------------------------------------------------------------------------
# Telemetry checker tests (mocked ES)
# ---------------------------------------------------------------------------

class TestTelemetryChecker:
    """Tests for TelemetryChecker.check_assertions (no ES needed)."""

    def setup_method(self) -> None:
        """Initialize a fresh TelemetryChecker for each test."""
        self.checker = TelemetryChecker()

    def test_field_assertion_passes(self) -> None:
        """Verify field assertion passes when event matches."""
        events: list[dict[str, object]] = [
            {"event_type": "intent_classified", "task_type": "analysis", "confidence": 0.8}
        ]
        results = self.checker.check_assertions(
            events,
            [fld("intent_classified", "task_type", "analysis")],
        )
        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].actual_value == "analysis"

    def test_field_assertion_fails_wrong_value(self) -> None:
        """Verify field assertion fails when actual value differs."""
        events: list[dict[str, object]] = [
            {"event_type": "intent_classified", "task_type": "conversational"}
        ]
        results = self.checker.check_assertions(
            events,
            [fld("intent_classified", "task_type", "analysis")],
        )
        assert results[0].passed is False
        assert results[0].actual_value == "conversational"

    def test_field_assertion_fails_missing_event(self) -> None:
        """Verify field assertion fails when event type is absent."""
        events: list[dict[str, object]] = [
            {"event_type": "other_event", "some_field": "value"}
        ]
        results = self.checker.check_assertions(
            events,
            [fld("intent_classified", "task_type", "analysis")],
        )
        assert results[0].passed is False
        assert results[0].actual_value is None

    def test_field_assertion_case_insensitive(self) -> None:
        """Verify field comparison is case-insensitive."""
        events: list[dict[str, object]] = [
            {"event_type": "intent_classified", "task_type": "ANALYSIS"}
        ]
        results = self.checker.check_assertions(
            events,
            [fld("intent_classified", "task_type", "analysis")],
        )
        assert results[0].passed is True

    def test_presence_assertion_found(self) -> None:
        """Verify presence assertion passes when event exists."""
        events: list[dict[str, object]] = [
            {"event_type": "hybrid_expansion_start", "sub_agent_count": 2}
        ]
        results = self.checker.check_assertions(
            events, [present("hybrid_expansion_start")],
        )
        assert results[0].passed is True

    def test_presence_assertion_not_found(self) -> None:
        """Verify presence assertion fails when event is missing."""
        events: list[dict[str, object]] = [{"event_type": "other_event"}]
        results = self.checker.check_assertions(
            events, [present("hybrid_expansion_start")],
        )
        assert results[0].passed is False

    def test_absence_assertion_not_found(self) -> None:
        """Verify absence assertion passes when event is missing."""
        events: list[dict[str, object]] = [{"event_type": "other_event"}]
        results = self.checker.check_assertions(
            events, [absent("hybrid_expansion_start")],
        )
        assert results[0].passed is True

    def test_absence_assertion_found(self) -> None:
        """Verify absence assertion fails when event is present."""
        events: list[dict[str, object]] = [{"event_type": "hybrid_expansion_start"}]
        results = self.checker.check_assertions(
            events, [absent("hybrid_expansion_start")],
        )
        assert results[0].passed is False

    def test_comparison_assertion_passes(self) -> None:
        """Verify comparison assertion passes when value satisfies threshold."""
        events: list[dict[str, object]] = [
            {"event_type": "hybrid_expansion_complete", "successes": 3}
        ]
        results = self.checker.check_assertions(
            events, [gte("hybrid_expansion_complete", "successes", 2)],
        )
        assert results[0].passed is True

    def test_comparison_assertion_fails(self) -> None:
        """Verify comparison assertion fails when value does not satisfy threshold."""
        events: list[dict[str, object]] = [
            {"event_type": "hybrid_expansion_complete", "successes": 1}
        ]
        results = self.checker.check_assertions(
            events, [gte("hybrid_expansion_complete", "successes", 2)],
        )
        assert results[0].passed is False

    def test_multiple_assertions(self) -> None:
        """Verify multiple assertions are all checked and all pass."""
        events: list[dict[str, object]] = [
            {"event_type": "intent_classified", "task_type": "analysis", "confidence": 0.8},
            {"event_type": "decomposition_assessed", "strategy": "hybrid"},
            {"event_type": "hybrid_expansion_start", "sub_agent_count": 2},
        ]
        results = self.checker.check_assertions(
            events,
            [
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "strategy", "hybrid"),
                present("hybrid_expansion_start"),
                absent("tool_call_completed"),
            ],
        )
        assert len(results) == 4
        assert all(r.passed for r in results)

    def test_event_field_fallback(self) -> None:
        """Verify checker also matches on 'event' field (not just 'event_type')."""
        events: list[dict[str, object]] = [
            {"event": "intent_classified", "task_type": "analysis"}
        ]
        results = self.checker.check_assertions(
            events,
            [fld("intent_classified", "task_type", "analysis")],
        )
        assert results[0].passed is True
