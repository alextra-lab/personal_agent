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
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from scripts.eval.fre1537.common import LONGHIST_SIZES, PAIRED_DRAWS, RunPaths, read_jsonl
from scripts.eval.fre1537.fixtures import load_fixtures

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
# ADR-0154 Amendment 1, 1.2: the paired added delay, per history split.
PAIRED_SPLIT_CHARS = 8000
PAIRED_SHORT_MAX_S = 3.0
PAIRED_SHORT_MIN = 28
PAIRED_SHORT_OF = 30
PAIRED_LONG_MAX_S = 10.0
PAIRED_LONG_MIN = 8
PAIRED_LONG_OF = 9

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
    thinking_blocks: int = 0
    reasoning_tokens: int = 0


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
        thinking_blocks=int(_num(row.get("thinking_blocks")) or 0),
        reasoning_tokens=int(_num(_mapping(row.get("usage")).get("reasoning_tokens")) or 0),
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
        paired: The ``short`` and ``long`` blocks of the paired arm (ADR-0154 Amendment 1, 1.6).
    """

    fingerprint: Mapping[str, object]
    checks: Sequence[Check]
    info: Sequence[str]
    paired: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

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
            "paired": {k: dict(v) for k, v in self.paired.items()},
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
        for p in _fingerprint_fields(fingerprint)
        if _dig(fingerprint, p) in (None, "", "unknown")
    ]
    return Check(
        "Configuration fingerprint complete",
        "all fields",
        "complete" if not missing else "missing " + ", ".join(missing),
        not missing,
    )


def _reasoning_text(chars: int, blocks: int, tokens: int) -> str:
    text = str(chars)
    if blocks:
        text += f", {blocks} thinking block(s)"
    if tokens:
        text += f", {tokens} reasoning token(s)"
    return text


def _fingerprint_fields(fingerprint: Mapping[str, object]) -> tuple[tuple[str, ...], ...]:
    """Return the fields a fingerprint must hold. A managed run also names the served model."""
    if "served_model" in _mapping(fingerprint.get("engine")):
        return (*_FINGERPRINT_FIELDS, ("engine", "served_model"))
    return _FINGERPRINT_FIELDS


def _long_call(rows: Sequence[Mapping[str, object]], key: str) -> float | None:
    chosen = [r for r in rows if _num(r.get("size_chars")) == LONG_HISTORY_CHARS]
    return _num(_mapping(chosen[-1].get(key)).get("secs")) if chosen else None


def _max_check(name: str, limit: float, measured: float | None) -> Check:
    requirement = f"<= {limit:g} s"
    if measured is None:
        return Check(name, requirement, "no data", False)
    return Check(name, requirement, f"{measured:.1f} s", measured <= limit)


@dataclass(frozen=True)
class PairSplit:
    """The pairs of one history split, after the key check.

    Attributes:
        name: ``short`` or ``long``.
        rows: The last row without an error for each pair key in the split.
        key_problems: The missing and unexpected keys of the file that feeds the split.
    """

    name: str
    rows: Sequence[Mapping[str, object]]
    key_problems: Sequence[str]

    def counted(self) -> list[Mapping[str, object]]:
        """Return the pairs that count: the planner declined and the pair is not void."""
        return [r for r in self.rows if r.get("declined") is True and not r.get("void")]


def _pair_key(row: Mapping[str, object], key_field: str) -> tuple[str, int]:
    return (str(row.get(key_field)), int(_num(row.get("draw")) or 0))


def _keyed_pairs(
    rows: Sequence[Mapping[str, object]], key_field: str, expected: set[tuple[str, int]]
) -> tuple[list[Mapping[str, object]], list[str]]:
    """Keep the last row without an error for each pair key, and check the keys.

    Args:
        rows: One paired rows file.
        key_field: ``label`` or ``size_chars``.
        expected: The fixed key set of the file (ADR-0154 Amendment 1, 1.1).

    Returns:
        The kept rows and a description of each missing or unexpected key.
    """
    kept: dict[tuple[str, int], Mapping[str, object]] = {}
    for r in rows:
        if "error" not in r:
            kept[_pair_key(r, key_field)] = r
    missing = sorted(expected - set(kept))
    unexpected = sorted(set(kept) - expected)
    problems = [f"missing {k}/{d}" for k, d in missing] + [
        f"unexpected {k}/{d}" for k, d in unexpected
    ]
    return list(kept.values()), problems


def expected_pair_keys() -> tuple[set[tuple[str, int]], set[tuple[str, int]]]:
    """Return the fixed pair keys: the decline fixtures and the long-history sizes, each by draw."""
    labels = [fx.label for fx in load_fixtures() if fx.expected == "decline"]
    draws = range(PAIRED_DRAWS)
    return (
        {(label, d) for label in labels for d in draws},
        {(str(size), d) for size in LONGHIST_SIZES for d in draws},
    )


def paired_splits(
    paired_rows: Sequence[Mapping[str, object]],
    longhist_paired_rows: Sequence[Mapping[str, object]],
) -> tuple[PairSplit, PairSplit]:
    """Check each paired file's keys, then split the pairs on ``history_chars`` at 8,000.

    A key problem of ``paired.jsonl`` fails the short split, and one of ``longhist_paired.jsonl``
    fails the long split, because each file holds the fixed key set of one split.

    Args:
        paired_rows: The rows of the decline fixtures.
        longhist_paired_rows: The rows of the long-history turns.

    Returns:
        The short split and the long split.
    """
    short_keys, long_keys = expected_pair_keys()
    short_file, short_problems = _keyed_pairs(paired_rows, "label", short_keys)
    long_file, long_problems = _keyed_pairs(longhist_paired_rows, "size_chars", long_keys)
    rows = [*short_file, *long_file]

    def is_long(r: Mapping[str, object]) -> bool:
        return (_num(r.get("history_chars")) or 0) >= PAIRED_SPLIT_CHARS

    return (
        PairSplit("short", [r for r in rows if not is_long(r)], short_problems),
        PairSplit("long", [r for r in rows if is_long(r)], long_problems),
    )


def _secs(row: Mapping[str, object], arm: str, call: str, key: str) -> float | None:
    return _num(_mapping(_mapping(row.get(arm)).get(call)).get(key))


def paired_block(split: PairSplit, of: int) -> dict[str, object]:
    """Return the ``report.json`` block of one split (ADR-0154 Amendment 1, 1.6).

    Args:
        split: The split.
        of: The number of pairs the split must hold.

    Returns:
        ``counted``, ``of``, ``added_s_p50``, ``planner_s_p50``, ``cache_loss_s_p50`` and ``void``,
        the count of void pairs per reason. The medians are over the counted pairs.
    """
    counted = split.counted()
    added = [x / 1000 for r in counted if (x := _num(r.get("added_ms"))) is not None]
    planner = [x for r in counted if (x := _secs(r, "planner", "planner", "secs")) is not None]
    loss = []
    for r in counted:
        with_planner = _secs(r, "planner", "primary", "ttft_any")
        alone = _secs(r, "single", "primary", "ttft_any")
        if with_planner is not None and alone is not None:
            loss.append(with_planner - alone)
    void: Counter[str] = Counter()
    for r in split.rows:
        reasons = r.get("void")
        if isinstance(reasons, list):
            void.update(str(x) for x in reasons)
    return {
        "counted": len(counted),
        "of": of,
        "added_s_p50": percentile(added, 0.5),
        "planner_s_p50": percentile(planner, 0.5),
        "cache_loss_s_p50": percentile(loss, 0.5),
        "void": dict(sorted(void.items())),
    }


def _paired_check(
    name: str, split: PairSplit, block: Mapping[str, object], minimum: int, limit: float
) -> Check:
    of = int(_num(block["of"]) or 0)
    requirement = f"<= {limit:g} s, >= {minimum}/{of}"
    median = _num(block["added_s_p50"])
    counted = int(_num(block["counted"]) or 0)
    text = "no counted pair" if median is None else f"median {median:.2f} s"
    text += f", {counted} of {len(split.rows)} counted"
    if split.key_problems:
        text += "; " + ", ".join(split.key_problems)
    passed = (
        not split.key_problems and counted >= minimum and median is not None and median <= limit
    )
    return Check(name, requirement, text, passed)


def score(
    decide_rows: Sequence[Mapping[str, object]],
    longhist_rows: Sequence[Mapping[str, object]],
    paired_rows: Sequence[Mapping[str, object]],
    longhist_paired_rows: Sequence[Mapping[str, object]],
    fingerprint: Mapping[str, object],
) -> Report:
    """Check every D7 threshold against the rows of one configuration.

    Args:
        decide_rows: Decision-arm rows, one per planner call.
        longhist_rows: Long-history arm rows, one per size.
        paired_rows: Paired-arm rows of the decline fixtures, one per fixture and draw.
        longhist_paired_rows: Paired-arm rows of the long-history turns, one per size and draw.
        fingerprint: The configuration fingerprint.

    Returns:
        The report. A threshold with too few draws or pairs is FAIL, never scaled.
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

    short, long = paired_splits(paired_rows, longhist_paired_rows)
    pairs = [*short.rows, *long.rows]
    # Every planner call of the run counts for the parse threshold: the decision draws, both planner
    # calls of the long-history arm, and the measured planner call of each pair. The planner prime of
    # a pair counts for the reasoning threshold only (Amendment 1, 1.6). No primary call counts.
    parsed_calls = [_mapping(r.get(k)) for r in longhist_rows for k in ("cold", "extended")] + [
        _mapping(_mapping(r.get("planner")).get("planner")) for r in pairs
    ]
    reasoning_calls = parsed_calls + [
        _mapping(_mapping(r.get("planner")).get("prime_planner")) for r in pairs
    ]
    invalid = sum(d.outcome == "invalid" for d in draws) + sum(
        classify(c.get("plan")) == "invalid" for c in parsed_calls if c
    )
    # A managed provider can think without a visible character: a redacted thinking block, or a
    # reasoning-token count with the text hidden. Both count (FRE-1516). Local rows carry neither field.
    side = [_draw({**c, "label": "side"}) for c in reasoning_calls if c]
    reasoning = max(
        [d.reasoning_chars for d in draws] + [d.reasoning_chars for d in side], default=0
    )
    blocks = sum(d.thinking_blocks for d in [*draws, *side])
    reasoning_tokens = max([d.reasoning_tokens for d in [*draws, *side]], default=0)
    declined = [d for d in draws if d.outcome == "decline"]
    tokens = [d.completion_tokens for d in declined if d.completion_tokens is not None]
    single_secs = [d.secs for d in declined if d.kind == "single" and d.secs is not None]
    tokens_p50 = percentile(tokens, 0.5)
    secs_p50 = percentile(single_secs, 0.5)
    paired = {
        "short": paired_block(short, PAIRED_SHORT_OF),
        "long": paired_block(long, PAIRED_LONG_OF),
    }

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
        Check(
            "Reasoning characters on any call",
            "0",
            _reasoning_text(reasoning, blocks, reasoning_tokens),
            reasoning == 0 and blocks == 0 and reasoning_tokens == 0,
        ),
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
        _paired_check(
            "Paired added delay, history < 8,000 chars (median)",
            short,
            paired["short"],
            PAIRED_SHORT_MIN,
            PAIRED_SHORT_MAX_S,
        ),
        _paired_check(
            "Paired added delay, history >= 8,000 chars (median)",
            long,
            paired["long"],
            PAIRED_LONG_MIN,
            PAIRED_LONG_MAX_S,
        ),
        _fingerprint_check(fingerprint),
    ]
    info = _info(draws, errored, longhist_rows, (short, long), paired)
    return Report(fingerprint, checks, info, paired)


def _info(
    draws: Sequence[Draw],
    errored: int,
    longhist_rows: Sequence[Mapping[str, object]],
    splits: Sequence[PairSplit],
    paired: Mapping[str, Mapping[str, object]],
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
    for split in splits:
        b = paired[split.name]
        out.append(
            f"paired {split.name}: {b['counted']} of {b['of']} counted, "
            f"added p50 {b['added_s_p50']} s, planner call p50 {b['planner_s_p50']} s, "
            f"cache loss p50 {b['cache_loss_s_p50']} s, void {b['void'] or 'none'}"
        )
        per_key: dict[str, list[float]] = defaultdict(list)
        for r in split.counted():
            key = str(r.get("label", r.get("size_chars")))
            if (ms := _num(r.get("added_ms"))) is not None:
                per_key[key].append(ms)
        if per_key:
            out.append(
                f"paired {split.name} median added_ms per key: "
                + ", ".join(f"{k} {percentile(v, 0.5):g}" for k, v in sorted(per_key.items()) if v)
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
        read_jsonl(paths.decide),
        read_jsonl(paths.longhist),
        read_jsonl(paths.paired),
        read_jsonl(paths.longhist_paired),
        fingerprint,
    )
    text = report.render()
    paths.rows.mkdir(parents=True, exist_ok=True)
    (paths.rows / "report.txt").write_text(text)
    (paths.rows / "report.json").write_text(json.dumps(report.to_json(), indent=2))
    sys.stdout.write(text)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
