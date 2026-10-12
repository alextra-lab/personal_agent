"""FRE-1594 / ADR-0156 A5 (T-B): the scoring of the reveal spike.

- **AC-1** (:func:`loop_count`): the draws that hold all five steps, and the first failing step of
  each other draw.
- **AC-2** (:func:`cache_verdict`, local only): the shortfall of a call is
  ``(cache_n + prompt_n + predicted_n of the previous call) - cache_n of this call``: the tokens
  of the previous call's whole context that this call did not reuse. A treatment call k and the
  control call k form a pair, for k = 2 and 3 (1-based). A successful call without timings fails
  the form (ADR AC-F3). A pair is void if either call reused more than its previous call's whole
  context (the lineage rule), or if a call in it was rejected by the provider (an AC-1 failure).
  A pair in which neither call reused a token is ``no_reuse`` and is not scored. A form with no
  scored pair is ``uninformative`` if a pair was ``no_reuse``, else ``undecided``, never ``pass``.

The baseline includes ``predicted_n`` because the local primary is a hybrid model (Gated
DeltaNet). llama.cpp cannot cut its recurrent state back to a shorter common prefix: it continues
from the live end state, which holds the generated tokens, or restores a context checkpoint, or
re-processes the whole prompt. The ADR lineage rule (``cache_n`` above the previous prompt is
void) would void the normal path, so the owner chose this baseline on 2026-10-12.
- **AC-3** (:func:`cloud_cache`): the cache fields of every cloud call, with no bound.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from scripts.eval.fre1594.reveal import Form
from scripts.eval.fre1594.transport import CACHE_KEYS

Row = Mapping[str, object]
MEASURED = (2, 3)  # the calls after the reveal, 1-based


def _timings(call: object) -> tuple[float, float, float] | None:
    """Return ``(cache_n, prompt_n, predicted_n)`` of a call, or None if one is missing."""
    timings = call.get("timings") if isinstance(call, Mapping) else None
    if not isinstance(timings, Mapping):
        return None
    values = [timings.get(key) for key in ("cache_n", "prompt_n", "predicted_n")]
    if not all(isinstance(v, int | float) for v in values):
        return None
    cache_n, prompt_n, predicted_n = (float(v) for v in values)  # type: ignore[arg-type]
    return cache_n, prompt_n, predicted_n


def shortfall(previous: Mapping[str, object], current: Mapping[str, object]) -> float:
    """Return the tokens of the previous call's whole context that this call did not reuse.

    Args:
        previous: The previous call's ``timings``.
        current: This call's ``timings``.

    Returns:
        ``(cache_n + prompt_n + predicted_n of previous) - cache_n of current``.
    """
    before = _timings({"timings": previous})
    after = _timings({"timings": current})
    assert before is not None and after is not None
    return sum(before) - after[0]


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
    generated: dict[str, float] = {}
    reused: list[float] = []
    for arm, calls in (("treatment", treatment), ("control", control)):
        if any(isinstance(c, Mapping) and c.get("ok") is False for c in calls[k - 2 : k]):
            # A rejected call evaluated no prompt: an AC-1 failure, not missing timings.
            pair["void"] = "provider_error"
            pair["arm"] = arm
            return pair
        before, after = _timings(calls[k - 2]), _timings(calls[k - 1])
        if before is None or after is None:
            pair["missing"] = arm
            return pair
        excess = after[0] - sum(before)
        if excess > 0:
            pair["void"] = "lineage"
            pair["excess"] = excess
            pair["arm"] = arm
        values[arm] = sum(before) - after[0]
        generated[arm] = max(0.0, after[0] - (before[0] + before[1]))
        reused.append(after[0])
    pair["treatment"], pair["control"] = values["treatment"], values["control"]
    pair["generated_reuse"] = generated
    pair["no_reuse"] = not any(reused)
    return pair


def cache_verdict(rows: Sequence[Row], form: Form) -> dict[str, object]:
    """Decide the AC-2 bound of one form on the local primary.

    Args:
        rows: The rows of the form.
        form: ``cache_safe`` or ``simple``.

    Returns:
        ``verdict`` (``pass``, ``fail``, ``uninformative`` or ``undecided``), ``reasons`` and
        every ``pairs`` entry.
    """
    pairs = [p for row in rows for k in MEASURED if (p := _pair(row, k)) is not None]
    reasons: list[str] = []
    if any("missing" in p for p in pairs):
        reasons.append("timings_missing")
    valid = [p for p in pairs if "missing" not in p and "void" not in p and not p["no_reuse"]]
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
    steps = (MEASURED[0],) if form == "cache_safe" else MEASURED  # the steps the form scores
    no_reuse = sum(1 for p in pairs if p.get("no_reuse") and p["k"] in steps)
    if reasons:
        verdict = "fail"
    elif not scored:
        verdict = "uninformative" if no_reuse else "undecided"
    else:
        verdict = "pass"
    return {
        "verdict": verdict,
        "reasons": reasons,
        "pairs_scored": len(scored),
        "pairs_void": sum(1 for p in pairs if "void" in p),
        "pairs_no_reuse": no_reuse,
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
