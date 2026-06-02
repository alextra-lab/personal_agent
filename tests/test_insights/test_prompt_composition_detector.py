"""Tests for the prompt-composition insights detector (FRE-409).

Covers:
  - ≥2 occurrences of a component_id → one Insight(insight_type='prompt_composition')
  - Below threshold → empty
  - Multiple components → one Insight per qualifying component_id
  - ES failure → empty, no exception
  - create_captain_log_proposals produces CONFIG_PROPOSAL with PERFORMANCE/LLM_CLIENT
  - _category_for_insight_type / _scope_for_insight_type mapping entries
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from personal_agent.captains_log.models import ChangeCategory, ChangeScope
from personal_agent.insights.engine import InsightsEngine


def _make_engine() -> InsightsEngine:
    engine = InsightsEngine()
    engine._memory = AsyncMock()  # type: ignore[assignment]
    engine._memory.connected = False
    engine._cost_tracker = AsyncMock()  # type: ignore[assignment]
    return engine


# ---------------------------------------------------------------------------
# Threshold tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDetectPromptCompositionProposals:
    async def test_two_occurrences_emits_insight(self) -> None:
        """≥2 reflections naming the same component_id → one prompt_composition insight."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_prompt_composition_proposal_buckets = AsyncMock(
            return_value=[
                {
                    "component_id": "skill_index",
                    "occurrences": 2,
                    "scope": "orchestrator",
                }
            ]
        )

        insights = await engine.detect_prompt_composition_proposals(days=7)

        assert len(insights) == 1
        insight = insights[0]
        assert insight.insight_type == "prompt_composition"
        assert insight.pattern_kind == "prompt_component_proposal"
        assert insight.actionable is True
        assert insight.confidence >= 0.55
        assert insight.evidence["component_id"] == "skill_index"
        assert insight.evidence["occurrences"] == 2

    async def test_one_occurrence_below_threshold_returns_empty(self) -> None:
        """<2 occurrences → insight NOT emitted."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_prompt_composition_proposal_buckets = AsyncMock(
            return_value=[
                {"component_id": "skill_index", "occurrences": 1, "scope": "orchestrator"}
            ]
        )

        insights = await engine.detect_prompt_composition_proposals(days=7)

        assert insights == []

    async def test_multiple_components_each_above_threshold(self) -> None:
        """Two qualifying components → two distinct insights."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_prompt_composition_proposal_buckets = AsyncMock(
            return_value=[
                {"component_id": "skill_index", "occurrences": 3, "scope": "orchestrator"},
                {"component_id": "memory_section", "occurrences": 2, "scope": "llm_client"},
            ]
        )

        insights = await engine.detect_prompt_composition_proposals(days=7)

        assert len(insights) == 2
        component_ids = {i.evidence["component_id"] for i in insights}
        assert component_ids == {"skill_index", "memory_section"}

    async def test_mixed_threshold_only_qualifying_emitted(self) -> None:
        """Only components at or above threshold are emitted."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_prompt_composition_proposal_buckets = AsyncMock(
            return_value=[
                {"component_id": "skill_index", "occurrences": 3, "scope": "orchestrator"},
                {"component_id": "tool_awareness", "occurrences": 1, "scope": "llm_client"},
            ]
        )

        insights = await engine.detect_prompt_composition_proposals(days=7)

        assert len(insights) == 1
        assert insights[0].evidence["component_id"] == "skill_index"

    async def test_es_failure_returns_empty(self) -> None:
        """ES query failure → empty list, no exception propagated."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_prompt_composition_proposal_buckets = AsyncMock(
            side_effect=Exception("ES connection refused")
        )

        insights = await engine.detect_prompt_composition_proposals(days=7)

        assert insights == []

    async def test_empty_buckets_returns_empty(self) -> None:
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_prompt_composition_proposal_buckets = AsyncMock(return_value=[])

        insights = await engine.detect_prompt_composition_proposals(days=7)

        assert insights == []

    async def test_confidence_increases_with_occurrences(self) -> None:
        """Higher occurrences → higher confidence (up to 0.90)."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_prompt_composition_proposal_buckets = AsyncMock(
            return_value=[
                {"component_id": "skill_index", "occurrences": 2, "scope": "orchestrator"},
                {"component_id": "memory_section", "occurrences": 7, "scope": "orchestrator"},
            ]
        )

        insights = await engine.detect_prompt_composition_proposals(days=7)

        confidence_by_component = {i.evidence["component_id"]: i.confidence for i in insights}
        assert confidence_by_component["memory_section"] > confidence_by_component["skill_index"]
        assert confidence_by_component["memory_section"] <= 0.90

    async def test_confidence_capped_at_0_90(self) -> None:
        """Confidence never exceeds 0.90 even at high occurrences."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_prompt_composition_proposal_buckets = AsyncMock(
            return_value=[
                {"component_id": "skill_index", "occurrences": 100, "scope": "orchestrator"}
            ]
        )

        insights = await engine.detect_prompt_composition_proposals(days=7)

        assert insights[0].confidence <= 0.90

    async def test_insight_title_contains_component_id(self) -> None:
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_prompt_composition_proposal_buckets = AsyncMock(
            return_value=[
                {"component_id": "deployment_context", "occurrences": 3, "scope": "llm_client"}
            ]
        )

        insights = await engine.detect_prompt_composition_proposals(days=7)

        assert "deployment_context" in insights[0].title

    async def test_evidence_includes_window_days(self) -> None:
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_prompt_composition_proposal_buckets = AsyncMock(
            return_value=[
                {"component_id": "skill_index", "occurrences": 2, "scope": "orchestrator"}
            ]
        )

        insights = await engine.detect_prompt_composition_proposals(days=14)

        assert insights[0].evidence["window_days"] == 14


# ---------------------------------------------------------------------------
# create_captain_log_proposals with prompt_composition insight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPromptCompositionProposal:
    async def test_proposal_has_performance_category(self) -> None:
        """create_captain_log_proposals maps prompt_composition → PERFORMANCE category."""
        from personal_agent.insights.engine import Insight

        engine = _make_engine()
        insight = Insight(
            insight_type="prompt_composition",
            pattern_kind="prompt_component_proposal",
            title="Reflection proposes changing prompt component 'skill_index' (2x, 7d)",
            summary="skill_index flagged 2 times",
            confidence=0.65,
            actionable=True,
            evidence={"component_id": "skill_index", "occurrences": 2, "window_days": 7},
        )

        proposals = await engine.create_captain_log_proposals([insight])

        assert len(proposals) == 1
        pc = proposals[0].proposed_change
        assert pc is not None
        assert pc.category == ChangeCategory.PERFORMANCE

    async def test_proposal_has_llm_client_scope(self) -> None:
        """create_captain_log_proposals maps prompt_composition → LLM_CLIENT scope."""
        from personal_agent.insights.engine import Insight

        engine = _make_engine()
        insight = Insight(
            insight_type="prompt_composition",
            pattern_kind="prompt_component_proposal",
            title="Reflection proposes changing prompt component 'skill_index' (2x, 7d)",
            summary="skill_index flagged",
            confidence=0.65,
            actionable=True,
            evidence={"component_id": "skill_index", "occurrences": 2, "window_days": 7},
        )

        proposals = await engine.create_captain_log_proposals([insight])

        pc = proposals[0].proposed_change
        assert pc is not None
        assert pc.scope == ChangeScope.LLM_CLIENT

    async def test_fingerprint_stable_across_digit_variation(self) -> None:
        """Two insight titles differing only in digit-runs produce the same fingerprint."""
        from personal_agent.insights.fingerprints import pattern_fingerprint

        title_a = "Reflection proposes changing prompt component 'skill_index' (2x, 7d)"
        title_b = "Reflection proposes changing prompt component 'skill_index' (5x, 7d)"
        fp_a = pattern_fingerprint("prompt_composition", "prompt_component_proposal", title_a)
        fp_b = pattern_fingerprint("prompt_composition", "prompt_component_proposal", title_b)
        assert fp_a == fp_b, (
            "Fingerprint should normalize digit-runs so that 2x and 5x produce the same key"
        )


# ---------------------------------------------------------------------------
# Mapping function coverage
# ---------------------------------------------------------------------------


class TestInsightTypeMappings:
    def test_category_mapping_prompt_composition_is_performance(self) -> None:
        from personal_agent.insights.engine import _category_for_insight_type

        assert _category_for_insight_type("prompt_composition") == ChangeCategory.PERFORMANCE

    def test_scope_mapping_prompt_composition_is_llm_client(self) -> None:
        from personal_agent.insights.engine import _scope_for_insight_type

        assert _scope_for_insight_type("prompt_composition") == ChangeScope.LLM_CLIENT
