"""D5: Knowledge confidence metadata for memory graph entities (ADR-0047).

KnowledgeWeight captures provenance and confidence for each fact stored in
the Neo4j knowledge graph.  Low-confidence facts (confidence < 0.4) receive
a soft relevance penalty in the recall controller scoring path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Source types ordered from most to least trustworthy.
SourceType = Literal["conversation", "tool_result", "web_search", "manual", "inferred"]

_DEFAULT_CONFIDENCE: dict[str, float] = {
    "conversation": 0.8,
    "tool_result": 0.7,
    "web_search": 0.6,
    "manual": 1.0,
    "inferred": 0.4,
}

# FRE-1020: co-authorship (ADR-0098 D6) is a *different axis* from ``source_type``.
# ``source_type`` records the **channel** a fact arrived through; every extracted Claim
# arrives through ``conversation``, so the channel vocabulary is structurally incapable of
# expressing *who asserted* the fact — which is why claim confidence was constant and, with
# it, the ADR-0098 D2 weaker-claim guard unreachable (the confidence comparison could never
# be unequal; only the observed_at staleness check still discriminated).
AssertedBy = Literal["user", "agent"]

# Authorship is an **uplift over the channel base**, and the agent tier *is* the channel
# base. The direction is load-bearing: demoting agent-derived below the base would put every
# pre-FRE-1020 row above the new floor, so no new claim could ever supersede a legacy one and
# the substrate would freeze. Uplifting instead leaves every existing path untouched and adds
# only one narrow REJECT — an agent-derived claim can no longer clobber a user-asserted fact.
USER_ASSERTED_UPLIFT = 0.1


class KnowledgeWeight(BaseModel):
    """Confidence and provenance metadata for a knowledge graph entity.

    Stored as properties on Neo4j entity nodes alongside entity data.
    Low-confidence facts (confidence < 0.4) receive a soft relevance penalty
    during recall controller scoring.

    Attributes:
        confidence: Confidence in [0.0, 1.0].  Facts with confidence < 0.4
            receive a -10 % relevance penalty in the recall controller.
        source_type: Origin of this fact.
        corroboration_count: Number of independent sources that confirmed this
            fact.  Incremented each time the same fact is re-observed.
        last_confirmed: UTC datetime of most recent corroboration (None if
            never corroborated after initial capture).
    """

    model_config = ConfigDict(frozen=True)

    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_type: SourceType = "inferred"
    corroboration_count: int = 0
    last_confirmed: datetime | None = None

    @classmethod
    def from_source(
        cls,
        source_type: str,
        base_confidence: float | None = None,
    ) -> "KnowledgeWeight":
        """Create a KnowledgeWeight with appropriate defaults for a source type.

        Args:
            source_type: Where this fact came from.  Must be one of
                ``conversation``, ``tool_result``, ``web_search``,
                ``manual``, or ``inferred``.
            base_confidence: Override the default confidence for this source.
                When None, the source-appropriate default is used.

        Returns:
            KnowledgeWeight with source-appropriate defaults.

        Example:
            >>> w = KnowledgeWeight.from_source("conversation")
            >>> w.confidence
            0.8
        """
        confidence = (
            base_confidence
            if base_confidence is not None
            else _DEFAULT_CONFIDENCE.get(source_type, 0.5)
        )
        return cls(confidence=confidence, source_type=source_type)  # type: ignore[arg-type]

    @classmethod
    def from_claim_provenance(
        cls,
        source_type: str,
        asserted_by: str,
    ) -> "KnowledgeWeight":
        """Create a weight from a Claim's channel *and* its co-authorship (FRE-1020).

        Realizes ADR-0098 D6's co-authorship→trust rule for durable Claims: the owner is
        the authority on their own life, so a fact they asserted themselves outranks one
        the agent asserted or inferred. ``asserted_by`` is derived in Python from the
        role-partitioned captured text and is never self-reported by the extraction model
        (ADR-0098 AC-9) — see
        :func:`~personal_agent.second_brain.entity_extraction._attribute_claim_authorship`.

        Anything other than ``"user"`` — including an absent or off-vocabulary value —
        yields the channel base, i.e. exactly the pre-FRE-1020 confidence, so an
        attribution miss is never worse than today's behaviour.

        Args:
            source_type: The origin channel (``conversation`` for extracted Claims).
            asserted_by: ``"user"`` (the owner stated it) or ``"agent"`` (the assistant
                asserted or inferred it).

        Returns:
            KnowledgeWeight whose confidence is the channel base, uplifted by
            :data:`USER_ASSERTED_UPLIFT` (clamped to 1.0) when user-asserted.

        Example:
            >>> KnowledgeWeight.from_claim_provenance("conversation", "user").confidence
            0.9
            >>> KnowledgeWeight.from_claim_provenance("conversation", "agent").confidence
            0.8
        """
        base = _DEFAULT_CONFIDENCE.get(source_type, 0.5)
        if asserted_by == "user":
            base = min(1.0, round(base + USER_ASSERTED_UPLIFT, 4))
        return cls(confidence=base, source_type=source_type)  # type: ignore[arg-type]
