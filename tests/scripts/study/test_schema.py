"""Tests for the ADR-0114 D2 evidence-layer schema setup (FRE-839).

Unit-level: mocked Neo4j driver, no real infra (see
``tests/scripts/study/test_run_ingest_integration.py`` for the real-infra
smoke test).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from scripts.study.schema import apply_schema


class _FakeResult:
    def __aiter__(self) -> AsyncIterator[dict]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[dict]:
        return
        yield  # pragma: no cover - makes this an async generator


class _FakeSession:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self.queries.append(query)
        return _FakeResult()

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeDriver:
    def __init__(self) -> None:
        self.fake_session = _FakeSession()

    def session(self) -> _FakeSession:
        return self.fake_session


@pytest.mark.asyncio
async def test_apply_schema_issues_all_expected_constraints_and_index() -> None:
    driver = _FakeDriver()

    await apply_schema(driver)

    joined = "\n".join(driver.fake_session.queries)
    assert "FOR (c:Concept) REQUIRE c.id IS UNIQUE" in joined
    assert "FOR (s:Surface) REQUIRE s.normalized_name IS UNIQUE" in joined
    assert "FOR (cat:Category) REQUIRE cat.normalized_name IS UNIQUE" in joined
    assert "FOR (e:Episode) REQUIRE e.id IS UNIQUE" in joined
    assert "FOR (m:Mention) REQUIRE m.id IS UNIQUE" in joined
    assert "FOR (a:MembershipAssertion) REQUIRE a.id IS UNIQUE" in joined
    assert "CREATE VECTOR INDEX concept_embedding" in joined
    assert "vector.dimensions`: 1024" in joined
    assert "vector.similarity_function`: 'cosine'" in joined


@pytest.mark.asyncio
async def test_apply_schema_statements_are_idempotent() -> None:
    """Every issued statement must be safely re-runnable (``IF NOT EXISTS``) —
    ``run_ingest.py`` calls ``apply_schema`` on every invocation, not just once.
    """
    driver = _FakeDriver()

    await apply_schema(driver)

    for query in driver.fake_session.queries:
        assert "IF NOT EXISTS" in query, query


@pytest.mark.asyncio
async def test_apply_schema_issues_exactly_one_statement_per_call() -> None:
    """No batching ambiguity — one driver.session().run() per DDL statement,
    matching Neo4j's requirement that schema statements run individually.
    """
    driver = _FakeDriver()

    await apply_schema(driver)

    assert len(driver.fake_session.queries) == 7  # 6 constraints + 1 vector index
