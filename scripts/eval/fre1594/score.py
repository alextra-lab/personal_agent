"""FRE-1594 / ADR-0156 A5 (T-B): the scoring of the reveal spike.

- **AC-1** (:func:`loop_count`): the draws that hold all five steps, and the first failing step of
  each other draw.
- **AC-2** (:func:`cache_verdict`, local only): the shortfall of a call is
  ``(cache_n + prompt_n of the previous call) - cache_n of this call``. A treatment call k and the
  control call k form a pair, for k = 2 and 3 (1-based). A call without timings fails the form
  (ADR AC-F3). A pair is void if either call reused more than its previous call's prompt (the ADR
  lineage rule). A form with no non-void pair at a measured step is ``undecided``, never ``pass``.
- **AC-3** (:func:`cloud_cache`): the cache fields of every cloud call, with no bound.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from scripts.eval.fre1594.reveal import Form
from scripts.eval.fre1594.transport import CACHE_KEYS

Row = Mapping[str, object]
MEASURED = (2, 3)  # the calls after the reveal, 1-based


def _timings(call: object) -> tuple[float, float] | None:
    timings = call.get("timings") if isinstance(call, Mapping) else None
    if not isinstance(timings, Mapping):
        return None
    cache_n, prompt_n = timings.get("cache_n"), timings.get("prompt_n")
    if not isinstance(cache_n, int | float) or not isinstance(prompt_n, int | float):
        return None
    return float(cache_n), float(prompt_n)


def shortfall(previous: Mapping[str, object], current: Mapping[str, object]) -> float:
    """Return the prompt tokens of the previous call that this call did not reuse.

    Args:
        previous: The previous call's ``timings``.
        current: This call's ``timings``.

    Returns:
        ``(cache_n + prompt_n of previous) - cache_n of current``.
    """
    before = _timings({"timings": previous})
    after = _timings({"timings": current})
    assert before is not None and after is not None
    return before[0] + before[1] - after[0]


def _calls(row: Row, arm: str) -> Sequence[object]:
    calls = row.get(arm)
    return calls if isinstance(calls, list) else []


def _pair(row: Row, k: int) -> dict[str, object] | None:
    """Score the pair at call k of a row, or None when either arm did not reach call k."""
    treatment, control = _calls(row, "treatment"), _calls(row, "control")
    if len(treatment) < k or len(control) < k:
        return None
    pair: dict[str, object] = {"prompt_id": row.get("prompt_id"), "draw": row.get("draw"), "k": k}
    values: dict[str, float] = {}
    for arm, calls in (("treatment", treatment), ("control", control)):
        before, after = _timings(calls[k - 2]), _timings(calls[k - 1])
        if before is None or after is None:
            pair["missing"] = arm
            return pair
        excess = after[0] - (before[0] + before[1])
        if excess > 0:
            pair["void"] = "lineage"
            pair["excess"] = excess
            pair["arm"] = arm
        values[arm] = before[0] + before[1] - after[0]
    pair["treatment"], pair["control"] = values["treatment"], values["control"]
    return pair


def cache_verdict(rows: Sequence[Row], form: Form) -> dict[str, object]:
    """Decide the AC-2 bound of one form on the local primary.

    Args:
        rows: The rows of the form.
        form: ``cache_safe`` or ``simple``.

    Returns:
        ``verdict`` (``pass``, ``fail`` or ``undecided``), ``reasons`` and every ``pairs`` entry.
    """
    pairs = [p for row in rows for k in MEASURED if (p := _pair(row, k)) is not None]
    reasons: list[str] = []
    if any("missing" in p for p in pairs):
        reasons.append("timings_missing")
    valid = [p for p in pairs if "missing" not in p and "void" not in p]
    if form == "cache_safe":
        scored = [p for p in valid if p["k"] == MEASURED[0]]
        if any(p["treatment"] > p["control"] for p in scored):  # type: ignore[operator]
            reasons.append("reveal_shortfall_above_control")
    else:
        scored = valid
        costly = Counter(
            (p["prompt_id"], p["draw"])
            for p in valid
            if p["treatment"] > p["control"]  # type: ignore[operator]
        )
        if any(n > 1 for n in costly.values()):
            reasons.append("more_than_one_costly_call_in_a_session")
    if reasons:
        verdict = "fail"
    elif not scored:
        verdict = "undecided"
    else:
        verdict = "pass"
    return {
        "verdict": verdict,
        "reasons": reasons,
        "pairs_scored": len(scored),
        "pairs_void": sum(1 for p in pairs if "void" in p),
        "pairs": pairs,
    }


def loop_count(rows: Sequence[Row]) -> dict[str, object]:
    """Count the draws that hold the full tool loop (AC-1).

    Args:
        rows: The rows of one provider and form.

    Returns:
        ``held``, ``draws`` and ``failed_steps`` (first failing step: count).
    """
    failed = Counter(str(row.get("failed_step")) for row in rows if not row.get("passed"))
    return {
        "held": sum(1 for row in rows if row.get("passed")),
        "draws": len(rows),
        "failed_steps": dict(sorted(failed.items())),
    }


def cloud_cache(rows: Sequence[Row]) -> list[dict[str, object]]:
    """Return the cache fields of every call of every cloud row (AC-3).

    Args:
        rows: The rows of one cloud form.

    Returns:
        One entry per row: ``prompt_id``, ``draw`` and the cache fields of each call of each arm.
    """
    out: list[dict[str, object]] = []
    for row in rows:
        entry: dict[str, object] = {"prompt_id": row.get("prompt_id"), "draw": row.get("draw")}
        for arm in ("treatment", "control"):
            entry[arm] = [
                {key: (call.get("usage") or {}).get(key) for key in CACHE_KEYS}
                for call in _calls(row, arm)
                if isinstance(call, Mapping)
            ]
        out.append(entry)
    return out
