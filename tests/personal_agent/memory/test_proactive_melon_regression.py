"""FRE-1041 AC-2 — the decisive case, at the emitted-candidate altitude.

The turn `"I would like to make a melon/canteloupe ice cream"`, asked twice on
2026-07-27. Its recall evidence record shows three entity candidates at 12:04 and
**zero** at 18:32, and it is the turn the whole FRE-1021 → FRE-1041 investigation
started from.

The root cause, measured live rather than reasoned from code:

* the lexical arm **does** surface the right entities for this message (`Melon` 5.10,
  `Cantaloupe ice cream` 5.31 against the live ``turn_entity_fulltext`` index), so
  candidacy was never the problem;
* lexical-only rows enter proactive scoring at ``recall_similarity_floor`` — deployed at
  **0.60** — so they clear ``proactive_memory_min_score`` comfortably. The threshold was
  never the gate either;
* the gate is the **top-``max_injected_items`` rank race**. Against the five episode
  scores actually recorded at 18:32 (0.6029 / 0.5768 / 0.5700 / 0.5693 / 0.5652), `Melon`
  scored 0.563 — **sixth of six**, losing the last slot by 0.002;
* the entity-overlap subscore is the only lever that can win that race, because embedding
  is pinned at the floor for lexical rows and recency/topic are already maxed. Its
  session-side set is the entity hint, and the capitalisation heuristic FRE-1041 removes
  could not produce "melon" from a lowercase message.

These tests assert through the real :func:`build_proactive_suggestions`, so every gate
applies — dedupe, ``min_score``, the candidate cap, ``max_injected_items``, the
diminishing floor and gap, and the token budget. Asserting only that a score crosses a
threshold would beg the question: crossing ``min_score`` is not the same as being emitted.

**Scope note (FRE-1061).** This fixture reproduces the *telemetry shape* of the 18:32
record — episode rows without entity fields, one turnless entity row — and the rank race
it loses. The production mechanism behind "zero entity candidates" turned out to be one
layer earlier: real dense rows are *(entity, best-turn)* pairs, and the pre-FRE-1061
payload conversion flattened every discussed entity into its episode before any race ran
(`telemetry/entity_recall_findings_explore_2026-07-30.md`). The production-shaped pair
fixtures live in ``test_proactive_entity_split.py``; this module remains the regression
for the hint/overlap lever (FRE-1041) and the gate naming (FRE-1060) on the recorded
shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from personal_agent.captains_log.turn_evidence import DropReason
from personal_agent.config.settings import get_settings
from personal_agent.memory.proactive import build_proactive_suggestions

MELON_MESSAGE = "I would like to make a melon/canteloupe ice cream"
LEXICAL_FLOOR = 0.60
"""The deployed ``AGENT_RECALL_SIMILARITY_FLOOR``; lexical-only rows enter here."""


@pytest.fixture
def live_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the deployed proactive scoring configuration.

    Set explicitly rather than inherited from the environment so the regression asserts
    the production shape wherever it runs.
    """
    settings = get_settings()
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
        ("proactive_memory_max_tokens", 5000),
    ):
        monkeypatch.setattr(settings, name, value, raising=False)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _episode_row(index: int, vector_score: float) -> dict[str, Any]:
    """An episode row as the 18:32 telemetry recorded it.

    Kind-collapsed: a real dense-arm row also carries entity fields (FRE-1061) — the
    production-shaped pair fixture lives in ``test_proactive_entity_split.py``.
    """
    return {
        "turn_id": f"turn-{index}",
        "user_message": f"an earlier conversation about topic {index}",
        "summary": f"summary {index}",
        "key_entities": [f"Unrelated{index}"],
        "vector_score": vector_score,
        "timestamp_iso": _now_iso(),
    }


def _melon_entity_row() -> dict[str, Any]:
    """The `Melon` entity as the lexical arm appends it: at the floor, no vector score.

    Populated ``description`` (FRE-1114): production's one real producer of these rows,
    ``MemoryService.suggest_proactive_raw``, always selects ``node.description`` from the
    dense arm's Cypher, so an entity actually missing one is discarded before it ever
    reaches the rank race this module tests (its own regression coverage lives in
    ``test_proactive_discards.py::TestEmptyDescriptionGate``). This module remains the
    regression for the hint/overlap lever and gate naming specifically, so the fixture
    needs a real description to stay eligible for that race at all.
    """
    return {
        "name": "Melon",
        "entity_type": "FoodOrIngredient",
        "description": "A honeydew or cantaloupe melon.",
        "key_entities": ["Melon"],
        "vector_score": LEXICAL_FLOOR,
        "timestamp_iso": _now_iso(),
        "mention_count": 3,
    }


def _rows() -> list[dict[str, Any]]:
    """Five episodes ranked just above the melon entity, plus the entity itself.

    The dense scores are chosen so the five episodes land in a narrow band immediately
    above the melon entity's hintless score, reproducing the 18:32 record where the
    entity came sixth of six and lost the last slot by 0.002.
    """
    episodes = [_episode_row(i, score) for i, score in enumerate([0.77, 0.76, 0.75, 0.74, 0.73])]
    return [*episodes, _melon_entity_row()]


def _names(candidates: list[Any]) -> list[str]:
    return [c.payload.get("name") for c in candidates if c.kind == "entity"]


class TestMelonTurnEntityAdmission:
    """AC-2: the melon turn yields an entity candidate where it currently yields none."""

    def test_without_an_entity_hint_the_melon_entity_is_not_emitted(
        self, live_scoring: None
    ) -> None:
        """Today's behaviour: episodes take all five slots and the entity is cut.

        This is the 18:32 record — five episodes, zero entity candidates.
        """
        suggestions = build_proactive_suggestions(
            _rows(),
            set(),
            MELON_MESSAGE,
            "t-before",
            None,
        )

        assert "Melon" not in _names(suggestions.candidates)
        assert len(suggestions.candidates) == 5
        assert all(c.kind == "episode" for c in suggestions.candidates)

    def test_the_gate_that_cuts_the_melon_entity_is_now_named(self, live_scoring: None) -> None:
        """FRE-1060 AC-3, at the unit altitude: *which* gate discards it.

        The turn above shows the entity missing. Until this ticket, nothing could say
        whether the ranked cap or the token budget removed it — and on the live turn both
        were consistent with the one log line, whose ``after_count`` of 5 equalled
        ``max_injected_items`` while its 470-token estimate sat 30 under the 500 threshold.
        Here the budget is deliberately slack (``max_tokens=5000``), so the answer must be
        the ranked cap, and the record now says so instead of leaving an absence.

        This is the prediction the live re-run checks against: if a live record instead
        names ``recall_token_budget``, the deployed configuration differs from the one
        pinned in ``live_scoring`` and *that* is the finding.
        """
        suggestions = build_proactive_suggestions(_rows(), set(), MELON_MESSAGE, "t-gate", None)

        melon = [d for d in suggestions.discarded if d.payload.get("name") == "Melon"]
        assert len(melon) == 1, "the entity must be a named drop, not an absence"
        assert melon[0].drop_reason is DropReason.RECALL_ITEM_CAP
        assert melon[0].relevance_score is not None, "a scored candidate that lost a race"

    def test_with_the_resolved_entity_hint_the_melon_entity_is_emitted(
        self, live_scoring: None
    ) -> None:
        """FRE-1041's fix: the resolver supplies `Melon`, overlap fires, it wins the race."""
        suggestions = build_proactive_suggestions(
            _rows(),
            {"Melon"},
            MELON_MESSAGE,
            "t-after",
            None,
        )

        assert "Melon" in _names(suggestions.candidates)

    def test_the_hint_promotes_the_entity_to_first_entity_rank(self, live_scoring: None) -> None:
        """Rank 6 of 6 becomes the top entity — the overlap subscore is the deciding lever.

        FRE-1062's episode floor walks the best episode first, so "first place" is now
        "first among entities"; the lever this regression pins (overlap promotion) is
        unchanged.
        """
        suggestions = build_proactive_suggestions(
            _rows(),
            {"Melon"},
            MELON_MESSAGE,
            "t-rank",
            None,
        )

        entity_names = [c.payload.get("name") for c in suggestions.candidates if c.kind == "entity"]
        assert entity_names and entity_names[0] == "Melon"

    def test_the_capitalisation_heuristic_could_never_have_supplied_the_hint(
        self, live_scoring: None
    ) -> None:
        """Why the gate was stuck shut: the message's only capitalised token is ``I``.

        The removed heuristic kept capitalised words longer than three characters, so on
        this turn it produced an empty set — which is exactly the empty-hint case above.
        """
        capitalised = [w for w in MELON_MESSAGE.split() if len(w) > 3 and w[0].isupper()]
        assert capitalised == []

    def test_an_unrelated_hint_does_not_promote_the_entity(self, live_scoring: None) -> None:
        """The lever is overlap with *this* entity, not the mere presence of any hint."""
        suggestions = build_proactive_suggestions(
            _rows(),
            {"Bicycle"},
            MELON_MESSAGE,
            "t-control",
            None,
        )

        assert "Melon" not in _names(suggestions.candidates)
