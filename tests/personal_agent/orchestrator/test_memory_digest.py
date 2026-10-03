"""Tests for FRE-1471: the planner memory digest (ADR-0154 D5, carrying ADR-0147 D1/D4).

One class per acceptance criterion. The digest is built from the renderer's own selection
and text, so most checks compare it against the renderer's own output for the same input.
"""

from __future__ import annotations

import itertools
import re
from typing import Any

import pytest

from personal_agent.captains_log.turn_evidence import MemoryItemKind
from personal_agent.orchestrator import executor
from personal_agent.orchestrator.executor import (
    _DEICTIC_DISAMBIGUATION,
    _build_planner_memory_digest,
    _render_memory_section_with_ids,
    _SelectedItem,
)
from personal_agent.orchestrator.expansion_types import PlannerMemoryDigest
from personal_agent.request_gateway.budget import estimate_tokens

_ITEM_LINE_RE = re.compile(r"(- |\d+\. )")


def _entity(name: str, description: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "type": "entity",
        "name": name,
        "entity_type": "Concept",
        "description": description,
        **extra,
    }


def _episode(conversation_id: str, summary: str | None, **extra: Any) -> dict[str, Any]:
    return {"type": "episode", "conversation_id": conversation_id, "summary": summary, **extra}


def _stance(target: str, affect: str | None) -> dict[str, Any]:
    return {"type": "stance", "target": target, "affect": affect}


def _behavioural(target: str, affect: str | None) -> dict[str, Any]:
    return {"type": "behavioural_stance", "target": target, "affect": affect}


def _rendered_item_lines(items: list[dict[str, Any]]) -> list[str]:
    """The renderer's own per-item lines, in the order it emits them."""
    section = _render_memory_section_with_ids(items)[0]
    return [line for line in section.split("\n") if _ITEM_LINE_RE.match(line)]


def _digest_lines(digest: PlannerMemoryDigest) -> list[str]:
    return digest.text.split("\n") if digest.text else []


def _assert_each_line_prefixes_a_rendered_line(
    digest: PlannerMemoryDigest, items: list[dict[str, Any]]
) -> None:
    rendered = _rendered_item_lines(items)
    for line in _digest_lines(digest):
        assert any(r.startswith(line) for r in rendered), line


def _interleave(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mix the kinds, so that relevance order differs from the renderer's section order."""
    mixed = itertools.zip_longest(*groups)
    return [item for row in mixed for item in row if item is not None]


def _wide_input() -> list[dict[str, Any]]:
    """47 eligible items, plus items the renderer must drop. Each dropped item has a leak token."""
    entities = [_entity(f"Ent{i}", f"entity description {i}") for i in range(14)]
    entities.append(_entity("Deictic", "the user prefers tea over coffee in the morning"))
    entities += [_entity(f"BLANKLEAKENT{i}", "   ") for i in range(2)]
    entities += [_entity(f"OVERCAPENT{i}", "over the entity cap") for i in range(8)]
    episodes = [_episode(f"conv{i}", f"episode summary {i}") for i in range(5)]
    episodes += [_episode(f"OVERCAPEP{i}", "over the episode cap") for i in range(3)]
    episodes.append(_episode("BLANKLEAKEP", "  ", user_message=None))
    stances = [_stance(f"topic{i}", f"likes topic {i}") for i in range(15)]
    stances += [_stance(f"OVERCAPST{i}", "over the stance cap") for i in range(5)]
    stances.append(_stance("BLANKLEAKST", ""))
    behavioural = [_behavioural(f"habit{i}", f"prefers habit {i}") for i in range(12)]
    behavioural += [_behavioural(f"OVERCAPBH{i}", "over the behavioural cap") for i in range(3)]
    sessions = [
        {"type": "session", "session_id": f"s{i}", "summary": "SESSIONLEAK"} for i in range(3)
    ]
    return _interleave(entities, episodes, stances, behavioural, sessions)


def _adversarial_input() -> list[dict[str, Any]]:
    """47 eligible items, each with a 600-character description, so every bound can engage."""
    words = " ".join(f"word{j}" for j in range(100))
    entities = [_entity(f"Big{i}", words) for i in range(15)]
    episodes = [_episode(f"bigconv{i}", words) for i in range(5)]
    stances = [_stance(f"bigtopic{i}", words) for i in range(15)]
    behavioural = [_behavioural(f"bighabit{i}", words) for i in range(12)]
    return _interleave(entities, episodes, stances, behavioural)


def _assert_arithmetic(digest: PlannerMemoryDigest) -> None:
    assert digest.item_count == min(digest.eligible_count, 20) - digest.dropped_count
    assert len(digest.item_keys) == digest.item_count
    assert len(digest.rendered_item_keys) == digest.eligible_count
    assert sum(digest.kind_counts.values()) == digest.item_count
    assert len(_digest_lines(digest)) == digest.item_count


class TestSelectionIsShared:
    """AC-1: one function selects; the renderer and the digest builder both follow it."""

    def test_neither_consumer_applies_a_second_filter_or_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A selection the real filters would reject: a blank description, and 22 entities
        # against a render cap of 15. A second filter or cap in either consumer shows here.
        selected = [_entity("SelBlank", "  ")] + [
            _entity(f"Sel{i}", f"selected {i}") for i in range(21)
        ]
        monkeypatch.setattr(
            executor,
            "_select_renderable_memory",
            lambda items: tuple(_SelectedItem(MemoryItemKind.ENTITY, e) for e in selected),
        )
        decoy = [_entity("Decoy", "not in the selection")]

        rendered = _render_memory_section_with_ids(decoy)[0]
        digest = _build_planner_memory_digest(decoy)

        for entity in selected:
            assert f"] {entity['name']}:" in rendered
        assert "Decoy" not in rendered
        assert digest.eligible_count == 22
        assert digest.item_count == 20  # the digest's own item cap, not a second render cap
        assert "SelBlank" in digest.text  # no blank filter in the builder
        assert "Sel18" in digest.text  # no entity cap in the builder
        assert "Decoy" not in digest.text


class TestDigestLineIsAPrefixOfTheRenderersLine:
    """AC-2: no field and no wording the renderer would not have emitted."""

    def test_every_line_is_a_prefix_over_a_mixed_input_of_more_than_47_items(self) -> None:
        items = _wide_input()
        assert len(items) > 47

        digest = _build_planner_memory_digest(items)

        assert len(_rendered_item_lines(items)) == 47  # the render caps bound the eligible set
        assert digest.eligible_count == 47
        assert digest.item_count > 0
        _assert_each_line_prefixes_a_rendered_line(digest, items)
        assert digest.item_keys == digest.rendered_item_keys[: digest.item_count]

    @pytest.mark.parametrize("leak", ["SESSIONLEAK", "BLANKLEAK", "OVERCAP"])
    def test_no_dropped_item_reaches_the_digest(self, leak: str) -> None:
        digest = _build_planner_memory_digest(_wide_input())

        assert leak not in digest.text

    def test_the_digest_carries_no_citation_identifier_or_guidance(self) -> None:
        digest = _build_planner_memory_digest(_wide_input())

        assert "[" not in digest.text.replace("[Concept]", "")
        assert "These are entities" not in digest.text
        assert "##" not in digest.text

    def test_lines_follow_the_relevance_order_not_the_section_order(self) -> None:
        items = [
            _stance("t", "likes t"),
            _episode("c", "episode text"),
            _entity("E", "entity text"),
            _behavioural("h", "prefers h"),
        ]

        digest = _build_planner_memory_digest(items)

        assert [key.kind for key in digest.item_keys] == [
            "stance",
            "episode",
            "entity",
            "behavioural_stance",
        ]
        assert dict(digest.kind_counts) == {
            "stance": 1,
            "episode": 1,
            "entity": 1,
            "behavioural_stance": 1,
        }

    def test_episode_numbers_match_the_renderers_when_other_kinds_come_first(self) -> None:
        items = [
            _entity("E0", "first"),
            _episode("a", "episode a"),
            _stance("t0", "likes t0"),
            _episode("b", "episode b"),
            _episode("c", "episode c"),
        ]

        digest = _build_planner_memory_digest(items)

        digest_episodes = [line for line in _digest_lines(digest) if line[0].isdigit()]
        assert digest_episodes == ["1. episode a", "2. episode b", "3. episode c"]
        rendered_episodes = [line for line in _rendered_item_lines(items) if line[0].isdigit()]
        assert digest_episodes == rendered_episodes

    def test_episode_numbers_hold_when_the_item_cap_cuts_the_episode_list(self) -> None:
        entities = [_entity(f"E{i}", f"entity {i}") for i in range(15)]
        stances = [_stance(f"t{i}", f"likes {i}") for i in range(3)]
        episodes = [_episode(f"c{i}", f"episode {i}") for i in range(5)]
        items = entities + stances + episodes

        digest = _build_planner_memory_digest(items)

        assert digest.eligible_count == 23
        assert digest.item_count == 20
        digest_episodes = [line for line in _digest_lines(digest) if line[0].isdigit()]
        assert digest_episodes == ["1. episode 0", "2. episode 1"]
        assert (
            digest_episodes
            == [line for line in _rendered_item_lines(items) if line[0].isdigit()][:2]
        )


class TestOneBoundedLinePerItem:
    """The per-line bound: how a long or multi-line renderer line becomes one short line."""

    def test_a_line_is_cut_at_a_clause_boundary_when_one_fits(self) -> None:
        first = "one two three four five six seven eight nine ten eleven twelve thirteen"
        items = [_entity("Cl", f"{first}, second clause that runs well past the one hundred bound")]

        digest = _build_planner_memory_digest(items)

        assert digest.text.endswith("thirteen,")
        assert _rendered_item_lines(items)[0].startswith(digest.text)

    def test_a_line_is_cut_at_a_word_boundary_when_no_clause_boundary_fits(self) -> None:
        items = [_entity("Long", "alpha " * 60)]

        digest = _build_planner_memory_digest(items)
        rendered = _rendered_item_lines(items)[0]

        assert 60 <= len(digest.text) <= 120
        assert digest.text.endswith("alpha")
        assert rendered[len(digest.text)] == " "

    @pytest.mark.parametrize("filler", ["x", "é"], ids=["ascii", "multibyte"])
    def test_a_single_long_token_is_cut_hard_at_the_bound(self, filler: str) -> None:
        items = [_entity("Tok", filler * 500)]

        digest = _build_planner_memory_digest(items)

        assert len(digest.text) == 120
        assert _rendered_item_lines(items)[0].startswith(digest.text)

    @pytest.mark.parametrize("separator", ["\n", "\r\n", "\r", " ", "\x0b"])
    def test_any_line_break_inside_the_line_ends_the_digest_line(self, separator: str) -> None:
        items = [_entity("Multi", f"first part{separator}second part SECONDLEAK")]

        digest = _build_planner_memory_digest(items)

        assert digest.item_count == 1
        assert len(digest.text.splitlines()) == 1
        assert "first part" in digest.text
        assert "SECONDLEAK" not in digest.text

    def test_a_line_break_in_a_name_ends_the_digest_line(self) -> None:
        items = [_stance("topic\nwith break", "BREAKLEAK affect")]

        digest = _build_planner_memory_digest(items)

        assert digest.item_count == 1
        assert digest.text == "- topic"

    def test_the_digest_never_carries_the_renderers_truncation_marker(self) -> None:
        items = [_entity("Huge", "y " * 800)]

        digest = _build_planner_memory_digest(items)

        assert "...[truncated" in _rendered_item_lines(items)[0]
        assert "truncated" not in digest.text
        assert len(digest.text) <= 120

    def test_a_deictic_line_is_cut_before_the_user_when_the_clarifier_does_not_fit(self) -> None:
        # FRE-1150: "the user" without the renderer's clarifier reads as the connected user.
        items = [_entity("Susan", "The user's stated name in the conversation.")]

        digest = _build_planner_memory_digest(items)
        rendered = _rendered_item_lines(items)[0]

        assert _DEICTIC_DISAMBIGUATION in rendered
        assert digest.text == "- [Concept] Susan:"
        assert rendered.startswith(digest.text)

    def test_a_deictic_phrase_beyond_the_cut_is_unaffected(self) -> None:
        items = [_entity("Late", "alpha " * 30 + "the user said so")]

        digest = _build_planner_memory_digest(items)

        assert "the user" not in digest.text
        assert digest.text.endswith("alpha")


class TestTheThreeBoundsHold:
    """AC-3: the bounds hold on input that makes each of them engage."""

    def test_bounds_hold_and_the_ceiling_provably_fired(self) -> None:
        items = _adversarial_input()
        assert len(items) == 47

        digest = _build_planner_memory_digest(items)
        lines = _digest_lines(digest)

        assert digest.eligible_count == 47
        assert digest.item_count <= 20
        assert all(len(line) <= 120 for line in lines)
        # The line bound engaged: every renderer line was longer than the bound.
        assert all(len(r) > 120 for r in _rendered_item_lines(items))
        assert estimate_tokens(digest.text) <= 300
        assert digest.estimated_tokens == estimate_tokens(digest.text)
        assert digest.max_line_chars == max(len(line) for line in lines)
        # The seeded input is large enough that the token ceiling dropped whole lines.
        assert digest.dropped_count > 0
        assert digest.item_count < 20
        _assert_each_line_prefixes_a_rendered_line(digest, items)
        assert digest.item_keys == digest.rendered_item_keys[: digest.item_count]


class TestTheArithmeticCloses:
    """AC-4: item_count == min(eligible_count, 20) - dropped_count on every digest."""

    def test_arithmetic_closes_over_constructed_digests(self) -> None:
        short_25 = (
            [_entity(f"E{i}", "d") for i in range(15)]
            + [_episode(f"c{i}", "e") for i in range(5)]
            + [_stance(f"t{i}", "a") for i in range(5)]
        )
        cases = {
            "empty": [],
            "small": [_entity("A", "a"), _stance("t", "likes t")],
            "twenty_five_no_drop": short_25,
            "adversarial": _adversarial_input(),
            "wide": _wide_input(),
        }
        digests = {name: _build_planner_memory_digest(items) for name, items in cases.items()}

        for digest in digests.values():
            _assert_arithmetic(digest)
        # The cases are not all trivially clean: the cap binds without a drop, and a drop fires.
        assert digests["twenty_five_no_drop"].eligible_count == 25
        assert digests["twenty_five_no_drop"].item_count == 20
        assert digests["twenty_five_no_drop"].dropped_count == 0
        assert digests["adversarial"].dropped_count > 0


class TestAnEmptyResultIsARealEmpty:
    """AC-5: nothing to say gives an empty text, not a header, a bullet or whitespace."""

    @pytest.mark.parametrize(
        "items",
        [
            [],
            [
                {"type": "session", "session_id": "s1", "summary": "SESSIONLEAK"},
                _entity("Blank", "   "),
                _entity("None", None),
                _episode("c1", "  ", user_message=None),
                _stance("t", ""),
                _behavioural("h", None),
                {"unknown": "shape"},
            ],
        ],
        ids=["empty_context", "every_item_filters_out"],
    )
    def test_empty_results(self, items: list[dict[str, Any]]) -> None:
        digest = _build_planner_memory_digest(items)

        assert digest.item_count == 0
        assert digest.text == ""
        assert digest.item_keys == ()
        assert digest.rendered_item_keys == ()
        assert digest.eligible_count == 0
        assert digest.dropped_count == 0
        assert digest.estimated_tokens == 0
        assert digest.max_line_chars == 0
        assert dict(digest.kind_counts) == {}


class TestABareIdentityIsNeverAKey:
    """AC-6: keys are compound (kind, identity, ordinal), so equal identities never collide."""

    def test_two_entities_sharing_a_name_and_an_empty_identity_get_distinct_keys(self) -> None:
        items = [
            _entity("Paris", "the capital of France"),
            _entity("Paris", "a character in a novel"),
            _stance("", "an affect with no target"),
        ]

        digest = _build_planner_memory_digest(items)

        assert digest.item_count == 3
        assert len(set(digest.item_keys)) == 3
        assert len(set(digest.rendered_item_keys)) == 3
        paris = [key for key in digest.item_keys if key.identity == "Paris"]
        assert len(paris) == 2
        assert paris[0].kind == paris[1].kind == "entity"
        assert paris[0].ordinal != paris[1].ordinal
        empty = [key for key in digest.item_keys if key.identity == ""]
        assert len(empty) == 1
        assert empty[0].kind == "stance"
