"""Pydantic types for proactive memory (ADR-0039)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from personal_agent.captains_log.turn_evidence import DropReason


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


class ProactiveMemoryDiscard(BaseModel):
    """One candidate the proactive path removed, with the gate that removed it (FRE-1060).

    The path applies eight successive gates and used to return only their survivors, so
    the discarded candidates ceased to exist before the turn evidence record was built
    and the record reported survivors as the population. Carrying each discard with its
    reason is what makes the gate measurable — which gate decides was previously
    unknowable from any record the system wrote.

    Deliberately not a wrapper around :class:`ProactiveMemoryCandidate`: that model
    requires a ``relevance_score`` in [0, 1], and the first gate fires *before* scoring.
    Reusing it would force a fabricated 0.0 for every duplicate — the same
    absence-dressed-as-a-value the record exists to eliminate. ``relevance_score`` is
    nullable here so "no score was computed" stays sayable.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["entity", "episode", "session_summary"]
    payload: dict[str, Any] = Field(
        description="Memory context dict, identical in shape to an emitted candidate's.",
    )
    relevance_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Final score, or None when the gate fired before scoring.",
    )
    drop_reason: DropReason = Field(
        description="The gate that removed it — one of the DropReason.RECALL_* members.",
    )


class ProactiveMemorySuggestions(BaseModel):
    """Result of suggest_relevant() after scoring and budget trim.

    ``candidates`` and ``discarded`` together account for **every** row retrieval
    returned: no row is silently lost between the graph read and this result.
    """

    candidates: list[ProactiveMemoryCandidate] = Field(default_factory=list)
    discarded: list[ProactiveMemoryDiscard] = Field(
        default_factory=list,
        description="Every candidate a gate removed, in the order the gates fired.",
    )
    query_embedding_ms: float | None = Field(
        default=None,
        description="Wall time to produce query embedding, milliseconds.",
    )
