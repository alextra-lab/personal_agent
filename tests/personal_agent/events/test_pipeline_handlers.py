"""Tests for Phase 3 pipeline consumer handlers (FRE-159 / ADR-0041).

The test environment (Python 3.11) does not have ``mcp`` installed, so the
entire ``personal_agent.captains_log`` and ``personal_agent.insights`` import
chains fail.  All lazily-imported modules inside the handlers are therefore
stubbed via ``sys.modules`` injection — we never import the real modules at
test collection time.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from datetime import datetime, timezone

from personal_agent.events.models import (
    CompactionQualityIncidentEvent,
    ConsolidationCompletedEvent,
    FeedbackReceivedEvent,
    PromotionIssueCreatedEvent,
    RequestCapturedEvent,
)
from personal_agent.events.pipeline_handlers import (
    build_compaction_quality_captain_log_handler,
    build_consolidation_insights_handler,
    build_consolidation_promotion_handler,
    build_feedback_insights_handler,
    build_feedback_suppression_handler,
    build_promotion_captain_log_handler,
)


# ---------------------------------------------------------------------------
# sys.modules stub helpers
# ---------------------------------------------------------------------------


def _insights_modules(engine_instance: MagicMock) -> dict[str, MagicMock]:
    """Stub the insights.engine module so InsightsEngine resolves to a mock."""
    import personal_agent.insights.fingerprints as _real_fingerprints

    mock_engine_mod = MagicMock()
    mock_engine_mod.InsightsEngine = MagicMock(return_value=engine_instance)
    return {
        "personal_agent.insights": MagicMock(),
        "personal_agent.insights.engine": mock_engine_mod,
        # fingerprints.py has no heavy deps — use the real module so the
        # handler can import it even when personal_agent.insights is mocked.
        "personal_agent.insights.fingerprints": _real_fingerprints,
    }


def _promotion_modules(pipeline_instance: MagicMock) -> dict[str, MagicMock]:
    """Stub the captains_log.promotion module and settings."""
    mock_promo_mod = MagicMock()
    mock_promo_mod.PromotionPipeline = MagicMock(return_value=pipeline_instance)
    mock_promo_mod.PromotionCriteria = MagicMock(return_value=MagicMock())

    mock_settings_mod = MagicMock()
    mock_settings_mod.get_settings = MagicMock(return_value=MagicMock(promotion_initial_cap=5))

    return {
        "personal_agent.captains_log": MagicMock(),
        "personal_agent.captains_log.promotion": mock_promo_mod,
        "personal_agent.config.settings": mock_settings_mod,
    }


def _captain_log_modules(manager_instance: MagicMock) -> dict[str, MagicMock]:
    """Stub the captains_log.manager + models modules."""
    mock_manager_mod = MagicMock()
    mock_manager_mod.CaptainLogManager = MagicMock(return_value=manager_instance)

    mock_models_mod = MagicMock()
    mock_models_mod.CaptainLogEntry = MagicMock(
        side_effect=lambda **kw: MagicMock(**kw)
    )
    mock_models_mod.CaptainLogEntryType = MagicMock()
    mock_models_mod.CaptainLogStatus = MagicMock()

    return {
        "personal_agent.captains_log": MagicMock(),
        "personal_agent.captains_log.manager": mock_manager_mod,
        "personal_agent.captains_log.models": mock_models_mod,
    }


def _suppression_modules(suppress_fn: MagicMock) -> dict[str, MagicMock]:
    """Stub the captains_log.suppression module."""
    mock_supp_mod = MagicMock()
    mock_supp_mod.record_rejection_suppression = suppress_fn

    return {
        "personal_agent.captains_log": MagicMock(),
        "personal_agent.captains_log.suppression": mock_supp_mod,
    }


# ---------------------------------------------------------------------------
# Event factories
# ---------------------------------------------------------------------------


def _consolidation_event(
    captures_processed: int = 5,
    entities_created: int = 3,
    entities_promoted: int = 1,
) -> ConsolidationCompletedEvent:
    return ConsolidationCompletedEvent(
        captures_processed=captures_processed,
        entities_created=entities_created,
        entities_promoted=entities_promoted,
        source_component="test",
    )


def _promotion_event(
    entry_id: str = "CL-20260101-001",
    linear_issue_id: str = "FRE-42",
    fingerprint: str | None = "fp-abc",
) -> PromotionIssueCreatedEvent:
    return PromotionIssueCreatedEvent(
        entry_id=entry_id,
        linear_issue_id=linear_issue_id,
        fingerprint=fingerprint,
        source_component="test",
    )


def _feedback_event(
    issue_id: str = "uuid-1",
    issue_identifier: str = "FRE-10",
    label: str = "Rejected",
    fingerprint: str | None = "fp-xyz",
) -> FeedbackReceivedEvent:
    return FeedbackReceivedEvent(
        issue_id=issue_id,
        issue_identifier=issue_identifier,
        label=label,
        fingerprint=fingerprint,
        source_component="test",
    )


# ---------------------------------------------------------------------------
# build_consolidation_insights_handler
# ---------------------------------------------------------------------------


class TestConsolidationInsightsHandler:
    """Tests for the consolidation → insights consumer."""

    @pytest.mark.asyncio
    async def test_runs_analyze_patterns_on_event(self) -> None:
        """Handler calls InsightsEngine.analyze_patterns when captures > 0."""
        mock_engine = MagicMock()
        mock_engine.analyze_patterns = AsyncMock(return_value=[])

        handler = build_consolidation_insights_handler()
        with pytest.MonkeyPatch().context() as mp:
            mp.setitem(sys.modules, "personal_agent.insights", MagicMock())
            mp.setitem(
                sys.modules,
                "personal_agent.insights.engine",
                MagicMock(InsightsEngine=MagicMock(return_value=mock_engine)),
            )
            await handler(_consolidation_event(captures_processed=3))

        mock_engine.analyze_patterns.assert_awaited_once_with(days=7)

    @pytest.mark.asyncio
    async def test_skips_when_no_captures(self) -> None:
        """Handler skips analysis when captures_processed is 0."""
        mock_engine = MagicMock()
        mock_engine.analyze_patterns = AsyncMock(return_value=[])

        handler = build_consolidation_insights_handler()
        with pytest.MonkeyPatch().context() as mp:
            mp.setitem(sys.modules, "personal_agent.insights", MagicMock())
            mp.setitem(
                sys.modules,
                "personal_agent.insights.engine",
                MagicMock(InsightsEngine=MagicMock(return_value=mock_engine)),
            )
            await handler(_consolidation_event(captures_processed=0))

        mock_engine.analyze_patterns.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_wrong_event_type(self) -> None:
        """Handler is a no-op for non-ConsolidationCompletedEvent events."""
        mock_engine = MagicMock()
        mock_engine.analyze_patterns = AsyncMock(return_value=[])

        handler = build_consolidation_insights_handler()
        with pytest.MonkeyPatch().context() as mp:
            mp.setitem(sys.modules, "personal_agent.insights", MagicMock())
            mp.setitem(
                sys.modules,
                "personal_agent.insights.engine",
                MagicMock(InsightsEngine=MagicMock(return_value=mock_engine)),
            )
            await handler(RequestCapturedEvent(trace_id="t", session_id="s", source_component="test"))

        mock_engine.analyze_patterns.assert_not_called()

    @pytest.mark.asyncio
    async def test_publishes_pattern_event_per_insight(self) -> None:
        """Handler publishes one InsightsPatternDetectedEvent per insight when wiring is enabled."""
        from personal_agent.events.models import (
            STREAM_INSIGHTS_PATTERN_DETECTED,
            InsightsPatternDetectedEvent,
        )

        published: list[tuple[str, object]] = []

        class _FakeBus:
            async def publish(self, stream: str, event: object, maxlen: object = None) -> None:
                published.append((stream, event))

        class _StubInsight:
            insight_type = "correlation"
            pattern_kind = ""
            title = "t"
            summary = "s"
            confidence = 0.7
            actionable = True
            evidence: dict[str, object] = {}

        mock_engine = MagicMock()
        mock_engine.analyze_patterns = AsyncMock(return_value=[_StubInsight()])
        mock_engine.create_captain_log_proposals = AsyncMock(return_value=[])

        import personal_agent.insights.fingerprints as _real_fingerprints

        handler = build_consolidation_insights_handler(event_bus=_FakeBus())
        with pytest.MonkeyPatch().context() as mp:
            mp.setitem(sys.modules, "personal_agent.insights", MagicMock())
            mp.setitem(sys.modules, "personal_agent.insights.fingerprints", _real_fingerprints)
            mp.setitem(
                sys.modules,
                "personal_agent.insights.engine",
                MagicMock(InsightsEngine=MagicMock(return_value=mock_engine)),
            )
            mp.setitem(
                sys.modules,
                "personal_agent.captains_log.manager",
                MagicMock(CaptainLogManager=MagicMock(return_value=MagicMock())),
            )
            mp.setitem(
                sys.modules,
                "personal_agent.config.settings",
                MagicMock(get_settings=MagicMock(return_value=MagicMock(insights_wiring_enabled=True))),
            )
            await handler(_consolidation_event(captures_processed=3))

        pattern_events = [(s, e) for s, e in published if s == STREAM_INSIGHTS_PATTERN_DETECTED]
        assert len(pattern_events) == 1
        evt = pattern_events[0][1]
        assert isinstance(evt, InsightsPatternDetectedEvent)
        assert evt.insight_type == "correlation"

    @pytest.mark.asyncio
    async def test_publishes_cost_anomaly_event_for_anomaly_insights(self) -> None:
        """Handler publishes InsightsCostAnomalyEvent when insight_type is 'anomaly'."""
        from personal_agent.events.models import (
            STREAM_INSIGHTS_COST_ANOMALY,
            InsightsCostAnomalyEvent,
        )

        published: list[tuple[str, object]] = []

        class _FakeBus:
            async def publish(self, stream: str, event: object, maxlen: object = None) -> None:
                published.append((stream, event))

        class _StubInsight:
            insight_type = "anomaly"
            pattern_kind = ""
            title = "Cost spike detected"
            summary = "spike"
            confidence = 0.75
            actionable = True
            evidence = {
                "observed_cost_usd": 4.12,
                "baseline_cost_usd": 1.28,
                "ratio": 3.22,
            }

        mock_engine = MagicMock()
        mock_engine.analyze_patterns = AsyncMock(return_value=[_StubInsight()])
        mock_engine.create_captain_log_proposals = AsyncMock(return_value=[])

        import personal_agent.insights.fingerprints as _real_fingerprints

        handler = build_consolidation_insights_handler(event_bus=_FakeBus())
        with pytest.MonkeyPatch().context() as mp:
            mp.setitem(sys.modules, "personal_agent.insights", MagicMock())
            mp.setitem(sys.modules, "personal_agent.insights.fingerprints", _real_fingerprints)
            mp.setitem(
                sys.modules,
                "personal_agent.insights.engine",
                MagicMock(InsightsEngine=MagicMock(return_value=mock_engine)),
            )
            mp.setitem(
                sys.modules,
                "personal_agent.captains_log.manager",
                MagicMock(CaptainLogManager=MagicMock(return_value=MagicMock())),
            )
            mp.setitem(
                sys.modules,
                "personal_agent.config.settings",
                MagicMock(get_settings=MagicMock(return_value=MagicMock(insights_wiring_enabled=True))),
            )
            await handler(_consolidation_event(captures_processed=3))

        cost_events = [(s, e) for s, e in published if s == STREAM_INSIGHTS_COST_ANOMALY]
        assert len(cost_events) == 1
        evt = cost_events[0][1]
        assert isinstance(evt, InsightsCostAnomalyEvent)
        assert evt.severity in {"low", "medium", "high"}
        assert abs(evt.ratio - 3.22) < 1e-6

    @pytest.mark.asyncio
    async def test_saves_captain_log_proposals_via_manager(self) -> None:
        """Handler calls CaptainLogManager.save_entry for every proposal."""
        mock_proposal = MagicMock()

        class _FakeBus:
            async def publish(self, stream: str, event: object, maxlen: object = None) -> None:
                pass

        class _StubInsight:
            insight_type = "trend"
            pattern_kind = ""
            title = "t"
            summary = "s"
            confidence = 0.7
            actionable = True
            evidence: dict[str, object] = {}

        mock_engine = MagicMock()
        mock_engine.analyze_patterns = AsyncMock(return_value=[_StubInsight()])
        mock_engine.create_captain_log_proposals = AsyncMock(return_value=[mock_proposal])

        mock_manager = MagicMock()
        mock_manager.save_entry = MagicMock(return_value=None)

        import personal_agent.insights.fingerprints as _real_fingerprints

        handler = build_consolidation_insights_handler(event_bus=_FakeBus())
        with pytest.MonkeyPatch().context() as mp:
            mp.setitem(sys.modules, "personal_agent.insights", MagicMock())
            mp.setitem(sys.modules, "personal_agent.insights.fingerprints", _real_fingerprints)
            mp.setitem(
                sys.modules,
                "personal_agent.insights.engine",
                MagicMock(InsightsEngine=MagicMock(return_value=mock_engine)),
            )
            mp.setitem(
                sys.modules,
                "personal_agent.captains_log.manager",
                MagicMock(CaptainLogManager=MagicMock(return_value=mock_manager)),
            )
            mp.setitem(
                sys.modules,
                "personal_agent.config.settings",
                MagicMock(get_settings=MagicMock(return_value=MagicMock(insights_wiring_enabled=True))),
            )
            await handler(_consolidation_event(captures_processed=3))

        mock_manager.save_entry.assert_called_once_with(mock_proposal)

    @pytest.mark.asyncio
    async def test_wiring_disabled_skips_bus_and_cl(self) -> None:
        """When insights_wiring_enabled=False, no bus publish and no CL save."""
        published: list[object] = []

        class _FakeBus:
            async def publish(self, stream: str, event: object, maxlen: object = None) -> None:
                published.append(event)

        class _StubInsight:
            insight_type = "trend"
            pattern_kind = ""
            title = "t"
            summary = "s"
            confidence = 0.7
            actionable = True
            evidence: dict[str, object] = {}

        mock_engine = MagicMock()
        mock_engine.analyze_patterns = AsyncMock(return_value=[_StubInsight()])
        mock_engine.create_captain_log_proposals = AsyncMock(return_value=[MagicMock()])

        mock_manager = MagicMock()

        handler = build_consolidation_insights_handler(event_bus=_FakeBus())
        with pytest.MonkeyPatch().context() as mp:
            mp.setitem(sys.modules, "personal_agent.insights", MagicMock())
            mp.setitem(
                sys.modules,
                "personal_agent.insights.engine",
                MagicMock(InsightsEngine=MagicMock(return_value=mock_engine)),
            )
            mp.setitem(
                sys.modules,
                "personal_agent.captains_log.manager",
                MagicMock(CaptainLogManager=MagicMock(return_value=mock_manager)),
            )
            mp.setitem(
                sys.modules,
                "personal_agent.config.settings",
                MagicMock(get_settings=MagicMock(return_value=MagicMock(insights_wiring_enabled=False))),
            )
            await handler(_consolidation_event(captures_processed=3))

        assert published == []
        mock_manager.save_entry.assert_not_called()
        mock_engine.create_captain_log_proposals.assert_not_called()


# ---------------------------------------------------------------------------
# build_consolidation_promotion_handler
# ---------------------------------------------------------------------------


class TestConsolidationPromotionHandler:
    """Tests for the consolidation → promotion consumer."""

    @pytest.mark.asyncio
    async def test_runs_promotion_pipeline(self) -> None:
        """Handler calls PromotionPipeline.run on consolidation.completed."""
        mock_pipeline = MagicMock()
        mock_pipeline.run = AsyncMock(return_value=[])

        handler = build_consolidation_promotion_handler(linear_client=None)
        with pytest.MonkeyPatch().context() as mp:
            for k, v in _promotion_modules(mock_pipeline).items():
                mp.setitem(sys.modules, k, v)
            await handler(_consolidation_event())

        mock_pipeline.run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ignores_wrong_event_type(self) -> None:
        """Handler is a no-op for non-ConsolidationCompletedEvent."""
        mock_pipeline = MagicMock()
        mock_pipeline.run = AsyncMock(return_value=[])

        handler = build_consolidation_promotion_handler()
        with pytest.MonkeyPatch().context() as mp:
            for k, v in _promotion_modules(mock_pipeline).items():
                mp.setitem(sys.modules, k, v)
            await handler(RequestCapturedEvent(trace_id="t", session_id="s", source_component="test"))

        mock_pipeline.run.assert_not_called()


# ---------------------------------------------------------------------------
# build_promotion_captain_log_handler
# ---------------------------------------------------------------------------


class TestPromotionCaptainLogHandler:
    """Tests for the promotion.issue_created → captain-log consumer."""

    @pytest.mark.asyncio
    async def test_saves_captain_log_entry(self) -> None:
        """Handler calls CaptainLogManager.save_entry with an OBSERVATION entry."""
        mock_manager_instance = MagicMock()
        mock_manager_instance.save_entry = MagicMock(return_value=None)

        handler = build_promotion_captain_log_handler()
        with pytest.MonkeyPatch().context() as mp:
            for k, v in _captain_log_modules(mock_manager_instance).items():
                mp.setitem(sys.modules, k, v)
            await handler(_promotion_event(linear_issue_id="FRE-99"))

        mock_manager_instance.save_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_wrong_event_type(self) -> None:
        """Handler is a no-op for non-PromotionIssueCreatedEvent."""
        mock_manager_instance = MagicMock()

        handler = build_promotion_captain_log_handler()
        with pytest.MonkeyPatch().context() as mp:
            for k, v in _captain_log_modules(mock_manager_instance).items():
                mp.setitem(sys.modules, k, v)
            await handler(RequestCapturedEvent(trace_id="t", session_id="s", source_component="test"))

        mock_manager_instance.save_entry.assert_not_called()


# ---------------------------------------------------------------------------
# build_compaction_quality_captain_log_handler (FRE-249 / ADR-0059)
# ---------------------------------------------------------------------------


def _cq_event(
    *,
    fingerprint: str = "fpcompact0000abc",
    trace_id: str = "trace-cq-1",
    session_id: str = "session-cq-1",
    noun_phrase: str = "caching system",
    dropped_entity: str = "redis-config",
    recall_cue: str = "what was our caching system again",
    tier_affected: str = "episodic",
    tokens_removed: int = 412,
) -> CompactionQualityIncidentEvent:
    return CompactionQualityIncidentEvent(
        trace_id=trace_id,
        session_id=session_id,
        fingerprint=fingerprint,
        noun_phrase=noun_phrase,
        dropped_entity=dropped_entity,
        recall_cue=recall_cue,
        tier_affected=tier_affected,
        tokens_removed=tokens_removed,
        detected_at=datetime.now(timezone.utc),
    )


class TestCompactionQualityCaptainLogHandler:
    """Tests for the context-quality → captain-log consumer (FRE-249)."""

    @pytest.mark.asyncio
    async def test_saves_captain_log_entry(self) -> None:
        mock_manager_instance = MagicMock()
        mock_manager_instance.save_entry = MagicMock(return_value=None)

        handler = build_compaction_quality_captain_log_handler(
            manager=mock_manager_instance
        )
        await handler(_cq_event())

        mock_manager_instance.save_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_lazy_manager_instantiated_when_not_passed(self) -> None:
        mock_manager_instance = MagicMock()
        mock_manager_instance.save_entry = MagicMock(return_value=None)

        handler = build_compaction_quality_captain_log_handler()
        with pytest.MonkeyPatch().context() as mp:
            for k, v in _captain_log_modules(mock_manager_instance).items():
                mp.setitem(sys.modules, k, v)
            await handler(_cq_event())

        mock_manager_instance.save_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_wrong_event_type(self) -> None:
        mock_manager_instance = MagicMock()
        handler = build_compaction_quality_captain_log_handler(
            manager=mock_manager_instance
        )
        await handler(
            RequestCapturedEvent(
                trace_id="t", session_id="s", source_component="test"
            )
        )
        mock_manager_instance.save_entry.assert_not_called()


# ---------------------------------------------------------------------------
# build_feedback_insights_handler
# ---------------------------------------------------------------------------


class TestFeedbackInsightsHandler:
    """Tests for the feedback.received → insights consumer (log-only stub)."""

    @pytest.mark.asyncio
    async def test_processes_feedback_event(self) -> None:
        """Handler runs without error for a valid feedback event."""
        handler = build_feedback_insights_handler()
        await handler(_feedback_event(label="Approved"))

    @pytest.mark.asyncio
    async def test_ignores_wrong_event_type(self) -> None:
        """Handler is a no-op for non-FeedbackReceivedEvent."""
        handler = build_feedback_insights_handler()
        await handler(RequestCapturedEvent(trace_id="t", session_id="s", source_component="test"))


# ---------------------------------------------------------------------------
# build_feedback_suppression_handler
# ---------------------------------------------------------------------------


class TestFeedbackSuppressionHandler:
    """Tests for the feedback.received → suppression consumer."""

    @pytest.mark.asyncio
    async def test_records_suppression_on_rejected(self) -> None:
        """Handler calls record_rejection_suppression for Rejected label."""
        mock_suppress = MagicMock()

        handler = build_feedback_suppression_handler()
        with pytest.MonkeyPatch().context() as mp:
            for k, v in _suppression_modules(mock_suppress).items():
                mp.setitem(sys.modules, k, v)
            await handler(_feedback_event(label="Rejected", fingerprint="fp-xyz"))

        mock_suppress.assert_called_once_with("fp-xyz", issue_identifier="FRE-10")

    @pytest.mark.asyncio
    async def test_skips_non_rejected_labels(self) -> None:
        """Handler is a no-op for labels other than 'Rejected'."""
        mock_suppress = MagicMock()

        handler = build_feedback_suppression_handler()
        with pytest.MonkeyPatch().context() as mp:
            for k, v in _suppression_modules(mock_suppress).items():
                mp.setitem(sys.modules, k, v)
            await handler(_feedback_event(label="Approved", fingerprint="fp-xyz"))

        mock_suppress.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_fingerprint(self) -> None:
        """Handler is a no-op when fingerprint is None."""
        mock_suppress = MagicMock()

        handler = build_feedback_suppression_handler()
        with pytest.MonkeyPatch().context() as mp:
            for k, v in _suppression_modules(mock_suppress).items():
                mp.setitem(sys.modules, k, v)
            await handler(_feedback_event(label="Rejected", fingerprint=None))

        mock_suppress.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_wrong_event_type(self) -> None:
        """Handler is a no-op for non-FeedbackReceivedEvent."""
        mock_suppress = MagicMock()

        handler = build_feedback_suppression_handler()
        with pytest.MonkeyPatch().context() as mp:
            for k, v in _suppression_modules(mock_suppress).items():
                mp.setitem(sys.modules, k, v)
            await handler(RequestCapturedEvent(trace_id="t", session_id="s", source_component="test"))

        mock_suppress.assert_not_called()
