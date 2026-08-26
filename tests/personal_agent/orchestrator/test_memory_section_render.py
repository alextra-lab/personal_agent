"""Task-assist recall renders every kind, and is bounded by stated constants (FRE-1010).

Three defects motivated this, all in one render site (``executor.step_llm_call``):

* a score-blind positional cap of 3 discarded candidates the upstream gates had
  already admitted — observed live on trace 94b70cd9, where two described
  ice-cream entities scoring 0.562/0.560 were cut against an admitted 0.563;
* the task-assist branch read ``summary``/``user_message``, keys an **entity**
  payload does not carry, so an entity rendered as a bare numbered bullet;
* the branch was chosen by the *first* item's type, so a mixed set was rendered
  wholesale by whichever renderer its top item happened to select.

The acceptance criteria are asserted at the executor seam rather than against the
pure renderer: ADR-0125 defines admission as the **final serialized model input**,
so a pure-function test could pass while the block never reached the wire.

Note on where the section lands: it is **not** in the system prompt. ADR-0081 §D2
inlines it as the volatile tail of the *current user turn*
(``_inline_volatile_into_last_user_message``), which is why it sits after every
cache breakpoint and cannot erode the cached prefix. ``test_section_rides_the_user_turn_not_the_system_prompt``
pins that so the question does not have to be re-investigated.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.captains_log.turn_evidence import DropReason


def _make_ctx(**overrides: object) -> object:
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    kwargs: dict[str, object] = {
        "session_id": "test-session",
        "trace_id": "test-trace",
        "user_message": "how do I make melon ice cream?",
        "mode": Mode.NORMAL,
        "channel": Channel.CHAT,
        "messages": [{"role": "user", "content": "how do I make melon ice cream?"}],
    }
    kwargs.update(overrides)
    return ExecutionContext(**kwargs)  # type: ignore[arg-type]


def _mock_llm() -> MagicMock:
    client = MagicMock()
    client.respond = AsyncMock(
        return_value={
            "content": "Use less water.",
            "tool_calls": [],
            "response_id": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    client.model_configs = {}
    return client


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals():
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


async def _run(ctx: object, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Drive the real ``step_llm_call`` and return the mocked client."""
    from personal_agent.config import settings
    from personal_agent.telemetry.trace import TraceContext

    monkeypatch.setattr(settings, "prefer_primitives_enabled", False)

    client = _mock_llm()
    session = MagicMock()
    session.add_message = AsyncMock()
    session.get_messages = AsyncMock(return_value=[])

    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=client),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
    ):
        from personal_agent.orchestrator.executor import step_llm_call

        await step_llm_call(ctx, session, TraceContext.new_trace())  # type: ignore[arg-type]
    return client


def _dispatched_user_text(client: MagicMock) -> str:
    """The text of the last user message actually dispatched to the provider."""
    messages = client.respond.call_args.kwargs["messages"]
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            return content if isinstance(content, str) else str(content)
    return ""


def _episode(ident: str, summary: str) -> dict[str, Any]:
    return {
        "type": "episode",
        "conversation_id": ident,
        "user_message": f"earlier question {ident}",
        "summary": summary,
        "key_entities": [],
    }


def _entity(name: str, description: str = "a described thing", **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "entity",
        "name": name,
        "entity_type": "CONCEPT",
        "description": description,
    }
    item.update(extra)
    return item


def _stance(target: str, affect: str) -> dict[str, Any]:
    return {"type": "stance", "target": target, "affect": affect}


def _behavioural_stance(target: str, affect: str) -> dict[str, Any]:
    return {"type": "behavioural_stance", "target": target, "affect": affect}


# ---------------------------------------------------------------------------
# Acceptance criteria — asserted at the executor seam
# ---------------------------------------------------------------------------


class TestAcceptanceAtTheSeam:
    """The ticket's HOW IT IS PROVEN, against the real dispatched input."""

    @pytest.mark.asyncio
    async def test_described_entity_beyond_position_three_reaches_the_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-1: the melon case. A described entity at index 4 of a mixed set must
        reach the model — under the cap of 3 it was recalled and then discarded.
        """
        from personal_agent.captains_log.turn_evidence import build_recall_candidates

        melon = "a frozen dessert whose texture needs adjustment with watery fruit like melon"
        memory = [
            _episode("t1", "we talked about desserts"),
            _episode("t2", "we talked about fruit"),
            _episode("t3", "we talked about freezing"),
            _entity("Ice cream", melon),
        ]
        ctx = _make_ctx(
            memory_context=memory, recall_candidates=build_recall_candidates(memory, {})
        )
        client = await _run(ctx, monkeypatch)

        assert melon in _dispatched_user_text(client)

    @pytest.mark.asyncio
    async def test_no_item_is_dropped_for_position_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-2: every item the upstream gates admitted renders and is recorded."""
        from personal_agent.captains_log.turn_evidence import build_recall_candidates

        memory = [_episode(f"t{i}", f"summary number {i}") for i in range(5)]
        ctx = _make_ctx(
            memory_context=memory, recall_candidates=build_recall_candidates(memory, {})
        )
        client = await _run(ctx, monkeypatch)

        text = _dispatched_user_text(client)
        for i in range(5):
            assert f"summary number {i}" in text
        evidence = ctx.turn_evidence  # type: ignore[attr-defined]
        assert {i.identity for i in evidence.recall.items if i.admitted} == {
            f"t{i}" for i in range(5)
        }

    @pytest.mark.asyncio
    async def test_admitted_entity_rendered_non_empty_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-3: an entity must never render as a bare numbered bullet."""
        from personal_agent.captains_log.turn_evidence import build_recall_candidates

        memory = [_episode("t1", "we talked about desserts"), _entity("Sorbet", "an icy dessert")]
        ctx = _make_ctx(
            memory_context=memory, recall_candidates=build_recall_candidates(memory, {})
        )
        client = await _run(ctx, monkeypatch)

        text = _dispatched_user_text(client)
        assert "Sorbet" in text
        assert "an icy dessert" in text
        # The empty-bullet signature: a numbered marker with nothing after it.
        assert "1. \n" not in text
        assert "2. \n" not in text

    @pytest.mark.asyncio
    async def test_record_admits_exactly_the_items_that_contributed_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-4: a blank-description entity contributes no content, so it is not admitted."""
        from personal_agent.captains_log.turn_evidence import build_recall_candidates

        memory = [
            _episode("t1", "we talked about desserts"),
            _entity("Sorbet", "an icy dessert"),
            _entity("Ghost", ""),
        ]
        ctx = _make_ctx(
            memory_context=memory, recall_candidates=build_recall_candidates(memory, {})
        )
        await _run(ctx, monkeypatch)

        evidence = ctx.turn_evidence  # type: ignore[attr-defined]
        assert {i.identity for i in evidence.recall.items if i.admitted} == {"t1", "Sorbet"}
        ghost = next(i for i in evidence.recall.items if i.identity == "Ghost")
        assert ghost.admitted is False
        assert ghost.drop_reason is DropReason.NOT_RENDERED

    @pytest.mark.asyncio
    async def test_section_rides_the_user_turn_not_the_system_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0081 §D2: the section is the volatile tail of the current user turn.

        Pinned because the opposite assumption — that it lands in the system prompt —
        implies a prompt-cache erosion risk that does not exist: every cache
        breakpoint precedes this content.
        """
        from personal_agent.captains_log.turn_evidence import build_recall_candidates

        marker = "zzz-distinctive-recalled-text-zzz"
        memory = [_entity("Sorbet", marker)]
        ctx = _make_ctx(
            memory_context=memory, recall_candidates=build_recall_candidates(memory, {})
        )
        client = await _run(ctx, monkeypatch)

        assert marker in _dispatched_user_text(client)
        assert marker not in (client.respond.call_args.kwargs.get("system_prompt") or "")

    @pytest.mark.asyncio
    async def test_entity_citation_identifier_resolves_against_the_turn_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRE-1296: an admitted entity's identifier must reach the model AND resolve.

        FRE-1280 minted identifiers that nothing ever rendered — a citation the model
        cannot see is a citation it can never copy. This proves the marker embedded in
        the dispatched text is the same identifier the turn's registry resolves.
        """
        import re

        from personal_agent.captains_log.turn_evidence import build_recall_candidates
        from personal_agent.grounding.source_registry import SourceRegistry

        memory = [_entity("Sorbet", "an icy dessert")]
        ctx = _make_ctx(
            memory_context=memory, recall_candidates=build_recall_candidates(memory, {})
        )
        ctx.source_registry = SourceRegistry(turn_id=ctx.trace_id)  # type: ignore[attr-defined]
        client = await _run(ctx, monkeypatch)

        text = _dispatched_user_text(client)
        match = re.search(r"\[(S\d+@[0-9a-f]+)\]", text)
        assert match is not None, text
        assert ctx.source_registry.resolve(match.group(1)) is not None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Unit — the renderer itself
# ---------------------------------------------------------------------------


class TestRendererDispatchesPerItemKind:
    def _render(self, items: list[dict[str, Any]]) -> tuple[str, tuple[str, ...]]:
        from personal_agent.orchestrator.executor import _render_memory_section_with_ids

        return _render_memory_section_with_ids(items)

    def test_mixed_set_renders_both_kinds(self) -> None:
        text, ids = self._render([_episode("t1", "a past chat"), _entity("Sorbet", "icy")])
        assert "a past chat" in text
        assert "Sorbet" in text and "icy" in text
        assert set(ids) == {"t1", "Sorbet"}

    def test_entity_only_set_still_renders_when_first_item_is_an_episode(self) -> None:
        """The old branch selection read item[0]'s type; a mixed set broke by construction."""
        text, ids = self._render([_episode("t1", "a past chat"), _entity("Sorbet", "icy")])
        assert "icy" in text
        assert "Sorbet" in ids

    def test_memory_graph_header_is_preserved(self) -> None:
        """sub_agent.py scans for this exact string (_MEMORY_CONTEXT_MARKER, FRE-505)."""
        from personal_agent.orchestrator.sub_agent import _MEMORY_CONTEXT_MARKER

        text, _ = self._render([_entity("Sorbet", "icy")])
        assert _MEMORY_CONTEXT_MARKER in text

    def test_blank_description_entity_contributes_no_line_and_no_id(self) -> None:
        text, ids = self._render([_entity("Sorbet", "icy"), _entity("Ghost", "")])
        assert "Ghost" not in text
        assert ids == ("Sorbet",)

    def test_empty_input_renders_nothing(self) -> None:
        assert self._render([]) == ("", ())

    def test_mention_count_key_is_read(self) -> None:
        text, _ = self._render([_entity("Sorbet", "icy", mention_count=328)])
        assert "328" in text

    def test_legacy_mentions_key_is_read(self) -> None:
        """_format_broad_recall writes 'mentions'; the other three producers write
        'mention_count'. Reading only one fabricated '(mentioned 1x)' for the rest.
        """
        text, _ = self._render([_entity("Sorbet", "icy", mentions=42)])
        assert "42" in text

    def test_absent_count_is_not_fabricated_as_one(self) -> None:
        text, _ = self._render([_entity("Sorbet", "icy")])
        assert "mentioned" not in text.lower()

    def test_zero_count_is_rendered_not_dropped(self) -> None:
        text, _ = self._render([_entity("Sorbet", "icy", mention_count=0)])
        assert "0" in text

    def test_stance_with_affect_renders(self) -> None:
        """ADR-0126 T1 (FRE-1015): a current, non-empty stance reaches the section."""
        text, ids = self._render([_stance("Python", "prefers over Java")])
        assert "Python" in text
        assert "prefers over Java" in text
        assert ids == ("stance:Python",)

    def test_stance_with_empty_affect_contributes_no_line_and_no_id(self) -> None:
        """D6: an empty-affect stance is filtered before render, never rendered blank."""
        text, ids = self._render([_stance("BarrageRepublicain", "")])
        assert "BarrageRepublicain" not in text
        assert ids == ()

    def test_stance_with_whitespace_only_affect_is_filtered(self) -> None:
        text, ids = self._render([_stance("BarrageRepublicain", "   ")])
        assert "BarrageRepublicain" not in text
        assert ids == ()

    def test_entity_and_its_own_stance_both_render_with_distinct_ids(self) -> None:
        """The BLOCKER this namespacing fixes: an entity and a same-target stance must
        not consume each other's rendered-id slot -- both render, each with its own
        correctly kind-namespaced identity.
        """
        text, ids = self._render(
            [_entity("Python", "a programming language"), _stance("Python", "prefers over Java")]
        )
        assert "a programming language" in text
        assert "prefers over Java" in text
        assert set(ids) == {"Python", "stance:Python"}

    def test_stance_section_header_present_when_populated(self) -> None:
        text, _ = self._render([_stance("Python", "prefers over Java")])
        assert "## What The User Thinks About Related Topics" in text

    def test_stance_section_absent_when_only_item_is_filtered(self) -> None:
        """AC-6: an empty-affect stance produces no stance section at all -- not an
        empty header, not an orphaned label.
        """
        text, _ = self._render([_stance("BarrageRepublicain", "")])
        assert "What The User Thinks" not in text

    def test_behavioural_stance_with_affect_renders(self) -> None:
        """ADR-0126 T2 (FRE-1017): a curated behavioural stance reaches the section."""
        text, ids = self._render(
            [_behavioural_stance("Artifact", "prefers explicit request before creation")]
        )
        assert "Artifact" in text
        assert "prefers explicit request before creation" in text
        assert ids == ("behavioural_stance:Artifact",)

    def test_behavioural_stance_with_empty_affect_contributes_no_line_and_no_id(self) -> None:
        """D6: an empty-affect behavioural stance is filtered before render."""
        text, ids = self._render([_behavioural_stance("Artifact", "")])
        assert "Artifact" not in text
        assert ids == ()

    def test_behavioural_stance_with_whitespace_only_affect_is_filtered(self) -> None:
        text, ids = self._render([_behavioural_stance("Artifact", "   ")])
        assert "Artifact" not in text
        assert ids == ()

    def test_behavioural_stance_and_topic_scoped_stance_sharing_target_both_render(self) -> None:
        """A curated target that is also topic-scoped-recalled the same turn -- both
        sections render, each with its own distinct namespaced id (T1's Design
        decision 2 accepts this as legitimate duplication, not a defect).
        """
        text, ids = self._render(
            [
                _stance("Artifact", "topic-scoped affect text"),
                _behavioural_stance("Artifact", "prefers explicit request before creation"),
            ]
        )
        assert "topic-scoped affect text" in text
        assert "prefers explicit request before creation" in text
        assert set(ids) == {"stance:Artifact", "behavioural_stance:Artifact"}

    def test_behavioural_stance_section_header_present_when_populated(self) -> None:
        text, _ = self._render(
            [_behavioural_stance("Artifact", "prefers explicit request before creation")]
        )
        assert "## Standing Behavioural Preferences" in text

    def test_behavioural_stance_section_absent_when_only_item_is_filtered(self) -> None:
        text, _ = self._render([_behavioural_stance("Artifact", "")])
        assert "Standing Behavioural Preferences" not in text


class TestRendererEmbedsCitationIdentifiers:
    """FRE-1296: a registered source's identifier rides alongside its rendered line.

    FRE-1280 shipped ``SourceRegistry.register_memory_item`` but nothing consumed the
    returned identifier except a telemetry log line — the model had no marker to copy.
    A ``registry`` argument is opt-in (default ``None``) so every pre-existing caller
    and test above is unaffected.
    """

    def _render(
        self, items: list[dict[str, Any]], registry: Any = None
    ) -> tuple[str, tuple[str, ...]]:
        from personal_agent.orchestrator.executor import _render_memory_section_with_ids

        return _render_memory_section_with_ids(items, registry)

    def test_entity_line_carries_its_registered_identifier(self) -> None:
        from personal_agent.grounding.source_registry import SourceRegistry

        registry = SourceRegistry(turn_id="trace-cite-entity")
        text, ids = self._render([_entity("Sorbet", "icy")], registry)

        assert ids == ("Sorbet",)
        (source,) = registry.sources()
        assert f"[{source.identifier}]" in text
        assert registry.resolve(source.identifier) is source

    def test_episode_line_carries_its_registered_identifier(self) -> None:
        from personal_agent.grounding.source_registry import SourceRegistry

        registry = SourceRegistry(turn_id="trace-cite-episode")
        text, ids = self._render([_episode("t1", "we talked about desserts")], registry)

        assert ids == ("t1",)
        (source,) = registry.sources()
        assert f"[{source.identifier}]" in text

    def test_stance_line_carries_its_registered_identifier(self) -> None:
        from personal_agent.grounding.source_registry import SourceRegistry

        registry = SourceRegistry(turn_id="trace-cite-stance")
        text, ids = self._render([_stance("Python", "prefers over Java")], registry)

        assert ids == ("stance:Python",)
        (source,) = registry.sources()
        assert f"[{source.identifier}]" in text
        # Found in review: the registered source must actually carry the affect text,
        # or the citation resolves to a source that can never pass D3(c) containment.
        assert "prefers over Java" in source.content

    def test_behavioural_stance_line_carries_its_registered_identifier(self) -> None:
        from personal_agent.grounding.source_registry import SourceRegistry

        registry = SourceRegistry(turn_id="trace-cite-behavioural")
        text, ids = self._render(
            [_behavioural_stance("Artifact", "prefers explicit request before creation")],
            registry,
        )

        assert ids == ("behavioural_stance:Artifact",)
        (source,) = registry.sources()
        assert f"[{source.identifier}]" in text
        assert "prefers explicit request before creation" in source.content

    def test_mixed_set_gives_each_item_its_own_distinct_identifier(self) -> None:
        from personal_agent.grounding.source_registry import SourceRegistry

        registry = SourceRegistry(turn_id="trace-cite-mixed")
        text, _ids = self._render(
            [_entity("Sorbet", "icy"), _episode("t1", "we talked about desserts")], registry
        )

        sources = registry.sources()
        assert len(sources) == 2
        assert len({s.identifier for s in sources}) == 2
        for source in sources:
            assert f"[{source.identifier}]" in text

    def test_no_registry_renders_no_identifier_backward_compatible(self) -> None:
        """The default (no registry) path must render byte-identical to before."""
        text, _ids = self._render([_entity("Sorbet", "icy")])
        assert "@" not in text

    def test_blank_description_entity_is_never_registered(self) -> None:
        """An item contributing no line must not mint an unused identifier either."""
        from personal_agent.grounding.source_registry import SourceRegistry

        registry = SourceRegistry(turn_id="trace-cite-blank")
        self._render([_entity("Ghost", "")], registry)

        assert registry.sources() == ()


class TestRendererIsBoundedByStatedConstants:
    """The volatile tail is outside the prompt cache, so its size is a per-turn cost."""

    def _render(self, items: list[dict[str, Any]]) -> tuple[str, tuple[str, ...]]:
        from personal_agent.orchestrator.executor import _render_memory_section_with_ids

        return _render_memory_section_with_ids(items)

    def test_oversized_summary_is_marked_not_silently_clipped(self) -> None:
        from personal_agent.orchestrator.executor import _MAX_ITEM_CHARS

        text, _ = self._render([_episode("t1", "x" * (_MAX_ITEM_CHARS + 500))])
        assert "...[truncated 500 chars]" in text

    def test_upstream_800_char_value_is_not_double_marked(self) -> None:
        """Gateway entity-match already writes mark_truncated(..., 800); the render
        bound sits above 800 + marker so it never truncates an already-marked value.
        """
        from personal_agent.captains_log.turn_evidence import mark_truncated

        upstream = mark_truncated("y" * 2000, 800)
        text, _ = self._render([_episode("t1", upstream)])
        assert text.count("...[truncated") == 1

    def test_episode_cardinality_is_bounded(self) -> None:
        from personal_agent.orchestrator.executor import _MAX_RENDERED_EPISODES

        items = [_episode(f"t{i}", f"summary {i}") for i in range(_MAX_RENDERED_EPISODES + 3)]
        _, ids = self._render(items)
        assert len(ids) == _MAX_RENDERED_EPISODES

    def test_entity_rank_cap_is_unchanged(self) -> None:
        """Pre-existing bound, deliberately retained (FRE-374 D1) — explicit non-goal."""
        from personal_agent.orchestrator.executor import _MAX_RENDERED_ENTITIES

        items = [_entity(f"E{i}") for i in range(_MAX_RENDERED_ENTITIES + 1)]
        _, ids = self._render(items)
        assert len(ids) == _MAX_RENDERED_ENTITIES

    def test_blank_items_do_not_consume_bound_slots(self) -> None:
        """The bound caps rendered content, not candidates considered.

        Cap-then-filter would let blank leading items burn the budget and exclude a
        later item that does have content — the same "recalled then discarded" shape
        this ticket fixes.
        """
        from personal_agent.orchestrator.executor import _MAX_RENDERED_EPISODES

        # Genuinely contentless: summary AND user_message both empty, else the
        # fallback correctly renders the user message and these are not blank at all.
        items = [
            {"type": "episode", "conversation_id": f"blank{i}", "summary": "", "user_message": ""}
            for i in range(_MAX_RENDERED_EPISODES)
        ]
        items.append(_episode("real", "genuine recalled content"))
        text, ids = self._render(items)

        assert "genuine recalled content" in text
        assert "real" in ids

    def test_whitespace_only_summary_falls_back_to_user_message(self) -> None:
        """``" "`` is truthy — a naive ``or`` chain would suppress a good user_message."""
        item = _episode("t1", " ")
        text, ids = self._render([item])

        assert item["user_message"] in text
        assert ids == ("t1",)

    def test_stance_rank_cap_equals_entity_cap(self) -> None:
        """ADR-0126 T1: deliberately the *same* constant as _MAX_RENDERED_ENTITIES, not
        an independent value -- a stance is only ever fetched for an entity the recall
        path already selected, so its rendered prefix must never exceed what the entity
        prefix already bounds (an independent cap could select a misaligned subset,
        which would make this an unstated second relevance decision).
        """
        from personal_agent.orchestrator.executor import (
            _MAX_RENDERED_ENTITIES,
            _MAX_RENDERED_STANCES,
        )

        assert _MAX_RENDERED_STANCES == _MAX_RENDERED_ENTITIES

        items = [_stance(f"S{i}", f"affect {i}") for i in range(_MAX_RENDERED_ENTITIES + 3)]
        _, ids = self._render(items)
        assert len(ids) == _MAX_RENDERED_ENTITIES

    def test_stance_cap_is_an_order_preserving_prefix(self) -> None:
        """The cap takes the first N in input order -- not a re-sort -- so it reflects
        the same relevance order recall already established upstream.
        """
        from personal_agent.orchestrator.executor import _MAX_RENDERED_ENTITIES

        items = [_stance(f"S{i}", f"affect {i}") for i in range(_MAX_RENDERED_ENTITIES + 3)]
        _, ids = self._render(items)
        assert ids == tuple(f"stance:S{i}" for i in range(_MAX_RENDERED_ENTITIES))

    def test_behavioural_stance_rank_cap_is_fixed_by_ac7_not_the_entity_cap(self) -> None:
        """ADR-0126 T2 (AC-7): the behavioural cap is a direct restatement of AC-7's own
        ceiling (12) -- unlike T1's stance cap, it is NOT derived from
        _MAX_RENDERED_ENTITIES, because this layer has no recall selection to ride on.
        """
        from personal_agent.orchestrator.executor import _MAX_RENDERED_BEHAVIOURAL_STANCES

        assert _MAX_RENDERED_BEHAVIOURAL_STANCES == 12

        items = [_behavioural_stance(f"B{i}", f"affect {i}") for i in range(15)]
        _, ids = self._render(items)
        assert len(ids) == 12

    def test_behavioural_stance_cap_is_an_order_preserving_prefix(self) -> None:
        items = [_behavioural_stance(f"B{i}", f"affect {i}") for i in range(15)]
        _, ids = self._render(items)
        assert ids == tuple(f"behavioural_stance:B{i}" for i in range(12))
