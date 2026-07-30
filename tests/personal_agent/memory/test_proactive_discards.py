"""FRE-1060 — the proactive selection gates, named per candidate.

``build_proactive_suggestions`` returned only its survivors, so the candidates its caps
and budgets discarded ceased to exist before the request gateway could record them. The
ADR-0125 D3 item 5 admission record therefore reported *post-selection survivors as
though they were the population* — five of twelve on the live melon turn, with seven
discards no durable artifact anywhere named.

Two halves, and the order matters:

:class:`TestSelectionUnchanged` is a **characterization oracle**, written and green
against the pre-change code. Every case pins the *exact ordered list of selected
identities* for one gate permutation. It exists because the ticket's failure clause —
*"it fails if the trim is made more generous without first being made visible"* — cannot
be proven by the conservation invariant in :class:`TestConservation`: that invariant stays
true if a bug admits one more candidate and discards one fewer. Only survivor equality
catches that, so only survivor equality can guard the clause.

:class:`TestGateAttribution` then asserts each discard names the gate that removed it.

Scoring is made deterministic by removing the two time- and text-dependent subscores
rather than by freezing a clock: ``timestamp_iso=None`` makes ``_recency_subscore``
return exactly 0.5, and ``session_topic_hint=None`` makes ``_topic_subscore`` return
exactly 0.5. With no session entities the overlap term is 0, so with the deployed
weights every final score is ``0.45 * vector_score + 0.15``.
"""

from __future__ import annotations

from typing import Any

import pytest

import personal_agent.memory.proactive as proactive_mod
from personal_agent.captains_log.turn_evidence import DropReason
from personal_agent.memory.proactive import build_proactive_suggestions

#: ``0.45 * vector_score + 0.15`` under the fixture below — stated so a reader can check
#: a case's expectations without running the scorer.
SCORES: dict[float, float] = {
    0.90: 0.555,
    0.80: 0.510,
    0.70: 0.465,
    0.60: 0.420,
    0.50: 0.375,
    0.40: 0.330,
    0.20: 0.240,
}


@pytest.fixture
def deployed_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the deployed proactive configuration.

    Set explicitly rather than inherited from the environment so the oracle asserts the
    production shape wherever it runs.
    """
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


def _episode(turn_id: str, vector_score: float) -> dict[str, Any]:
    """An episode row whose only live subscore is the embedding.

    ``timestamp_iso`` is omitted deliberately: a real timestamp would make the recency
    subscore a function of wall-clock time and the oracle would drift.
    """
    return {
        "turn_id": turn_id,
        "user_message": f"an earlier conversation {turn_id}",
        "summary": f"summary {turn_id}",
        "key_entities": [],
        "vector_score": vector_score,
        "timestamp_iso": None,
    }


def _selected(suggestions: Any) -> list[str]:
    """Ordered identities of the emitted candidates."""
    return [
        c.payload.get("conversation_id") or c.payload.get("name") for c in suggestions.candidates
    ]


def _run(rows: list[dict[str, Any]], trace: str) -> Any:
    """Score ``rows`` with every time- and topic-dependent term neutralised."""
    return build_proactive_suggestions(rows, set(), None, trace, None)


def _fixed_tokens(monkeypatch: pytest.MonkeyPatch, cost: int) -> None:
    """Give every payload the same token cost.

    Patched by module attribute because that is how ``build_proactive_suggestions``
    resolves it, matching the existing idiom in ``test_proactive.py``.
    """
    monkeypatch.setattr(proactive_mod, "_estimate_payload_tokens", lambda _p: cost)


class TestSelectionUnchanged:
    """The oracle: which candidates are emitted, per gate, must not move.

    Every expectation here was captured from the pre-FRE-1060 implementation. A failure
    in this class means selection behaviour changed — which this ticket forbids.
    """

    def test_no_gate_fires(self, deployed_scoring: None) -> None:
        """Under the cap, above the floor, inside the budget: everything is emitted."""
        rows = [_episode("t1", 0.90), _episode("t2", 0.80), _episode("t3", 0.70)]

        assert _selected(_run(rows, "t-none")) == ["t1", "t2", "t3"]

    def test_item_cap_takes_the_first_five(self, deployed_scoring: None) -> None:
        """``max_injected_items=5`` — the ranked cap FRE-1021/1041/1053 reason about."""
        rows = [_episode(f"t{i}", 0.90 - i * 0.01) for i in range(8)]

        assert _selected(_run(rows, "t-item-cap")) == ["t0", "t1", "t2", "t3", "t4"]

    def test_candidate_cap_truncates_before_selection(
        self, deployed_scoring: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``max_candidates=10`` bounds what selection ever sees.

        The item cap is lifted above the candidate cap so the latter is the binding
        gate; without that, the item cap would mask it.
        """
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_injected_items", 20)
        rows = [_episode(f"t{i}", 0.90 - i * 0.005) for i in range(14)]

        assert _selected(_run(rows, "t-cand-cap")) == [f"t{i}" for i in range(10)]

    def test_token_budget_stops_mid_list(
        self, deployed_scoring: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cumulative cost stops selection: 3 x 200 fits under 500, the fourth does not."""
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", 500)
        _fixed_tokens(monkeypatch, 200)
        rows = [_episode(f"t{i}", 0.90 - i * 0.01) for i in range(5)]

        assert _selected(_run(rows, "t-budget")) == ["t0", "t1"]

    def test_oversized_payload_is_skipped_not_terminal(
        self, deployed_scoring: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A payload larger than the whole budget is stepped over; later rows survive.

        This is the loop's only ``continue`` branch, and it is why the oversized drop
        cannot be attributed to the terminal gate.
        """
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", 500)
        monkeypatch.setattr(
            proactive_mod,
            "_estimate_payload_tokens",
            lambda p: 900 if p.get("conversation_id") == "t1" else 100,
        )
        rows = [_episode("t0", 0.90), _episode("t1", 0.80), _episode("t2", 0.70)]

        assert _selected(_run(rows, "t-oversized")) == ["t0", "t2"]

    def test_score_floor_is_terminal(self, deployed_scoring: None) -> None:
        """``diminishing_score_floor=0.35`` stops the loop below 0.35.

        The scores are chosen so the *gap* gate cannot fire first: 0.375 then 0.330 is a
        drop of 0.045, well inside the 0.15 gap, so the floor is the binding gate. A
        steeper pair would break on the gap and this case would silently test that
        instead — which is exactly the gate confusion the ticket is about.
        """
        rows = [_episode("t0", 0.50), _episode("t1", 0.40)]

        assert _selected(_run(rows, "t-floor")) == ["t0"]

    def test_score_gap_is_terminal(self, deployed_scoring: None) -> None:
        """A drop over ``diminishing_score_gap=0.15`` vs the previous pick stops the loop.

        0.555 then 0.375 is a gap of 0.18. The floor does not fire here (0.375 > 0.35),
        so this isolates the gap gate.
        """
        rows = [_episode("t0", 0.90), _episode("t1", 0.50)]

        assert _selected(_run(rows, "t-gap")) == ["t0"]

    def test_sub_threshold_rows_never_become_candidates(self, deployed_scoring: None) -> None:
        """``min_score=0.30`` cuts 0.240 before scoring produces a candidate."""
        rows = [_episode("t0", 0.90), _episode("t1", 0.20)]

        assert _selected(_run(rows, "t-threshold")) == ["t0"]

    def test_duplicate_turn_ids_collapse_to_the_first(self, deployed_scoring: None) -> None:
        """Dedupe keeps the first row per ``turn_id``; vector order is best-first."""
        rows = [_episode("t0", 0.90), _episode("t0", 0.80), _episode("t1", 0.70)]

        assert _selected(_run(rows, "t-dupe")) == ["t0", "t1"]


class TestGateAttribution:
    """AC-1 and AC-2 — every discard names the gate that removed it."""

    def test_item_cap_discards_are_named(self, deployed_scoring: None) -> None:
        """AC-2 gate A: the ranked cap. Its losers are drops, not absences."""
        rows = [_episode(f"t{i}", 0.90 - i * 0.01) for i in range(8)]

        out = _run(rows, "t-attr-item-cap")

        assert [d.drop_reason for d in out.discarded] == [DropReason.RECALL_ITEM_CAP] * 3
        assert [d.payload["conversation_id"] for d in out.discarded] == ["t5", "t6", "t7"]

    def test_token_budget_discards_are_named(
        self, deployed_scoring: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-2 gate B: the token budget, distinguishable from the cap above.

        The two are mutually exclusive within one invocation — the loop breaks on the
        first terminal condition — so AC-2's "one of each" is two records, not one.
        """
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", 500)
        _fixed_tokens(monkeypatch, 200)
        rows = [_episode(f"t{i}", 0.90 - i * 0.01) for i in range(5)]

        out = _run(rows, "t-attr-budget")

        assert [d.drop_reason for d in out.discarded] == [DropReason.RECALL_TOKEN_BUDGET] * 3
        assert [d.payload["conversation_id"] for d in out.discarded] == ["t2", "t3", "t4"]

    def test_oversized_and_terminal_gates_are_distinguished(
        self, deployed_scoring: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One invocation, two different reasons: the skipped row and the cut tail.

        The oversized row is at an index below the terminal stop, so it must not be
        re-attributed to the terminal gate.
        """
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", 500)
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_injected_items", 2)
        monkeypatch.setattr(
            proactive_mod,
            "_estimate_payload_tokens",
            lambda p: 900 if p.get("conversation_id") == "t1" else 100,
        )
        rows = [_episode(f"t{i}", 0.90 - i * 0.01) for i in range(5)]

        out = _run(rows, "t-attr-mixed")
        by_id = {d.payload["conversation_id"]: d.drop_reason for d in out.discarded}

        assert _selected(out) == ["t0", "t2"]
        assert by_id["t1"] is DropReason.RECALL_ITEM_OVERSIZED
        assert by_id["t3"] is DropReason.RECALL_ITEM_CAP
        assert by_id["t4"] is DropReason.RECALL_ITEM_CAP

    def test_candidate_cap_discards_are_named(
        self, deployed_scoring: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rows past ``max_candidates`` never reached selection — a distinct gate."""
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_injected_items", 20)
        rows = [_episode(f"t{i}", 0.90 - i * 0.005) for i in range(12)]

        out = _run(rows, "t-attr-cand-cap")

        assert [d.drop_reason for d in out.discarded] == [DropReason.RECALL_CANDIDATE_CAP] * 2
        assert [d.payload["conversation_id"] for d in out.discarded] == ["t10", "t11"]

    def test_score_floor_and_gap_are_distinguished(self, deployed_scoring: None) -> None:
        """Two score gates, two reasons — they demand different remedies.

        Note how close the inputs are: 0.50/0.40 breaks on the floor and 0.90/0.50 on the
        gap. Before this ticket both reported as one unnamed trim.
        """
        floor_out = _run([_episode("t0", 0.50), _episode("t1", 0.40)], "t-f")
        gap_out = _run([_episode("t0", 0.90), _episode("t1", 0.50)], "t-g")

        assert [d.drop_reason for d in floor_out.discarded] == [DropReason.RECALL_SCORE_FLOOR]
        assert [d.drop_reason for d in gap_out.discarded] == [DropReason.RECALL_SCORE_GAP]

    def test_sub_threshold_rows_are_recorded_with_their_score(self, deployed_scoring: None) -> None:
        """The relevance gate is a drop with a score, not an absence.

        The owner's scope call (2026-07-30): every retrieved row is recorded, so a reader
        can name which row the relevance gate removed rather than only how many.
        """
        out = _run([_episode("t0", 0.90), _episode("t1", 0.20)], "t-attr-threshold")

        assert [d.drop_reason for d in out.discarded] == [DropReason.RECALL_SCORE_THRESHOLD]
        assert out.discarded[0].relevance_score == pytest.approx(SCORES[0.20])

    def test_a_duplicate_row_is_not_recorded_as_a_drop(self, deployed_scoring: None) -> None:
        """A dedupe collapse is not a loss, so it must not appear as a discard.

        A duplicate shares the kept row's ``turn_id`` and therefore its identity. Recording
        it as a drop put the same identity in the record twice — once admitted, once
        dropped — asserting that a memory was lost when that memory reached the model, and
        inflating ``candidate_count`` so the census over-reported recall loss. Caught by
        code review; the fix is to record no drop, since nothing was dropped.
        """
        out = _run([_episode("t0", 0.90), _episode("t0", 0.80)], "t-attr-dupe")

        assert _selected(out) == ["t0"]
        assert out.discarded == [], "the collapsed row is not a discarded candidate"

    def test_no_identity_is_ever_both_admitted_and_dropped(self, deployed_scoring: None) -> None:
        """The invariant the duplicate bug violated, asserted directly.

        Stated as a property over the whole result rather than as a duplicate-specific
        case, so any future gate that reuses a surviving row's identity fails here.
        """
        rows = [
            _episode("t0", 0.90),
            _episode("t0", 0.80),  # duplicate of the kept t0
            _episode("t1", 0.20),  # sub-threshold
            _episode("t2", 0.70),
        ]

        out = _run(rows, "t-attr-no-overlap")

        emitted = {c.payload.get("conversation_id") for c in out.candidates}
        dropped = {d.payload.get("conversation_id") for d in out.discarded}
        assert emitted & dropped == set(), f"identity in both sets: {emitted & dropped}"


class TestConservation:
    """Secondary check: bookkeeping loses nothing.

    Deliberately *not* the guard for the ticket's failure clause — this holds even if a
    bug admits one more candidate and discards one fewer. :class:`TestSelectionUnchanged`
    is what pins the survivors.
    """

    @pytest.mark.parametrize(
        ("case", "rows", "max_tokens", "max_items"),
        [
            ("item_cap", [_episode(f"t{i}", 0.90 - i * 0.01) for i in range(8)], 100_000, 5),
            (
                "candidate_cap",
                [_episode(f"t{i}", 0.90 - i * 0.005) for i in range(14)],
                100_000,
                20,
            ),
            ("score_floor", [_episode("t0", 0.50), _episode("t1", 0.40)], 100_000, 5),
            ("score_gap", [_episode("t0", 0.90), _episode("t1", 0.50)], 100_000, 5),
            (
                "threshold",
                [_episode("t0", 0.90), _episode("t1", 0.20), _episode("t2", 0.20)],
                100_000,
                5,
            ),
            (
                "duplicates",
                [_episode("t0", 0.90), _episode("t0", 0.80), _episode("t1", 0.70)],
                100_000,
                5,
            ),
            ("none", [_episode("t0", 0.90), _episode("t1", 0.80)], 100_000, 5),
        ],
    )
    def test_every_deduplicated_row_is_selected_or_discarded(
        self,
        deployed_scoring: None,
        monkeypatch: pytest.MonkeyPatch,
        case: str,
        rows: list[dict[str, Any]],
        max_tokens: int,
        max_items: int,
    ) -> None:
        """No row that could have reached the model may vanish unaccounted for.

        The population is the **deduplicated** rows, not the raw retrieval: a collapsed
        duplicate is the same memory as the row that was kept, so counting it separately
        would double-count one memory (see
        :meth:`TestGateAttribution.test_a_duplicate_row_is_not_recorded_as_a_drop`). The
        retrieval-to-dedupe delta stays visible as counts on the trim event.
        """
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", max_tokens)
        monkeypatch.setattr(
            proactive_mod.settings, "proactive_memory_max_injected_items", max_items
        )
        distinct = len({r["turn_id"] for r in rows})

        out = _run(rows, f"t-conserve-{case}")

        assert len(out.candidates) + len(out.discarded) == distinct


class TestTheTrimEventIsEmitted:
    """The per-turn log surface, which is what a reader reaches for first."""

    def test_the_terminal_gate_is_named_on_the_event(
        self, deployed_scoring: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``stop_reason`` is the field whose absence misled the melon investigation."""
        rows = [_episode(f"t{i}", 0.90 - i * 0.01) for i in range(8)]

        with caplog.at_level("INFO"):
            _run(rows, "t-log-gate")

        events = [r for r in caplog.records if "proactive_memory_budget_trimmed" in r.getMessage()]
        assert len(events) == 1
        assert "recall_item_cap" in events[0].getMessage()

    def test_the_event_does_not_fire_when_selection_trimmed_nothing(
        self, deployed_scoring: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A turn where selection trimmed nothing must not emit ``budget_trimmed``.

        An earlier revision widened the guard to ``if discarded:`` so pre-selection gates
        would show up here too. Code review confirmed that corrupts the series: the event
        is *named* ``budget_trimmed`` and existing consumers read it as the trim signal, so
        firing it with ``before_count == after_count`` and a null ``stop_reason`` puts a
        step change in that series at the deploy boundary with no config change behind it.
        Every scored candidate is emitted here, so nothing was trimmed and nothing is said.
        """
        rows = [_episode("t0", 0.90), _episode("t0", 0.80), _episode("t1", 0.80)]

        with caplog.at_level("INFO"):
            out = _run(rows, "t-log-dupe")

        assert len(out.candidates) == 2, "both distinct rows were emitted"
        assert not [
            r for r in caplog.records if "proactive_memory_budget_trimmed" in r.getMessage()
        ], "no trim happened, so no trim event"

    def test_no_discards_emits_nothing(
        self, deployed_scoring: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A turn that lost nothing must not report a trim."""
        with caplog.at_level("INFO"):
            _run([_episode("t0", 0.90), _episode("t1", 0.80)], "t-log-clean")

        assert not [
            r for r in caplog.records if "proactive_memory_budget_trimmed" in r.getMessage()
        ]
