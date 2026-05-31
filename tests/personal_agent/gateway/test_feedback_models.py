"""Tests for the UserTurnRating data model (FRE-407)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from personal_agent.gateway.feedback_models import UserTurnRating


class TestUserTurnRatingToEsDoc:
    """Round-trip serialisation via to_es_doc()."""

    def _make_rating(
        self,
        trace_id: str = "trace-abc",
        session_id: str = "sess-123",
        rating: int = 2,
        prompt_callsite: str | None = "orchestrator.primary",
        prompt_static_prefix_hash: str | None = "hash-static",
        prompt_dynamic_hash: str | None = "hash-dynamic",
        prompt_component_ids: tuple[str, ...] = ("comp-a", "comp-b"),
        rated_at: datetime | None = None,
    ) -> UserTurnRating:
        return UserTurnRating(
            trace_id=trace_id,
            session_id=session_id,
            rating=rating,
            prompt_callsite=prompt_callsite,
            prompt_static_prefix_hash=prompt_static_prefix_hash,
            prompt_dynamic_hash=prompt_dynamic_hash,
            prompt_component_ids=prompt_component_ids,
            rated_at=rated_at or datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_to_es_doc_all_fields_present(self) -> None:
        """All fields are serialised correctly."""
        rating = self._make_rating()
        doc = rating.to_es_doc()

        assert doc["trace_id"] == "trace-abc"
        assert doc["session_id"] == "sess-123"
        assert doc["rating"] == 2
        assert doc["prompt_callsite"] == "orchestrator.primary"
        assert doc["prompt_static_prefix_hash"] == "hash-static"
        assert doc["prompt_dynamic_hash"] == "hash-dynamic"
        assert doc["prompt_component_ids"] == ["comp-a", "comp-b"]
        assert doc["rated_at"] == "2026-05-31T12:00:00+00:00"

    def test_to_es_doc_null_identity_fields(self) -> None:
        """Null identity fields are serialised as null (not omitted)."""
        rating = self._make_rating(
            prompt_callsite=None,
            prompt_static_prefix_hash=None,
            prompt_dynamic_hash=None,
            prompt_component_ids=(),
        )
        doc = rating.to_es_doc()
        assert doc["prompt_callsite"] is None
        assert doc["prompt_static_prefix_hash"] is None
        assert doc["prompt_dynamic_hash"] is None
        assert doc["prompt_component_ids"] == []

    def test_to_es_doc_returns_dict(self) -> None:
        """to_es_doc always returns a plain dict."""
        rating = self._make_rating()
        assert isinstance(rating.to_es_doc(), dict)

    def test_model_is_frozen(self) -> None:
        """UserTurnRating is immutable (frozen dataclass)."""
        rating = self._make_rating()
        with pytest.raises((AttributeError, TypeError)):
            rating.rating = 3  # type: ignore[misc]

    def test_prompt_component_ids_tuple_preserved(self) -> None:
        """prompt_component_ids is stored as tuple but serialised as list."""
        rating = self._make_rating(prompt_component_ids=("x", "y", "z"))
        assert isinstance(rating.prompt_component_ids, tuple)
        doc = rating.to_es_doc()
        assert doc["prompt_component_ids"] == ["x", "y", "z"]
