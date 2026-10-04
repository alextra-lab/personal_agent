"""FRE-1545 — the proactive reranker bound keeps true matches and traces to its artifact.

AC-1: the configured bound, applied to the whole proactive labelled positive set, admits at
least 90% and rejects the median top-ranked non-match. AC-5: the bound traces to a committed
artifact naming the serving reranker, the date and the probe set, and ``config_guard`` raises
a finding when the artifact names a component that is not serving.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

import pytest

from personal_agent.config.calibration import (
    CALIBRATION_DIR,
    PROACTIVE_RERANK_RELEVANCE_BOUND_FILE,
    RerankerRelevanceCalibration,
    load_reranker_calibration,
)
from personal_agent.config.config_guard import check_proactive_rerank_bound_calibration, repo_root
from personal_agent.config.model_loader import resolve_role_definition
from personal_agent.config.settings import AppConfig

SERVING = "rerank-2.5"


@pytest.fixture(scope="module")
def committed() -> RerankerRelevanceCalibration:
    """The committed proactive reranker calibration."""
    loaded = load_reranker_calibration(repo_root(), PROACTIVE_RERANK_RELEVANCE_BOUND_FILE)
    assert loaded is not None, "the committed calibration artifact is missing"
    return loaded


class TestAC1TheBoundKeepsTrueMatches:
    """The configured bound, against the committed populations it was chosen on."""

    def test_admits_at_least_ninety_percent_of_the_whole_positive_set(
        self, committed: RerankerRelevanceCalibration
    ) -> None:
        bound = AppConfig().proactive_memory_rerank_relevance_bound
        assert bound is not None
        admitted = sum(1 for s in committed.positive_scores if s >= bound)
        share = admitted / len(committed.positive_scores)
        assert share >= 0.90, f"admits {share:.1%} of {len(committed.positive_scores)}"

    def test_rejects_the_median_top_ranked_non_match(
        self, committed: RerankerRelevanceCalibration
    ) -> None:
        bound = AppConfig().proactive_memory_rerank_relevance_bound
        assert bound is not None
        median = statistics.median(committed.negative_scores)
        assert median == pytest.approx(committed.negative_median_rerank, abs=1e-6)
        assert median < bound

    def test_both_candidate_kinds_were_measured(
        self, committed: RerankerRelevanceCalibration
    ) -> None:
        """The gate binds entity and episode candidates, so both populations are present."""
        assert committed.entity_positive_scores
        assert committed.turn_positive_scores
        assert len(committed.entity_positive_scores) + len(committed.turn_positive_scores) == len(
            committed.positive_scores
        )


class TestAC5TheBoundTracesToTheArtifact:
    """The artifact names what it measured, and the configured value is its value."""

    def test_names_the_serving_reranker_the_date_and_the_probe_set(
        self, committed: RerankerRelevanceCalibration
    ) -> None:
        serving = resolve_role_definition("reranker")
        assert serving is not None
        assert committed.component.role == "reranker"
        assert committed.component.model == serving.id
        assert committed.measured_on.isoformat() == "2026-10-04"
        assert (repo_root() / committed.probe_set).is_file()
        assert committed.incompatible is False

    def test_the_configured_bound_is_the_artifact_bound(
        self, committed: RerankerRelevanceCalibration
    ) -> None:
        assert AppConfig().proactive_memory_rerank_relevance_bound == committed.bound

    def test_the_committed_state_raises_no_finding(self) -> None:
        assert check_proactive_rerank_bound_calibration(repo_root()) == []


def _artifact(**overrides: Any) -> dict[str, Any]:
    """A well-formed reranker calibration payload, compatible unless overridden."""
    payload: dict[str, Any] = {
        "component": {"role": "reranker", "model": SERVING, "dimensions": 1},
        "measured_on": "2026-10-04",
        "probe_set": "scripts/eval/fre435_memory_recall/semantic_probe.yaml",
        "incompatible": False,
        "incompatible_reason": None,
        "bound": 0.42,
        "positive_scores": [0.9, 0.8, 0.7, 0.45],
        "negative_scores": [0.41, 0.30, 0.25],
        "entity_positive_scores": [0.9, 0.8],
        "turn_positive_scores": [0.7, 0.45],
        "positive_admitted_share": 1.0,
        "negative_median_rerank": 0.30,
        "parity": {
            "tolerance": 0.02,
            "p1_production_aggregates": {"pos_median": 0.75, "neg_median": 0.30},
            "p1_independent_aggregates": {"pos_median": 0.74, "neg_median": 0.31},
            "sanity_relevant_score": 0.88,
            "sanity_irrelevant_score": 0.02,
            "listwise_max_delta": 0.004,
        },
    }
    payload.update(overrides)
    return payload


def _write(root: Path, payload: dict[str, Any] | str) -> None:
    path = root / CALIBRATION_DIR / PROACTIVE_RERANK_RELEVANCE_BOUND_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def _settings(bound: float | None) -> AppConfig:
    return AppConfig(proactive_memory_rerank_relevance_bound=bound)


class TestTheGuard:
    """Every configured proactive reranker bound traces to a live calibration."""

    def test_an_artifact_naming_a_component_not_serving_is_a_finding(self, tmp_path: Path) -> None:
        """AC-5's own failure condition. The configured value is left in force."""
        _write(
            tmp_path,
            _artifact(component={"role": "reranker", "model": "rerank-2.5-lite", "dimensions": 1}),
        )
        settings = _settings(0.42)
        findings = check_proactive_rerank_bound_calibration(tmp_path, settings)
        assert "proactive_rerank_bound_calibration_stale" in {f.check for f in findings}
        assert settings.proactive_memory_rerank_relevance_bound == 0.42

    def test_matching_bound_and_artifact_is_silent(self, tmp_path: Path) -> None:
        _write(tmp_path, _artifact())
        assert check_proactive_rerank_bound_calibration(tmp_path, _settings(0.42)) == []

    def test_configured_bound_with_no_artifact_is_a_finding(self, tmp_path: Path) -> None:
        findings = check_proactive_rerank_bound_calibration(tmp_path, _settings(0.42))
        assert [f.check for f in findings] == ["proactive_rerank_bound_calibration_missing"]

    def test_no_bound_and_no_artifact_is_silent(self, tmp_path: Path) -> None:
        assert check_proactive_rerank_bound_calibration(tmp_path, _settings(None)) == []

    def test_mismatched_bound_is_a_finding(self, tmp_path: Path) -> None:
        _write(tmp_path, _artifact())
        findings = check_proactive_rerank_bound_calibration(tmp_path, _settings(0.55))
        assert "proactive_rerank_bound_calibration_mismatch" in {f.check for f in findings}

    def test_malformed_artifact_is_a_finding(self, tmp_path: Path) -> None:
        _write(tmp_path, "{not json")
        findings = check_proactive_rerank_bound_calibration(tmp_path, _settings(0.42))
        assert [f.check for f in findings] == ["proactive_rerank_bound_calibration_malformed"]

    def test_a_bound_configured_despite_incompatibility_is_a_finding(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            _artifact(
                incompatible=True,
                incompatible_reason="no bound satisfies both constraints",
                bound=None,
                positive_admitted_share=None,
            ),
        )
        findings = check_proactive_rerank_bound_calibration(tmp_path, _settings(0.42))
        assert "proactive_rerank_bound_configured_despite_incompatibility" in {
            f.check for f in findings
        }
