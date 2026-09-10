"""Committed calibration artifacts for relevance bounds (ADR-0148 D4, FRE-1477).

ADR-0148 D4 requires that every configured relevance bound trace to a committed
calibration measured against the component that actually produced its scores, and that a
missing or stale calibration leave the previous bound in force while raising a
``config_guard`` finding. This module is the read side of that contract: the typed shape
of the artifact, and the loader ``config_guard`` and the tests share.

The artifacts live in ``config/calibration/``. They are data, not configuration templates:
a bound is a *measurement*, and the measurement's provenance -- which model, at which
dimension, on which date, over which probe set -- is the part that makes the number
trustworthy. A hand-written constant with no artifact behind it is the condition ADR-0148
AC-10 exists to fail on.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Directory holding the committed calibration artifacts, relative to the repository root.
CALIBRATION_DIR = "config/calibration"

#: Filename of the proactive path's embedder-arm calibration.
PROACTIVE_RELEVANCE_BOUND_FILE = "proactive_relevance_bound.json"


class CalibrationComponent(BaseModel):
    """The component whose scores a calibration measured.

    ADR-0148 D4 carries one bound per path, each calibrated in its own score space,
    because a reranker's scale is not comparable to Neo4j embedding space (FRE-695).
    Naming the component is what lets ``config_guard`` detect that a bound was measured
    against something other than what now serves.

    Attributes:
        role: Which component produced the scores.
        model: The model identifier, as the serving configuration names it.
        dimensions: The embedding width the measurement ran at. Not the model's native
            width when the two differ -- FRE-826 measured the managed 8B arm's peak at
            1024 against a native 4096, and separation is dimension-dependent, so the
            served width is the one that describes the measurement.
    """

    model_config = ConfigDict(frozen=True)

    role: Literal["embedder", "reranker"]
    model: str = Field(description="Model identifier as the serving configuration names it.")
    dimensions: int = Field(gt=0, description="Embedding width the measurement ran at.")


class ParityRecord(BaseModel):
    """The parity evidence a calibration must carry before its numbers are trusted.

    FRE-694 established the discipline: validate the instrument against the production
    path before reporting any number from it. Two assertions carry that here, and both
    must hold.

    ``P1`` reproduces FRE-694's own structure -- an independently authored offline
    harness compared against the Neo4j path on three aggregates. That comparison catches
    shared corpus-construction, text-formatting, embedding-mode and aggregation error,
    which a single pipeline compared against itself cannot.

    ``P2`` closes the caveat FRE-694 stated about its own check: *"this is an
    embedding-geometry test, not a Neo4j-HNSW index-fidelity test."* It compares, per
    candidate, the score the vector index returned against the cosine computed
    client-side over the same embedding.

    The raw pairs are recorded rather than the deltas, so a re-run recomputes the
    assertion instead of re-reading a stored conclusion.

    Attributes:
        tolerance: Maximum absolute difference either assertion may show. FRE-694's own
            value is 0.02.
        p1_offline_aggregates: Positive median, negative median and negative maximum from
            the independently authored offline harness, in Neo4j score space.
        p1_index_aggregates: The same three statistics from the Neo4j vector index.
        p2_pairs: ``(index_score, offline_score)`` for each scored candidate.
    """

    model_config = ConfigDict(frozen=True)

    tolerance: float = Field(
        gt=0.0, description="Maximum absolute difference either check may show."
    )
    p1_offline_aggregates: dict[str, float] = Field(
        description="pos_median / neg_median / neg_max from the offline harness."
    )
    p1_index_aggregates: dict[str, float] = Field(
        description="pos_median / neg_median / neg_max from the Neo4j vector index."
    )
    p2_pairs: list[tuple[float, float]] = Field(
        description="Per-candidate (index score, client-side cosine) pairs, Neo4j score space."
    )


class RelevanceCalibration(BaseModel):
    """One committed relevance-bound calibration.

    A calibration reports a bound only when one satisfies both of ADR-0148 D4's
    constraints: strictly above the median top-ranked non-match, and admitting at least
    90% of the whole labelled positive set. When no value satisfies both, the artifact
    records the incompatibility and carries no bound. That is a finding for the owner --
    the response is a reranker-side bound or a different arm -- and never a quietly
    chosen number.

    Both score populations are recorded whole. AC-4's denominator is the entire labelled
    positive set, and storing only a share would let the denominator be re-cut after the
    fact.

    Attributes:
        component: What produced the scores.
        measured_on: The date the measurement ran.
        probe_set: Repository path of the labelled probe set.
        incompatible: True when no bound satisfies both constraints.
        incompatible_reason: Why, when ``incompatible``. None otherwise.
        bound_neo4j_space: The chosen bound in Neo4j score space, or None.
        bound_embedding_term: The same bound after ``_normalize_vector_score``'s
            ``max(0, 2x - 1)``. This is the space the configured setting uses, because it
            is the value the proactive path scores on.
        positive_scores: Every labelled positive, in Neo4j score space, per expected
            entity. A labelled positive the index did not return is absent rather than
            scored zero, and counts as not admitted.
        negative_scores: Each query's strongest non-match, in Neo4j score space.
        positive_admitted_share: Share of ``positive_scores`` the bound admits, or None
            when incompatible.
        negative_median_neo4j: Median of ``negative_scores``. The value the bound must
            reject, and the value a gate test drives its fixture at.
        parity: The instrument validation.
    """

    model_config = ConfigDict(frozen=True)

    component: CalibrationComponent
    measured_on: date
    probe_set: str
    incompatible: bool
    incompatible_reason: str | None = None
    bound_neo4j_space: float | None = Field(default=None, ge=0.0, le=1.0)
    bound_embedding_term: float | None = Field(default=None, ge=0.0, le=1.0)
    positive_scores: list[float]
    negative_scores: list[float]
    positive_admitted_share: float | None = Field(default=None, ge=0.0, le=1.0)
    negative_median_neo4j: float = Field(ge=0.0, le=1.0)
    parity: ParityRecord


def calibration_path(root: Path, filename: str = PROACTIVE_RELEVANCE_BOUND_FILE) -> Path:
    """Resolve a calibration artifact's path under the repository root.

    Args:
        root: The repository root.
        filename: The artifact filename.

    Returns:
        The artifact path, which may not exist.
    """
    return root / CALIBRATION_DIR / filename


def load_relevance_calibration(
    root: Path, filename: str = PROACTIVE_RELEVANCE_BOUND_FILE
) -> RelevanceCalibration | None:
    """Load a committed relevance calibration.

    Args:
        root: The repository root.
        filename: The artifact filename.

    Returns:
        The parsed calibration, or None when the artifact does not exist. A missing
        artifact is a reportable state rather than an error: ADR-0148 D4 requires it to
        raise a ``config_guard`` finding while leaving the previous bound in force, so
        the caller decides, not this loader.

    Raises:
        ValueError: If the artifact exists but is not valid JSON, or does not match the
            schema. A malformed artifact is never treated as an absent one -- that would
            silently downgrade a corrupted measurement into "not calibrated yet".
    """
    path = calibration_path(root, filename)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"calibration artifact {path} is not valid JSON: {exc}") from exc
    return RelevanceCalibration.model_validate(payload)
