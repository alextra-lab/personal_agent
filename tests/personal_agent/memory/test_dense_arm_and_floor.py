"""Unit tests for the dense recall arm, its noise-guard floor, and lexical kind.

Substrate-free: a fake async Neo4j session feeds canned rows, so these run under
``make test`` (no :7688 needed). See FRE-724 (multi-path seam) — the noise-guard
floor is applied inside the dense ANN (ADR-0103 per-arm guard), and the lexical
arm stamps ``kind`` so the fused set is resolvable downstream.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from personal_agent.config.settings import get_settings
from personal_agent.memory.service import MemoryService


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeSession:
    """Minimal async session: returns the same canned rows for any query."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def run(self, *args: Any, **kwargs: Any) -> _FakeResult:
        return _FakeResult(self._rows)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeDriver:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def session(self) -> _FakeSession:
        return _FakeSession(self._rows)


def _service_with_rows(rows: list[dict[str, Any]]) -> MemoryService:
    service = MemoryService()  # fre-375-allow: no substrate touched; driver is a fake
    service.connected = True
    service.driver = _FakeDriver(rows)  # type: ignore[assignment]
    return service


class TestDenseFloor:
    """The noise-guard floor drops below-floor entities before ranking (AC-1/§5)."""

    @pytest.mark.asyncio
    async def test_floor_zero_keeps_all(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "recall_similarity_floor", 0.0, raising=False)
        service = _service_with_rows(
            [{"item_id": "e1", "score": 0.8}, {"item_id": "e2", "score": 0.3}]
        )
        session = _FakeSession([{"item_id": "e1", "score": 0.8}, {"item_id": "e2", "score": 0.3}])
        ranked = await service._dense_vector_search_ranked(session, [1.0, 0.0], 10, "true", {})
        assert [r.item_id for r in ranked] == ["e1", "e2"]
        assert all(r.kind == "entity" for r in ranked)

    @pytest.mark.asyncio
    async def test_floor_drops_below_and_reranks_1_based(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "recall_similarity_floor", 0.6, raising=False)
        session = _FakeSession([{"item_id": "e1", "score": 0.8}, {"item_id": "e2", "score": 0.3}])
        service = _service_with_rows([])
        ranked = await service._dense_vector_search_ranked(session, [1.0, 0.0], 10, "true", {})
        assert [r.item_id for r in ranked] == ["e1"]
        assert ranked[0].rank == 1

    @pytest.mark.asyncio
    async def test_zero_embedding_short_circuits(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "recall_similarity_floor", 0.0, raising=False)
        session = _FakeSession([{"item_id": "e1", "score": 0.8}])
        service = _service_with_rows([])
        ranked = await service._dense_vector_search_ranked(session, [0.0, 0.0], 10, "true", {})
        assert ranked == []


class TestDenseRecallArm:
    """The public dense arm: embed → ANN → RankedResult (entity kind)."""

    @pytest.mark.asyncio
    async def test_returns_ranked_entities(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "recall_similarity_floor", 0.0, raising=False)
        service = _service_with_rows(
            [{"item_id": "e1", "score": 0.9}, {"item_id": "e2", "score": 0.7}]
        )
        with patch(
            "personal_agent.memory.service.generate_embedding",
            return_value=[1.0, 0.0, 0.0],
        ):
            ranked = await service.dense_recall_arm("vision")
        assert [r.item_id for r in ranked] == ["e1", "e2"]
        assert [r.rank for r in ranked] == [1, 2]
        assert all(r.kind == "entity" for r in ranked)

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self) -> None:
        service = _service_with_rows([])
        assert await service.dense_recall_arm("   ") == []

    @pytest.mark.asyncio
    async def test_disconnected_returns_empty(self) -> None:
        service = MemoryService()  # fre-375-allow: no substrate touched
        service.connected = False
        service.driver = None
        assert await service.dense_recall_arm("vision") == []
