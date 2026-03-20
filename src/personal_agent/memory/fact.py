"""Fact and promotion types for semantic memory.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.3
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from personal_agent.memory.protocol import MemoryType


@dataclass(frozen=True)
class Fact:
    """A stable assertion promoted to semantic memory."""

    fact_id: str
    assertion: str
    confidence: float
    source_episode_ids: list[str]
    entity_name: str
    entity_type: str
    memory_type: MemoryType
    created_at: datetime


@dataclass(frozen=True)
class PromotionCandidate:
    """An entity evaluated for promotion to semantic memory."""

    entity_name: str
    entity_type: str
    mention_count: int
    first_seen: datetime
    last_seen: datetime
    source_turn_ids: list[str]
    description: str | None = None

    def stability_score(self) -> float:
        """Compute stability score: mention_factor (0-0.5) + time_factor (0-0.5)."""
        mention_factor = min(self.mention_count / 100.0, 0.5)
        days = (self.last_seen - self.first_seen).total_seconds() / 86400.0
        time_factor = min(days / 90.0, 0.5)
        return mention_factor + time_factor


@dataclass(frozen=True)
class PromotionResult:
    """Result of a promotion batch."""

    promoted_count: int
    skipped_count: int
    facts_created: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.promoted_count > 0
