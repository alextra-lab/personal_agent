"""Tests for Fact and promotion types."""

from __future__ import annotations

from datetime import datetime, timezone

from personal_agent.memory.fact import (
    Fact,
    PromotionCandidate,
    PromotionResult,
)
from personal_agent.memory.protocol import MemoryType


class TestFact:
    def test_construction(self) -> None:
        fact = Fact(
            fact_id="fact-001",
            assertion="User prefers Google-style docstrings",
            confidence=0.85,
            source_episode_ids=["turn-123", "turn-456"],
            entity_name="coding conventions",
            entity_type="Concept",
            memory_type=MemoryType.SEMANTIC,
            created_at=datetime.now(tz=timezone.utc),
        )
        assert fact.confidence == 0.85
        assert len(fact.source_episode_ids) == 2

    def test_frozen(self) -> None:
        fact = Fact(
            fact_id="f1",
            assertion="test",
            confidence=0.5,
            source_episode_ids=[],
            entity_name="test",
            entity_type="Concept",
            memory_type=MemoryType.SEMANTIC,
            created_at=datetime.now(tz=timezone.utc),
        )
        try:
            fact.confidence = 0.9  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestPromotionCandidate:
    def test_stability_score(self) -> None:
        candidate = PromotionCandidate(
            entity_name="Neo4j",
            entity_type="Technology",
            mention_count=10,
            first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_seen=datetime(2026, 3, 15, tzinfo=timezone.utc),
            source_turn_ids=["t1"] * 10,
            description="Graph database",
        )
        score = candidate.stability_score()
        assert 0.0 <= score <= 1.0
        assert score > 0.5


class TestPromotionResult:
    def test_success(self) -> None:
        result = PromotionResult(
            promoted_count=3, skipped_count=7,
            facts_created=["f1", "f2", "f3"], errors=[],
        )
        assert result.success is True

    def test_partial_failure(self) -> None:
        result = PromotionResult(
            promoted_count=2, skipped_count=5,
            facts_created=["f1", "f2"],
            errors=["Entity X: Neo4j write failed"],
        )
        assert result.success is True
        assert len(result.errors) == 1
