"""FRE-1477 — the proactive relevance gate (ADR-0148 D4).

FRE-1287 removed the subscore floors and left the weights. The candidate that decides
admission is the *top-ranked non-match* -- on a query whose answer is not in the corpus,
the nearest neighbour the vector index returns is exactly that item -- and at its measured
score, recency still carries it over the 0.30 bar with zero entity overlap and zero topic
hits. FRE-1287's own test never saw this, because it pinned ``vector_score=0.5``, which is
orthogonal, and orthogonal maps to 0.0 under the rescale FRE-1287 itself added.

Every fixture here is driven at the **measured** value instead, read from the committed
calibration (``config/calibration/proactive_relevance_bound.json``) rather than restated,
so no test can drift from the measurement it claims to encode.

**No bound is configured in production.** The calibration reports that the serving arm
does not separate the two populations at the rate ADR-0148 D4 requires, so it reported the
incompatibility and stopped rather than pick a number -- the case D4 reserves. The gate
ships inert. These tests supply the bound the measurement implies, so the mechanism is
proven and ready for the arm or the reranker-side bound the owner chooses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

import personal_agent.memory.proactive as proactive_mod
from personal_agent.captains_log.turn_evidence import DropReason
from personal_agent.config.calibration import RelevanceCalibration, load_relevance_calibration
from personal_agent.config.config_guard import repo_root
from personal_agent.memory.proactive import _normalize_vector_score, build_proactive_suggestions

#: How far above the measured median the implied bound sits. Mirrors the harness's own
#: ``BOUND_STEP`` -- the bound must be *strictly* above the median so that median is
#: rejected (ADR-0148 D4).
_BOUND_STEP = 0.001


@pytest.fixture(scope="module")
def calibration() -> RelevanceCalibration:
    """The committed calibration for the serving embedder arm."""
    loaded = load_relevance_calibration(repo_root())
    assert loaded is not None, "the committed calibration artifact is missing"
    return loaded


@pytest.fixture
def measured_non_match(calibration: RelevanceCalibration) -> float:
    """The median top-ranked non-match, in Neo4j score space, as measured."""
    return calibration.negative_median_neo4j


@pytest.fixture
def implied_bound(measured_non_match: float) -> float:
    """The bound the measurement implies, in normalized embedding-term space.

    Not a configured value -- see the module docstring. This is the number the
    calibration would have committed had the positive constraint also held, and it is the
    number AC-1 asks the gate to reject the measured non-match at.
    """
    return _normalize_vector_score(measured_non_match + _BOUND_STEP)


@pytest.fixture
def deployed_scoring(monkeypatch: pytest.MonkeyPatch, implied_bound: float) -> None:
    """Pin the deployed proactive configuration, with the implied bound in force."""
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
        ("proactive_memory_relevance_bound", implied_bound),
        ("proactive_memory_relevance_gate_enabled", True),
    ):
        monkeypatch.setattr(s, name, value, raising=False)


def _now_iso() -> str:
    """A same-instant timestamp, so recency sits at its maximum of ~1.0.

    Computed rather than hardcoded: a literal date silently decays as the calendar moves,
    and a fixture whose recency quietly falls stops testing the case it was written for.
    """
    return datetime.now(timezone.utc).isoformat()


def _entity_row(
    *,
    name: str = "Irrelevant",
    vector_score: float,
    vector_score_measured: bool = True,
    timestamp_iso: str | None = None,
    key_entities: list[str] | None = None,
    description: str = "some description",
) -> dict[str, Any]:
    """A turnless entity row, so it never picks up an episode sibling."""
    return {
        "name": name,
        "entity_type": "Thing",
        "description": description,
        "vector_score": vector_score,
        "vector_score_measured": vector_score_measured,
        "timestamp_iso": timestamp_iso if timestamp_iso is not None else _now_iso(),
        "key_entities": key_entities or [],
    }


class TestAC1RejectionAtTheMeasuredNonMatch:
    """AC-1 — the gate rejects at the measured non-match, not at orthogonal."""

    def test_the_median_top_ranked_non_match_is_not_admitted(
        self, deployed_scoring: None, measured_non_match: float
    ) -> None:
        row = _entity_row(vector_score=measured_non_match)
        out = build_proactive_suggestions([row], set(), "completely unrelated hint", "t-ac1", None)
        assert out.candidates == []

    def test_the_fixture_carries_no_overlap_and_no_topic_evidence(
        self, deployed_scoring: None, measured_non_match: float
    ) -> None:
        """The gate must be what rejects it -- not a missing subscore elsewhere."""
        row = _entity_row(vector_score=measured_non_match)
        out = build_proactive_suggestions([row], set(), "completely unrelated hint", "t-ac1b", None)
        assert len(out.discarded) == 1
        assert out.discarded[0].drop_reason is DropReason.RECALL_RELEVANCE_BOUND


class TestAC2TheTestCanFail:
    """AC-2 — a test that passes both before and after the change proves nothing."""

    def test_the_same_fixture_is_admitted_with_the_gate_disabled(
        self,
        deployed_scoring: None,
        monkeypatch: pytest.MonkeyPatch,
        measured_non_match: float,
    ) -> None:
        monkeypatch.setattr(
            proactive_mod.settings, "proactive_memory_relevance_gate_enabled", False
        )
        row = _entity_row(vector_score=measured_non_match)
        out = build_proactive_suggestions([row], set(), "completely unrelated hint", "t-ac2", None)
        assert len(out.candidates) == 1

    def test_recency_is_what_carries_it_over_the_bar(
        self,
        deployed_scoring: None,
        monkeypatch: pytest.MonkeyPatch,
        measured_non_match: float,
    ) -> None:
        """The ticket's claim, arithmetically: at the measured non-match the candidate
        clears the 0.30 bar only because recency is in the sum being compared.
        """
        monkeypatch.setattr(
            proactive_mod.settings, "proactive_memory_relevance_gate_enabled", False
        )
        row = _entity_row(vector_score=measured_non_match)
        out = build_proactive_suggestions([row], set(), "completely unrelated hint", "t-ac2b", None)
        embedding_alone = 0.45 * _normalize_vector_score(measured_non_match)
        assert embedding_alone < 0.30
        assert out.candidates[0].relevance_score >= 0.30


class TestAC3TheGatedValueIsTheScorersOwnOutput:
    """AC-3 — the predicate compares ``_normalize_vector_score``'s output.

    A recording wrapper would prove nothing: the score combination already calls the
    normalizer, so merely observing a call passes even when the gate reads the raw row
    value. This uses a sentinel instead -- the patched normalizer returns a value that
    *flips* the decision. A gate reading the normalizer admits; a gate reading the raw row
    rejects.

    The rejecting direction asserts the **drop reason**, never bare emptiness. The
    deployed ``diminishing_score_floor`` of 0.35 also removes a weak candidate, so an
    assertion on an empty candidate list can be satisfied by that floor while the gate
    does nothing -- both sentinel tests passed against the ungated code before this was
    tightened.
    """

    def test_the_gate_admits_when_the_normalizer_returns_above_the_bound(
        self,
        deployed_scoring: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sentinel = 1.0
        monkeypatch.setattr(proactive_mod, "_normalize_vector_score", lambda _score: sentinel)
        row = _entity_row(vector_score=0.0)  # raw value far below any bound
        out = build_proactive_suggestions([row], set(), "completely unrelated hint", "t-ac3", None)
        assert len(out.candidates) == 1, "the gate did not read the normalizer's output"

    def test_the_gate_rejects_when_the_normalizer_returns_below_the_bound(
        self,
        deployed_scoring: None,
        monkeypatch: pytest.MonkeyPatch,
        implied_bound: float,
    ) -> None:
        """The other direction, asserting the gate is what rejected it."""
        sentinel = max(0.0, implied_bound - 0.1)
        monkeypatch.setattr(proactive_mod, "_normalize_vector_score", lambda _score: sentinel)
        row = _entity_row(vector_score=1.0)  # raw value above any bound
        out = build_proactive_suggestions([row], set(), "completely unrelated hint", "t-ac3b", None)
        assert out.candidates == []
        assert out.discarded[0].drop_reason is DropReason.RECALL_RELEVANCE_BOUND


class TestAC6ARelevanceRejectionIsSeparable:
    """AC-6 — a relevance rejection must not read as a threshold rejection."""

    def test_the_drop_reason_is_the_relevance_bound(
        self, deployed_scoring: None, measured_non_match: float
    ) -> None:
        row = _entity_row(vector_score=measured_non_match)
        out = build_proactive_suggestions([row], set(), "unrelated hint", "t-ac6", None)
        assert out.discarded[0].drop_reason is DropReason.RECALL_RELEVANCE_BOUND

    def test_the_drop_reason_is_not_the_score_threshold(
        self, deployed_scoring: None, measured_non_match: float
    ) -> None:
        row = _entity_row(vector_score=measured_non_match)
        out = build_proactive_suggestions([row], set(), "unrelated hint", "t-ac6b", None)
        assert out.discarded[0].drop_reason is not DropReason.RECALL_SCORE_THRESHOLD

    def test_no_score_is_recorded_because_none_was_computed(
        self, deployed_scoring: None, measured_non_match: float
    ) -> None:
        """The gate fires ahead of the combination, so there is no final score to carry.
        ``ProactiveMemoryDiscard.relevance_score`` reserves None for exactly this.
        """
        row = _entity_row(vector_score=measured_non_match)
        out = build_proactive_suggestions([row], set(), "unrelated hint", "t-ac6c", None)
        assert out.discarded[0].relevance_score is None


def _admitted_positives(
    monkeypatch: pytest.MonkeyPatch, calibration: RelevanceCalibration, *, gate_on: bool
) -> set[int]:
    """Indices of the labelled positives the path admits, one probe per turn.

    One row per turn deliberately: the candidate cap and the item cap would otherwise let
    an aggregate hold while individual probes swap places behind it.
    """
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_relevance_gate_enabled", gate_on)
    admitted: set[int] = set()
    for index, score in enumerate(calibration.positive_scores):
        row = _entity_row(name=f"Positive{index}", vector_score=score)
        result = build_proactive_suggestions([row], set(), "unrelated hint", f"t-ac7-{index}", None)
        if result.candidates:
            admitted.add(index)
    return admitted


class TestAC7RelevantCandidatesAreStillAdmitted:
    """AC-7 — per probe, not in aggregate.

    ADR-0148 AC-6 rejects an aggregate that holds while individual probes swap outcomes,
    so each labelled positive is driven as its own single-row turn.

    The criterion is asserted against the **shipped** configuration, which carries no
    bound. Asserting it against the implied bound instead would be asserting a
    configuration the calibration explicitly refused -- and the second test below is the
    measurement of why.
    """

    def test_no_labelled_positive_loses_admission_as_shipped(
        self,
        deployed_scoring: None,
        monkeypatch: pytest.MonkeyPatch,
        calibration: RelevanceCalibration,
    ) -> None:
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_relevance_bound", None)
        before = _admitted_positives(monkeypatch, calibration, gate_on=False)
        after = _admitted_positives(monkeypatch, calibration, gate_on=True)
        assert before - after == set(), f"probes lost admission: {sorted(before - after)}"

    def test_the_implied_bound_would_suppress_genuine_relevance(
        self,
        deployed_scoring: None,
        monkeypatch: pytest.MonkeyPatch,
        calibration: RelevanceCalibration,
    ) -> None:
        """Why no bound is configured, measured through the production code path.

        The calibration reported that the lowest bound rejecting the median top-ranked
        non-match admits under 90% of the labelled positives. This drives that same bound
        through ``build_proactive_suggestions`` and confirms the loss is real rather than
        an artefact of the harness's own arithmetic -- an independent check on the
        calibration, through the code the bound would actually govern.
        """
        before = _admitted_positives(monkeypatch, calibration, gate_on=False)
        after = _admitted_positives(monkeypatch, calibration, gate_on=True)
        assert before - after, (
            "the implied bound suppressed no labelled positive, so the calibration's "
            "reported incompatibility does not reproduce through the production path"
        )
        assert calibration.incompatible is True
        assert calibration.bound_neo4j_space is None


class TestConservation:
    """Every deduplicated candidate is either emitted or recorded as discarded."""

    def test_a_gated_candidate_is_conserved_in_the_record(
        self, deployed_scoring: None, measured_non_match: float, calibration: RelevanceCalibration
    ) -> None:
        rows = [
            _entity_row(name="Rejected", vector_score=measured_non_match),
            _entity_row(name="Admitted", vector_score=max(calibration.positive_scores)),
        ]
        out = build_proactive_suggestions(rows, set(), "unrelated hint", "t-cons", None)
        assert len(out.candidates) + len(out.discarded) == len(rows)


class TestOverlapAndTopicEvidenceExemptTheCandidate:
    """The gate binds only where there is no relevance evidence at all (ADR-0148 D4)."""

    def test_entity_overlap_exempts_a_low_scoring_candidate(
        self, deployed_scoring: None, measured_non_match: float
    ) -> None:
        row = _entity_row(
            name="Shared", vector_score=measured_non_match, key_entities=["Shared", "A", "B"]
        )
        out = build_proactive_suggestions(
            [row], {"Shared", "A", "B"}, "unrelated hint", "t-overlap", None
        )
        assert len(out.candidates) == 1

    def test_a_topic_hit_exempts_a_low_scoring_candidate(
        self, deployed_scoring: None, measured_non_match: float
    ) -> None:
        row = _entity_row(name="Neo4j", vector_score=measured_non_match, key_entities=["Neo4j"])
        out = build_proactive_suggestions([row], set(), "neo4j graph database", "t-topic", None)
        assert len(out.candidates) == 1


class TestDGAConstantIsNotRelevanceEvidence:
    """D-G — the FRE-724 lexical augment enters candidates carrying a *constant*.

    ``_augment_proactive_with_lexical`` (``service.py:1124``) appends lexical-arm entity
    hits with ``vector_score = recall_similarity_floor``, a configuration value rather
    than a measurement. Production runs that path. Judging such a candidate against a
    calibrated relevance bound would decide admission on a number that is not a relevance
    measurement at all -- the pathology ADR-0148 D4 exists to stop -- so the row carries
    its provenance and the gate treats an unmeasured score as no evidence.
    """

    def test_an_unmeasured_score_is_rejected_however_high_the_constant(
        self, deployed_scoring: None
    ) -> None:
        row = _entity_row(vector_score=0.99, vector_score_measured=False)
        out = build_proactive_suggestions([row], set(), "unrelated hint", "t-dg", None)
        assert out.candidates == []
        assert out.discarded[0].drop_reason is DropReason.RECALL_RELEVANCE_BOUND

    def test_a_measured_score_at_the_same_value_is_admitted(self, deployed_scoring: None) -> None:
        """The provenance flag is what decides, not the value."""
        row = _entity_row(vector_score=0.99, vector_score_measured=True)
        out = build_proactive_suggestions([row], set(), "unrelated hint", "t-dg2", None)
        assert len(out.candidates) == 1

    def test_an_unmeasured_score_with_entity_overlap_is_still_admitted(
        self, deployed_scoring: None
    ) -> None:
        """A lexical hit that also shares session entities has evidence of its own."""
        row = _entity_row(
            name="Shared",
            vector_score=0.60,
            vector_score_measured=False,
            key_entities=["Shared", "A", "B"],
        )
        out = build_proactive_suggestions(
            [row], {"Shared", "A", "B"}, "unrelated hint", "t-dg3", None
        )
        assert len(out.candidates) == 1


class TestTheGateIsInertWithoutACalibratedBound:
    """ADR-0148 D4: a missing calibration never silently defaults to zero.

    This is production's current state -- the serving arm did not separate the two
    populations at the required rate, so no bound is configured.
    """

    def test_no_bound_admits_exactly_as_before(
        self, deployed_scoring: None, monkeypatch: pytest.MonkeyPatch, measured_non_match: float
    ) -> None:
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_relevance_bound", None)
        row = _entity_row(vector_score=measured_non_match)
        out = build_proactive_suggestions([row], set(), "unrelated hint", "t-inert", None)
        assert len(out.candidates) == 1

    def test_the_shipped_configuration_has_no_bound(self) -> None:
        """Guards against a bound being configured in the case ADR-0148 D4 reserves."""
        from personal_agent.config.settings import AppConfig

        assert AppConfig().proactive_memory_relevance_bound is None
