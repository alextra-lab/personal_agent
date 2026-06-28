# tests/personal_agent/memory/test_hybrid_search.py
"""Tests for hybrid search (vector + keyword + graph traversal).

Hybrid search combines:
1. Vector similarity (embedding cosine distance)
2. Keyword matching (existing entity name/type MERGE)
3. Graph traversal (relationship-based discovery)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.memory.models import MemoryQuery, TurnNode
from personal_agent.memory.service import MemoryService


def _make_service() -> MemoryService:
    """Build a MemoryService instance bypassing __init__ for unit testing."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._query_feedback_by_key = {}

    mock_session = AsyncMock()
    # keyword query: result.values() → []
    keyword_result = AsyncMock()
    keyword_result.values = AsyncMock(return_value=[])
    # vector query: result.data() → []
    vector_result = AsyncMock()
    vector_result.data = AsyncMock(return_value=[])
    # entity importance query (inside _calculate_relevance_scores): async iterable
    importance_result = AsyncMock()
    importance_result.__aiter__ = MagicMock(return_value=iter([]))

    call_count = 0

    async def _run_side_effect(*args: object, **kwargs: object) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return keyword_result
        if call_count == 2:
            return vector_result
        return importance_result

    mock_session.run = AsyncMock(side_effect=_run_side_effect)

    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return service


class TestHybridQueryMemory:
    @pytest.mark.asyncio
    async def test_vector_search_called_when_query_text_provided(self) -> None:
        """When query_text is provided, vector search should run."""
        service = _make_service()

        with patch(
            "personal_agent.memory.service.generate_embedding",
            new_callable=AsyncMock,
            return_value=[0.1] * 768,
        ) as mock_embed:
            query = MemoryQuery(entity_names=["Redis"], limit=10)
            await service.query_memory(query, query_text="Tell me about Redis caching")
            mock_embed.assert_called_once()

    @pytest.mark.asyncio
    async def test_keyword_only_when_no_query_text(self) -> None:
        """Without query_text, only keyword search runs (backward compatible)."""
        service = _make_service()

        with patch(
            "personal_agent.memory.service.generate_embedding",
            new_callable=AsyncMock,
        ) as mock_embed:
            query = MemoryQuery(entity_names=["Redis"], limit=10)
            await service.query_memory(query)
            mock_embed.assert_not_called()


def _make_turn(turn_id: str, key_entities: list[str], minutes_ago: int = 0) -> TurnNode:
    """Build a TurnNode for scoring tests."""
    return TurnNode(
        turn_id=turn_id,
        timestamp=datetime.utcnow() - timedelta(minutes=minutes_ago),
        user_message="test",
        key_entities=key_entities,
    )


class TestRelevanceScoring:
    """Tests for _calculate_relevance_scores with and without vector scores."""

    @pytest.fixture
    def service(self) -> MemoryService:
        svc = MemoryService.__new__(MemoryService)
        svc.connected = True
        svc.driver = None  # skip entity importance fetch
        svc._query_feedback_by_key = {}
        return svc

    @pytest.mark.asyncio
    async def test_no_vector_scores_uses_original_weights(self, service: MemoryService) -> None:
        """Without vector_scores, entity_match gets 0.4 weight (original)."""
        conv = _make_turn("t1", key_entities=["Redis"], minutes_ago=0)
        query = MemoryQuery(entity_names=["Redis"], limit=10)

        scores_no_vec = await service._calculate_relevance_scores([conv], query, vector_scores=None)
        scores_empty_vec = await service._calculate_relevance_scores(
            [conv], query, vector_scores={}
        )

        # Both None and empty dict should produce identical scores
        assert scores_no_vec["t1"] == pytest.approx(scores_empty_vec["t1"], abs=0.001)
        # Score includes entity_match (full match 1/1) — at least 0.4
        assert scores_no_vec["t1"] >= 0.39

    @pytest.mark.asyncio
    async def test_vector_scores_boost_matching_conversations(self, service: MemoryService) -> None:
        """With vector_scores, matching conversations get the vector component."""
        conv = _make_turn("t1", key_entities=["Redis"], minutes_ago=0)
        query = MemoryQuery(entity_names=["Redis"], limit=10)

        scores_no_vec = await service._calculate_relevance_scores([conv], query, vector_scores=None)
        scores_with_vec = await service._calculate_relevance_scores(
            [conv], query, vector_scores={"Redis": 0.9}
        )

        # Vector similarity should boost the score beyond keyword-only
        assert scores_with_vec["t1"] > scores_no_vec["t1"]
        # The boost should be approximately 0.9 * 0.25 = 0.225
        boost = scores_with_vec["t1"] - scores_no_vec["t1"]
        assert boost == pytest.approx(0.125, abs=0.05)

    @pytest.mark.asyncio
    async def test_no_vector_overlap_uses_nonhybrid_weights(self, service: MemoryService) -> None:
        """Conversations with no vector-matching entities get non-hybrid weights."""
        conv = _make_turn("t1", key_entities=["PostgreSQL"], minutes_ago=0)
        query = MemoryQuery(entity_names=["PostgreSQL"], limit=10)

        scores_no_vec = await service._calculate_relevance_scores([conv], query, vector_scores=None)
        # Vector scores exist but for a different entity — should NOT deflate
        scores_unrelated_vec = await service._calculate_relevance_scores(
            [conv], query, vector_scores={"Redis": 0.95}
        )

        # No overlap: falls back to non-hybrid weights — identical to no vector
        assert scores_unrelated_vec["t1"] == pytest.approx(scores_no_vec["t1"], abs=0.001)

    @pytest.mark.asyncio
    async def test_mixed_overlap_scores_correctly(self, service: MemoryService) -> None:
        """Some conversations match vector, some don't — scored independently."""
        # Use well-separated timestamps for deterministic recency
        conv_match = _make_turn("t1", key_entities=["Redis"], minutes_ago=5)
        conv_no_match = _make_turn("t2", key_entities=["PostgreSQL"], minutes_ago=60)
        query = MemoryQuery(entity_names=["Redis", "PostgreSQL"], limit=10)

        scores = await service._calculate_relevance_scores(
            [conv_match, conv_no_match], query, vector_scores={"Redis": 0.9}
        )

        # t1 (recent, vector match) should outscore t2 (old, no vector match)
        assert scores["t1"] > scores["t2"]
        # t2 should NOT be deflated — non-hybrid weights apply
        assert scores["t2"] > 0.1

    @pytest.mark.asyncio
    async def test_reranker_scores_boost_relevance(self, service: MemoryService) -> None:
        """With reranker_scores, matching conversations get the reranker component."""
        conv = _make_turn("t1", key_entities=["Redis"], minutes_ago=0)
        query = MemoryQuery(entity_names=["Redis"], limit=10)

        scores_no_rerank = await service._calculate_relevance_scores(
            [conv], query, vector_scores={"Redis": 0.9}, reranker_scores=None
        )
        scores_with_rerank = await service._calculate_relevance_scores(
            [conv], query, vector_scores={"Redis": 0.9}, reranker_scores={"t1": 0.95}
        )

        # Reranker should boost the score
        assert scores_with_rerank["t1"] > scores_no_rerank["t1"]

    @pytest.mark.asyncio
    async def test_reranker_without_vector_still_works(self, service: MemoryService) -> None:
        """Reranker scores should work even without vector scores."""
        conv = _make_turn("t1", key_entities=["Redis"], minutes_ago=0)
        query = MemoryQuery(entity_names=["Redis"], limit=10)

        scores_base = await service._calculate_relevance_scores(
            [conv], query, vector_scores=None, reranker_scores=None
        )
        scores_rerank_only = await service._calculate_relevance_scores(
            [conv], query, vector_scores=None, reranker_scores={"t1": 0.8}
        )

        # Reranker alone should boost score when vector is absent
        assert scores_rerank_only["t1"] > scores_base["t1"]

    @pytest.mark.asyncio
    async def test_empty_reranker_scores_uses_hybrid_weights(self, service: MemoryService) -> None:
        """Empty reranker_scores dict should behave like None (no reranker)."""
        conv = _make_turn("t1", key_entities=["Redis"], minutes_ago=0)
        query = MemoryQuery(entity_names=["Redis"], limit=10)

        scores_none = await service._calculate_relevance_scores(
            [conv], query, vector_scores={"Redis": 0.9}, reranker_scores=None
        )
        scores_empty = await service._calculate_relevance_scores(
            [conv], query, vector_scores={"Redis": 0.9}, reranker_scores={}
        )

        assert scores_none["t1"] == pytest.approx(scores_empty["t1"], abs=0.001)


class TestSelectRerankCandidates:
    """FRE-672: bound the reranker input to the top-N candidates by vector score."""

    def test_selects_highest_vector_score_first(self) -> None:
        """Candidates are ordered by their max key-entity vector score, descending."""
        from personal_agent.memory.service import _select_rerank_candidates

        convs = [
            _make_turn("t0", key_entities=["A"]),  # 0.2
            _make_turn("t1", key_entities=["B"]),  # 0.9
            _make_turn("t2", key_entities=["C"]),  # 0.5
        ]
        vector_scores = {"A": 0.2, "B": 0.9, "C": 0.5}

        indices = _select_rerank_candidates(convs, vector_scores, input_cap=10)

        # Descending by score: t1 (0.9), t2 (0.5), t0 (0.2)
        assert indices == [1, 2, 0]

    def test_conversation_score_is_max_over_key_entities(self) -> None:
        """A conversation's vector score is the max across its key_entities."""
        from personal_agent.memory.service import _select_rerank_candidates

        convs = [
            _make_turn("t0", key_entities=["A", "B"]),  # max(0.1, 0.8) = 0.8
            _make_turn("t1", key_entities=["C"]),  # 0.4
        ]
        vector_scores = {"A": 0.1, "B": 0.8, "C": 0.4}

        indices = _select_rerank_candidates(convs, vector_scores, input_cap=10)

        assert indices == [0, 1]

    def test_caps_input_at_n(self) -> None:
        """With M >> N candidates, the selector returns at most N indices (AC-1)."""
        from personal_agent.memory.service import _select_rerank_candidates

        convs = [_make_turn(f"t{i}", key_entities=[f"E{i}"]) for i in range(200)]
        vector_scores = {f"E{i}": i / 200.0 for i in range(200)}

        indices = _select_rerank_candidates(convs, vector_scores, input_cap=50)

        assert len(indices) == 50
        # The 50 highest-scoring entities are E150..E199 → indices 150..199.
        assert set(indices) == set(range(150, 200))
        # Highest score first.
        assert indices[0] == 199

    def test_no_vector_hit_scores_zero_stable_order(self) -> None:
        """Candidates with no vector hit score 0.0; ties keep input order (stable)."""
        from personal_agent.memory.service import _select_rerank_candidates

        convs = [
            _make_turn("t0", key_entities=["unmatched"]),  # 0.0
            _make_turn("t1", key_entities=["matched"]),  # 0.7
            _make_turn("t2", key_entities=[]),  # 0.0
        ]
        vector_scores = {"matched": 0.7}

        indices = _select_rerank_candidates(convs, vector_scores, input_cap=10)

        # t1 first (0.7), then the two zero-score turns in input order (t0, t2).
        assert indices == [1, 0, 2]

    def test_empty_vector_scores_returns_first_n_in_order(self) -> None:
        """With no vector scores, all score 0.0 → first N in candidate order."""
        from personal_agent.memory.service import _select_rerank_candidates

        convs = [_make_turn(f"t{i}", key_entities=[f"E{i}"]) for i in range(10)]

        indices = _select_rerank_candidates(convs, {}, input_cap=3)

        assert indices == [0, 1, 2]


def _make_service_returning(nodes: list[dict[str, object]]) -> MemoryService:
    """Build a MemoryService whose candidate query returns the given Turn nodes.

    generate_embedding is expected to be patched to return a zero vector so the
    vector query is skipped and the first session.run() call is the candidate
    query (returning ``nodes`` via .values()); subsequent calls are the entity
    importance query (empty async iterable).
    """
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._query_feedback_by_key = {}

    mock_session = AsyncMock()
    candidate_result = AsyncMock()
    candidate_result.values = AsyncMock(return_value=[[node] for node in nodes])
    importance_result = AsyncMock()
    importance_result.__aiter__ = MagicMock(return_value=iter([]))

    call_count = 0

    async def _run_side_effect(*args: object, **kwargs: object) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return candidate_result
        return importance_result

    mock_session.run = AsyncMock(side_effect=_run_side_effect)
    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return service


class TestRerankerInputBounding:
    """FRE-672: query_memory passes at most reranker_input_cap docs to rerank()."""

    @pytest.mark.asyncio
    async def test_rerank_receives_at_most_input_cap_documents(self) -> None:
        """With M >> N candidates, rerank() is called with at most N documents (AC-1)."""
        from personal_agent.config import get_settings

        input_cap = get_settings().reranker_input_cap
        m = input_cap * 3
        nodes = [
            {
                "turn_id": f"t{i}",
                "timestamp": (datetime.utcnow() - timedelta(minutes=i)).isoformat(),
                "user_message": f"message {i}",
                "summary": f"summary {i}",
                "key_entities": [f"E{i}"],
                "properties": "{}",
            }
            for i in range(m)
        ]
        service = _make_service_returning(nodes)
        query = MemoryQuery(entity_names=["E0"], limit=10)

        captured: dict[str, object] = {}

        async def _fake_rerank(query: str, documents: object, top_k: int | None = None) -> list:
            captured["documents"] = documents
            return []

        with (
            patch(
                "personal_agent.memory.service.generate_embedding",
                new_callable=AsyncMock,
                return_value=[0.0] * 768,
            ),
            patch("personal_agent.memory.reranker.rerank", side_effect=_fake_rerank),
        ):
            await service.query_memory(query, query_text="tell me about E0")

        assert "documents" in captured, "rerank() was not called"
        assert len(captured["documents"]) <= input_cap  # type: ignore[arg-type]
        assert len(captured["documents"]) == input_cap  # type: ignore[arg-type]
