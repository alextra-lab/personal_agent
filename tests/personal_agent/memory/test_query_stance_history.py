"""Unit tests for MemoryService.query_stance_history (ADR-0126 D5, FRE-1018).

Exercises the Cypher/ordering logic against a fake Neo4j driver — no live graph, so these
run in ``make test``. The live behavioural proof (AC-5, chain-on-pull half, against a real
graph and the real search_memory tool) lives in ``test_adr_0126_supersession_chain.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

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
    affect: str, valid_from: str, valid_to: str | None, mastery: float | None = None
) -> dict[str, Any]:
    return {
        "affect": affect,
        "mastery": mastery,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "valid_from": valid_from,
        "valid_to": valid_to,
        "invalid_at": valid_to,
    }


@pytest.mark.asyncio
async def test_not_authenticated_returns_empty_no_query() -> None:
    service, recorder = _fake_service(rows=[_stance_row("prefers", "2026-01-01", None)])
    result = await service.query_stance_history("Sorbet", authenticated=False)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_blank_target_returns_empty_no_query() -> None:
    service, recorder = _fake_service(rows=[_stance_row("prefers", "2026-01-01", None)])
    result = await service.query_stance_history("   ", authenticated=True)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_not_connected_returns_empty() -> None:
    service, recorder = _fake_service(rows=[_stance_row("prefers", "2026-01-01", None)])
    service.connected = False
    result = await service.query_stance_history("Sorbet", authenticated=True)
    assert result == []
    assert recorder == []


@pytest.mark.asyncio
async def test_returns_chain_oldest_first_with_is_current() -> None:
    rows = [
        _stance_row("prefers", "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"),
        _stance_row("prefers a sorbet-leaning texture", "2026-02-01T00:00:00+00:00", None),
    ]
    service, recorder = _fake_service(rows=rows)
    result = await service.query_stance_history("Sorbet", authenticated=True)

    assert len(result) == 2
    assert result[0]["affect"] == "prefers"
    assert result[0]["is_current"] is False
    assert result[1]["affect"] == "prefers a sorbet-leaning texture"
    assert result[1]["is_current"] is True
    assert all(r["target"] == "Sorbet" for r in result)

    query, params = recorder[0]
    assert "HAS_STANCE" in query
    assert "ORDER BY s.valid_from ASC" in query
    assert params["target"] == "Sorbet"


@pytest.mark.asyncio
async def test_no_stance_for_target_returns_empty() -> None:
    service, _ = _fake_service(rows=[])
    result = await service.query_stance_history("Unknown", authenticated=True)
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

    result = await service.query_stance_history("Sorbet", authenticated=True)
    assert result == []
