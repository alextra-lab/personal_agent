"""Unit tests for MemoryService.query_claims (ADR-0126 D4 — Claims pull path, FRE-1016).

Exercises the Cypher/ranking logic against a fake Neo4j driver — no live graph, so
these run in ``make test``. The live behavioural proof (AC-4, both halves, against a
real graph and the real search_memory tool/assemble_context pipeline) lives in
``test_adr_0126_claims_pull.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from personal_agent.memory.service import MemoryService

_USER = UUID("00000000-0000-0000-0000-0000000000aa")


class _FakeClaimsResult:
    """Minimal stand-in for ``neo4j.AsyncResult`` supporting ``async for``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[dict[str, Any]]:
        for row in self._rows:
            yield row


class _FakeSession:
    """Records every ``run(query, **params)`` and answers with canned rows."""

    def __init__(
        self, rows: list[dict[str, Any]], recorder: list[tuple[str, dict[str, Any]]]
    ) -> None:
        self._rows = rows
        self._recorder = recorder

    async def run(self, query: str, **params: Any) -> _FakeClaimsResult:
        self._recorder.append((query, params))
        return _FakeClaimsResult(self._rows)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def _fake_service(
    rows: list[dict[str, Any]] | None = None,
) -> tuple[MemoryService, list[tuple[str, dict[str, Any]]]]:
    service = MemoryService()  # fre-375-allow: unit test with a fake driver, no real connection
    recorder: list[tuple[str, dict[str, Any]]] = []
    service.driver = type(
        "FakeDriver", (), {"session": staticmethod(lambda: _FakeSession(rows or [], recorder))}
    )()
    service.connected = True
    return service, recorder


def _claim_row(
    claim_id: str, content: str, embedding: list[float], confidence: float = 0.8
) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "content": content,
        "confidence": confidence,
        "knowledge_class": "Personal",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "embedding": embedding,
    }


@pytest.mark.asyncio
async def test_no_user_id_returns_empty_no_query() -> None:
    service, recorder = _fake_service(rows=[_claim_row("c1", "x", [1.0, 0.0])])
    result = await service.query_claims("anything", user_id=None, authenticated=True)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_not_authenticated_returns_empty_no_query() -> None:
    service, recorder = _fake_service(rows=[_claim_row("c1", "x", [1.0, 0.0])])
    result = await service.query_claims("anything", user_id=_USER, authenticated=False)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_blank_query_text_returns_empty_no_query() -> None:
    service, recorder = _fake_service(rows=[_claim_row("c1", "x", [1.0, 0.0])])
    result = await service.query_claims("   ", user_id=_USER, authenticated=True)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_not_connected_returns_empty() -> None:
    service, recorder = _fake_service(rows=[_claim_row("c1", "x", [1.0, 0.0])])
    service.connected = False
    result = await service.query_claims("anything", user_id=_USER, authenticated=True)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_zero_vector_embedder_outage_returns_empty_no_query() -> None:
    """A degraded embedder must not dump an arbitrary slice of the user's claims."""
    service, recorder = _fake_service(rows=[_claim_row("c1", "x", [1.0, 0.0])])
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(return_value=[0.0, 0.0]),
    ):
        result = await service.query_claims("anything", user_id=_USER, authenticated=True)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_ranks_by_descending_cosine_similarity() -> None:
    rows = [
        _claim_row("low", "irrelevant fact", [0.0, 1.0]),
        _claim_row("high", "the lease ends in june", [1.0, 0.0]),
    ]
    service, recorder = _fake_service(rows=rows)
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(return_value=[1.0, 0.0]),
    ):
        result = await service.query_claims("lease", user_id=_USER, authenticated=True)

    assert [c["claim_id"] for c in result] == ["high", "low"]
    query, params = recorder[0]
    assert "cl.valid_to IS NULL AND cl.invalid_at IS NULL" in query
    assert "HAS_FACT" in query
    assert params["user_id"] == str(_USER)


@pytest.mark.asyncio
async def test_rows_missing_embedding_or_claim_id_are_skipped() -> None:
    rows = [
        {**_claim_row("ok", "kept", [1.0, 0.0]), "embedding": None},
        {**_claim_row("ok2", "kept too", [1.0, 0.0]), "claim_id": None},
        _claim_row("good", "the real one", [1.0, 0.0]),
    ]
    service, _ = _fake_service(rows=rows)
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(return_value=[1.0, 0.0]),
    ):
        result = await service.query_claims("q", user_id=_USER, authenticated=True)
    assert [c["claim_id"] for c in result] == ["good"]


@pytest.mark.asyncio
async def test_respects_limit() -> None:
    rows = [_claim_row(f"c{i}", f"fact {i}", [1.0, 0.0]) for i in range(5)]
    service, _ = _fake_service(rows=rows)
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(return_value=[1.0, 0.0]),
    ):
        result = await service.query_claims("q", user_id=_USER, authenticated=True, limit=2)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_result_shape_is_claim_specific() -> None:
    """Distinguishability half of AC-4(b): keys are claim-specific, not entity/turn shaped."""
    service, _ = _fake_service(rows=[_claim_row("c1", "the lease ends in june", [1.0, 0.0])])
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(return_value=[1.0, 0.0]),
    ):
        result = await service.query_claims("lease", user_id=_USER, authenticated=True)

    assert len(result) == 1
    claim = result[0]
    assert set(claim.keys()) == {
        "claim_id",
        "content",
        "confidence",
        "knowledge_class",
        "observed_at",
    }
    assert claim["content"] == "the lease ends in june"


@pytest.mark.asyncio
async def test_db_error_is_caught_and_returns_empty() -> None:
    service = MemoryService()  # fre-375-allow: unit test with a fake driver, no real connection
    service.connected = True

    class _RaisingSession:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

        async def __aenter__(self) -> _RaisingSession:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    service.driver = type("FakeDriver", (), {"session": staticmethod(lambda: _RaisingSession())})()

    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(return_value=[1.0, 0.0]),
    ):
        result = await service.query_claims("q", user_id=uuid4(), authenticated=True)
    assert result == []
