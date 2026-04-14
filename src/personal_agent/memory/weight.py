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
