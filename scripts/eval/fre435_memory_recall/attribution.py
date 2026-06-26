"""FRE-488 — hypothesis attribution (ADR-0087 §D4/D5).

Maps a failed recall case to the dominant hypothesis, keyed on the metric
pattern. The ordering follows the §D5 gates: write-path (H1/H2) is checked
before retrieval-path (H3/H4), which is checked before the architecture residual
(H5/H6). This is *diagnostic* attribution for a Phase-1 scaffold — it explains
why a case failed; it does not build any fix.

The cutoffs here (e.g. the description-integrity bar) are scaffold defaults; the
real §D5 numeric cutoffs are a named FRE-491 deliverable calibrated with the
owner against the pedagogical bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Scaffold default; the real bar is calibrated in FRE-491 (ADR-0087 §D5).
DESCRIPTION_INTEGRITY_BAR = 0.5


class Hypothesis(Enum):
    """The ADR-0087 §D4 hypothesis a failure is attributed to."""

    PASS = "pass"
    H1_WRITE_GAP = "H1"
    H2_FROZEN_DESCRIPTION = "H2"
    H3_RETRIEVAL_RANK = "H3"
    H4_THRESHOLD_FN = "H4"
    H5_H6_ARCHITECTURE = "H5_H6"


@dataclass(frozen=True)
class AttributionInput:
    """The per-case signals attribution keys on.

    Attributes:
        failed: Whether the case failed (computed by the harness from the
            headline signals: false-negative OR retrieval-miss@prod_k OR
            write-gap OR description below bar).
        expected_writes: Entities the case expected to land.
        entities_landed: Entities that actually landed.
        description_integrity: Proxy integrity in ``[0, 1]`` (``None`` if unscored).
        false_negative: Whether the system denied / returned nothing despite
            relevant context existing.
        recall_at_prod_k: recall at the production ``k`` (``None`` if no relevant).
        recall_at_max_k: recall at the widest swept ``k`` (``None`` if no relevant).
    """

    failed: bool
    expected_writes: int
    entities_landed: int
    description_integrity: float | None
    false_negative: bool | None
    recall_at_prod_k: float | None
    recall_at_max_k: float | None


def attribute(inp: AttributionInput) -> Hypothesis:
    """Attribute a (possibly failed) case to a hypothesis.

    Gate ordering (ADR-0087 §D5): write-path → retrieval-path → architecture.
    Within retrieval, "not retrievable at any k" is treated as the root cause
    ahead of a denial, because the denial is then a *consequence* of the
    retrieval failure.

    Args:
        inp: The per-case attribution signals.

    Returns:
        The dominant :class:`Hypothesis` (``PASS`` when the case did not fail).
    """
    if not inp.failed:
        return Hypothesis.PASS

    # --- Write-path gate (D5.1) ---------------------------------------------
    if inp.expected_writes > 0 and inp.entities_landed == 0:
        return Hypothesis.H1_WRITE_GAP
    if (
        inp.description_integrity is not None
        and inp.description_integrity < DESCRIPTION_INTEGRITY_BAR
    ):
        return Hypothesis.H2_FROZEN_DESCRIPTION

    # --- Retrieval-path gate (D5.2) -----------------------------------------
    # Landed but never surfaces even at the widest k -> retrieval can't reach it.
    if inp.recall_at_max_k is not None and inp.recall_at_max_k == 0.0:
        return Hypothesis.H3_RETRIEVAL_RANK
    # Denied despite the fact being present AND retrievable -> threshold/query.
    if inp.false_negative:
        return Hypothesis.H4_THRESHOLD_FN
    # In the index at large k but missed at production k -> ranked too low.
    if (
        inp.recall_at_prod_k is not None
        and inp.recall_at_prod_k == 0.0
        and inp.recall_at_max_k is not None
        and inp.recall_at_max_k > 0.0
    ):
        return Hypothesis.H3_RETRIEVAL_RANK

    # --- Architecture gate (D5.3) -------------------------------------------
    # Failed, fact present and retrievable, no write/desc/rank/denial cause:
    # the structure cannot represent what was asked (diagnostic only, Phase 1).
    return Hypothesis.H5_H6_ARCHITECTURE
