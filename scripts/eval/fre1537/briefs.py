"""FRE-1514 AC-2: count the brief goals that introduce an entity absent from the user's message.

Compares two probe arms of the same fixtures: the rule arm (the planner prompt with the FRE-1514 scope
rule) and the baseline arm (the prompt on ``main`` without it). The counting rule below is committed
before either arm runs, so it is not fitted to their output.

**The counting rule.**

- A brief is one task of an expansion draw: a ``HYBRID`` or ``DECOMPOSE`` reply with at least one task
  (``score.classify``). Its goal and constraints are read in full from the row's raw ``content``.
  Rows are de-duplicated as the D7 scorer does.
- The reference text is the user's message: the fixture's current query (``prompts.json``). A follow-up's
  history is not reference text.
- A term is a lower-case run of letters and digits that holds at least one letter, is 3 or more characters
  long, and is in neither ``STOP_WORDS`` nor ``INSTRUCTION_WORDS``.
- A stem strips a possessive ``'s``, then ``ies`` to ``y``, then a final ``s`` (not ``ss``), then a final
  ``e``. A goal term is known when its stem equals a stem of the reference text, or when one stem is a
  prefix of the other and the shorter has at least ``PREFIX_MIN`` characters.
- A goal introduces an entity when it holds at least one term that is not known.

"Entity" is therefore a novel content term, not a named entity. The rule is lexical and blind to meaning.
It counts both arms the same way, so the comparison is fair even where a single count is debatable.

**The verdict.** PASS when the rule arm has fewer such goals than the baseline AND a lower rate of them
(such goals divided by all goals). The rate stops a count that falls only because the rule arm expands
less from reading as a pass.

Run from the repo root:

    uv run python -m scripts.eval.fre1537.briefs --rule-run <run> --baseline-run <run> --tag planner

It writes ``briefs.txt`` and ``briefs.json`` beside the rule arm's rows. The exit code is 1 on FAIL.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from scripts.eval.fre1537.common import RunPaths, read_jsonl
from scripts.eval.fre1537.llama import load_plan_json
from scripts.eval.fre1537.score import _dedupe, classify, percentile

PREFIX_MIN = 5
MIN_TERM_CHARS = 3

STOP_WORDS: frozenset[str] = frozenset(
    """
    a about above after again against all also am an and any are as at be because been before being
    below between both but by can could did do does doing down during each either else etc few for from
    further had has have having he her here hers him his how however i if in into is it its itself just
    may me might more most must my no nor not now of off on once one only or other our ours out over own
    per same she should so some such than that the their theirs them then there these they this those
    through to too under until up upon us very via was we were what when where whether which while who
    whom whose why will with within without would yes yet you your yours
    """.split()
)

# Words that tell a worker how to do or report its task, not what the task is about. Fixed before
# either arm ran (FRE-1514): the briefing rules ask for an end point and a count, so these words
# appear in narrow and broad goals alike.
INSTRUCTION_WORDS: frozenset[str] = frozenset(
    """
    answer answers based best brief briefly check collect compare compile concise confirm count describe
    detail details determine done end explain fact facts find focus gather give identify include
    including information item items key least list look main maximum minimum note number output point
    points provide question rank relevant report research result results return search short source
    sources specific state summarise summarize summary task top user using worker
    """.split()
)

_TOKEN = re.compile(r"[^\W_]+")


def stem(word: str) -> str:
    """Return the stem of a lower-case word under the counting rule.

    Args:
        word: One lower-case token.

    Returns:
        The word without a possessive ``'s``, with ``ies`` folded to ``y``, a final ``s`` (not ``ss``)
        removed, and then a final ``e`` removed.
    """
    w = word.removesuffix("'s").removesuffix("’s")
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        w = w[:-1]
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    return w


def terms(text: str) -> list[str]:
    """Return the terms of ``text`` in order, under the counting rule.

    Args:
        text: Any text.

    Returns:
        Lower-case tokens that hold a letter, have at least ``MIN_TERM_CHARS`` characters, and are not
        stop or instruction words.
    """
    out: list[str] = []
    for token in _TOKEN.findall(text.lower()):
        if len(token) < MIN_TERM_CHARS or not any(c.isalpha() for c in token):
            continue
        if token in STOP_WORDS or token in INSTRUCTION_WORDS:
            continue
        out.append(token)
    return out


def _known(s: str, reference: frozenset[str]) -> bool:
    if s in reference:
        return True
    for r in reference:
        short, long_ = (s, r) if len(s) <= len(r) else (r, s)
        if len(short) >= PREFIX_MIN and long_.startswith(short):
            return True
    return False


def novel_terms(goal: str, reference_text: str) -> list[str]:
    """Return the goal's terms that the reference text does not hold.

    Args:
        goal: A task goal.
        reference_text: The user's message.

    Returns:
        The novel terms, in goal order. Empty when the goal introduces no entity.
    """
    reference = frozenset(stem(t) for t in _TOKEN.findall(reference_text.lower()))
    return [t for t in terms(goal) if not _known(stem(t), reference)]


@dataclass(frozen=True)
class Task:
    """One brief: the goal and the constraints the planner wrote for a worker."""

    goal: str
    constraints: tuple[str, ...] = ()

    @property
    def chars(self) -> int:
        """Return the length of the brief: the goal plus every constraint."""
        return len(self.goal) + sum(len(c) for c in self.constraints)


def tasks_from_content(content: str | None) -> list[Task]:
    """Read every task of a planner reply in full.

    Args:
        content: The raw reply text of a decision row.

    Returns:
        One ``Task`` per task object. Empty for a decline or an unreadable reply.
    """
    data = load_plan_json(content)
    raw = data.get("tasks") if data is not None else None
    if not isinstance(raw, list):
        return []
    out: list[Task] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        constraints = item.get("constraints")
        out.append(
            Task(
                goal=str(item.get("goal") or ""),
                constraints=tuple(str(c) for c in constraints)
                if isinstance(constraints, list)
                else (),
            )
        )
    return out


@dataclass(frozen=True)
class GoalCount:
    """One scored goal, kept so a reader can audit every count."""

    label: str
    trial: int
    kind: str
    goal: str
    novel: tuple[str, ...]


@dataclass(frozen=True)
class ArmStats:
    """The AC-2 figures of one arm."""

    expansions: int
    goals: int
    goals_with_entity: int
    novel_term_total: int
    tasks_per_expansion: float
    goal_chars_p50: int | None
    goal_chars_mean: float | None
    brief_chars_p50: int | None
    brief_chars_mean: float | None
    by_kind: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    scored: tuple[GoalCount, ...] = ()

    @property
    def rate(self) -> float:
        """Return the share of goals that introduce an entity, 0.0 with no goal."""
        return self.goals_with_entity / self.goals if self.goals else 0.0


def _p50(values: Sequence[int]) -> int | None:
    p = percentile([float(v) for v in values], 0.5)
    return None if p is None else int(p)


def _mean(values: Sequence[int]) -> float | None:
    return statistics.fmean(values) if values else None


def arm_stats(rows: Sequence[Mapping[str, object]], queries: Mapping[str, str]) -> ArmStats:
    """Count one arm's briefs under the counting rule.

    Args:
        rows: The arm's ``decide.jsonl`` rows.
        queries: The user's message per fixture label.

    Returns:
        The arm's figures. A fixture without a query has an empty reference, so all its terms count.
    """
    good, _ = _dedupe(rows)
    scored: list[GoalCount] = []
    tasks_all: list[Task] = []
    expansions = 0
    for row in good:
        if classify(row.get("plan")) != "expand":
            continue
        tasks = tasks_from_content(str(row.get("content") or ""))
        if not tasks:
            continue
        expansions += 1
        tasks_all.extend(tasks)
        label = str(row.get("label"))
        for task in tasks:
            scored.append(
                GoalCount(
                    label=label,
                    trial=int(str(row.get("trial"))),
                    kind=str(row.get("kind", "single")),
                    goal=task.goal,
                    novel=tuple(novel_terms(task.goal, queries.get(label, ""))),
                )
            )
    by_kind: dict[str, tuple[int, int]] = {}
    for kind in sorted({g.kind for g in scored}):
        mine = [g for g in scored if g.kind == kind]
        by_kind[kind] = (sum(1 for g in mine if g.novel), len(mine))
    goal_chars = [len(t.goal) for t in tasks_all]
    brief_chars = [t.chars for t in tasks_all]
    return ArmStats(
        expansions=expansions,
        goals=len(scored),
        goals_with_entity=sum(1 for g in scored if g.novel),
        novel_term_total=sum(len(g.novel) for g in scored),
        tasks_per_expansion=len(tasks_all) / expansions if expansions else 0.0,
        goal_chars_p50=_p50(goal_chars),
        goal_chars_mean=_mean(goal_chars),
        brief_chars_p50=_p50(brief_chars),
        brief_chars_mean=_mean(brief_chars),
        by_kind=by_kind,
        scored=tuple(scored),
    )


def scorer_sha256() -> str:
    """Return the SHA-256 of this file, so a report names the counting rule that produced it."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


@dataclass(frozen=True)
class Arm:
    """One arm as the report shows it."""

    name: str
    run_dir: Path
    prompt_hash: str
    system: str
    first_row_ts: str | None
    stats: ArmStats


def load_arm(name: str, paths: RunPaths) -> Arm:
    """Read one arm's prompts and rows and count its briefs.

    Args:
        name: ``rule`` or ``baseline``.
        paths: The arm's run directory and tag.

    Returns:
        The arm.
    """
    prompts = json.loads(paths.prompts.read_text())
    fixtures = prompts.get("fixtures") or {}
    queries = {str(k): str((v or {}).get("query") or "") for k, v in fixtures.items()}
    rows = read_jsonl(paths.decide)
    stamps = sorted(str(r["ts"]) for r in rows if r.get("ts"))
    return Arm(
        name=name,
        run_dir=paths.run_dir,
        prompt_hash=str(prompts.get("prompt_hash") or ""),
        system=str(prompts.get("system") or ""),
        first_row_ts=stamps[0] if stamps else None,
        stats=arm_stats(rows, queries),
    )


@dataclass(frozen=True)
class Comparison:
    """The AC-2 result: both arms and the verdict."""

    rule: Arm
    baseline: Arm

    @property
    def passed(self) -> bool:
        """Return True when both the count and the rate of entity-introducing goals fall."""
        r, b = self.rule.stats, self.baseline.stats
        return b.goals > 0 and r.goals_with_entity < b.goals_with_entity and r.rate < b.rate

    def prompt_diff(self) -> list[str]:
        """Return the lines that differ between the two system prompts, prefixed ``-`` or ``+``."""
        diff = difflib.unified_diff(
            self.baseline.system.splitlines(), self.rule.system.splitlines(), lineterm="", n=0
        )
        return [
            f"{line[0]} {line[1:]}"
            for line in diff
            if line[:1] in "+-" and not line.startswith(("---", "+++"))
        ]

    def render(self) -> str:
        """Render the report as text."""

        def fmt(x: float | int | None, spec: str = "") -> str:
            return "n/a" if x is None else format(x, spec)

        r, b = self.rule.stats, self.baseline.stats
        rows = [
            ("expansions (draws)", fmt(b.expansions), fmt(r.expansions)),
            ("goals", fmt(b.goals), fmt(r.goals)),
            ("goals introducing an entity", fmt(b.goals_with_entity), fmt(r.goals_with_entity)),
            ("  rate", f"{100 * b.rate:.0f}%", f"{100 * r.rate:.0f}%"),
        ]
        for kind in sorted(set(b.by_kind) | set(r.by_kind)):
            bk, rk = b.by_kind.get(kind, (0, 0)), r.by_kind.get(kind, (0, 0))
            rows.append((f"  {kind} fixtures", f"{bk[0]}/{bk[1]}", f"{rk[0]}/{rk[1]}"))
        rows += [
            (
                "novel terms per goal",
                fmt(b.novel_term_total / b.goals if b.goals else None, ".2f"),
                fmt(r.novel_term_total / r.goals if r.goals else None, ".2f"),
            ),
            (
                "tasks per expansion",
                fmt(b.tasks_per_expansion, ".2f"),
                fmt(r.tasks_per_expansion, ".2f"),
            ),
            (
                "goal chars p50 / mean",
                f"{fmt(b.goal_chars_p50)} / {fmt(b.goal_chars_mean, '.0f')}",
                f"{fmt(r.goal_chars_p50)} / {fmt(r.goal_chars_mean, '.0f')}",
            ),
            (
                "brief chars p50 / mean",
                f"{fmt(b.brief_chars_p50)} / {fmt(b.brief_chars_mean, '.0f')}",
                f"{fmt(r.brief_chars_p50)} / {fmt(r.brief_chars_mean, '.0f')}",
            ),
        ]
        width = max(len(x[0]) for x in rows)
        lines = [
            "FRE-1514 AC-2 — brief goals that introduce an entity absent from the user's message",
            "",
            f"scorer sha256     {scorer_sha256()}",
        ]
        for arm in (self.baseline, self.rule):
            lines.append(
                f"{arm.name:<9} run   {arm.run_dir}  prompt {arm.prompt_hash}  first row {arm.first_row_ts}"
            )
        lines += ["", "System prompt, baseline -> rule"]
        lines += [f"  {x}" for x in self.prompt_diff()] or ["  (identical)"]
        lines += ["", f"  {'':<{width}}  {'baseline':>12}  {'rule':>12}"]
        lines += [f"  {n:<{width}}  {bv:>12}  {rv:>12}" for n, bv, rv in rows]
        verdict = "PASS" if self.passed else "FAIL"
        lines += [
            "",
            f"RESULT: {verdict} (needs the count and the rate both lower in the rule arm)",
        ]
        return "\n".join(lines) + "\n"

    def to_json(self) -> dict[str, object]:
        """Return the figures, every scored goal and the verdict as JSON-ready data."""

        def arm(a: Arm) -> dict[str, object]:
            s = a.stats
            return {
                "run_dir": str(a.run_dir),
                "prompt_hash": a.prompt_hash,
                "first_row_ts": a.first_row_ts,
                "expansions": s.expansions,
                "goals": s.goals,
                "goals_with_entity": s.goals_with_entity,
                "rate": s.rate,
                "by_kind": {k: list(v) for k, v in s.by_kind.items()},
                "novel_term_total": s.novel_term_total,
                "tasks_per_expansion": s.tasks_per_expansion,
                "goal_chars_p50": s.goal_chars_p50,
                "goal_chars_mean": s.goal_chars_mean,
                "brief_chars_p50": s.brief_chars_p50,
                "brief_chars_mean": s.brief_chars_mean,
                "scored": [
                    {
                        "label": g.label,
                        "trial": g.trial,
                        "kind": g.kind,
                        "goal": g.goal,
                        "novel": list(g.novel),
                    }
                    for g in s.scored
                ],
            }

        return {
            "scorer_sha256": scorer_sha256(),
            "passed": self.passed,
            "prompt_diff": self.prompt_diff(),
            "baseline": arm(self.baseline),
            "rule": arm(self.rule),
        }


def compare(rule: RunPaths, baseline: RunPaths) -> Comparison:
    """Load both arms and compare them.

    Args:
        rule: The rule arm's run directory and tag.
        baseline: The baseline arm's run directory and tag.

    Returns:
        The comparison.
    """
    return Comparison(rule=load_arm("rule", rule), baseline=load_arm("baseline", baseline))


def main(argv: Sequence[str] | None = None) -> int:
    """Compare two arms and write ``briefs.txt`` and ``briefs.json`` beside the rule arm's rows.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 on PASS, else 1.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rule-run", type=Path, required=True)
    parser.add_argument("--baseline-run", type=Path, required=True)
    parser.add_argument("--tag", default="planner")
    args = parser.parse_args(argv)
    rule = RunPaths(args.rule_run, args.tag)
    result = compare(rule, RunPaths(args.baseline_run, args.tag))
    text = result.render()
    (rule.rows / "briefs.txt").write_text(text)
    (rule.rows / "briefs.json").write_text(json.dumps(result.to_json(), indent=2))
    sys.stdout.write(text)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
