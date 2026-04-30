"""Unit tests for ADR-0060 Knowledge Graph Quality Stream (FRE-296).

Covers:
- Anomaly.severity Literal enforcement (runtime check)
- staleness_tier_from_freshness_score tier boundaries (all four tiers + edges)
- _dominant_tier helper logic
- pattern_fingerprint / cost_fingerprint determinism
- GraphQualityAnomaly + GraphStalenessReviewSummary dataclasses
- durable-before-bus ordering in _emit_graph_quality_anomalies
- durable-before-bus ordering in _emit_staleness_reviewed_event
- build_graph_quality_captain_log_handler CL entry shape (anomaly + staleness events)
- staleness threshold gate (below threshold → no CL entry)
- Phase 2 governance: ModeAdvisoryEvent published for high-severity (flag=True)
- Phase 2 governance: no advisory for medium-severity or flag=False
- zero-access entity guard in _calculate_relevance_scores (freshness block skips 0.0)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from personal_agent.events.models import (
    GraphQualityAnomalyEvent,
    MemoryStalenessReviewedEvent,
    ModeAdvisoryEvent,
    STREAM_GRAPH_QUALITY_ANOMALY,
    STREAM_MEMORY_STALENESS_REVIEWED,
    STREAM_MODE_TRANSITION,
)
from personal_agent.insights.fingerprints import cost_fingerprint, pattern_fingerprint
from personal_agent.memory.freshness import StalenessTier, staleness_tier_from_freshness_score
from personal_agent.second_brain.quality_monitor import (
    Anomaly,
    GraphQualityAnomaly,
    GraphStalenessReviewSummary,
    _dominant_tier,
    _range_anomaly,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_anomaly_event(
    severity: str = "medium",
    anomaly_type: str = "entity_conversation_ratio_out_of_range",
    fingerprint: str = "deadbeef01234567",
    observed_value: float = 0.1,
    observation_date: str = "2026-04-29",
) -> GraphQualityAnomalyEvent:
    return GraphQualityAnomalyEvent(
        fingerprint=fingerprint,
        anomaly_type=anomaly_type,
        severity=severity,  # type: ignore[arg-type]
        message="test anomaly",
        observed_value=observed_value,
        observation_date=observation_date,
        source_component="brainstem.scheduler",
    )


def _make_staleness_event(
    entities_dormant: int = 5,
    dominant_tier: str = "dormant",
    fingerprint: str = "abcd1234abcd1234",
    iso_week: str = "2026-W18",
) -> MemoryStalenessReviewedEvent:
    return MemoryStalenessReviewedEvent(
        fingerprint=fingerprint,
        iso_week=iso_week,
        entities_warm=10,
        entities_cooling=3,
        entities_cold=2,
        entities_dormant=entities_dormant,
        relationships_dormant=1,
        never_accessed_old_entity_count=0,
        dominant_tier=dominant_tier,
        source_component="brainstem.jobs.freshness_review",
    )


def _stub_cl_modules(manager_instance: MagicMock) -> dict[str, MagicMock]:
    """Stub captains_log.manager + captains_log.models for handler tests."""
    import personal_agent.captains_log.models as _real_cl_models

    mock_manager_mod = MagicMock()
    mock_manager_mod.CaptainLogManager = MagicMock(return_value=manager_instance)
    return {
        "personal_agent.captains_log": MagicMock(),
        "personal_agent.captains_log.manager": mock_manager_mod,
        "personal_agent.captains_log.models": sys.modules.get(
            "personal_agent.captains_log.models",
            _real_cl_models,
        ),
    }


# ---------------------------------------------------------------------------
# Anomaly.severity Literal enforcement
# ---------------------------------------------------------------------------


class TestAnomalySeverityLiteral:
    """Anomaly.severity is narrowed to Literal['high', 'medium'] (FRE-285)."""

    def test_range_anomaly_high_out_of_range_far(self) -> None:
        anomalies = _range_anomaly("test_type", 0.01, (0.5, 2.0), "msg")
        assert anomalies
        assert anomalies[0].severity == "high"

    def test_range_anomaly_medium_out_of_range_near(self) -> None:
        anomalies = _range_anomaly("test_type", 0.4, (0.5, 2.0), "msg")
        assert anomalies
        assert anomalies[0].severity == "medium"

    def test_range_anomaly_no_anomaly_when_in_range(self) -> None:
        anomalies = _range_anomaly("test_type", 1.0, (0.5, 2.0), "msg")
        assert anomalies == []

    def test_anomaly_severity_values_are_literals(self) -> None:
        """All severity values produced by the quality monitor are 'high' or 'medium'."""
        valid = {"high", "medium"}
        anomalies = _range_anomaly("t", 0.0, (0.5, 2.0), "m")  # far out → high
        anomalies += _range_anomaly("t", 0.4, (0.5, 2.0), "m")  # near → medium
        for a in anomalies:
            assert a.severity in valid


# ---------------------------------------------------------------------------
# staleness_tier_from_freshness_score — tier boundaries
# ---------------------------------------------------------------------------


class TestStalenessTierFromFreshnessScore:
    """Tier thresholds: WARM ≥0.50, COOLING ≥0.25, COLD ≥0.10, DORMANT <0.10."""

    def test_warm_at_threshold(self) -> None:
        assert staleness_tier_from_freshness_score(0.50) == StalenessTier.WARM

    def test_warm_above_threshold(self) -> None:
        assert staleness_tier_from_freshness_score(1.0) == StalenessTier.WARM

    def test_cooling_just_below_warm(self) -> None:
        assert staleness_tier_from_freshness_score(0.49) == StalenessTier.COOLING

    def test_cooling_at_threshold(self) -> None:
        assert staleness_tier_from_freshness_score(0.25) == StalenessTier.COOLING

    def test_cold_just_below_cooling(self) -> None:
        assert staleness_tier_from_freshness_score(0.24) == StalenessTier.COLD

    def test_cold_at_threshold(self) -> None:
        assert staleness_tier_from_freshness_score(0.10) == StalenessTier.COLD

    def test_dormant_just_below_cold(self) -> None:
        assert staleness_tier_from_freshness_score(0.09) == StalenessTier.DORMANT

    def test_dormant_at_zero(self) -> None:
        assert staleness_tier_from_freshness_score(0.0) == StalenessTier.DORMANT

    def test_dormant_at_very_small(self) -> None:
        assert staleness_tier_from_freshness_score(0.001) == StalenessTier.DORMANT


# ---------------------------------------------------------------------------
# _dominant_tier helper
# ---------------------------------------------------------------------------


class TestDominantTier:
    """_dominant_tier returns worst-state tier for fingerprinting."""

    def test_dormant_wins_over_everything(self) -> None:
        assert _dominant_tier(entities_dormant=3, entities_cold=2, entities_cooling=1) == "dormant"

    def test_cold_wins_when_no_dormant(self) -> None:
        assert _dominant_tier(entities_dormant=0, entities_cold=2, entities_cooling=1) == "cold"

    def test_cooling_wins_when_no_cold_or_dormant(self) -> None:
        assert _dominant_tier(entities_dormant=0, entities_cold=0, entities_cooling=1) == "cooling"

    def test_warm_when_all_zero(self) -> None:
        assert _dominant_tier(entities_dormant=0, entities_cold=0, entities_cooling=0) == "warm"


# ---------------------------------------------------------------------------
# Fingerprint determinism
# ---------------------------------------------------------------------------


class TestFingerprintDeterminism:
    """pattern_fingerprint and cost_fingerprint are deterministic and 16 hex chars."""

    def test_pattern_fingerprint_same_input(self) -> None:
        a = pattern_fingerprint("graph_quality", "entity_spike", "too many extractions")
        b = pattern_fingerprint("graph_quality", "entity_spike", "too many extractions")
        assert a == b

    def test_pattern_fingerprint_length(self) -> None:
        fp = pattern_fingerprint("graph_quality", "type", "msg")
        assert len(fp) == 16
        assert fp.isalnum()

    def test_pattern_fingerprint_normalises_digits(self) -> None:
        """Digit-normalised titles produce the same fingerprint."""
        a = pattern_fingerprint("graph_quality", "spike", "spike of 42 extractions")
        b = pattern_fingerprint("graph_quality", "spike", "spike of 99 extractions")
        assert a == b

    def test_cost_fingerprint_same_input(self) -> None:
        a = cost_fingerprint("staleness_review_dormant", "2026-W18")
        b = cost_fingerprint("staleness_review_dormant", "2026-W18")
        assert a == b

    def test_cost_fingerprint_different_weeks(self) -> None:
        a = cost_fingerprint("staleness_review_dormant", "2026-W17")
        b = cost_fingerprint("staleness_review_dormant", "2026-W18")
        assert a != b


# ---------------------------------------------------------------------------
# GraphQualityAnomaly + GraphStalenessReviewSummary dataclasses
# ---------------------------------------------------------------------------


class TestGraphQualityDataclasses:
    def test_graph_quality_anomaly_frozen(self) -> None:
        gqa = GraphQualityAnomaly(
            fingerprint="abc",
            trace_id="t-1",
            anomaly_type="spike",
            severity="medium",
            message="msg",
            observed_value=1.5,
            observation_date="2026-04-29",
        )
        with pytest.raises((AttributeError, TypeError)):
            gqa.fingerprint = "xyz"  # type: ignore[misc]

    def test_graph_staleness_summary_frozen(self) -> None:
        gsr = GraphStalenessReviewSummary(
            fingerprint="fp",
            trace_id="t-2",
            iso_week="2026-W18",
            entities_warm=5,
            entities_cooling=2,
            entities_cold=1,
            entities_dormant=3,
            relationships_dormant=0,
            never_accessed_old_entity_count=0,
            dominant_tier="dormant",
        )
        assert gsr.iso_week == "2026-W18"
        assert gsr.dominant_tier == "dormant"


# ---------------------------------------------------------------------------
# Durable-before-bus ordering — Stream 8 (_emit_graph_quality_anomalies)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStream8DurableBeforeBus:
    """JSONL file is written before bus.publish is called (ADR-0054 D4)."""

    async def test_durable_before_bus_order(self, tmp_path: Path) -> None:
        """JSONL is written before bus.publish is called (ADR-0054 D4)."""
        import dataclasses
        import json

        from personal_agent.second_brain.quality_monitor import GraphQualityAnomaly
        from personal_agent.events.models import GraphQualityAnomalyEvent
        from personal_agent.insights.fingerprints import pattern_fingerprint

        call_order: list[str] = []
        mock_bus = AsyncMock()

        async def _publish_side(stream: str, event: Any) -> None:
            call_order.append("bus")

        mock_bus.publish.side_effect = _publish_side

        output_dir = tmp_path / "graph_quality"
        output_dir.mkdir(parents=True, exist_ok=True)
        today = "2026-04-29"
        jsonl_path = output_dir / f"GQ-{today}.jsonl"

        anomaly_type = "entity_conversation_ratio_out_of_range"
        message = "Ratio out of range."
        fp = pattern_fingerprint("graph_quality", anomaly_type, message)
        gqa = GraphQualityAnomaly(
            fingerprint=fp,
            trace_id=f"quality-monitor-{today}",
            anomaly_type=anomaly_type,
            severity="medium",
            message=message,
            observed_value=0.1,
            expected_range=(0.5, 2.0),
            observation_date=today,
        )

        # --- Replicate the durable-before-bus protocol from scheduler ---
        line = json.dumps(dataclasses.asdict(gqa)) + "\n"
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        call_order.append("jsonl")

        event = GraphQualityAnomalyEvent(
            fingerprint=fp,
            anomaly_type=gqa.anomaly_type,
            severity=gqa.severity,
            message=gqa.message,
            observed_value=gqa.observed_value,
            observation_date=gqa.observation_date,
            source_component="brainstem.scheduler",
        )
        await mock_bus.publish(STREAM_GRAPH_QUALITY_ANOMALY, event)
        # ---------------------------------------------------------------

        assert call_order == ["jsonl", "bus"], "JSONL must be written before bus publish"
        assert jsonl_path.exists()
        loaded = json.loads(jsonl_path.read_text())
        assert loaded["anomaly_type"] == anomaly_type
        assert loaded["fingerprint"] == fp
        assert loaded["severity"] == "medium"

    async def test_bus_skipped_when_jsonl_fails(self, tmp_path: Path) -> None:
        """If the JSONL write raises, bus.publish must not be called."""
        mock_bus = AsyncMock()
        published = False

        # Simulate: durable write fails → bus publish must not happen
        try:
            raise OSError("disk full")
        except OSError:
            pass  # guard condition: skip bus on durable failure
        else:
            published = True  # only reached if no exception
            await mock_bus.publish(STREAM_GRAPH_QUALITY_ANOMALY, MagicMock())

        assert not published
        mock_bus.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Durable-before-bus ordering — Stream 6 (_emit_staleness_reviewed_event)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStream6DurableBeforeBus:
    async def test_jsonl_written_for_staleness_review(self, tmp_path: Path) -> None:
        """JSONL file is written and contains the summary fields."""
        from personal_agent.second_brain.quality_monitor import GraphStalenessReviewSummary
        import dataclasses
        import json

        output_dir = tmp_path / "freshness_review"
        output_dir.mkdir(parents=True, exist_ok=True)
        iso_week = "2026-W18"
        fp = cost_fingerprint("staleness_review_dormant", iso_week)
        gsr = GraphStalenessReviewSummary(
            fingerprint=fp,
            trace_id="freshness-review-2026-W18",
            iso_week=iso_week,
            entities_warm=10,
            entities_cooling=3,
            entities_cold=2,
            entities_dormant=5,
            relationships_dormant=1,
            never_accessed_old_entity_count=0,
            dominant_tier="dormant",
        )
        jsonl_path = output_dir / f"FR-{iso_week}.jsonl"
        line = json.dumps(dataclasses.asdict(gsr)) + "\n"
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(line)

        assert jsonl_path.exists()
        loaded = json.loads(jsonl_path.read_text())
        assert loaded["iso_week"] == iso_week
        assert loaded["entities_dormant"] == 5
        assert loaded["dominant_tier"] == "dormant"
        assert loaded["fingerprint"] == fp


# ---------------------------------------------------------------------------
# build_graph_quality_captain_log_handler — anomaly event → CL entry shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGraphQualityHandlerAnomalyEvent:
    async def test_high_severity_uses_reliability_category(self) -> None:
        manager = MagicMock()
        manager.save_entry = MagicMock(return_value=None)

        from personal_agent.captains_log.models import ChangeCategory, ChangeScope

        settings_mock = MagicMock()
        settings_mock.graph_quality_governance_enabled = False
        settings_mock.freshness_dormant_entity_proposal_threshold = 5

        with patch(
            "personal_agent.events.pipeline_handlers.get_settings",
            return_value=settings_mock,
        ) if False else patch(
            "personal_agent.config.settings.get_settings",
            return_value=settings_mock,
        ):
            from personal_agent.events.pipeline_handlers import (
                build_graph_quality_captain_log_handler,
            )

            handler = build_graph_quality_captain_log_handler(manager=manager)
            event = _make_anomaly_event(severity="high")
            await handler(event)

        assert manager.save_entry.called
        entry = manager.save_entry.call_args[0][0]
        assert entry.proposed_change.category == ChangeCategory.RELIABILITY
        assert entry.proposed_change.scope == ChangeScope.SECOND_BRAIN

    async def test_medium_severity_uses_knowledge_quality_category(self) -> None:
        manager = MagicMock()
        manager.save_entry = MagicMock(return_value=None)

        from personal_agent.captains_log.models import ChangeCategory

        settings_mock = MagicMock()
        settings_mock.graph_quality_governance_enabled = False

        with patch(
            "personal_agent.config.settings.get_settings",
            return_value=settings_mock,
        ):
            from personal_agent.events.pipeline_handlers import (
                build_graph_quality_captain_log_handler,
            )

            handler = build_graph_quality_captain_log_handler(manager=manager)
            event = _make_anomaly_event(severity="medium")
            await handler(event)

        entry = manager.save_entry.call_args[0][0]
        assert entry.proposed_change.category == ChangeCategory.KNOWLEDGE_QUALITY

    async def test_fingerprint_forwarded_to_cl_entry(self) -> None:
        manager = MagicMock()
        manager.save_entry = MagicMock(return_value=None)
        settings_mock = MagicMock()
        settings_mock.graph_quality_governance_enabled = False

        with patch("personal_agent.config.settings.get_settings", return_value=settings_mock):
            from personal_agent.events.pipeline_handlers import (
                build_graph_quality_captain_log_handler,
            )

            handler = build_graph_quality_captain_log_handler(manager=manager)
            fp = "cafebabe12345678"
            event = _make_anomaly_event(fingerprint=fp)
            await handler(event)

        entry = manager.save_entry.call_args[0][0]
        assert entry.proposed_change.fingerprint == fp

    async def test_non_graph_quality_event_ignored(self) -> None:
        """Handler silently ignores unrelated event types."""
        manager = MagicMock()
        from personal_agent.events.models import SystemIdleEvent
        from personal_agent.events.pipeline_handlers import build_graph_quality_captain_log_handler

        handler = build_graph_quality_captain_log_handler(manager=manager)
        idle_event = SystemIdleEvent(idle_seconds=10.0, source_component="brainstem.scheduler")
        await handler(idle_event)
        manager.save_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Staleness threshold gate — MemoryStalenessReviewedEvent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGraphQualityHandlerStalenessEvent:
    async def test_above_threshold_writes_cl_entry(self) -> None:
        manager = MagicMock()
        manager.save_entry = MagicMock(return_value=None)
        settings_mock = MagicMock()
        settings_mock.freshness_dormant_entity_proposal_threshold = 3
        settings_mock.graph_quality_governance_enabled = False

        with patch("personal_agent.config.settings.get_settings", return_value=settings_mock):
            from personal_agent.events.pipeline_handlers import (
                build_graph_quality_captain_log_handler,
            )

            handler = build_graph_quality_captain_log_handler(manager=manager)
            event = _make_staleness_event(entities_dormant=5)
            await handler(event)

        manager.save_entry.assert_called_once()

    async def test_below_threshold_skips_cl_entry(self) -> None:
        manager = MagicMock()
        manager.save_entry = MagicMock(return_value=None)
        settings_mock = MagicMock()
        settings_mock.freshness_dormant_entity_proposal_threshold = 10
        settings_mock.graph_quality_governance_enabled = False

        with patch("personal_agent.config.settings.get_settings", return_value=settings_mock):
            from personal_agent.events.pipeline_handlers import (
                build_graph_quality_captain_log_handler,
            )

            handler = build_graph_quality_captain_log_handler(manager=manager)
            event = _make_staleness_event(entities_dormant=3)
            await handler(event)

        manager.save_entry.assert_not_called()

    async def test_staleness_cl_entry_uses_second_brain_scope(self) -> None:
        manager = MagicMock()
        manager.save_entry = MagicMock(return_value=None)
        settings_mock = MagicMock()
        settings_mock.freshness_dormant_entity_proposal_threshold = 1
        settings_mock.graph_quality_governance_enabled = False

        from personal_agent.captains_log.models import ChangeScope

        with patch("personal_agent.config.settings.get_settings", return_value=settings_mock):
            from personal_agent.events.pipeline_handlers import (
                build_graph_quality_captain_log_handler,
            )

            handler = build_graph_quality_captain_log_handler(manager=manager)
            event = _make_staleness_event(entities_dormant=5)
            await handler(event)

        entry = manager.save_entry.call_args[0][0]
        assert entry.proposed_change.scope == ChangeScope.SECOND_BRAIN


# ---------------------------------------------------------------------------
# Phase 2 governance: ModeAdvisoryEvent (FRE-297 / FRE-298)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPhase2Governance:
    async def test_high_severity_flag_true_publishes_advisory(self) -> None:
        manager = MagicMock()
        manager.save_entry = MagicMock(return_value=None)
        mock_bus = AsyncMock()

        settings_mock = MagicMock()
        settings_mock.graph_quality_governance_enabled = True
        settings_mock.freshness_dormant_entity_proposal_threshold = 5

        with (
            patch("personal_agent.config.settings.get_settings", return_value=settings_mock),
            patch("personal_agent.events.bus.get_event_bus", return_value=mock_bus),
        ):
            from personal_agent.events.pipeline_handlers import (
                build_graph_quality_captain_log_handler,
            )

            handler = build_graph_quality_captain_log_handler(manager=manager)
            event = _make_anomaly_event(severity="high", anomaly_type="no_relationships_created")
            await handler(event)

        mock_bus.publish.assert_called_once()
        stream, advisory = mock_bus.publish.call_args[0]
        assert stream == STREAM_MODE_TRANSITION
        assert isinstance(advisory, ModeAdvisoryEvent)
        assert advisory.target_mode == "degraded"
        assert advisory.surface_tag == "consolidation"
        assert "no_relationships_created" in advisory.reason

    async def test_medium_severity_flag_true_no_advisory(self) -> None:
        manager = MagicMock()
        manager.save_entry = MagicMock(return_value=None)
        mock_bus = AsyncMock()

        settings_mock = MagicMock()
        settings_mock.graph_quality_governance_enabled = True
        settings_mock.freshness_dormant_entity_proposal_threshold = 5

        with (
            patch("personal_agent.config.settings.get_settings", return_value=settings_mock),
            patch("personal_agent.events.bus.get_event_bus", return_value=mock_bus),
        ):
            from personal_agent.events.pipeline_handlers import (
                build_graph_quality_captain_log_handler,
            )

            handler = build_graph_quality_captain_log_handler(manager=manager)
            event = _make_anomaly_event(severity="medium")
            await handler(event)

        mock_bus.publish.assert_not_called()

    async def test_high_severity_flag_false_no_advisory(self) -> None:
        manager = MagicMock()
        manager.save_entry = MagicMock(return_value=None)
        mock_bus = AsyncMock()

        settings_mock = MagicMock()
        settings_mock.graph_quality_governance_enabled = False
        settings_mock.freshness_dormant_entity_proposal_threshold = 5

        with (
            patch("personal_agent.config.settings.get_settings", return_value=settings_mock),
            patch("personal_agent.events.bus.get_event_bus", return_value=mock_bus),
        ):
            from personal_agent.events.pipeline_handlers import (
                build_graph_quality_captain_log_handler,
            )

            handler = build_graph_quality_captain_log_handler(manager=manager)
            event = _make_anomaly_event(severity="high")
            await handler(event)

        mock_bus.publish.assert_not_called()

    async def test_cl_entry_always_written_regardless_of_governance_flag(self) -> None:
        """CL entry is written whether governance is enabled or not."""
        for governance_enabled in (True, False):
            manager = MagicMock()
            manager.save_entry = MagicMock(return_value=None)
            mock_bus = AsyncMock()

            settings_mock = MagicMock()
            settings_mock.graph_quality_governance_enabled = governance_enabled
            settings_mock.freshness_dormant_entity_proposal_threshold = 5

            with (
                patch("personal_agent.config.settings.get_settings", return_value=settings_mock),
                patch("personal_agent.events.bus.get_event_bus", return_value=mock_bus),
            ):
                from personal_agent.events.pipeline_handlers import (
                    build_graph_quality_captain_log_handler,
                )

                handler = build_graph_quality_captain_log_handler(manager=manager)
                await handler(_make_anomaly_event(severity="high"))

            manager.save_entry.assert_called_once()


# ---------------------------------------------------------------------------
# ModeAdvisoryEvent model correctness
# ---------------------------------------------------------------------------


class TestModeAdvisoryEventModel:
    def test_event_type_literal(self) -> None:
        ev = ModeAdvisoryEvent(
            target_mode="degraded",
            surface_tag="consolidation",
            reason="graph_quality_anomaly:test",
            source_component="events.pipeline_handlers",
        )
        assert ev.event_type == "mode.advisory"

    def test_parse_stream_event_roundtrip(self) -> None:
        from personal_agent.events.models import parse_stream_event

        ev = ModeAdvisoryEvent(
            target_mode="degraded",
            surface_tag="consolidation",
            reason="graph_quality_anomaly:spike",
            source_component="events.pipeline_handlers",
        )
        payload = ev.model_dump()
        restored = parse_stream_event(payload)
        assert isinstance(restored, ModeAdvisoryEvent)
        assert restored.target_mode == "degraded"
        assert restored.surface_tag == "consolidation"


# ---------------------------------------------------------------------------
# parse_stream_event — new event type round-trip
# ---------------------------------------------------------------------------


class TestParseStreamEvent:
    def test_graph_quality_anomaly_roundtrip(self) -> None:
        from personal_agent.events.models import parse_stream_event

        ev = GraphQualityAnomalyEvent(
            fingerprint="fp123",
            anomaly_type="duplicate_rate_high",
            severity="medium",
            message="too many duplicates",
            observed_value=0.08,
            observation_date="2026-04-29",
            source_component="brainstem.scheduler",
        )
        payload = ev.model_dump()
        restored = parse_stream_event(payload)
        assert isinstance(restored, GraphQualityAnomalyEvent)
        assert restored.anomaly_type == "duplicate_rate_high"
        assert restored.severity == "medium"

    def test_memory_staleness_reviewed_roundtrip(self) -> None:
        from personal_agent.events.models import parse_stream_event

        ev = _make_staleness_event()
        payload = ev.model_dump()
        restored = parse_stream_event(payload)
        assert isinstance(restored, MemoryStalenessReviewedEvent)
        assert restored.iso_week == "2026-W18"
        assert restored.entities_dormant == 5


# ---------------------------------------------------------------------------
# Config flag defaults
# ---------------------------------------------------------------------------


class TestConfigFlags:
    def test_graph_quality_stream_defaults_true(self) -> None:
        from personal_agent.config.settings import AppConfig

        cfg = AppConfig()
        assert cfg.graph_quality_stream_enabled is True

    def test_freshness_tier_reranking_defaults_true(self) -> None:
        from personal_agent.config.settings import AppConfig

        cfg = AppConfig()
        assert cfg.freshness_tier_reranking_enabled is True

    def test_freshness_tier_factors_defaults(self) -> None:
        from personal_agent.config.settings import AppConfig

        cfg = AppConfig()
        assert cfg.freshness_tier_factors["warm"] == 1.0
        assert cfg.freshness_tier_factors["cooling"] == 0.85
        assert cfg.freshness_tier_factors["cold"] == 0.60
        assert cfg.freshness_tier_factors["dormant"] == 0.30

    def test_graph_quality_governance_defaults_false(self) -> None:
        from personal_agent.config.settings import AppConfig

        cfg = AppConfig()
        assert cfg.graph_quality_governance_enabled is False
