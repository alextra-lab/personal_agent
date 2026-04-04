"""Pydantic types for proactive memory (ADR-0039)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProactiveScoreComponents(BaseModel):
    """Decomposed scores for debugging (no raw user text)."""

    embedding: float = Field(ge=0.0, le=1.0, description="Normalized vector similarity.")
    entity_overlap: float = Field(
        ge=0.0, le=1.0, description="Session vs candidate entity overlap."
    )
    recency: float = Field(ge=0.0, le=1.0, description="Recency sub-score.")
    topic_coherence: float = Field(
        ge=0.0,
        le=1.0,
        description="Topic proxy coherence (MVP stub / keyword overlap).",
    )


class ProactiveMemoryCandidate(BaseModel):
    """One ranked proactive memory item."""

    kind: Literal["entity", "episode", "session_summary"]
    payload: dict[str, Any] = Field(
        description="Memory context dict for LLM (same shapes as recall path).",
    )
    relevance_score: float = Field(ge=0.0, le=1.0)
    score_components: ProactiveScoreComponents


class ProactiveMemorySuggestions(BaseModel):
    """Result of suggest_relevant() after scoring and budget trim."""

    candidates: list[ProactiveMemoryCandidate] = Field(default_factory=list)
    query_embedding_ms: float | None = Field(
        default=None,
        description="Wall time to produce query embedding, milliseconds.",
    )
