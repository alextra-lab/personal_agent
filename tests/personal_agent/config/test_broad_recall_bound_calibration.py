"""FRE-1479 — the broad-recall bound traces to a committed reranker calibration (ADR-0148 AC-10).

The reranker sibling of ``test_relevance_bound_calibration.py``. It is a separate check
rather than a widened one because the two bounds live in incomparable score spaces
(FRE-695) and are measured against different components, so a single check would have to
decide which component a finding referred to.

The criterion these serve is ADR-0148 AC-10: *"a hand-written constant that happens to
satisfy AC-4 and AC-5 fails here, which is the point."*
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from personal_agent.config.calibration import (
    BROAD_RECALL_RELEVANCE_BOUND_FILE,
    CALIBRATION_DIR,
    RerankerRelevanceCalibration,
    load_reranker_calibration,
)
from personal_agent.config.config_guard import check_broad_recall_bound_calibration
from personal_agent.config.settings import AppConfig

SERVING = "rerank-2.5"


def _artifact(**overrides: Any) -> dict[str, Any]:
    """A well-formed reranker calibration payload, compatible unless overridden."""
    payload: dict[str, Any] = {
        "component": {"role": "reranker", "model": SERVING, "dimensions": 1},
        "measured_on": "2026-09-10",
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
            "p1_production_aggregates": {"pos_median": 0.75, "neg_median": 0.30, "neg_max": 0.41},
            "p1_independent_aggregates": {"pos_median": 0.74, "neg_median": 0.31, "neg_max": 0.40},
            "sanity_relevant_score": 0.88,
            "sanity_irrelevant_score": 0.02,
            "listwise_max_delta": 0.004,
        },
    }
    payload.update(overrides)
    return payload


def _write(root: Path, payload: dict[str, Any] | str) -> None:
    path = root / CALIBRATION_DIR / BROAD_RECALL_RELEVANCE_BOUND_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def _settings(bound: float | None) -> AppConfig:
    return AppConfig(broad_recall_relevance_bound=bound)


class TestTheLoader:
    """The artifact parses, and a malformed one is never mistaken for an absent one."""

    def test_absent_artifact_is_none(self, tmp_path: Path) -> None:
        assert load_reranker_calibration(tmp_path) is None

    def test_well_formed_artifact_parses(self, tmp_path: Path) -> None:
        _write(tmp_path, _artifact())
        calibration = load_reranker_calibration(tmp_path)
        assert isinstance(calibration, RerankerRelevanceCalibration)
        assert calibration.bound == 0.42
        assert calibration.component.model == SERVING

    def test_malformed_artifact_raises_rather_than_reading_as_absent(self, tmp_path: Path) -> None:
        """A corrupted measurement must never silently downgrade to 'not calibrated yet'."""
        _write(tmp_path, "{not json")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_reranker_calibration(tmp_path)

    def test_the_two_artifacts_do_not_share_a_schema(self, tmp_path: Path) -> None:
        """The embedder artifact's Neo4j-space fields are not accepted here, and vice versa.

        Guards the mistake a shared base class would have invited: a bound in one score
        space validating against the other's model (FRE-695).
        """
        embedder_shaped = _artifact()
        del embedder_shaped["bound"]
        del embedder_shaped["negative_median_rerank"]
        embedder_shaped["bound_neo4j_space"] = 0.68
        embedder_shaped["bound_embedding_term"] = 0.36
        embedder_shaped["negative_median_neo4j"] = 0.679
        _write(tmp_path, embedder_shaped)
        with pytest.raises(ValueError):
            load_reranker_calibration(tmp_path)


class TestTheGuard:
    """AC-6: every configured bound traces to a live calibration, or a finding is raised."""

    def test_no_bound_and_no_artifact_is_silent(self, tmp_path: Path) -> None:
        assert check_broad_recall_bound_calibration(tmp_path, _settings(None)) == []

    def test_configured_bound_with_no_artifact_is_a_finding(self, tmp_path: Path) -> None:
        """The hand-written constant ADR-0148 AC-10 exists to fail on."""
        findings = check_broad_recall_bound_calibration(tmp_path, _settings(0.42))
        assert [f.check for f in findings] == ["broad_recall_bound_calibration_missing"]

    def test_matching_bound_and_artifact_is_silent(self, tmp_path: Path) -> None:
        _write(tmp_path, _artifact())
        assert check_broad_recall_bound_calibration(tmp_path, _settings(0.42)) == []

    def test_mismatched_bound_is_a_finding(self, tmp_path: Path) -> None:
        _write(tmp_path, _artifact())
        findings = check_broad_recall_bound_calibration(tmp_path, _settings(0.55))
        assert "broad_recall_bound_calibration_mismatch" in {f.check for f in findings}

    def test_induced_component_mismatch_is_a_finding(self, tmp_path: Path) -> None:
        """AC-6's own induced case: set the artifact's component to one not serving.

        The configured value is deliberately unchanged by the check — ADR-0148 D4 requires
        the previous bound to stay in force while the staleness finding stands.
        """
        _write(
            tmp_path,
            _artifact(component={"role": "reranker", "model": "rerank-2.5-lite", "dimensions": 1}),
        )
        settings = _settings(0.42)
        findings = check_broad_recall_bound_calibration(tmp_path, settings)
        assert "broad_recall_bound_calibration_stale" in {f.check for f in findings}
        assert settings.broad_recall_relevance_bound == 0.42

    def test_bound_configured_despite_incompatibility_is_a_finding(self, tmp_path: Path) -> None:
        """ADR-0148 AC-5 fails a bound configured in D4's reserved case."""
        _write(
            tmp_path,
            _artifact(
                incompatible=True,
                incompatible_reason="no bound satisfies both constraints.",
                bound=None,
                positive_admitted_share=None,
            ),
        )
        findings = check_broad_recall_bound_calibration(tmp_path, _settings(0.42))
        names = {f.check for f in findings}
        assert "broad_recall_bound_configured_despite_incompatibility" in names

    def test_standing_incompatibility_with_no_bound_is_not_a_finding(self, tmp_path: Path) -> None:
        """FRE-1477's rule, restated: check_config.py fails CI on any finding at all.

        A measured incompatibility awaiting an owner decision is a result, not a repository
        violation, and making it a finding would wedge every build until the owner acts.
        """
        _write(
            tmp_path,
            _artifact(
                incompatible=True,
                incompatible_reason="no bound satisfies both constraints.",
                bound=None,
                positive_admitted_share=None,
            ),
        )
        assert check_broad_recall_bound_calibration(tmp_path, _settings(None)) == []

    def test_malformed_artifact_is_a_finding_not_an_exception(self, tmp_path: Path) -> None:
        _write(tmp_path, "{not json")
        findings = check_broad_recall_bound_calibration(tmp_path, _settings(0.42))
        assert [f.check for f in findings] == ["broad_recall_bound_calibration_malformed"]


class TestTheCommittedArtifact:
    """AC-7, read from what this branch actually commits rather than from prose."""

    def test_the_committed_artifact_is_consistent_with_the_configured_bound(self) -> None:
        """Whatever the calibration returned, the repository state agrees with it.

        Covers both outcomes deliberately. A compatible calibration must admit at least 90%
        of the WHOLE labelled positive set and reject the median top-ranked non-match; an
        incompatible one must configure no bound at all. Asserting only the first would let
        the reserved case pass by never being exercised.
        """
        from personal_agent.config.calibration import repository_root

        calibration = load_reranker_calibration(repository_root())
        assert calibration is not None, "FRE-1479 must commit a calibration artifact"
        configured = AppConfig().broad_recall_relevance_bound

        if calibration.incompatible:
            assert calibration.bound is None
            assert configured is None, (
                "a bound configured in D4's reserved case is chosen, not measured"
            )
            assert calibration.incompatible_reason
            return

        assert calibration.bound is not None
        assert configured == calibration.bound
        admitted = sum(1 for s in calibration.positive_scores if s >= calibration.bound)
        share = admitted / len(calibration.positive_scores)
        assert share >= 0.90, f"bound admits only {share:.1%} of the whole positive set"
        assert calibration.bound > calibration.negative_median_rerank
