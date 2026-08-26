"""Unit tests for MemoryService.query_claims_history (ADR-0126 D5, FRE-1018).

Exercises the Cypher/chain-walk logic against a fake Neo4j driver — no live graph, so these
run in ``make test``. Codex plan-review (2026-07-28) caught two real gaps in the original
design, both regression-guarded here:

- Ranking only CURRENT claims by similarity misses a chain when the query best-matches a
  SUPERSEDED ancestor whose current descendant has drifted semantically
  (``test_query_matching_superseded_ancestor_still_returns_full_chain``).
- ``assert_claim`` can supersede MULTIPLE current claims in one write (facet-aware matching),
  all stamped with the SAME new ``superseded_by`` — a single-valued predecessor map would
  silently drop all but one ancestor on key collision
  (``test_fan_in_multiple_predecessors_all_returned``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from personal_agent.memory.service import MemoryService

_USER = UUID("00000000-0000-0000-0000-0000000000bb")
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


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
    claim_id: str,
    content: str,
    embedding: list[float],
    *,
    observed_at: datetime = _T0,
    valid_to: str | None = None,
    invalid_at: str | None = None,
    superseded_by: str | None = None,
    supersession_reason: str | None = None,
    confidence: float = 0.8,
    asserted_by: str | None = "agent",
) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "content": content,
        "confidence": confidence,
        "observed_at": observed_at.isoformat(),
        "valid_to": valid_to,
        "invalid_at": invalid_at,
        "superseded_by": superseded_by,
        "supersession_reason": supersession_reason,
        "embedding": embedding,
        "asserted_by": asserted_by,
    }


def _mock_embed(vector: list[float]) -> AsyncMock:
    return AsyncMock(return_value=vector)


@pytest.mark.asyncio
async def test_no_user_id_returns_empty_no_query() -> None:
    service, recorder = _fake_service(rows=[_claim_row("c1", "x", [1.0, 0.0])])
    result = await service.query_claims_history("anything", user_id=None, authenticated=True)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_not_authenticated_returns_empty_no_query() -> None:
    service, recorder = _fake_service(rows=[_claim_row("c1", "x", [1.0, 0.0])])
    result = await service.query_claims_history("anything", user_id=_USER, authenticated=False)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_blank_query_returns_empty_no_query() -> None:
    service, recorder = _fake_service(rows=[_claim_row("c1", "x", [1.0, 0.0])])
    result = await service.query_claims_history("   ", user_id=_USER, authenticated=True)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_zero_vector_embedder_outage_returns_empty_no_query() -> None:
    service, recorder = _fake_service(rows=[_claim_row("c1", "x", [1.0, 0.0])])
    with patch("personal_agent.memory.service.generate_embedding", new=_mock_embed([0.0, 0.0])):
        result = await service.query_claims_history("anything", user_id=_USER, authenticated=True)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_no_candidates_at_all_returns_empty() -> None:
    service, _ = _fake_service(rows=[])
    with patch("personal_agent.memory.service.generate_embedding", new=_mock_embed([1.0, 0.0])):
        result = await service.query_claims_history("q", user_id=_USER, authenticated=True)
    assert result == []


@pytest.mark.asyncio
async def test_three_claim_chain_returned_oldest_first_when_query_matches_current() -> None:
    """A superseded by B superseded by C (C current); query matches C best."""
    rows = [
        _claim_row(
            "A",
            "lease ends in january",
            [1.0, 0.0],
            observed_at=_T0,
            valid_to="t1",
            invalid_at="t1",
            superseded_by="B",
            supersession_reason="correction",
        ),
        _claim_row(
            "B",
            "lease ends in march",
            [1.0, 0.0],
            observed_at=_T0 + timedelta(days=30),
            valid_to="t2",
            invalid_at="t2",
            superseded_by="C",
            supersession_reason="evolution",
        ),
        _claim_row(
            "C",
            "lease ends in june",
            [1.0, 0.0],
            observed_at=_T0 + timedelta(days=90),
        ),
    ]
    service, _ = _fake_service(rows=rows)
    with patch("personal_agent.memory.service.generate_embedding", new=_mock_embed([1.0, 0.0])):
        result = await service.query_claims_history("lease", user_id=_USER, authenticated=True)

    assert [c["claim_id"] for c in result] == ["A", "B", "C"]
    assert result[0]["superseded_by"] == "B"
    assert result[1]["superseded_by"] == "C"
    assert result[2]["superseded_by"] is None
    assert [c["is_current"] for c in result] == [False, False, True]
    assert result[0]["supersession_reason"] == "correction"


@pytest.mark.asyncio
async def test_query_matching_superseded_ancestor_still_returns_full_chain() -> None:
    """Codex finding #1 regression: query best-matches the OLDEST (superseded) claim.

    Ranking only current claims would miss this chain entirely; the fix walks forward
    from the best match to the chain's current head before collecting ancestors.
    """
    rows = [
        _claim_row(
            "A",
            "very specific old fact",
            [1.0, 0.0],  # best match to the query below
            valid_to="t1",
            invalid_at="t1",
            superseded_by="B",
        ),
        _claim_row(
            "B",
            "a drifted current fact",
            [0.0, 1.0],  # poor match to the query
            observed_at=_T0 + timedelta(days=30),
        ),
    ]
    service, _ = _fake_service(rows=rows)
    # Query embedding matches A (the superseded row) far better than B (current).
    with patch("personal_agent.memory.service.generate_embedding", new=_mock_embed([1.0, 0.0])):
        result = await service.query_claims_history("old fact", user_id=_USER, authenticated=True)

    assert [c["claim_id"] for c in result] == ["A", "B"]
    assert result[-1]["is_current"] is True


@pytest.mark.asyncio
async def test_fan_in_multiple_predecessors_all_returned() -> None:
    """Codex finding #2 regression: two claims superseded by the SAME new claim.

    A single-valued predecessor map ({new_id: old_id}) would silently drop one of X/Y on
    key collision. The fan-in-aware (list-valued) walk must return both.
    """
    rows = [
        _claim_row("X", "fact one", [1.0, 0.0], valid_to="t1", invalid_at="t1", superseded_by="Z"),
        _claim_row("Y", "fact two", [1.0, 0.0], valid_to="t1", invalid_at="t1", superseded_by="Z"),
        _claim_row("Z", "merged fact", [1.0, 0.0], observed_at=_T0 + timedelta(days=1)),
    ]
    service, _ = _fake_service(rows=rows)
    with patch("personal_agent.memory.service.generate_embedding", new=_mock_embed([1.0, 0.0])):
        result = await service.query_claims_history("fact", user_id=_USER, authenticated=True)

    ids = {c["claim_id"] for c in result}
    assert ids == {"X", "Y", "Z"}
    current = [c for c in result if c["is_current"]]
    assert [c["claim_id"] for c in current] == ["Z"]


@pytest.mark.asyncio
async def test_rows_missing_embedding_or_claim_id_are_skipped_as_candidates() -> None:
    rows = [
        {**_claim_row("bad", "x", [1.0, 0.0]), "embedding": None},
        {**_claim_row("bad2", "x", [1.0, 0.0]), "claim_id": None},
        _claim_row("good", "the real one", [1.0, 0.0]),
    ]
    service, _ = _fake_service(rows=rows)
    with patch("personal_agent.memory.service.generate_embedding", new=_mock_embed([1.0, 0.0])):
        result = await service.query_claims_history("q", user_id=_USER, authenticated=True)
    assert [c["claim_id"] for c in result] == ["good"]


@pytest.mark.asyncio
async def test_dangling_superseded_by_pointer_does_not_raise() -> None:
    """A predecessor pointing to a claim_id absent from the fetched set is defensive-skipped."""
    rows = [
        _claim_row(
            "A", "fact", [1.0, 0.0], valid_to="t1", invalid_at="t1", superseded_by="missing"
        ),
    ]
    service, _ = _fake_service(rows=rows)
    with patch("personal_agent.memory.service.generate_embedding", new=_mock_embed([1.0, 0.0])):
        result = await service.query_claims_history("fact", user_id=_USER, authenticated=True)
    # "A" is the best (only) match; forward-walk to "missing" would KeyError if not guarded —
    # confirm it doesn't raise, and returns at least the matched row itself.
    assert any(c["claim_id"] == "A" for c in result)


@pytest.mark.asyncio
async def test_asserted_by_canonicalizes_to_user_or_agent() -> None:
    """FRE-1302: exact-match canonicalization, mirroring query_claims/query_current_stances."""
    rows = [
        _claim_row(
            "A",
            "user fact",
            [1.0, 0.0],
            valid_to="t1",
            invalid_at="t1",
            superseded_by="B",
            asserted_by="user",
        ),
        _claim_row(
            "B",
            "agent fact",
            [1.0, 0.0],
            observed_at=_T0 + timedelta(days=30),
            asserted_by=None,  # pre-FRE-1302 row: no asserted_by property at all
        ),
    ]
    service, recorder = _fake_service(rows=rows)
    with patch("personal_agent.memory.service.generate_embedding", new=_mock_embed([1.0, 0.0])):
        result = await service.query_claims_history("fact", user_id=_USER, authenticated=True)

    by_id = {c["claim_id"]: c["asserted_by"] for c in result}
    assert by_id == {"A": "user", "B": "agent"}
    query, _ = recorder[0]
    assert "cl.asserted_by AS asserted_by" in query


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

    with patch("personal_agent.memory.service.generate_embedding", new=_mock_embed([1.0, 0.0])):
        result = await service.query_claims_history("q", user_id=_USER, authenticated=True)
    assert result == []
