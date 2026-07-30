"""FRE-1062 — two stated selection rules: episode floor, then mentioned-entity pins.

Born from the first live post-FRE-1061 melon turn (trace ``4eca4070``): the literally
mentioned ``Melon`` entity ranked 8th and was cut by ``recall_item_cap`` behind entities
the message never names, while the answer's entire substance rode the single admitted
episode, which survived by rank luck. The rules:

1. **Episode floor, first in the walk** — the best-ranked episode that clears the
   existing ``diminishing_score_floor`` is admitted before anything else can consume
   its slot or token budget.
2. **Mentioned-entity pin, next** — up to two entity candidates the message literally
   names (FRE-1041 resolver output) are admitted ahead of the rank walk, still subject
   to ``min_score``, the token budget and the oversize skip.

Walk order is admission priority, not presentation — the renderer partitions items by
kind — so these tests assert set membership and gate attribution, not prompt order,
except where the guarantee itself is the order (the floor consuming budget first).

Scoring is deterministic as in ``test_proactive_discards.py``: ``timestamp_iso=None``
pins recency at 0.5, no topic hint pins topic at 0.5, no session entities pins overlap
at 0 — final score is ``0.45 * vector_score + 0.15`` under the deployed weights.
"""

from __future__ import annotations

from typing import Any

import pytest

import personal_agent.memory.proactive as proactive_mod
from personal_agent.captains_log.turn_evidence import DropReason
from personal_agent.memory.proactive import build_proactive_suggestions


@pytest.fixture
def deployed_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the deployed proactive configuration (same shape as the FRE-1060 oracle)."""
    s = proactive_mod.settings
    for name, value in (
        ("proactive_memory_w_embedding", 0.45),
        ("proactive_memory_w_entity", 0.25),
        ("proactive_memory_w_recency", 0.20),
        ("proactive_memory_w_topic", 0.10),
        ("proactive_memory_min_score", 0.30),
        ("proactive_memory_max_candidates", 10),
        ("proactive_memory_max_injected_items", 5),
        ("proactive_memory_diminishing_score_floor", 0.35),
        ("proactive_memory_diminishing_score_gap", 0.15),
        ("proactive_memory_recency_half_life_days", 30.0),
        ("proactive_memory_max_tokens", 100_000),
    ):
        monkeypatch.setattr(s, name, value, raising=False)


def _pair_row(name: str, turn_id: str, vector_score: float) -> dict[str, Any]:
    """A production-shaped dense row: entity fields AND best-turn fields."""
    return {
        "name": name,
        "entity_type": "FoodOrIngredient",
        "description": f"Description of {name}",
        "vector_score": vector_score,
        "turn_id": turn_id,
        "session_id": "other",
        "timestamp_iso": None,
        "user_message": f"we discussed {name}",
        "summary": f"summary about {name}",
        "key_entities": [name],
        "mention_count": None,
    }


def _entity_row(name: str, vector_score: float) -> dict[str, Any]:
    """A turnless entity row — yields an entity candidate only."""
    row = _pair_row(name, "", vector_score)
    row["turn_id"] = None
    row["user_message"] = None
    row["summary"] = None
    return row


def _episode_row(turn_id: str, vector_score: float) -> dict[str, Any]:
    """A nameless episode row — yields an episode candidate only."""
    row = _pair_row("", turn_id, vector_score)
    row["name"] = None
    row["key_entities"] = []
    return row


def _ids(items: list[Any]) -> list[tuple[str, str]]:
    return [
        (
            c.kind,
            c.payload.get("name") if c.kind == "entity" else c.payload.get("conversation_id"),
        )
        for c in items
    ]


class TestMentionedEntityPin:
    """AC-1 / AC-3 / bounds — the literal mention is finally worth admission."""

    def test_a_mentioned_entity_past_the_cap_is_admitted(self, deployed_scoring: None) -> None:
        """AC-1: the live melon shape — unmentioned associates outrank the named topic.

        Five associate pair rows produce ten candidates that fill the window; the
        mentioned entity ranks 11th–12th and, before FRE-1062, never survives. The
        displaced tail is still attributed ``recall_item_cap`` — the pin is a
        permutation of the walk, not a new gate.
        """
        rows = [_pair_row(f"Associate{i}", f"t-a{i}", 0.90 - i * 0.01) for i in range(5)] + [
            _pair_row("Melon", "t-melon", 0.70)
        ]

        out = build_proactive_suggestions(
            rows, set(), None, "t-ac1", None, mentioned_entity_names=["Melon"]
        )

        admitted = _ids(out.candidates)
        assert ("entity", "Melon") in admitted
        assert DropReason.RECALL_ITEM_CAP in {d.drop_reason for d in out.discarded}

    def test_a_sub_threshold_mention_is_not_pinned(self, deployed_scoring: None) -> None:
        """AC-3: ``min_score`` stays the noise gate — a pin cannot resurrect noise."""
        rows = [_pair_row("Associate0", "t-a0", 0.90), _pair_row("Melon", "t-melon", 0.20)]

        out = build_proactive_suggestions(
            rows, set(), None, "t-ac3-threshold", None, mentioned_entity_names=["Melon"]
        )

        assert ("entity", "Melon") not in _ids(out.candidates)
        melon_drops = [d for d in out.discarded if d.payload.get("name") == "Melon"]
        assert melon_drops and melon_drops[0].drop_reason is DropReason.RECALL_SCORE_THRESHOLD

    def test_an_oversized_pin_is_stepped_over(
        self, deployed_scoring: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-3: the oversize skip applies to pins; the floor and rest still admit."""
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", 500)
        monkeypatch.setattr(
            proactive_mod,
            "_estimate_payload_tokens",
            lambda p: 900 if p.get("type") == "entity" and p.get("name") == "Melon" else 100,
        )
        rows = [_pair_row("Associate0", "t-a0", 0.90), _pair_row("Melon", "t-melon", 0.70)]

        out = build_proactive_suggestions(
            rows, set(), None, "t-ac3-oversized", None, mentioned_entity_names=["Melon"]
        )

        assert ("entity", "Melon") not in _ids(out.candidates)
        assert ("episode", "t-a0") in _ids(out.candidates), "floor unaffected by the bad pin"
        by_name = {d.payload.get("name"): d.drop_reason for d in out.discarded}
        assert by_name["Melon"] is DropReason.RECALL_ITEM_OVERSIZED

    def test_the_pin_bound_is_two(
        self, deployed_scoring: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Three resolved mentions pin only the best two — the bound is real."""
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_injected_items", 3)
        rows = [
            _entity_row("Associate0", 0.90),
            _entity_row("Associate1", 0.89),
            _entity_row("M1", 0.50),
            _entity_row("M2", 0.45),
            _entity_row("M3", 0.40),
        ]

        out = build_proactive_suggestions(
            rows, set(), None, "t-pin-bound", None, mentioned_entity_names=["M1", "M2", "M3"]
        )

        admitted = _ids(out.candidates)
        assert ("entity", "M1") in admitted and ("entity", "M2") in admitted
        assert ("entity", "M3") not in admitted, "the third mention competes on rank like anyone"


class TestEpisodeFloor:
    """AC-2 / the starvation case — the substance carrier cannot be crowded out."""

    def test_all_entity_top_ranks_still_admit_the_best_episode(
        self, deployed_scoring: None
    ) -> None:
        """AC-2: five entity rows outrank the only episode; the floor admits it anyway."""
        rows = [_entity_row(f"E{i}", 0.90 - i * 0.01) for i in range(5)] + [
            _pair_row("Weak", "t-weak", 0.50)
        ]

        out = build_proactive_suggestions(rows, set(), None, "t-ac2", None)

        assert ("episode", "t-weak") in _ids(out.candidates)

    def test_the_floor_episode_gets_budget_before_pins(
        self, deployed_scoring: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The codex starvation case: two 240-token pins, 500 budget, 30-token episode.

        Pins-first would spend 480 and the episode would die on ``recall_token_budget``.
        Floor-first admits the episode, then exactly one pin fits.
        """
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", 500)
        monkeypatch.setattr(
            proactive_mod,
            "_estimate_payload_tokens",
            lambda p: 30 if p.get("type") == "episode" else 240,
        )
        rows = [
            _entity_row("M1", 0.90),
            _entity_row("M2", 0.89),
            _pair_row("Weak", "t-weak", 0.50),
        ]

        out = build_proactive_suggestions(
            rows, set(), None, "t-floor-budget", None, mentioned_entity_names=["M1", "M2"]
        )

        admitted = _ids(out.candidates)
        assert ("episode", "t-weak") in admitted, "the guarantee must survive pin spending"
        assert ("entity", "M1") in admitted
        assert ("entity", "M2") not in admitted, "the second pin hits the token budget"

    def test_a_sub_floor_episode_is_not_reserved(self, deployed_scoring: None) -> None:
        """The quality guard: an episode below the diminishing floor earns no slot.

        0.45 * 0.40 + 0.15 = 0.33 < 0.35 — today's floor-stop behaviour is preserved
        exactly (this is also AC-4's shape: with no reservation and no pins, the walk
        is the pre-FRE-1062 loop).
        """
        rows = [_episode_row("t0", 0.40), _episode_row("t1", 0.38)]

        out = build_proactive_suggestions(rows, set(), None, "t-sub-floor", None)

        assert out.candidates == []
        assert {d.drop_reason for d in out.discarded} == {DropReason.RECALL_SCORE_FLOOR}

    def test_no_mentions_no_qualifying_episode_matches_legacy_selection(
        self, deployed_scoring: None
    ) -> None:
        """AC-4: empty head → the ordered survivors equal the pre-change walk."""
        rows = [_episode_row(f"t{i}", 0.90 - i * 0.01) for i in range(8)]

        out = build_proactive_suggestions(rows, set(), None, "t-ac4", None)

        assert [c.payload.get("conversation_id") for c in out.candidates] == [
            "t0",
            "t1",
            "t2",
            "t3",
            "t4",
        ]


class TestAccountingUnderTheRules:
    """AC-5 — conservation, disjointness and the event fields survive the reorder."""

    def test_conservation_and_disjointness_with_pins_and_floor(
        self, deployed_scoring: None
    ) -> None:
        rows = [_pair_row(f"Associate{i}", f"t-a{i}", 0.90 - i * 0.01) for i in range(5)] + [
            _pair_row("Melon", "t-melon", 0.70),
            _pair_row("Noise", "t-noise", 0.20),
        ]
        # 7 rows → 14 candidates, no shared identities → 14 deduplicated.

        out = build_proactive_suggestions(
            rows, set(), None, "t-ac5", None, mentioned_entity_names=["Melon"]
        )

        assert len(out.candidates) + len(out.discarded) == 14
        assert set(_ids(out.candidates)) & set(_ids(out.discarded)) == set()

    def test_the_floor_identity_appears_exactly_once(self, deployed_scoring: None) -> None:
        """The reserved episode must not re-enter the rest region (codex finding 4)."""
        rows = [_pair_row("Solo", "t-solo", 0.90)]

        out = build_proactive_suggestions(rows, set(), None, "t-floor-once", None)

        everything = _ids(out.candidates) + _ids(out.discarded)
        assert everything.count(("episode", "t-solo")) == 1

    def test_a_max_candidates_of_one_does_not_break_the_window(
        self, deployed_scoring: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legal config edge (codex finding 3): the single window slot goes to the floor."""
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_candidates", 1)
        rows = [_pair_row("Melon", "t-melon", 0.90)]

        out = build_proactive_suggestions(
            rows, set(), None, "t-window-one", None, mentioned_entity_names=["Melon"]
        )

        assert _ids(out.candidates) == [("episode", "t-melon")]
        assert [d.drop_reason for d in out.discarded] == [DropReason.RECALL_CANDIDATE_CAP]

    def test_the_trim_event_carries_the_rule_fields(
        self, deployed_scoring: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        rows = [_pair_row(f"Associate{i}", f"t-a{i}", 0.90 - i * 0.01) for i in range(5)] + [
            _pair_row("Melon", "t-melon", 0.70)
        ]

        with caplog.at_level("INFO"):
            build_proactive_suggestions(
                rows, set(), None, "t-event", None, mentioned_entity_names=["Melon"]
            )

        events = [r for r in caplog.records if "proactive_memory_budget_trimmed" in r.getMessage()]
        assert len(events) == 1
        message = events[0].getMessage()
        assert "pinned_mention_count" in message
        assert "episode_floor_applied" in message
