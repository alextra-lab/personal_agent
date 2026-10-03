"""FRE-1511 / ADR-0154 D7: the scorer.

Reads the rows of one replay configuration and checks every D7 threshold. Each threshold prints
PASS or FAIL. Decline-correct and expand-correct are separate rates, each with a 95% Wilson interval.
The report never prints one pooled or weighted figure.

Run from the repo root:

    uv run python -m scripts.eval.fre1537.score --run-dir <run> --tag thinking_off

The exit code is 1 when any threshold is FAIL.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from scripts.eval.fre1537.common import RunPaths, read_jsonl

Outcome = Literal["decline", "expand", "invalid"]

# ADR-0154 D7 thresholds. Changing one needs an owner-approved amendment of the ADR.
DECLINE_MIN, DECLINE_OF = 28, 30
EXPAND_MIN, EXPAND_OF = 43, 48
FOLLOWUP_MIN, FOLLOWUP_OF = 11, 12
COMPLETION_TOKENS_P50_MAX = 40
DECLINED_SINGLE_TURN_P50_MAX_S = 2.0
LONG_HISTORY_CHARS = 60_000
LONG_EXTENDED_MAX_S = 10.0
LONG_COLD_MAX_S = 60.0

_FINGERPRINT_FIELDS: tuple[tuple[str, ...], ...] = (
    ("engine", "build"),
    ("model", "name"),
    ("model", "quant"),
    ("planner_mode", "name"),
    ("planner_mode", "params"),
    ("system_prompt_sha256",),
)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Return the Wilson score interval of ``k`` successes in ``n`` trials, as fractions."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (centre - half, centre + half)


def rate_text(k: int, n: int) -> str:
    """Format a rate with its 95% Wilson interval, for example ``30/30 = 100% (89–100%)``."""
    if n == 0:
        return "n=0"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n:.0f}% ({100 * lo:.0f}–{100 * hi:.0f}%)"


def percentile(values: Sequence[float], q: float) -> float | None:
    """Return the value at index ``round(q * (n - 1))`` of the sorted values (Appendix A4's rule)."""
    xs = sorted(values)
    if not xs:
        return None
    return xs[min(len(xs) - 1, round(q * (len(xs) - 1)))]


def _mapping(x: object) -> Mapping[str, object]:
    return x if isinstance(x, Mapping) else {}


def _num(x: object) -> float | None:
    return float(x) if isinstance(x, int | float) and not isinstance(x, bool) else None


def classify(plan: object) -> Outcome:
    """Classify a parsed plan by the validation rule of ADR-0154 D2.

    ``SINGLE`` with no tasks is a decline. ``HYBRID`` or ``DECOMPOSE`` with at least one task is an
    expansion. Anything else, including a parse error, is invalid.
    """
    p = _mapping(plan)
    if not p or p.get("parse_error"):
        return "invalid"
    strategy = p.get("strategy")
    n = p.get("task_count")
    count = n if isinstance(n, int) and not isinstance(n, bool) else -1
    if strategy == "SINGLE" and count == 0:
        return "decline"
    if strategy in ("HYBRID", "DECOMPOSE") and count >= 1:
        return "expand"
    return "invalid"


@dataclass(frozen=True)
class Draw:
    """One scored planner call."""

    label: str
    expected: str
    kind: str
    outcome: Outcome
    secs: float | None
    reasoning_chars: int
    completion_tokens: float | None


def _dedupe(rows: Sequence[Mapping[str, object]]) -> tuple[list[Mapping[str, object]], int]:
    """Keep the last good row per ``(label, trial)``. Count keys that only ever errored."""
    good: dict[tuple[str, str], Mapping[str, object]] = {}
    failed: set[tuple[str, str]] = set()
    for r in rows:
        key = (str(r.get("label")), str(r.get("trial")))
        if "error" in r:
            failed.add(key)
        else:
            good[key] = r
    return list(good.values()), len(failed - set(good))


def _draw(row: Mapping[str, object]) -> Draw:
    return Draw(
        label=str(row.get("label")),
        expected=str(row.get("expected")),
        kind=str(row.get("kind", "single")),
        outcome=classify(row.get("plan")),
        secs=_num(row.get("secs")),
        reasoning_chars=int(_num(row.get("reasoning_chars")) or 0),
        completion_tokens=_num(_mapping(row.get("usage")).get("completion_tokens")),
    )


@dataclass(frozen=True)
class Check:
    """One threshold: its requirement, the measured value and the verdict."""

    name: str
    requirement: str
    measured: str
    passed: bool


@dataclass(frozen=True)
class Report:
    """The scorer's result.

    Attributes:
        fingerprint: The configuration fingerprint of the run.
        checks: One entry per D7 threshold, plus the fingerprint check.
        info: Informational lines. They decide nothing.
    """

    fingerprint: Mapping[str, object]
    checks: Sequence[Check]
    info: Sequence[str]

    @property
    def passed(self) -> bool:
        """Return True when every check passed."""
        return all(c.passed for c in self.checks)

    def render(self) -> str:
        """Render the report as text."""
        width = max(len(c.name) for c in self.checks)
        lines = ["FRE-1537 planner probe — scorer (ADR-0154 D7)", "", "Configuration fingerprint"]
        lines += [
            f"  {x}" for x in json.dumps(self.fingerprint, indent=2, sort_keys=True).splitlines()
        ]
        lines += ["", "Thresholds"]
        for c in self.checks:
            verdict = "PASS" if c.passed else "FAIL"
            lines.append(f"  {verdict}  {c.name:<{width}}  {c.requirement:<14}  {c.measured}")
        lines += ["", "Informational (decides nothing)"]
        lines += [f"  {x}" for x in self.info]
        n_pass = sum(c.passed for c in self.checks)
        lines += [
            "",
            f"RESULT: {'PASS' if self.passed else 'FAIL'} ({n_pass}/{len(self.checks)} thresholds)",
        ]
        return "\n".join(lines) + "\n"

    def to_json(self) -> dict[str, object]:
        """Return the checks and the verdict as JSON-ready data."""
        return {
            "result": "PASS" if self.passed else "FAIL",
            "fingerprint": dict(self.fingerprint),
            "checks": [
                {
                    "name": c.name,
                    "requirement": c.requirement,
                    "measured": c.measured,
                    "passed": c.passed,
                }
                for c in self.checks
            ],
        }


def _count_check(name: str, minimum: int, of: int, correct: int, n: int) -> Check:
    requirement = f">= {minimum}/{of}"
    if n != of:
        return Check(name, requirement, f"{correct}/{n}, need {of} draws", False)
    return Check(name, requirement, rate_text(correct, n), correct >= minimum)


def _dig(d: Mapping[str, object], path: tuple[str, ...]) -> object:
    cur: object = d
    for key in path:
        cur = _mapping(cur).get(key)
    return cur


def _fingerprint_check(fingerprint: Mapping[str, object]) -> Check:
    missing = [
        ".".join(p)
        for p in _FINGERPRINT_FIELDS
        if _dig(fingerprint, p) in (None, "", {}, "unknown")
    ]
    return Check(
        "Configuration fingerprint complete",
        "all fields",
        "complete" if not missing else "missing " + ", ".join(missing),
        not missing,
    )


def _long_call(rows: Sequence[Mapping[str, object]], key: str) -> float | None:
    chosen = [r for r in rows if _num(r.get("size_chars")) == LONG_HISTORY_CHARS]
    return _num(_mapping(chosen[-1].get(key)).get("secs")) if chosen else None


def _max_check(name: str, limit: float, measured: float | None) -> Check:
    requirement = f"<= {limit:g} s"
    if measured is None:
        return Check(name, requirement, "no data", False)
    return Check(name, requirement, f"{measured:.1f} s", measured <= limit)


def score(
    decide_rows: Sequence[Mapping[str, object]],
    longhist_rows: Sequence[Mapping[str, object]],
    timing_rows: Sequence[Mapping[str, object]],
    fingerprint: Mapping[str, object],
) -> Report:
    """Check every D7 threshold against the rows of one configuration.

    Args:
        decide_rows: Decision-arm rows, one per planner call.
        longhist_rows: Long-history arm rows, one per size.
        timing_rows: Timing-arm rows (informational only).
        fingerprint: The configuration fingerprint.

    Returns:
        The report. A threshold with too few draws is FAIL, never scaled.
    """
    good, errored = _dedupe(decide_rows)
    draws = [_draw(r) for r in good]
    scored = [d for d in draws if d.expected in ("decline", "expand")]

    def correct(group: Sequence[Draw], want: Outcome) -> tuple[int, int]:
        return sum(d.outcome == want for d in group), len(group)

    dec = [d for d in scored if d.expected == "decline"]
    exp = [d for d in scored if d.expected == "expand"]
    fu_dec = [d for d in dec if d.kind == "followup"]
    fu_exp = [d for d in exp if d.kind == "followup"]

    invalid = sum(d.outcome == "invalid" for d in draws)
    long_reasoning = [
        int(_num(_mapping(r.get(k)).get("reasoning_chars")) or 0)
        for r in longhist_rows
        for k in ("cold", "extended")
    ]
    reasoning = max([d.reasoning_chars for d in draws] + long_reasoning, default=0)
    declined = [d for d in draws if d.outcome == "decline"]
    tokens = [d.completion_tokens for d in declined if d.completion_tokens is not None]
    single_secs = [d.secs for d in declined if d.kind == "single" and d.secs is not None]
    tokens_p50 = percentile(tokens, 0.5)
    secs_p50 = percentile(single_secs, 0.5)

    checks = [
        _count_check("Decline-correct", DECLINE_MIN, DECLINE_OF, *correct(dec, "decline")),
        _count_check("Expand-correct", EXPAND_MIN, EXPAND_OF, *correct(exp, "expand")),
        _count_check(
            "Follow-ups decline-correct", FOLLOWUP_MIN, FOLLOWUP_OF, *correct(fu_dec, "decline")
        ),
        _count_check(
            "Follow-ups expand-correct", FOLLOWUP_MIN, FOLLOWUP_OF, *correct(fu_exp, "expand")
        ),
        Check("Plans that fail to parse or validate", "0", str(invalid), invalid == 0),
        Check("Reasoning characters on any call", "0", str(reasoning), reasoning == 0),
        Check(
            "Completion tokens, declined calls (p50)",
            f"<= {COMPLETION_TOKENS_P50_MAX}",
            "no data" if tokens_p50 is None else f"{tokens_p50:g}",
            tokens_p50 is not None and tokens_p50 <= COMPLETION_TOKENS_P50_MAX,
        ),
        Check(
            "Declined call time, single-turn (p50)",
            f"<= {DECLINED_SINGLE_TURN_P50_MAX_S:g} s",
            "no data" if secs_p50 is None else f"{secs_p50:.2f} s",
            secs_p50 is not None and secs_p50 <= DECLINED_SINGLE_TURN_P50_MAX_S,
        ),
        _max_check(
            "Planner call, 60,000 chars, extended",
            LONG_EXTENDED_MAX_S,
            _long_call(longhist_rows, "extended"),
        ),
        _max_check(
            "Planner call, 60,000 chars, cold", LONG_COLD_MAX_S, _long_call(longhist_rows, "cold")
        ),
        _fingerprint_check(fingerprint),
    ]
    return Report(fingerprint, checks, _info(draws, errored, longhist_rows, timing_rows))


def _info(
    draws: Sequence[Draw],
    errored: int,
    longhist_rows: Sequence[Mapping[str, object]],
    timing_rows: Sequence[Mapping[str, object]],
) -> list[str]:
    out = [f"call errors: {errored}", f"draws scored: {len(draws)}"]
    per: dict[str, list[Outcome]] = defaultdict(list)
    for d in draws:
        per[d.label].append(d.outcome)
    out.append(
        "per fixture (declined/draws): "
        + ", ".join(f"{k} {v.count('decline')}/{len(v)}" for k, v in sorted(per.items()))
    )
    for want, word in (("decline", "declined"), ("expand", "expanded")):
        secs = [d.secs for d in draws if d.outcome == want and d.secs is not None]
        out.append(f"{word} call secs p50 / p90: {percentile(secs, 0.5)} / {percentile(secs, 0.9)}")
    expanded_tokens = [
        d.completion_tokens for d in draws if d.outcome == "expand" and d.completion_tokens
    ]
    out.append(f"expanded completion tokens p50: {percentile(expanded_tokens, 0.5)}")
    for r in sorted(longhist_rows, key=lambda r: _num(r.get("size_chars")) or 0):
        out.append(
            f"long history {int(_num(r.get('size_chars')) or 0)} chars: "
            f"cold {_num(_mapping(r.get('cold')).get('secs'))} s, "
            f"extended {_num(_mapping(r.get('extended')).get('secs'))} s"
        )
    base = []
    with_planner = []
    for r in timing_rows:
        t1 = _num(_mapping(r.get("T1_primary")).get("ttft_any"))
        planner = _num(_mapping(r.get("T3_planner")).get("secs"))
        t3 = _num(_mapping(r.get("T3_primary")).get("ttft_any"))
        if t1 is not None and planner is not None and t3 is not None:
            base.append(t1)
            with_planner.append(planner + t3 - t1)
    if with_planner:
        out.append(
            f"added time to first token, paired per fixture (n={len(with_planner)}): "
            f"p50 {percentile(with_planner, 0.5):.1f} s, p90 {percentile(with_planner, 0.9):.1f} s"
        )
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """Score one run and write ``report.txt`` and ``report.json`` beside its rows.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 when every threshold passed, else 1.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--tag", default="thinking_off")
    args = parser.parse_args(argv)
    paths = RunPaths(args.run_dir, args.tag)
    fingerprint = json.loads(paths.fingerprint.read_text()) if paths.fingerprint.exists() else {}
    report = score(
        read_jsonl(paths.decide), read_jsonl(paths.longhist), read_jsonl(paths.timing), fingerprint
    )
    text = report.render()
    paths.rows.mkdir(parents=True, exist_ok=True)
    (paths.rows / "report.txt").write_text(text)
    (paths.rows / "report.json").write_text(json.dumps(report.to_json(), indent=2))
    sys.stdout.write(text)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
