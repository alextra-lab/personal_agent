"""FRE-817 -- pure ADR-0112 AC-4 margin-decision function.

Free of any ``personal_agent`` / substrate / network import so it is fully
unit-testable independent of any live embedder run. Mirrors the shape of
``scripts.eval.fre720_insights_separation.decision.decide_branch`` -- a small
pure decision consumed by a downstream ticket (FRE-821, the adoption ticket).

AC-4 (ADR-0112): "if a closed/API-only model is selected, its nDCG exceeds the
best open-weight candidate by the pre-registered margin; else the open-weight
spine is retained. Fails if there is no A/B artifact, or a closed model is
adopted on a noise-level win." :func:`decide_embedder` makes the latter
failure mode structurally unreachable -- a closed candidate can only ever win
by having already cleared ``margin``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

#: Declared BEFORE any run (the "pre-registered" requirement). Grounded in the
#: fixed real-query corpus's own granularity, not an arbitrary round number:
#: ``semantic_probe.yaml`` (FRE-670) has 54 cases, so one case flipping moves
#: the aggregate mean nDCG by ~= 1/54 ~= 0.019. 0.05 is ~2.6x that -- wide
#: enough that a margin "clear" cannot be a single-case fluke, which is the
#: "not a noise-level win" bar AC-4 asks for. This is the ONLY margin ever
#: used end-to-end: the driver never exposes a CLI override.
PRE_REGISTERED_MARGIN_NDCG: float = 0.05

EmbedderKind = Literal["open_weight", "closed"]


@dataclass(frozen=True)
class EmbedderCandidate:
    """One embedder arm's measured result on the fixed real-query corpus.

    Attributes:
        name: Arm label (e.g. ``"0.6b"``, ``"8b-ovh"``).
        kind: ``"open_weight"`` or ``"closed"`` (ADR-0112 D4's custody axis).
        mean_ndcg: Mean nDCG@5 (the pre-registered decision metric) over the
            corpus's non-control cases.
    """

    name: str
    kind: EmbedderKind
    mean_ndcg: float


@dataclass(frozen=True)
class EmbedderDecision:
    """The AC-4 verdict.

    Attributes:
        winner: The selected arm's name.
        winner_kind: The selected arm's kind.
        margin_cleared: ``True``/``False`` when a closed candidate competed
            and either cleared or missed the margin; ``None`` when no closed
            candidate was in the run (the margin gate never applied).
        reasoning: Human-readable justification for the ticket/PR record.
    """

    winner: str
    winner_kind: EmbedderKind
    margin_cleared: bool | None
    reasoning: str


def decide_embedder(candidates: Sequence[EmbedderCandidate], margin: float) -> EmbedderDecision:
    """ADR-0112 AC-4: pick the embedder, gating a closed winner on the margin.

    Args:
        candidates: Measured arms (at least one ``"open_weight"``).
        margin: The pre-registered nDCG margin a closed candidate must clear
            to beat the best open-weight candidate. Must be > 0.

    Returns:
        The :class:`EmbedderDecision`.

    Raises:
        ValueError: If ``candidates`` is empty, if there is no
            ``"open_weight"`` candidate (AC-4's retained default requires
            one), or if ``margin`` is not strictly positive (a zero/negative
            margin would let a closed model win on a tie or a loss).
    """
    if not candidates:
        raise ValueError("no embedder candidates to decide between -- no A/B artifact")
    if margin <= 0:
        raise ValueError(f"margin must be > 0 (a pre-registered gate), got {margin}")

    open_weight = [c for c in candidates if c.kind == "open_weight"]
    closed = [c for c in candidates if c.kind == "closed"]
    if not open_weight:
        raise ValueError("AC-4 requires at least one open-weight candidate as the retained default")

    best_open = max(open_weight, key=lambda c: c.mean_ndcg)
    if not closed:
        return EmbedderDecision(
            winner=best_open.name,
            winner_kind="open_weight",
            margin_cleared=None,
            reasoning=(
                f"no closed candidate competed; best open-weight arm "
                f"'{best_open.name}' (nDCG={best_open.mean_ndcg:.4f}) wins by measurement"
            ),
        )

    best_closed = max(closed, key=lambda c: c.mean_ndcg)
    delta = best_closed.mean_ndcg - best_open.mean_ndcg
    cleared = delta >= margin
    if cleared:
        return EmbedderDecision(
            winner=best_closed.name,
            winner_kind="closed",
            margin_cleared=True,
            reasoning=(
                f"closed candidate '{best_closed.name}' (nDCG={best_closed.mean_ndcg:.4f}) "
                f"beats open-weight best '{best_open.name}' (nDCG={best_open.mean_ndcg:.4f}) "
                f"by {delta:.4f} >= margin {margin:.4f} -- adopted"
            ),
        )
    return EmbedderDecision(
        winner=best_open.name,
        winner_kind="open_weight",
        margin_cleared=False,
        reasoning=(
            f"closed candidate '{best_closed.name}' (nDCG={best_closed.mean_ndcg:.4f}) "
            f"beat open-weight best '{best_open.name}' (nDCG={best_open.mean_ndcg:.4f}) "
            f"by only {delta:.4f} < margin {margin:.4f} -- noise-level win, "
            f"open-weight spine retained"
        ),
    )
