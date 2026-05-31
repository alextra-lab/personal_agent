"""Data model for per-turn user value ratings (FRE-407).

Users assign a 0–3 rating to each assistant turn.  The rating is stored in
Elasticsearch (``user-turn-ratings-*``) keyed on ``trace_id`` — one doc per
turn, overwritten on re-rate (idempotent).  An NDJSON file under
``telemetry/user_feedback/`` provides an append-only audit trail; it is NOT
used for aggregation.

The ``prompt_*`` identity fields are best-effort denormalisations sourced at
write time from ``agent-logs-*`` model_call_completed events.  A write-time
miss leaves them null — the Insights consumer joins at read time and treats
that join as the authoritative source for per-callsite metrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class UserTurnRating:
    """Immutable value-rating record for a single assistant turn.

    Keyed on ``trace_id`` — one record per turn in Elasticsearch (re-rate
    overwrites).  ``prompt_*`` fields are best-effort write-time denorms from
    ``agent-logs-*``; they may be null when the rating arrives before ES
    has indexed the model_call_completed event.

    Attributes:
        trace_id: UUID of the rated turn (join key across all substrates).
        session_id: Session that produced the turn.
        rating: User value score — 0 (no value) through 3 (wow).
        prompt_callsite: Dotted callsite of the primary reasoning call, e.g.
            ``"orchestrator.primary"``.  Null when identity lookup missed.
        prompt_static_prefix_hash: Hash of the static prompt prefix at call
            time.  Null when identity lookup missed.
        prompt_dynamic_hash: Hash of the dynamic prompt section.  Null when
            identity lookup missed.
        prompt_component_ids: Ordered tuple of prompt component identifiers
            included in the call.  Empty when identity lookup missed.
        rated_at: UTC datetime when the rating was recorded.
    """

    trace_id: str
    session_id: str
    rating: int
    prompt_callsite: str | None
    prompt_static_prefix_hash: str | None
    prompt_dynamic_hash: str | None
    prompt_component_ids: tuple[str, ...]
    rated_at: datetime

    def to_es_doc(self) -> dict[str, Any]:
        """Serialise to an Elasticsearch-ready flat dict.

        ``rated_at`` is formatted as ISO 8601.
        ``prompt_component_ids`` is converted from tuple to list so ES
        receives a JSON array.

        Returns:
            Flat dict safe to pass to ``schedule_es_index``.
        """
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "rating": self.rating,
            "prompt_callsite": self.prompt_callsite,
            "prompt_static_prefix_hash": self.prompt_static_prefix_hash,
            "prompt_dynamic_hash": self.prompt_dynamic_hash,
            "prompt_component_ids": list(self.prompt_component_ids),
            "rated_at": self.rated_at.isoformat(),
        }
