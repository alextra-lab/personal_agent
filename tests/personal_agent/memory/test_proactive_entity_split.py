"""FRE-1061 — one raw row is an (entity, best-turn) pair; both candidates must exist.

The proactive Cypher is entity-anchored: every dense row carries the entity's
``name``/``entity_type``/``description`` *and* its best cross-session turn. The
pre-FRE-1061 ``_build_payload_for_row`` forced a binary choice and always chose the
episode when a turn with text existed, so an entity that had ever been discussed in
another session could not reach the model as an entity (7,442 of 7,446 production
entities — `telemetry/entity_recall_findings_explore_2026-07-30.md`). These tests pin
the split: a pair row yields **two** candidates, deduped by kind-appropriate identity,
with every FRE-1060 gate and accounting invariant intact over the candidate population.

Scoring is deterministic the same way `test_proactive_discards.py` makes it so:
``timestamp_iso=None`` pins recency at 0.5, ``session_topic_hint=None`` pins topic at
0.5, no session entities pins overlap at 0 — every final score is
``0.45 * vector_score + 0.15`` under the deployed weights.
"""

from __future__ import annotations

from typing import Any

import pytest

import personal_agent.memory.proactive as proactive_mod
from personal_agent.captains_log.turn_evidence import DropReason
from personal_agent.memory.proactive import (
    _split_row_payloads,
    build_proactive_suggestions,
)


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


def _pair_row(
    *,
    name: str | None = "Melon",
    turn_id: str | None = "turn-1",
    vector_score: float = 0.77,
    user_message: str | None = "we discussed melon ice cream",
    summary: str | None = "melon ice cream recipe talk",
    description: str | None = "A watery, low-sugar fruit",
    key_entities: list[str] | None = None,
) -> dict[str, Any]:
    """A production-shaped dense-arm row: entity fields AND best-turn fields."""
    return {
        "name": name,
        "entity_type": "FoodOrIngredient",
        "description": description,
        "vector_score": vector_score,
        "turn_id": turn_id,
        "session_id": "other-session",
        "timestamp_iso": None,
        "user_message": user_message,
        "summary": summary,
        "key_entities": key_entities if key_entities is not None else [name] if name else [],
        "mention_count": 0,
    }


def _ids(items: list[Any]) -> list[tuple[str, str]]:
    """(kind, identity) pairs — entity identity is name, episode identity is turn id."""
    out: list[tuple[str, str]] = []
    for c in items:
        if c.kind == "entity":
            out.append(("entity", c.payload.get("name")))
        else:
            out.append(("episode", c.payload.get("conversation_id")))
    return out


class TestSplitRowPayloads:
    """AC-1: the pair splits; neither kind erases the other."""

    def test_a_pair_row_yields_entity_then_episode(self) -> None:
        """A row with a name and a turn with text yields both kinds, entity first.

        Entity-first is load-bearing: the selection sort is stable, so at the equal
        score the pair shares, the entity precedes its sibling episode.
        """
        payloads = _split_row_payloads(_pair_row())

        assert [kind for kind, _ in payloads] == ["entity", "episode"]

    def test_the_entity_payload_keeps_name_type_description(self) -> None:
        """The exact fields the pre-FRE-1061 conversion discarded."""
        kind, payload = _split_row_payloads(_pair_row())[0]

        assert kind == "entity"
        assert payload["type"] == "entity"
        assert payload["name"] == "Melon"
        assert payload["entity_type"] == "FoodOrIngredient"
        assert payload["description"] == "A watery, low-sugar fruit"

    def test_the_episode_payload_is_unchanged(self) -> None:
        """FRE-1004 identity and ADR-0125 D5 fallback semantics carry over intact."""
        kind, payload = _split_row_payloads(_pair_row())[1]

        assert kind == "episode"
        assert payload["type"] == "episode"
        assert payload["conversation_id"] == "turn-1"
        assert payload["user_message"] == "we discussed melon ice cream"
        assert payload["summary"] == "melon ice cream recipe talk"

    def test_episode_summary_falls_back_to_marked_truncation(self) -> None:
        """ADR-0125 D5: a digest-less episode gets a marked, not silently clipped, excerpt."""
        long_message = "considering the tradeoffs between options " * 20
        row = _pair_row(user_message=long_message, summary=None)

        _, payload = _split_row_payloads(row)[1]

        assert len(payload["summary"]) < len(long_message)
        assert "...[truncated" in payload["summary"]

    def test_mention_count_passes_through_real_or_absent(self) -> None:
        """A real graph count passes through; no count stays None, never a fabricated 0.

        The renderer omits an absent count but prints whatever value is present, so a
        defaulted 0 would render "(mentioned 0x)" on every entity line (FRE-1061
        review finding — the service previously hardcoded 0, invisible only because
        entities never rendered).
        """
        row = _pair_row()
        row["mention_count"] = 7
        assert _split_row_payloads(row)[0][1]["mention_count"] == 7

        row["mention_count"] = None
        assert _split_row_payloads(row)[0][1]["mention_count"] is None

    def test_a_nameless_row_yields_episode_only(self) -> None:
        """No name → no entity candidate; the tie-break is a preference, not an invention."""
        payloads = _split_row_payloads(_pair_row(name=None))

        assert [kind for kind, _ in payloads] == ["episode"]

    def test_a_turnless_named_row_yields_entity_only(self) -> None:
        """The pre-FRE-1061 entity branch, unchanged for never-discussed entities."""
        payloads = _split_row_payloads(_pair_row(turn_id=None))

        assert [kind for kind, _ in payloads] == ["entity"]

    def test_a_named_row_with_a_textless_turn_yields_entity_only(self) -> None:
        """A turn with neither ``user_message`` nor ``summary`` cannot render as an episode."""
        payloads = _split_row_payloads(_pair_row(user_message=None, summary=None))

        assert [kind for kind, _ in payloads] == ["entity"]

    def test_an_empty_row_falls_back_to_the_unknown_entity(self) -> None:
        """Legacy accounting fallback: no row silently vanishes from the population."""
        payloads = _split_row_payloads(_pair_row(name=None, turn_id=None))

        assert [kind for kind, _ in payloads] == ["entity"]
        assert payloads[0][1]["name"] == "unknown"


class TestMelonShapeAdmission:
    """AC-2, at the proactive-selection altitude: the entity is offered AND admitted."""

    def test_the_melon_pair_offers_and_admits_the_entity(self, deployed_scoring: None) -> None:
        """The production shape that produced 'zero entity candidates' for months.

        Five discussed-topic pair rows — every one carries a name and a cross-session
        turn with text, like 7,442 of the 7,446 production entities. Before FRE-1061
        this fixture yields five episodes and no entity at any rank.
        """
        rows = [
            _pair_row(name="Melon", turn_id="t-melon", vector_score=0.77),
            _pair_row(name="Cantaloupe", turn_id="t-cant", vector_score=0.76),
            _pair_row(name="Ice cream", turn_id="t-ice", vector_score=0.75),
            _pair_row(name="Hummus", turn_id="t-hum", vector_score=0.74),
            _pair_row(name="Comté", turn_id="t-comte", vector_score=0.73),
        ]

        out = build_proactive_suggestions(rows, set(), None, "t-melon-shape", None)

        admitted = _ids(out.candidates)
        assert ("entity", "Melon") in admitted, "the entity must be admitted, not just offered"
        offered = _ids(out.candidates) + _ids(out.discarded)
        assert ("episode", "t-melon") in offered, "the episode half is offered, not erased"

    def test_the_top_named_row_puts_its_entity_at_rank_one(self, deployed_scoring: None) -> None:
        """Stable sort + entity-first emission: the pair shares a score, entity precedes."""
        rows = [
            _pair_row(name="Melon", turn_id="t-melon", vector_score=0.77),
            _pair_row(name="Cantaloupe", turn_id="t-cant", vector_score=0.76),
        ]

        out = build_proactive_suggestions(rows, set(), None, "t-rank-one", None)

        assert out.candidates[0].kind == "entity"
        assert out.candidates[0].payload["name"] == "Melon"


class TestKindAppropriateDedupe:
    """AC-3: dedupe collapses shared identity per kind — it no longer erases entities."""

    def test_entities_sharing_a_best_turn_all_survive(self, deployed_scoring: None) -> None:
        """The 29→13 melon-turn loss: distinct entities, one shared best turn.

        Before FRE-1061 the second row vanished entirely at the row-level turn-id
        dedupe. Now only the shared episode collapses; both entities stand.
        """
        rows = [
            _pair_row(name="Melon", turn_id="t-shared", vector_score=0.77),
            _pair_row(name="Cantaloupe", turn_id="t-shared", vector_score=0.76),
        ]

        out = build_proactive_suggestions(rows, set(), None, "t-shared-turn", None)

        admitted = _ids(out.candidates)
        assert ("entity", "Melon") in admitted
        assert ("entity", "Cantaloupe") in admitted
        assert admitted.count(("episode", "t-shared")) == 1

    def test_a_true_duplicate_collapses_both_kinds_without_discards(
        self, deployed_scoring: None
    ) -> None:
        """Same name AND same turn: one entity, one episode, nothing recorded as a drop.

        The owner's 2026-07-30 call stands: a dedupe collapse is not a loss — the
        identity reached the model on the kept candidate.
        """
        rows = [
            _pair_row(name="Melon", turn_id="t-1", vector_score=0.77),
            _pair_row(name="Melon", turn_id="t-1", vector_score=0.70),
        ]

        out = build_proactive_suggestions(rows, set(), None, "t-true-dupe", None)

        assert sorted(_ids(out.candidates)) == [("entity", "Melon"), ("episode", "t-1")]
        assert out.discarded == []


class TestGatesOverMixedKinds:
    """AC-4 / AC-5: the FRE-1060 gates and accounting hold over the candidate population."""

    def test_an_oversized_entity_is_stepped_over_and_its_episode_survives(
        self, deployed_scoring: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex plan-review case: the oversized skip must not take the sibling with it."""
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", 500)
        monkeypatch.setattr(
            proactive_mod,
            "_estimate_payload_tokens",
            lambda p: 900 if p.get("type") == "entity" and p.get("name") == "Melon" else 100,
        )
        rows = [_pair_row(name="Melon", turn_id="t-melon", vector_score=0.77)]

        out = build_proactive_suggestions(rows, set(), None, "t-oversized-entity", None)

        assert _ids(out.candidates) == [("episode", "t-melon")]
        assert [(d.drop_reason, d.payload.get("name")) for d in out.discarded] == [
            (DropReason.RECALL_ITEM_OVERSIZED, "Melon")
        ]

    def test_item_cap_drops_are_named_for_either_kind(self, deployed_scoring: None) -> None:
        """The ranked cap now cuts a mixed tail; every loser still names its gate."""
        rows = [
            _pair_row(name=f"E{i}", turn_id=f"t-{i}", vector_score=0.90 - i * 0.01)
            for i in range(4)
        ]

        out = build_proactive_suggestions(rows, set(), None, "t-mixed-cap", None)

        assert len(out.candidates) == 5
        cut = [d for d in out.discarded if d.drop_reason is DropReason.RECALL_ITEM_CAP]
        assert len(cut) == 3, "8 candidates from 4 pair rows, 5 admitted, 3 cut by the cap"
        assert {d.kind for d in cut} == {"entity", "episode"}

    def test_conservation_holds_over_deduplicated_candidates(self, deployed_scoring: None) -> None:
        """Emitted + discarded == deduplicated candidates, mixed kinds and shared turns."""
        rows = [
            _pair_row(name="A", turn_id="t-x", vector_score=0.90),
            _pair_row(name="B", turn_id="t-x", vector_score=0.85),  # episode collapses
            _pair_row(name="A", turn_id="t-y", vector_score=0.80),  # entity collapses
            _pair_row(name="C", turn_id=None, vector_score=0.75),  # entity only
            _pair_row(name=None, turn_id="t-z", vector_score=0.20),  # episode only, sub-threshold
        ]
        # Deduplicated candidates: entities {A, B, C} + episodes {t-x, t-y, t-z} = 6.

        out = build_proactive_suggestions(rows, set(), None, "t-conserve-mixed", None)

        assert len(out.candidates) + len(out.discarded) == 6

    def test_no_kind_identity_is_both_admitted_and_dropped(self, deployed_scoring: None) -> None:
        """The FRE-1060 invariant, restated over (kind, identity) for mixed populations."""
        rows = [
            _pair_row(name=f"E{i}", turn_id=f"t-{i}", vector_score=0.90 - i * 0.01)
            for i in range(6)
        ]

        out = build_proactive_suggestions(rows, set(), None, "t-no-overlap-mixed", None)

        assert set(_ids(out.candidates)) & set(_ids(out.discarded)) == set()


class TestTrimEventFields:
    """The trim event's counts stay honest: rows and candidates are different units."""

    def test_the_event_reports_row_and_candidate_counts_separately(
        self, deployed_scoring: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`deduped_row_count` is gone; its unit changed, so its name had to (codex).

        Four pair rows (one shared turn) → 8 split candidates → 7 deduplicated → 5
        admitted. The item cap fires, so the event is emitted and must carry the new
        fields.
        """
        rows = [
            _pair_row(name="A", turn_id="t-x", vector_score=0.90),
            _pair_row(name="B", turn_id="t-x", vector_score=0.85),
            _pair_row(name="C", turn_id="t-y", vector_score=0.80),
            _pair_row(name="D", turn_id="t-z", vector_score=0.75),
        ]

        with caplog.at_level("INFO"):
            build_proactive_suggestions(rows, set(), None, "t-event-fields", None)

        events = [r for r in caplog.records if "proactive_memory_budget_trimmed" in r.getMessage()]
        assert len(events) == 1
        message = events[0].getMessage()
        assert "retrieved_row_count" in message
        assert "split_candidate_count" in message
        assert "deduped_candidate_count" in message
        assert "deduped_row_count" not in message
