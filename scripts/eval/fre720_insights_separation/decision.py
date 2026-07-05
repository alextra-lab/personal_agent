"""FRE-720 -- pure D10 branch-decision function (ADR-0105 D10 / AC-8).

Free of any ``personal_agent`` / substrate import so it is fully unit-testable
against a synthetic ``SeparationStats``, independent of any live embedder run.
FRE-721 (T7) mechanically checks its shipped dedup branch against this function's
result via ``probe_result.json["decision"]`` (see the FRE-720 plan's Downstream
contract).
"""

from __future__ import annotations

from typing import Literal

from scripts.eval.fre435_memory_recall.separation_report import SeparationStats


def decide_branch(stats: SeparationStats) -> Literal["semantic", "fallback"]:
    """ADR-0105 D10: adopt semantic dedup only if positives/negatives cleanly separate.

    Args:
        stats: The measured positive/negative cosine separation summary.

    Returns:
        `"semantic"` iff `stats.clean_floor` (`max(negatives) < min(positives)`,
        the ADR-0103 clean-floor definition); otherwise `"fallback"` (explicit
        category + facet grouping, per D10's non-vector fallback).
    """
    return "semantic" if stats.clean_floor else "fallback"
