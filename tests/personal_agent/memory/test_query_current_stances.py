"""Unit tests for MemoryService.query_current_stances (ADR-0126 D1/D2/D5/D6, FRE-1015).

Exercises the Cypher/batching logic against a fake Neo4j driver -- no live graph, so these
run in ``make test``. The live behavioural proof (AC-1/AC-5/AC-6, against a real graph and
the real render/wire pipeline) lives in ``test_adr_0126_topic_scoped_stance_push.py``.

A dedicated live-Neo4j class at the bottom of this file proves ``valid_to``/``invalid_at``
are each independently enforced by the query -- a fake driver can't prove that (it just
returns canned rows), and ``assert_stance`` always sets both fields together on
supersession, so no fixture built through it can isolate either predicate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio

from personal_agent.memory.service import MemoryService


class _FakeStanceResult:
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

    async def run(self, query: str, **params: Any) -> _FakeStanceResult:
        self._recorder.append((query, params))
        return _FakeStanceResult(self._rows)

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


def _stance_row(
    target: str, affect: str, mastery: float | None = None, asserted_by: str | None = None
) -> dict[str, Any]:
    """``asserted_by`` defaults to None -- what a real RETURN gives back for a legacy edge
    with no property (FRE-1299), so tests that don't care about authorship exercise the
    same absence-denies path exactly.
    """
    return {"target": target, "affect": affect, "mastery": mastery, "asserted_by": asserted_by}


@pytest.mark.asyncio
async def test_not_authenticated_returns_empty_no_query() -> None:
    service, recorder = _fake_service(rows=[_stance_row("Python", "prefers over Java")])
    result = await service.query_current_stances(["Python"], authenticated=False)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_empty_targets_returns_empty_no_query() -> None:
    service, recorder = _fake_service(rows=[_stance_row("Python", "prefers over Java")])
    result = await service.query_current_stances([], authenticated=True)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_not_connected_returns_empty() -> None:
    service, recorder = _fake_service(rows=[_stance_row("Python", "prefers over Java")])
    service.connected = False
    result = await service.query_current_stances(["Python"], authenticated=True)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_returns_current_stance_for_each_target() -> None:
    rows = [
        _stance_row("Python", "prefers over Java"),
        _stance_row("Sorbet", "prefers a sorbet-leaning texture"),
    ]
    service, recorder = _fake_service(rows=rows)
    result = await service.query_current_stances(["Python", "Sorbet"], authenticated=True)

    assert len(result) == 2
    by_target = {r["target"]: r for r in result}
    assert by_target["Python"]["affect"] == "prefers over Java"
    assert by_target["Sorbet"]["affect"] == "prefers a sorbet-leaning texture"

    query, params = recorder[0]
    assert params["targets"] == ["Python", "Sorbet"]


@pytest.mark.asyncio
async def test_returns_and_canonicalizes_asserted_by() -> None:
    """FRE-1299: the read path selects asserted_by and canonicalizes by exact match --
    "user" stays "user", "agent" stays "agent", and a legacy row with no property at all
    (None) denies the same as any off-vocabulary value, never passing through unclassified.
    """
    rows = [
        _stance_row("Python", "prefers over Java", asserted_by="user"),
        _stance_row("staging deploy", "wary of it", asserted_by="agent"),
        _stance_row("legacy topic", "old stance"),  # asserted_by defaults to None
    ]
    service, recorder = _fake_service(rows=rows)

    result = await service.query_current_stances(
        ["Python", "staging deploy", "legacy topic"], authenticated=True
    )

    query, _ = recorder[0]
    assert "s.asserted_by AS asserted_by" in query
    by_target = {r["target"]: r for r in result}
    assert by_target["Python"]["asserted_by"] == "user"
    assert by_target["staging deploy"]["asserted_by"] == "agent"
    assert by_target["legacy topic"]["asserted_by"] == "agent"


@pytest.mark.asyncio
async def test_batches_all_targets_in_one_call() -> None:
    """A batched UNWIND query, not N round trips -- one call regardless of target count."""
    service, recorder = _fake_service(rows=[])
    await service.query_current_stances(["A", "B", "C"], authenticated=True)
    assert len(recorder) == 1
    assert recorder[0][1]["targets"] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_query_requires_both_current_predicates() -> None:
    """Cypher-text assertion: a fake driver can't prove filtering behaviourally (see the
    live test for that), but it can prove the query asks for both predicates.
    """
    service, recorder = _fake_service(rows=[])
    await service.query_current_stances(["Python"], authenticated=True)
    query, _ = recorder[0]
    assert "valid_to IS NULL" in query
    assert "invalid_at IS NULL" in query


@pytest.mark.asyncio
async def test_empty_affect_row_is_still_returned() -> None:
    """D6: filtering happens at render, not fetch -- an empty-affect row (e.g. the ADR's
    Barrage républicain case) must reach the caller so the renderer's filter is provably
    doing the work, not the query silently doing it first.
    """
    service, _ = _fake_service(rows=[_stance_row("BarrageRepublicain", "")])
    result = await service.query_current_stances(["BarrageRepublicain"], authenticated=True)
    assert len(result) == 1
    assert result[0]["affect"] == ""


@pytest.mark.asyncio
async def test_no_stance_for_target_is_simply_absent() -> None:
    service, _ = _fake_service(rows=[])
    result = await service.query_current_stances(["Unknown"], authenticated=True)
    assert result == []


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

    result = await service.query_current_stances(["Python"], authenticated=True)
    assert result == []


# ---------------------------------------------------------------------------
# Live -- proves valid_to and invalid_at are each independently enforced.
# ---------------------------------------------------------------------------
#
# assert_stance() (memory/service.py) always sets both fields together on supersession
# (never one without the other), so a fixture built through the real write path cannot
# isolate either predicate. These rows are written by direct Cypher instead, specifically
# to construct the two cases assert_stance can never produce: valid_to set alone, and
# invalid_at set alone. If the query's WHERE clause degenerated to checking only one of
# the two predicates, one of these two cases would silently leak through as "current."


class TestCurrentOnlyPredicatesAreIndependentlyEnforced:
    pytestmark = pytest.mark.integration

    _OWNER_UID = UUID("00000000-0000-0000-0000-00000000ad26")

    @pytest_asyncio.fixture
    async def owner_service(self) -> AsyncIterator[MemoryService]:
        service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
        if not await service.connect():
            pytest.skip("Neo4j not available (make test-infra-up)")

        assert service.driver is not None
        async with service.driver.session() as s:
            await s.run("MATCH (e:Entity) WHERE e.name STARTS WITH 'FRE1015_' DETACH DELETE e")
            result = await s.run("MATCH (p:Person {is_owner: true}) RETURN count(p) AS n")
            record = await result.single()
            if record is None or record["n"] == 0:
                await s.run(
                    "CREATE (:Person {user_id: $user_id, is_owner: true,"
                    " name: 'FRE1015 Test Owner'})",
                    user_id=str(self._OWNER_UID),
                )

        yield service

        async with service.driver.session() as s:
            await s.run(
                "MATCH (:Person {is_owner: true})-[r:HAS_STANCE]->"
                "(e:Entity) WHERE e.name STARTS WITH 'FRE1015_' DELETE r"
            )
            await s.run("MATCH (e:Entity) WHERE e.name STARTS WITH 'FRE1015_' DETACH DELETE e")
        await service.disconnect()

    @pytest.mark.asyncio
    async def test_valid_to_set_alone_is_excluded(self, owner_service: MemoryService) -> None:
        assert owner_service.driver is not None
        async with owner_service.driver.session() as s:
            await s.run("CREATE (:Entity {name: 'FRE1015_ValidToOnly', class: 'World'})")
            await s.run(
                "MATCH (o:Person {is_owner: true}), (e:Entity {name: 'FRE1015_ValidToOnly'})\n"
                "CREATE (o)-[:HAS_STANCE {affect: 'stale', valid_from: '2026-01-01T00:00:00Z',"
                " valid_to: '2026-02-01T00:00:00Z', invalid_at: null}]->(e)"
            )

        result = await owner_service.query_current_stances(
            ["FRE1015_ValidToOnly"], authenticated=True
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_invalid_at_set_alone_is_excluded(self, owner_service: MemoryService) -> None:
        assert owner_service.driver is not None
        async with owner_service.driver.session() as s:
            await s.run("CREATE (:Entity {name: 'FRE1015_InvalidAtOnly', class: 'World'})")
            await s.run(
                "MATCH (o:Person {is_owner: true}), (e:Entity {name: 'FRE1015_InvalidAtOnly'})\n"
                "CREATE (o)-[:HAS_STANCE {affect: 'retracted', valid_from: '2026-01-01T00:00:00Z',"
                " valid_to: null, invalid_at: '2026-02-01T00:00:00Z'}]->(e)"
            )

        result = await owner_service.query_current_stances(
            ["FRE1015_InvalidAtOnly"], authenticated=True
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_neither_set_is_included(self, owner_service: MemoryService) -> None:
        assert owner_service.driver is not None
        async with owner_service.driver.session() as s:
            await s.run("CREATE (:Entity {name: 'FRE1015_BothNull', class: 'World'})")
            await s.run(
                "MATCH (o:Person {is_owner: true}), (e:Entity {name: 'FRE1015_BothNull'})\n"
                "CREATE (o)-[:HAS_STANCE {affect: 'current', valid_from: '2026-01-01T00:00:00Z',"
                " valid_to: null, invalid_at: null}]->(e)"
            )

        result = await owner_service.query_current_stances(["FRE1015_BothNull"], authenticated=True)
        assert len(result) == 1
        assert result[0]["affect"] == "current"
