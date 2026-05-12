"""Tests for missing-skill pattern analysis in InsightsEngine (FRE-328).

Verifies that ``detect_missing_skill_patterns`` produces an Insight only when a
skill name clears both thresholds:

- ``MIN_MISSING_SKILL_REQUESTS`` (3+) requests
- ``MIN_MISSING_SKILL_SESSIONS`` (2+) distinct sessions

The insight then flows through ``create_captain_log_proposals`` into the
existing Captain's Log → promotion → Linear pipeline (covered by other tests).
"""

from unittest.mock import AsyncMock

import pytest

from personal_agent.insights.engine import (
    MIN_MISSING_SKILL_REQUESTS,
    MIN_MISSING_SKILL_SESSIONS,
    InsightsEngine,
)


@pytest.mark.asyncio
class TestDetectMissingSkillEmpty:
    """No data / under-threshold cases return empty list."""

    async def test_returns_empty_when_no_buckets(self) -> None:
        """No missing_skill_requested events → no insights."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[]
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        assert insights == []

    async def test_below_request_threshold_skipped(self) -> None:
        """Skill requested fewer than MIN_MISSING_SKILL_REQUESTS times → no insight."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[("rare-skill", MIN_MISSING_SKILL_REQUESTS - 1, 3)],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        assert insights == []

    async def test_below_session_threshold_skipped(self) -> None:
        """Skill requested often but in a single session → no insight."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[("loop-skill", 10, MIN_MISSING_SKILL_SESSIONS - 1)],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        assert insights == []

    async def test_es_failure_returns_empty(self) -> None:
        """ES error → analysis_failed logged, returns []."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("ES unavailable"),
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        assert insights == []


@pytest.mark.asyncio
class TestDetectMissingSkillProducesInsight:
    """Above-threshold clusters produce structured insights."""

    async def test_clears_thresholds_produces_insight(self) -> None:
        """3+ requests across 2+ sessions → one missing_skill insight."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[("kibana-dashboard-builder", 5, 3)],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        assert len(insights) == 1
        ins = insights[0]
        assert ins.insight_type == "missing_skill"
        assert ins.pattern_kind == "missing_skill_requested"
        assert ins.actionable is True
        assert ins.evidence["requested_name"] == "kibana-dashboard-builder"
        assert ins.evidence["request_count"] == 5
        assert ins.evidence["distinct_sessions"] == 3
        assert ins.evidence["window_days"] == 7
        assert "kibana-dashboard-builder" in ins.title
        assert "kibana-dashboard-builder" in ins.summary
        assert 0.60 <= ins.confidence <= 0.95

    async def test_confidence_scales_with_request_count(self) -> None:
        """Higher request counts produce higher confidence (clamped to 0.95)."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                ("hot-skill", 50, 10),
                ("warm-skill", 3, 2),
            ],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        by_name = {i.evidence["requested_name"]: i for i in insights}
        assert by_name["hot-skill"].confidence > by_name["warm-skill"].confidence
        assert by_name["hot-skill"].confidence <= 0.95

    async def test_multiple_skills_produce_multiple_insights(self) -> None:
        """Each qualifying skill name becomes its own insight."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                ("skill-a", 4, 2),
                ("skill-b", 3, 2),
                ("skill-c", 1, 1),  # below thresholds → filtered
            ],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        names = {i.evidence["requested_name"] for i in insights}
        assert names == {"skill-a", "skill-b"}


@pytest.mark.asyncio
class TestMissingSkillFingerprint:
    """Insight → Captain's Log proposal fingerprint dedups on skill name."""

    async def test_proposal_fingerprint_per_skill_name(self) -> None:
        """Same skill name → same fingerprint (existing dedup increments seen_count)."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[("repeat-skill", 5, 3)],
        )
        first = await engine.detect_missing_skill_patterns(days=7)
        proposals_a = await engine.create_captain_log_proposals(first)

        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[("repeat-skill", 12, 6)],
        )
        second = await engine.detect_missing_skill_patterns(days=7)
        proposals_b = await engine.create_captain_log_proposals(second)

        assert proposals_a and proposals_b
        fp_a = proposals_a[0].proposed_change.fingerprint  # type: ignore[union-attr]
        fp_b = proposals_b[0].proposed_change.fingerprint  # type: ignore[union-attr]
        assert fp_a == fp_b, "fingerprint must be stable per skill name across runs"

    async def test_different_skills_get_different_fingerprints(self) -> None:
        """Different skill names → distinct fingerprints."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                ("skill-x", 5, 3),
                ("skill-y", 5, 3),
            ],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        proposals = await engine.create_captain_log_proposals(insights)
        fingerprints = {
            p.proposed_change.fingerprint  # type: ignore[union-attr]
            for p in proposals
            if p.proposed_change is not None
        }
        assert len(fingerprints) == 2
