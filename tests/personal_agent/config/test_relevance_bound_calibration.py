"""FRE-1477 — the committed calibration, and the guard binding it to configuration.

ADR-0148 D4 requires that a configured relevance bound trace to a calibration measured
against the component that actually produced its scores, and that a missing or stale
calibration leave the previous bound in force while raising a finding. These tests assert
both halves against the artifact this repository actually commits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.eval.fre1477_relevance_calibration.analysis import (
    MIN_POSITIVE_SHARE,
    admitted_share,
    aggregate_deltas,
    pair_deltas,
    parity_aggregates,
)

from personal_agent.config.calibration import (
    PROACTIVE_RELEVANCE_BOUND_FILE,
    RelevanceCalibration,
    calibration_path,
    load_relevance_calibration,
)
from personal_agent.config.config_guard import check_relevance_bound_calibration, repo_root
from personal_agent.config.settings import AppConfig


@pytest.fixture(scope="module")
def calibration() -> RelevanceCalibration:
    """The committed calibration for the serving embedder arm."""
    loaded = load_relevance_calibration(repo_root())
    assert loaded is not None, "the committed calibration artifact is missing"
    return loaded


def _write_variant(tmp_path: Path, **overrides: object) -> Path:
    """Write a copy of the committed artifact with fields replaced, under a fake root."""
    payload = json.loads(calibration_path(repo_root()).read_text(encoding="utf-8"))
    payload.update(overrides)
    root = tmp_path / "repo"
    target = calibration_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return root


class TestTheArtifactExistsAndDescribesItsMeasurement:
    """AC-5 — the artifact names its component and the date it measured."""

    def test_the_artifact_is_committed_and_parses(self, calibration: RelevanceCalibration) -> None:
        assert calibration_path(repo_root()).name == PROACTIVE_RELEVANCE_BOUND_FILE

    def test_it_names_the_serving_embedder_arm(self, calibration: RelevanceCalibration) -> None:
        settings = AppConfig()
        assert calibration.component.role == "embedder"
        assert calibration.component.model == settings.managed_embedding_model
        assert calibration.component.dimensions == settings.embedding_dimensions

    def test_it_names_the_date_and_the_probe_set(self, calibration: RelevanceCalibration) -> None:
        assert calibration.measured_on.isoformat() >= "2026-09-10"
        assert Path(calibration.probe_set).is_file()

    def test_it_records_both_score_populations_whole(
        self, calibration: RelevanceCalibration
    ) -> None:
        """AC-4's denominator is every labelled positive, so the population is stored
        rather than a share that could be re-cut after the fact.
        """
        assert len(calibration.positive_scores) > 0
        assert len(calibration.negative_scores) > 0


class TestTheParityAssertionReRuns:
    """AC-5 — the recorded parity assertion re-runs.

    Recomputed from the recorded populations and score pairs, never read back as a stored
    delta compared against a stored tolerance -- that would assert nothing.
    """

    def test_p1_aggregates_recompute_from_the_recorded_populations(
        self, calibration: RelevanceCalibration
    ) -> None:
        recomputed = parity_aggregates(calibration.positive_scores, calibration.negative_scores)
        recorded = calibration.parity.p1_index_aggregates
        for metric, value in recomputed.items():
            assert value == pytest.approx(recorded[metric], abs=1e-5)

    def test_p1_holds_within_the_recorded_tolerance(
        self, calibration: RelevanceCalibration
    ) -> None:
        deltas = aggregate_deltas(
            calibration.parity.p1_index_aggregates, calibration.parity.p1_offline_aggregates
        )
        assert max(deltas.values()) <= calibration.parity.tolerance

    def test_p2_deltas_recompute_from_the_recorded_pairs(
        self, calibration: RelevanceCalibration
    ) -> None:
        deltas = pair_deltas(calibration.parity.p2_pairs)
        assert max(deltas) <= calibration.parity.tolerance

    def test_the_negative_median_matches_its_own_population(
        self, calibration: RelevanceCalibration
    ) -> None:
        """The value AC-1's fixture is driven at must be the population's own median."""
        recomputed = parity_aggregates(calibration.positive_scores, calibration.negative_scores)
        assert calibration.negative_median_neo4j == pytest.approx(
            recomputed["neg_median"], abs=1e-5
        )


class TestAC4TheBoundDoesNotSuppressGenuineRelevance:
    """AC-4 — both halves must hold, or no bound may be configured.

    The serving arm did not satisfy them. These tests assert the calibration reported that
    rather than choosing a number, and that the shortfall is real when recomputed from the
    recorded populations.
    """

    def test_the_calibration_reports_the_incompatibility(
        self, calibration: RelevanceCalibration
    ) -> None:
        assert calibration.incompatible is True
        assert calibration.incompatible_reason

    def test_no_bound_was_chosen(self, calibration: RelevanceCalibration) -> None:
        assert calibration.bound_neo4j_space is None
        assert calibration.bound_embedding_term is None
        assert calibration.positive_admitted_share is None

    def test_no_bound_is_configured_either(self) -> None:
        """AC-4 fails a bound configured in the case ADR-0148 D4 reserves."""
        assert AppConfig().proactive_memory_relevance_bound is None

    def test_the_shortfall_recomputes_from_the_recorded_populations(
        self, calibration: RelevanceCalibration
    ) -> None:
        """The lowest bound that rejects the median admits under 90% of the positives."""
        lowest = calibration.negative_median_neo4j + 0.001
        assert admitted_share(calibration.positive_scores, lowest) < MIN_POSITIVE_SHARE

    def test_the_median_non_match_is_not_separable_from_the_positives(
        self, calibration: RelevanceCalibration
    ) -> None:
        """The clouds overlap: FRE-694's verdict, reproduced on the deployed arm."""
        assert (
            max(calibration.negative_scores)
            > sorted(calibration.positive_scores)[len(calibration.positive_scores) // 2]
        )


class TestTheGuardOnTheCommittedState:
    """The guard's findings on what this repository actually commits.

    A standing incompatibility with no bound configured is a measurement awaiting an
    owner decision, not a repository violation. ``scripts/check_config.py`` fails CI on
    any finding at all, so raising one here would wedge every build until the owner acts.
    """

    def test_the_committed_state_is_clean(self) -> None:
        assert check_relevance_bound_calibration(repo_root()) == []

    def test_configuring_a_bound_against_it_is_a_violation(self) -> None:
        """ADR-0148 AC-5 fails a bound configured in the case D4 reserves."""
        settings = AppConfig(proactive_memory_relevance_bound=0.36)
        findings = check_relevance_bound_calibration(repo_root(), settings)
        assert "relevance_bound_configured_despite_incompatibility" in {f.check for f in findings}


class TestAC5TheGuardCatchesAStaleOrAbsentCalibration:
    """AC-5 — an induced mismatch raises a finding, and leaves the bound in force."""

    def test_an_arm_other_than_the_serving_one_raises_a_staleness_finding(
        self, tmp_path: Path
    ) -> None:
        root = _write_variant(
            tmp_path,
            component={"role": "embedder", "model": "Qwen3-Embedding-0.6B", "dimensions": 1024},
        )
        findings = check_relevance_bound_calibration(root)
        assert "relevance_bound_calibration_stale" in {f.check for f in findings}

    def test_a_dimension_other_than_the_serving_one_raises_it_too(self, tmp_path: Path) -> None:
        """Separation is dimension-dependent on this very model (FRE-694), so a matching
        model name at the wrong width is still the wrong measurement.
        """
        settings = AppConfig()
        root = _write_variant(
            tmp_path,
            component={
                "role": "embedder",
                "model": settings.managed_embedding_model,
                "dimensions": 4096,
            },
        )
        findings = check_relevance_bound_calibration(root)
        assert "relevance_bound_calibration_stale" in {f.check for f in findings}

    def test_the_induced_mismatch_leaves_the_configured_bound_untouched(
        self, tmp_path: Path
    ) -> None:
        """The guard reports; it never mutates settings. That is what "leaves the previous
        bound in force" means in practice.
        """
        settings = AppConfig()
        before = settings.proactive_memory_relevance_bound
        root = _write_variant(
            tmp_path,
            component={"role": "embedder", "model": "Some-Other-Model", "dimensions": 512},
        )
        check_relevance_bound_calibration(root, settings)
        assert settings.proactive_memory_relevance_bound == before

    def test_a_configured_bound_with_no_artifact_raises(self, tmp_path: Path) -> None:
        settings = AppConfig(proactive_memory_relevance_bound=0.4)
        findings = check_relevance_bound_calibration(tmp_path / "empty", settings)
        assert [f.check for f in findings] == ["relevance_bound_calibration_missing"]

    def test_no_bound_and_no_artifact_raises_nothing(self, tmp_path: Path) -> None:
        """Not yet calibrated is a state, not a violation."""
        settings = AppConfig(proactive_memory_relevance_bound=None)
        assert check_relevance_bound_calibration(tmp_path / "empty", settings) == []

    def test_a_bound_disagreeing_with_the_artifact_raises(self, tmp_path: Path) -> None:
        root = _write_variant(
            tmp_path,
            incompatible=False,
            incompatible_reason=None,
            bound_neo4j_space=0.7,
            bound_embedding_term=0.4,
            positive_admitted_share=0.95,
        )
        settings = AppConfig(proactive_memory_relevance_bound=0.31)
        findings = check_relevance_bound_calibration(root, settings)
        assert "relevance_bound_calibration_mismatch" in {f.check for f in findings}

    def test_an_agreeing_bound_raises_nothing(self, tmp_path: Path) -> None:
        """The guard must be able to pass, or its failures prove nothing."""
        root = _write_variant(
            tmp_path,
            incompatible=False,
            incompatible_reason=None,
            bound_neo4j_space=0.7,
            bound_embedding_term=0.4,
            positive_admitted_share=0.95,
        )
        settings = AppConfig(proactive_memory_relevance_bound=0.4)
        assert check_relevance_bound_calibration(root, settings) == []

    def test_a_malformed_artifact_is_never_read_as_an_absent_one(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        target = calibration_path(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not json", encoding="utf-8")
        findings = check_relevance_bound_calibration(root, AppConfig())
        assert [f.check for f in findings] == ["relevance_bound_calibration_malformed"]
