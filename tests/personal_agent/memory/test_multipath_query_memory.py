"""Unit tests for MemoryService._multipath_query_memory / _resolve_fused_turns (FRE-1021).

FRE-1021 root cause: on the multipath entity-name path, every fused item -- entity-kind or
turn-kind alike -- was resolved into TurnNodes, so ``MemoryQueryResult.entities`` was always
empty regardless of fusion rank. These tests exercise the resolution step against a fake Neo4j
driver (no live graph, so they run in ``make test``); the arm/fusion mechanism itself is covered
by ``test_multipath_core.py`` and is mocked out here via ``_multipath_fused_recall``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from personal_agent.events import AccessContext
from personal_agent.memory.fusion import FusedResult, MultiPathRecallResult
from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.service import MemoryService


class _FakeValuesResult:
    """Minimal stand-in for ``neo4j.AsyncResult`` supporting ``await result.values()``."""

    def __init__(self, rows: list[list[Any]]) -> None:
        self._rows = rows

    async def values(self) -> list[list[Any]]:
        return self._rows


class _FakeSession:
    """Routes ``run(query, **params)`` to canned rows by matching on the Cypher's node alias.

    Records every call so tests can assert on the exact Cypher/params sent (the FRE-229
    visibility-chokepoint regression guard).
    """

    def __init__(
        self,
        turn_rows: list[list[Any]],
        entity_rows: list[list[Any]],
        recorder: list[tuple[str, dict[str, Any]]],
    ) -> None:
        self._turn_rows = turn_rows
        self._entity_rows = entity_rows
        self._recorder = recorder

    async def run(self, query: str, **params: Any) -> _FakeValuesResult:
        self._recorder.append((query, params))
        if "MATCH (t:Turn" in query:
            return _FakeValuesResult(self._turn_rows)
        if "MATCH (e:Entity" in query:
            return _FakeValuesResult(self._entity_rows)
        raise AssertionError(f"unexpected query shape: {query}")

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def _fake_service(
    turn_rows: list[list[Any]] | None = None,
    entity_rows: list[list[Any]] | None = None,
) -> tuple[MemoryService, list[tuple[str, dict[str, Any]]]]:
    service = MemoryService()  # fre-375-allow: unit test with a fake driver, no real connection
    recorder: list[tuple[str, dict[str, Any]]] = []
    service.driver = type(
        "FakeDriver",
        (),
        {
            "session": staticmethod(
                lambda: _FakeSession(turn_rows or [], entity_rows or [], recorder)
            )
        },
    )()
    service.connected = True
    return service, recorder


def _turn_row(turn_id: str, minutes_ago: int = 0) -> dict[str, Any]:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "turn_id": turn_id,
        "trace_id": turn_id,
        "session_id": "s1",
        "sequence_number": 1,
        "timestamp": ts.isoformat(),
        "summary": f"summary of {turn_id}",
        "user_message": f"message {turn_id}",
        "assistant_response": "ok",
        "key_entities": [],
        "properties": "{}",
    }


def _entity_row(name: str, days_ago_last_seen: int = 0) -> dict[str, Any]:
    last_seen = datetime.now(timezone.utc) - timedelta(days=days_ago_last_seen)
    return {
        "name": name,
        "entity_type": "Concept",
        "description": f"{name} description",
        "mention_count": 3,
        "first_seen": last_seen.isoformat(),
        "last_seen": last_seen.isoformat(),
        "properties": "{}",
    }


def _query(limit: int = 5, hard_recency_days: int | None = None) -> MemoryQuery:
    return MemoryQuery(limit=limit, hard_recency_days=hard_recency_days)


async def _run(
    service: MemoryService,
    query: MemoryQuery,
    fused_items: list[FusedResult],
):
    service._multipath_fused_recall = AsyncMock(  # type: ignore[method-assign]
        return_value=MultiPathRecallResult(
            items=fused_items,
            arms_executed=["dense"],
            arms_failed=[],
            per_arm_counts={"dense": len(fused_items)},
            fused_set_size=len(fused_items),
            path="entity",
        )
    )
    return await service._multipath_query_memory(
        query,
        "python vs java",
        access_context=AccessContext.CONTEXT_ASSEMBLY,
        trace_id="t1",
        session_id="s1",
        user_id=None,
        authenticated=False,
    )


class TestEntityResolution:
    """Red-on-current-code: the core FRE-1021 regression proof."""

    @pytest.mark.asyncio
    async def test_entity_kind_item_resolves_to_entities_not_turns(self) -> None:
        service, _ = _fake_service(
            turn_rows=[[_turn_row("turn-1")]],
            entity_rows=[["eid-python", _entity_row("Python")]],
        )
        result = await _run(
            service,
            _query(limit=5),
            [
                FusedResult(item_id="eid-python", score=0.9, arm_count=1, kind="entity"),
                FusedResult(item_id="turn-1", score=0.8, arm_count=1, kind="turn"),
            ],
        )
        assert len(result.entities) == 1
        assert result.entities[0].name == "Python"
        assert result.entities[0].entity_type == "Concept"
        assert result.entities[0].description == "Python description"
        assert len(result.conversations) == 1
        assert result.conversations[0].turn_id == "turn-1"
        assert list(result.relevance_scores.keys()) == ["turn-1"]

    @pytest.mark.asyncio
    async def test_combined_limit_shared_across_kinds(self) -> None:
        service, _ = _fake_service(
            turn_rows=[[_turn_row("t1")], [_turn_row("t2")], [_turn_row("t3")]],
            entity_rows=[
                ["e1", _entity_row("E1")],
                ["e2", _entity_row("E2")],
                ["e3", _entity_row("E3")],
            ],
        )
        fused = [
            FusedResult(item_id="e1", score=0.95, arm_count=1, kind="entity"),
            FusedResult(item_id="t1", score=0.90, arm_count=1, kind="turn"),
            FusedResult(item_id="e2", score=0.85, arm_count=1, kind="entity"),
            FusedResult(item_id="t2", score=0.80, arm_count=1, kind="turn"),
            FusedResult(item_id="e3", score=0.75, arm_count=1, kind="entity"),
            FusedResult(item_id="t3", score=0.70, arm_count=1, kind="turn"),
        ]
        result = await _run(service, _query(limit=4), fused)
        assert [e.name for e in result.entities] == ["E1", "E2"]
        assert [c.turn_id for c in result.conversations] == ["t1", "t2"]

    @pytest.mark.asyncio
    async def test_freshness_access_ids_include_direct_entities(self, monkeypatch) -> None:
        from personal_agent.memory import service as service_module

        published: list[Any] = []

        class _FakeBus:
            async def publish(self, stream: str, event: Any) -> None:
                published.append(event)

        monkeypatch.setattr(service_module.settings, "freshness_enabled", True, raising=False)
        monkeypatch.setattr(service_module, "get_event_bus", lambda: _FakeBus())

        service, _ = _fake_service(entity_rows=[["e1", _entity_row("Sorbet")]])
        await _run(
            service,
            _query(limit=5),
            [FusedResult(item_id="e1", score=0.9, arm_count=1, kind="entity")],
        )
        assert len(published) == 1
        assert "Sorbet" in published[0].entity_ids

    @pytest.mark.asyncio
    async def test_telemetry_result_count_includes_entities(self, caplog) -> None:
        import structlog

        service, _ = _fake_service(entity_rows=[["e1", _entity_row("Sorbet")]])
        with structlog.testing.capture_logs() as captured:
            await _run(
                service,
                _query(limit=5),
                [FusedResult(item_id="e1", score=0.9, arm_count=1, kind="entity")],
            )
        completed = [e for e in captured if e.get("event") == "memory_query_completed"]
        assert completed, "expected a memory_query_completed log event"
        assert completed[0]["result_count"] == 1


class TestVisibilityAndBoundaries:
    """Regression / boundary coverage -- required, not claimed as the red-first proof."""

    @pytest.mark.asyncio
    async def test_entity_resolution_still_visibility_scoped(self) -> None:
        service, recorder = _fake_service(entity_rows=[["e1", _entity_row("Python")]])
        await _run(
            service,
            _query(limit=5),
            [FusedResult(item_id="e1", score=0.9, arm_count=1, kind="entity")],
        )
        entity_calls = [q for q, _p in recorder if "MATCH (e:Entity" in q]
        assert entity_calls, "expected an entity resolution query"
        query_text, params = next((q, p) for q, p in recorder if "MATCH (e:Entity" in q)
        assert "e.visibility" in query_text or "vis_authenticated" in query_text
        assert "vis_authenticated" in params
        assert "vis_user_id" in params

    @pytest.mark.asyncio
    async def test_no_entity_ids_skips_entity_query(self) -> None:
        service, recorder = _fake_service(turn_rows=[[_turn_row("t1")]])
        await _run(
            service,
            _query(limit=5),
            [FusedResult(item_id="t1", score=0.9, arm_count=1, kind="turn")],
        )
        assert not any("MATCH (e:Entity" in q for q, _p in recorder)

    @pytest.mark.asyncio
    async def test_no_turn_ids_skips_turn_query(self) -> None:
        service, recorder = _fake_service(entity_rows=[["e1", _entity_row("Python")]])
        await _run(
            service,
            _query(limit=5),
            [FusedResult(item_id="e1", score=0.9, arm_count=1, kind="entity")],
        )
        assert not any("MATCH (t:Turn" in q for q, _p in recorder)

    @pytest.mark.asyncio
    async def test_duplicate_and_unresolved_items_do_not_consume_budget(self) -> None:
        service, _ = _fake_service(
            turn_rows=[[_turn_row("t1")]],
            entity_rows=[["e1", _entity_row("E1")]],
            # "e-missing" and "e1" (dup) resolve via the same canned row set; the fake
            # driver returns only e1's row, so "e-missing" resolves to nothing.
        )
        fused = [
            FusedResult(item_id="e1", score=0.95, arm_count=1, kind="entity"),
            FusedResult(item_id="e1", score=0.94, arm_count=1, kind="entity"),  # duplicate
            FusedResult(item_id="e-missing", score=0.93, arm_count=1, kind="entity"),
            FusedResult(item_id="t1", score=0.80, arm_count=1, kind="turn"),
        ]
        result = await _run(service, _query(limit=5), fused)
        assert len(result.entities) == 1
        assert result.entities[0].name == "E1"
        assert len(result.conversations) == 1

    @pytest.mark.asyncio
    async def test_hard_recency_filters_entities(self) -> None:
        service, _ = _fake_service(
            entity_rows=[
                ["e-fresh", _entity_row("Fresh", days_ago_last_seen=1)],
                ["e-stale", _entity_row("Stale", days_ago_last_seen=90)],
            ],
        )
        fused = [
            FusedResult(item_id="e-stale", score=0.95, arm_count=1, kind="entity"),
            FusedResult(item_id="e-fresh", score=0.90, arm_count=1, kind="entity"),
        ]
        result = await _run(service, _query(limit=5, hard_recency_days=30), fused)
        assert [e.name for e in result.entities] == ["Fresh"]

    @pytest.mark.asyncio
    async def test_limit_one_boundary(self) -> None:
        service, _ = _fake_service(
            turn_rows=[[_turn_row("t1")]],
            entity_rows=[["e1", _entity_row("E1")]],
        )
        fused = [
            FusedResult(item_id="e1", score=0.95, arm_count=1, kind="entity"),
            FusedResult(item_id="t1", score=0.90, arm_count=1, kind="turn"),
        ]
        result = await _run(service, _query(limit=1), fused)
        assert len(result.entities) + len(result.conversations) == 1
        assert len(result.entities) == 1
