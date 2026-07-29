"""Unit tests for graph-anchored entity-mention resolution (FRE-1041).

Substrate-free: a fake async Neo4j session feeds canned full-text rows, so these run
under ``make test`` (no :7688 needed).

The resolver replaces the capitalisation heuristic that gated entity recall
(``request_gateway/context.py:_capitalized_entity_hints``). It retrieves entity hits
from the existing ``turn_entity_fulltext`` index and keeps only those the message
literally mentions, so it can surface lowercase subjects and cannot invent a
sentence-initial stopword as an entity name.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from personal_agent.memory.service import MemoryService


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeSession:
    """Records the query and parameters it was run with, then returns canned rows."""

    def __init__(self, rows: list[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._calls = calls

    async def run(self, query: str, **params: Any) -> _FakeResult:
        self._calls.append({"query": query, "params": params})
        return _FakeResult(self._rows)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeDriver:
    def __init__(self, rows: list[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._calls = calls

    def session(self) -> _FakeSession:
        return _FakeSession(self._rows, self._calls)


class _ExplodingSession:
    async def run(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("index unavailable")

    async def __aenter__(self) -> _ExplodingSession:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _ExplodingDriver:
    def session(self) -> _ExplodingSession:
        return _ExplodingSession()


def _service(
    rows: list[dict[str, Any]], calls: list[dict[str, Any]] | None = None
) -> MemoryService:
    service = MemoryService()  # fre-375-allow: no substrate touched; driver is a fake
    service.connected = True
    service.driver = _FakeDriver(rows, calls if calls is not None else [])  # type: ignore[assignment]
    return service


class TestResolveMessageEntityNames:
    """Retrieve from the full-text index, then keep only literal mentions."""

    @pytest.mark.asyncio
    async def test_keeps_literal_mentions_and_drops_fulltext_fuzz(self) -> None:
        """The index over-returns; only names the message says survive."""
        service = _service(
            [
                {"name": "Ice cream"},
                {"name": "Ice cream maker"},
                {"name": "Melon"},
                {"name": "Yogurt ice cream"},
            ]
        )
        names = await service.resolve_message_entity_names(
            "I would like to make a melon/canteloupe ice cream", trace_id="t-1"
        )
        assert names == ["Ice cream", "Melon"]

    @pytest.mark.asyncio
    async def test_resolves_the_decisive_lowercase_case(self) -> None:
        """FRE-1041's decisive turn: the heuristic returned nothing here."""
        service = _service([{"name": "Melon"}, {"name": "Cantaloupe ice cream"}])
        names = await service.resolve_message_entity_names(
            "I would like to make a melon/canteloupe ice cream", trace_id="t-1"
        )
        assert names == ["Melon"]

    @pytest.mark.asyncio
    async def test_cannot_emit_a_sentence_initial_stopword(self) -> None:
        """The guard is structural: only names the graph holds can be returned."""
        service = _service([])
        names = await service.resolve_message_entity_names(
            "What should I cook tonight?", trace_id="t-1"
        )
        assert names == []

    @pytest.mark.asyncio
    async def test_preserves_graph_casing_for_case_sensitive_overlap(self) -> None:
        """``_overlap_subscore`` intersects case-sensitively; casing must survive."""
        service = _service([{"name": "Melon"}])
        assert await service.resolve_message_entity_names("a melon", trace_id="t-1") == ["Melon"]

    @pytest.mark.asyncio
    async def test_applies_visibility_scoping(self) -> None:
        """Every graph read is visibility-scoped (FRE-229)."""
        calls: list[dict[str, Any]] = []
        service = _service([{"name": "Melon"}], calls)
        user_id: UUID = uuid4()
        await service.resolve_message_entity_names(
            "a melon", trace_id="t-1", user_id=user_id, authenticated=True
        )
        assert calls[0]["params"]["vis_authenticated"] is True
        assert calls[0]["params"]["vis_user_id"] == str(user_id)
        assert "node.visibility" in calls[0]["query"]

    @pytest.mark.asyncio
    async def test_escapes_lucene_special_characters(self) -> None:
        """A raw slash would be a Lucene parse error, not a match."""
        calls: list[dict[str, Any]] = []
        service = _service([{"name": "Melon"}], calls)
        await service.resolve_message_entity_names("a melon/canteloupe", trace_id="t-1")
        assert "\\/" in calls[0]["params"]["query_text"]

    @pytest.mark.asyncio
    async def test_returns_empty_when_disconnected(self) -> None:
        """No driver means no hints, not an exception."""
        service = MemoryService()  # fre-375-allow: no substrate touched
        service.connected = False
        service.driver = None
        assert await service.resolve_message_entity_names("a melon", trace_id="t-1") == []

    @pytest.mark.asyncio
    async def test_returns_empty_for_blank_message(self) -> None:
        """A blank message never reaches the index."""
        calls: list[dict[str, Any]] = []
        service = _service([{"name": "Melon"}], calls)
        assert await service.resolve_message_entity_names("   ", trace_id="t-1") == []
        assert calls == []

    @pytest.mark.asyncio
    async def test_fails_to_empty_on_index_error(self) -> None:
        """A graph or index failure degrades to no hints rather than failing the turn."""
        service = MemoryService()  # fre-375-allow: no substrate touched; driver is a fake
        service.connected = True
        service.driver = _ExplodingDriver()  # type: ignore[assignment]
        assert await service.resolve_message_entity_names("a melon", trace_id="t-1") == []

    @pytest.mark.asyncio
    async def test_caps_the_returned_names(self) -> None:
        """The hint set is bounded, as the heuristic's ten-name cap was."""
        service = _service([{"name": f"Entity{n}"} for n in range(40)])
        message = " ".join(f"entity{n}" for n in range(40))
        assert len(await service.resolve_message_entity_names(message, trace_id="t-1")) <= 10
