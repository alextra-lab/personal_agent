"""Unit tests for evaluation harness components.

Tests the data model, telemetry checker (mocked ES), and runner (mocked HTTP).
These do NOT require a running agent — they verify harness correctness.
"""

from __future__ import annotations

from types import SimpleNamespace

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
from tests.evaluation.harness.run import select_paths
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


class TestSelectPaths:
    """Tests for CLI path selection (no agent)."""

    def test_categories_merges_decomposition_and_expansion(self) -> None:
        """--categories decomposition expansion yields 7 unique paths in dataset order."""
        args = SimpleNamespace(
            paths=None,
            categories=["decomposition", "expansion"],
            category=None,
            skip_setup=False,
        )
        paths = select_paths(args)
        ids = [p.path_id for p in paths]
        assert len(ids) == 7
        assert len(set(ids)) == 7
        assert set(ids) == {
            "CP-08",
            "CP-09",
            "CP-10",
            "CP-11",
            "CP-16",
            "CP-17",
            "CP-18",
        }

    def test_categories_context_management_count(self) -> None:
        """Context Management slug matches eight paths (CP-19 family + CP-20)."""
        args = SimpleNamespace(
            paths=None,
            categories=["context_management"],
            category=None,
            skip_setup=False,
        )
        paths = select_paths(args)
        assert len(paths) == 8


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
        events: list[dict[str, object]] = [{"event_type": "other_event", "some_field": "value"}]
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
            events,
            [present("hybrid_expansion_start")],
        )
        assert results[0].passed is True

    def test_presence_assertion_not_found(self) -> None:
        """Verify presence assertion fails when event is missing."""
        events: list[dict[str, object]] = [{"event_type": "other_event"}]
        results = self.checker.check_assertions(
            events,
            [present("hybrid_expansion_start")],
        )
        assert results[0].passed is False

    def test_absence_assertion_not_found(self) -> None:
        """Verify absence assertion passes when event is missing."""
        events: list[dict[str, object]] = [{"event_type": "other_event"}]
        results = self.checker.check_assertions(
            events,
            [absent("hybrid_expansion_start")],
        )
        assert results[0].passed is True

    def test_absence_assertion_found(self) -> None:
        """Verify absence assertion fails when event is present."""
        events: list[dict[str, object]] = [{"event_type": "hybrid_expansion_start"}]
        results = self.checker.check_assertions(
            events,
            [absent("hybrid_expansion_start")],
        )
        assert results[0].passed is False

    def test_comparison_assertion_passes(self) -> None:
        """Verify comparison assertion passes when value satisfies threshold."""
        events: list[dict[str, object]] = [
            {"event_type": "hybrid_expansion_complete", "successes": 3}
        ]
        results = self.checker.check_assertions(
            events,
            [gte("hybrid_expansion_complete", "successes", 2)],
        )
        assert results[0].passed is True

    def test_comparison_assertion_fails(self) -> None:
        """Verify comparison assertion fails when value does not satisfy threshold."""
        events: list[dict[str, object]] = [
            {"event_type": "hybrid_expansion_complete", "successes": 1}
        ]
        results = self.checker.check_assertions(
            events,
            [gte("hybrid_expansion_complete", "successes", 2)],
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
        events: list[dict[str, object]] = [{"event": "intent_classified", "task_type": "analysis"}]
        results = self.checker.check_assertions(
            events,
            [fld("intent_classified", "task_type", "analysis")],
        )
        assert results[0].passed is True


# ---------------------------------------------------------------------------
# Prompt identity attribution tests (FRE-408, ADR-0078 P4)
# ---------------------------------------------------------------------------


class TestPromptIdentityAttribution:
    """Tests for FRE-408: eval runner prompt identity capture and report bucketing."""

    # --- TurnResult model ---

    def test_turn_result_prompt_identity_defaults_none(self) -> None:
        """TurnResult prompt identity fields default to None."""
        turn = TurnResult(
            turn_index=0,
            user_message="hello",
            response_text="hi",
            trace_id="t1",
            assertion_results=(),
            response_time_ms=100.0,
        )
        assert turn.prompt_callsite is None
        assert turn.prompt_static_prefix_hash is None
        assert turn.prompt_dynamic_hash is None
        assert turn.rating is None

    def test_turn_result_stores_prompt_identity(self) -> None:
        """TurnResult stores prompt identity fields when supplied."""
        turn = TurnResult(
            turn_index=1,
            user_message="q",
            response_text="a",
            trace_id="t2",
            assertion_results=(),
            response_time_ms=200.0,
            prompt_callsite="orchestrator.primary",
            prompt_static_prefix_hash="a3f7b2c1d4e8f901",
            prompt_dynamic_hash="9c1e8a4d2f067890",
        )
        assert turn.prompt_callsite == "orchestrator.primary"
        assert turn.prompt_static_prefix_hash == "a3f7b2c1d4e8f901"
        assert turn.prompt_dynamic_hash == "9c1e8a4d2f067890"
        assert turn.rating is None

    # --- TelemetryChecker.extract_prompt_identity ---

    def setup_method(self) -> None:
        """Initialize TelemetryChecker for each test."""
        self.checker = TelemetryChecker()

    def test_extract_prompt_identity_from_mcc_event(self) -> None:
        """extract_prompt_identity returns fields from model_call_completed event."""
        events: list[dict[str, object]] = [
            {
                "event_type": "model_call_completed",
                "prompt_callsite": "orchestrator.primary",
                "prompt_static_prefix_hash": "a3f7b2c1",
                "prompt_dynamic_hash": "9c1e8a4d",
            }
        ]
        callsite, sph, dyn = self.checker.extract_prompt_identity(events)
        assert callsite == "orchestrator.primary"
        assert sph == "a3f7b2c1"
        assert dyn == "9c1e8a4d"

    def test_extract_prompt_identity_event_key_fallback(self) -> None:
        """extract_prompt_identity also matches on 'event' key."""
        events: list[dict[str, object]] = [
            {
                "event": "model_call_completed",
                "prompt_callsite": "gateway.chat",
                "prompt_static_prefix_hash": "deadbeef",
                "prompt_dynamic_hash": "cafebabe",
            }
        ]
        callsite, sph, dyn = self.checker.extract_prompt_identity(events)
        assert callsite == "gateway.chat"
        assert sph == "deadbeef"
        assert dyn == "cafebabe"

    def test_extract_prompt_identity_returns_none_when_no_mcc(self) -> None:
        """extract_prompt_identity returns (None, None, None) with no mcc event."""
        events: list[dict[str, object]] = [
            {"event_type": "intent_classified", "task_type": "analysis"}
        ]
        callsite, sph, dyn = self.checker.extract_prompt_identity(events)
        assert callsite is None
        assert sph is None
        assert dyn is None

    def test_extract_prompt_identity_empty_events(self) -> None:
        """extract_prompt_identity returns (None, None, None) for empty list."""
        callsite, sph, dyn = self.checker.extract_prompt_identity([])
        assert callsite is None
        assert sph is None
        assert dyn is None

    def test_extract_prompt_identity_prefers_orchestrator_primary(self) -> None:
        """extract_prompt_identity picks orchestrator.primary over other callsites."""
        events: list[dict[str, object]] = [
            {
                "event_type": "model_call_completed",
                "prompt_callsite": "gateway.chat",
                "prompt_static_prefix_hash": "aaa",
                "prompt_dynamic_hash": "bbb",
            },
            {
                "event_type": "model_call_completed",
                "prompt_callsite": "orchestrator.primary",
                "prompt_static_prefix_hash": "ccc",
                "prompt_dynamic_hash": "ddd",
            },
        ]
        callsite, sph, dyn = self.checker.extract_prompt_identity(events)
        assert callsite == "orchestrator.primary"
        assert sph == "ccc"
        assert dyn == "ddd"

    # --- _build_prompt_version_summary ---

    def _make_result(
        self,
        path_id: str,
        turns_data: list[tuple[str | None, int | None]],
    ) -> PathResult:
        """Build a PathResult with turns carrying given (static_prefix_hash, rating) pairs."""
        result = PathResult(
            path_id=path_id,
            path_name=path_id,
            category="Test",
            session_id="sid",
        )
        for i, (sph, rating) in enumerate(turns_data):
            result.turns.append(
                TurnResult(
                    turn_index=i,
                    user_message="q",
                    response_text="a",
                    trace_id=f"t{i}",
                    assertion_results=(),
                    response_time_ms=100.0,
                    prompt_callsite="orchestrator.primary" if sph else None,
                    prompt_static_prefix_hash=sph,
                    rating=rating,
                )
            )
        return result

    def test_prompt_version_summary_empty(self) -> None:
        """_build_prompt_version_summary returns empty list for no results."""
        from tests.evaluation.harness.report import _build_prompt_version_summary

        rows = _build_prompt_version_summary([])
        assert rows == []

    def test_prompt_version_summary_groups_by_hash(self) -> None:
        """Turns with same static_prefix_hash land in same bucket."""
        from tests.evaluation.harness.report import _build_prompt_version_summary

        result = self._make_result(
            "CP-T",
            [("hash_a", None), ("hash_a", None), ("hash_b", None)],
        )
        rows = _build_prompt_version_summary([result])
        counts = {r["static_prefix_hash"]: r["n_turns"] for r in rows}
        assert counts["hash_a"] == 2
        assert counts["hash_b"] == 1

    def test_prompt_version_summary_null_ratings_shown_as_none(self) -> None:
        """Buckets with all null ratings show None for mean/median/p25/p75."""
        from tests.evaluation.harness.report import _build_prompt_version_summary

        result = self._make_result("CP-T", [("hash_x", None), ("hash_x", None)])
        rows = _build_prompt_version_summary([result])
        row = rows[0]
        assert row["n_rated"] == 0
        assert row["mean_rating"] is None
        assert row["median_rating"] is None

    def test_prompt_version_summary_computes_statistics(self) -> None:
        """Buckets with ratings compute mean, median, p25, p75."""
        from tests.evaluation.harness.report import _build_prompt_version_summary

        result = self._make_result(
            "CP-T",
            [("hash_a", 1), ("hash_a", 2), ("hash_a", 3), ("hash_a", 2)],
        )
        rows = _build_prompt_version_summary([result])
        assert len(rows) == 1
        row = rows[0]
        assert row["n_turns"] == 4
        assert row["n_rated"] == 4
        # mean of [1,2,3,2] = 2.0
        assert row["mean_rating"] == 2.0
        # median of [1,2,2,3] = 2.0
        assert row["median_rating"] == 2.0

    def test_prompt_version_summary_sorted_by_n_turns(self) -> None:
        """Buckets are sorted descending by n_turns."""
        from tests.evaluation.harness.report import _build_prompt_version_summary

        result = self._make_result(
            "CP-T",
            [("small", None), ("big", None), ("big", None), ("big", None)],
        )
        rows = _build_prompt_version_summary([result])
        assert rows[0]["static_prefix_hash"] == "big"
        assert rows[1]["static_prefix_hash"] == "small"

    # --- Report output ---

    def test_markdown_report_includes_prompt_version_section(self) -> None:
        """Markdown report includes '## Prompt Version Summary' section."""
        from tests.evaluation.harness.report import generate_markdown_report

        result = self._make_result("CP-T", [("hash_a", None)])
        md = generate_markdown_report([result])
        assert "## Prompt Version Summary" in md
        assert "hash_a" in md

    def test_json_report_includes_prompt_version_summary(self) -> None:
        """JSON report includes 'prompt_version_summary' key."""
        from tests.evaluation.harness.report import generate_json_report

        result = self._make_result("CP-T", [("hash_a", None)])
        report = generate_json_report([result])
        assert "prompt_version_summary" in report
        assert isinstance(report["prompt_version_summary"], list)

    def test_json_report_turn_includes_prompt_identity_fields(self) -> None:
        """JSON report turn dicts include prompt identity fields."""
        from tests.evaluation.harness.report import generate_json_report

        result = self._make_result("CP-T", [("hash_a", None)])
        report = generate_json_report([result])
        paths = report["paths"]
        assert isinstance(paths, list)
        turn = paths[0]["turns"][0]  # type: ignore[index]
        assert "prompt_static_prefix_hash" in turn
        assert turn["prompt_static_prefix_hash"] == "hash_a"
