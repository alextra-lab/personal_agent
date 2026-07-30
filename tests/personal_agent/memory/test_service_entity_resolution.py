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
    async def test_never_invents_a_name_the_graph_does_not_hold(self) -> None:
        """AC-3: the stopword guard is structural, not a list.

        The index is deliberately seeded with real candidates here. An empty index
        would prove nothing — "empty in, empty out" holds under any implementation,
        including one with containment entirely broken. Seeding it means the only way
        to return nothing is to correctly reject both candidates as unmentioned.

        The output is drawn from the graph's names, never from the message's own words,
        so ``What`` cannot be emitted no matter how the message is capitalised. The
        heuristic this replaces returned exactly ``["What"]`` for this message — see
        :meth:`test_the_removed_heuristic_would_have_emitted_the_stopword`.
        """
        service = _service([{"name": "Melon"}, {"name": "Cooking"}])
        names = await service.resolve_message_entity_names(
            "What should I cook tonight?", trace_id="t-1"
        )
        assert names == []
        assert "What" not in names

    @pytest.mark.asyncio
    async def test_emits_only_the_mentioned_subset_of_real_candidates(self) -> None:
        """The guard discriminates: a mentioned candidate survives beside a rejected one."""
        service = _service([{"name": "Melon"}, {"name": "Cooking"}])
        names = await service.resolve_message_entity_names(
            "What melon should I buy tonight?", trace_id="t-1"
        )
        assert names == ["Melon"]

    def test_the_removed_heuristic_would_have_emitted_the_stopword(self) -> None:
        """The before-state this guard replaces, pinned so the delta is legible.

        The capitalisation heuristic kept capitalised words longer than three
        characters, so a sentence-initial ``What`` reached recall as an entity name on
        32.2 % of real turns (FRE-1041 census, N=90).
        """
        message = "What should I cook tonight?"
        heuristic = [w.strip('",.:;!?') for w in message.split() if len(w) > 3 and w[0].isupper()]
        assert heuristic == ["What"]

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


class TestResolutionIsObservable:
    """FRE-1060 AC-4 — the resolver leaves a success-path record.

    It had none. During the melon-turn investigation its silence was not evidence in
    either direction, and the only way to establish whether it had run was to call it
    directly against the live graph — unavailable to anyone reading logs after the fact.
    """

    @pytest.mark.asyncio
    async def test_a_resolution_logs_the_names_and_the_trace(self, caplog) -> None:
        """The names and the trace id, so the record joins to the turn."""
        service = _service([{"name": "Melon"}, {"name": "Cantaloupe ice cream"}])

        with caplog.at_level("INFO"):
            await service.resolve_message_entity_names(
                "I would like to make a melon/canteloupe ice cream", trace_id="t-obs"
            )

        events = [r for r in caplog.records if "entity_mentions_resolved" in r.getMessage()]
        assert len(events) == 1
        message = events[0].getMessage()
        assert "Melon" in message
        assert "t-obs" in message

    @pytest.mark.asyncio
    async def test_an_empty_resolution_is_logged_too(self, caplog) -> None:
        """ "Resolved nothing" must be distinguishable from "never ran".

        Logging only non-empty results would preserve exactly the ambiguity AC-4 exists to
        remove: a reader seeing no event could not tell a resolver that found nothing from
        one that was never reached.
        """
        service = _service([{"name": "Bicycle"}])

        with caplog.at_level("INFO"):
            names = await service.resolve_message_entity_names("a melon", trace_id="t-empty")

        assert names == []
        events = [r for r in caplog.records if "entity_mentions_resolved" in r.getMessage()]
        assert len(events) == 1, "an empty resolution is still a resolution"

    @pytest.mark.asyncio
    async def test_a_failed_index_read_does_not_claim_a_resolution(self, caplog) -> None:
        """The degraded path keeps its own warning and must not also log success."""
        service = MemoryService()  # fre-375-allow: no substrate touched; driver is a fake
        service.connected = True
        service.driver = _ExplodingDriver()  # type: ignore[assignment]

        with caplog.at_level("INFO"):
            assert await service.resolve_message_entity_names("a melon", trace_id="t-fail") == []

        assert not [r for r in caplog.records if "entity_mentions_resolved" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_a_disconnected_driver_still_logs_with_a_reason(self, caplog) -> None:
        """The gap code review confirmed: the guard return logged nothing.

        The docstring promises the event is emitted unconditionally so that "resolved
        nothing" is distinguishable from "never ran" — but the blank-message and
        disconnected-driver guard returned [] before ever reaching the log. An operator
        greping a trace after a dropped Neo4j connection found silence and would conclude
        the resolver was never reached, which is the same failure the log exists to close,
        in the one case where the cause is infrastructure rather than scoring.
        """
        service = MemoryService()  # fre-375-allow: no substrate touched; driver is a fake
        service.connected = False
        service.driver = None

        with caplog.at_level("INFO"):
            assert await service.resolve_message_entity_names("a melon", trace_id="t-down") == []

        events = [r for r in caplog.records if "entity_mentions_resolved" in r.getMessage()]
        assert len(events) == 1, "an infrastructure short-circuit is still an outcome"
        assert "not_connected" in events[0].getMessage()

    @pytest.mark.asyncio
    async def test_a_blank_message_is_distinguishable_from_a_dead_driver(self, caplog) -> None:
        """Two causes, two reasons — the point of naming the guard that fired."""
        service = _service([{"name": "Melon"}])

        with caplog.at_level("INFO"):
            assert await service.resolve_message_entity_names("   ", trace_id="t-blank") == []

        events = [r for r in caplog.records if "entity_mentions_resolved" in r.getMessage()]
        assert len(events) == 1
        assert "blank_message" in events[0].getMessage()
