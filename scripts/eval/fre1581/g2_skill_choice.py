"""FRE-1581 / ADR-0156 D10 G2: does the model load the right skill, and no skill when none is needed?

Runs every scenario of ``skills/*/evals.yaml`` and the 20 prompts of ``no_skill_prompts.yaml`` as
one fresh session each, one draw, on the eval treatment gateway (:9003) through ``IsolatedArmRunner``
(FRE-375, FRE-1372). The primary model is the gateway's default binding. Each turn is scored from the
``read_skill_invoked`` events of its trace in ``elasticsearch-eval``:

- a scenario is right only if its expected skill loads and no other skill loads;
- a no-skill prompt is wrong if any skill loads.

AC-2 fails if fewer than 33 scenarios ran, fewer than 90% are right, or more than 2 of the 20 no-skill
prompts load a skill. A worker that loads a skill counts like the primary. Each row also records whether
a worker called ``read_skill`` (from the sub-agent captures), so the report can show the primary-only
split.

One JSONL row per turn, appended when it lands. A rerun resumes and skips ids with a row without an
error. Run from the repo root, after ``make eval-infra-up`` from ``/opt/seshat``:

    export NEO4J_PASSWORD=...   # the eval Neo4j password only
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. uv run python -u scripts/eval/fre1581/g2_skill_choice.py --run-id <id>
    PYTHONPATH=. uv run python scripts/eval/fre1581/g2_skill_choice.py --run-id <id> --score-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
SKILLS_DIR = REPO_ROOT / "skills"
NO_SKILL_PROMPTS = HERE / "no_skill_prompts.yaml"
OUT_DIR = REPO_ROOT / "telemetry" / "evaluation" / "fre1581-g2"
EVAL_ES = "http://localhost:9202"
EVAL_CHAT = "http://localhost:9003"

# ADR-0156 AC-2 bounds. Changing one needs the owner's approval before the run.
MIN_SCENARIOS = 33
MIN_RIGHT_SHARE = 0.90
MAX_NO_SKILL_LOADS = 2
NO_SKILL_COUNT = 20

Kind = Literal["scenario", "no_skill"]


@dataclass(frozen=True)
class Item:
    """One G2 turn.

    Attributes:
        id: Unique id: ``<skill>#<n>`` for a scenario, ``nsNN`` for a no-skill prompt.
        kind: ``scenario`` or ``no_skill``.
        prompt: The user message.
        expected_skill: The skill a scenario must load, or ``None`` for a no-skill prompt.
    """

    id: str
    kind: Kind
    prompt: str
    expected_skill: str | None


def load_items(skills_dir: Path = SKILLS_DIR, no_skill: Path = NO_SKILL_PROMPTS) -> list[Item]:
    """Load every evals.yaml scenario, then the no-skill prompts.

    A scenario that names no ``expected_skill`` expects the skill of its own file.

    Args:
        skills_dir: The directory of agent skill folders.
        no_skill: The no-skill prompts file.

    Returns:
        The items, scenarios first in skill order.
    """
    items: list[Item] = []
    for path in sorted(skills_dir.glob("*/evals.yaml")):
        data = yaml.safe_load(path.read_text())
        for n, sc in enumerate(data["scenarios"]):
            # A scenario without `expected_skill` expects the skill of its own file.
            expected = str(sc.get("expected_skill") or data["skill"])
            items.append(Item(f"{data['skill']}#{n}", "scenario", str(sc["prompt"]), expected))
    for p in yaml.safe_load(no_skill.read_text())["prompts"]:
        items.append(Item(str(p["id"]), "no_skill", str(p["prompt"]), None))
    return items


def is_right(kind: str, expected: str | None, loaded: Sequence[str]) -> bool:
    """Score one turn by ADR-0156 AC-2.

    Args:
        kind: ``scenario`` or ``no_skill``.
        expected: The expected skill of a scenario.
        loaded: Every skill name that ``read_skill_invoked`` reported on the turn, repeats included.

    Returns:
        For a scenario: the expected skill loaded and no other. For a no-skill prompt: nothing loaded.
    """
    if kind == "no_skill":
        return not loaded
    return set(loaded) == {expected}


@dataclass(frozen=True)
class G2Result:
    """The G2 verdict.

    Attributes:
        scenarios_run: Scenarios with a row without an error.
        scenarios_right: Of those, the right ones.
        no_skill_run: No-skill prompts with a row without an error.
        no_skill_loaded: Of those, the ones on which a skill loaded.
        primary_only_right: Scenarios right when worker loads are ignored, for rows where no worker
            called ``read_skill`` (informational).
        wrong: The ids of the wrong turns, with what loaded.
        errored: The ids that have only error rows. They are not scored, so a rerun must
            complete them; the report names them so that none leaves the count unseen.
    """

    scenarios_run: int
    scenarios_right: int
    no_skill_run: int
    no_skill_loaded: int
    primary_only_right: int
    wrong: Sequence[str]
    errored: Sequence[str] = ()

    @property
    def passed(self) -> bool:
        """Return True when every AC-2 bound holds."""
        return (
            self.scenarios_run >= MIN_SCENARIOS
            and self.scenarios_right >= MIN_RIGHT_SHARE * self.scenarios_run
            and self.no_skill_run == NO_SKILL_COUNT
            and self.no_skill_loaded <= MAX_NO_SKILL_LOADS
        )

    def render(self) -> str:
        """Render the verdict as text."""
        share = self.scenarios_right / self.scenarios_run if self.scenarios_run else 0.0
        lines = [
            "FRE-1581 G2 — skill choice (ADR-0156 AC-2)",
            f"  scenarios run: {self.scenarios_run} (need >= {MIN_SCENARIOS})",
            f"  right skill and no other: {self.scenarios_right}/{self.scenarios_run} = {share:.0%}"
            f" (need >= {MIN_RIGHT_SHARE:.0%})",
            f"  no-skill prompts run: {self.no_skill_run} (need {NO_SKILL_COUNT})",
            f"  no-skill prompts that loaded a skill: {self.no_skill_loaded}"
            f" (need <= {MAX_NO_SKILL_LOADS})",
            f"  informational, primary-only rows right: {self.primary_only_right}",
            "  wrong: " + ("; ".join(self.wrong) or "none"),
            "  errored, not scored: " + (", ".join(self.errored) or "none"),
            f"RESULT: {'PASS' if self.passed else 'FAIL'}",
        ]
        return "\n".join(lines) + "\n"


def score(rows: Sequence[Mapping[str, object]]) -> G2Result:
    """Score G2 from the runner rows. The last row without an error per id counts.

    Args:
        rows: The runner rows.

    Returns:
        The verdict.
    """
    kept: dict[str, Mapping[str, object]] = {}
    for r in rows:
        if "error" not in r:
            kept[str(r["id"])] = r
    run = list(kept.values())
    scenarios = [r for r in run if r["kind"] == "scenario"]
    no_skill = [r for r in run if r["kind"] == "no_skill"]

    def loaded(r: Mapping[str, object]) -> list[str]:
        value = r.get("skills_loaded")
        return [str(x) for x in value] if isinstance(value, list) else []

    def right(r: Mapping[str, object]) -> bool:
        expected = r.get("expected_skill")
        return is_right(str(r["kind"]), None if expected is None else str(expected), loaded(r))

    wrong = [f"{r['id']} loaded {loaded(r) or 'nothing'}" for r in run if not right(r)]
    return G2Result(
        scenarios_run=len(scenarios),
        scenarios_right=sum(right(r) for r in scenarios),
        no_skill_run=len(no_skill),
        no_skill_loaded=sum(bool(loaded(r)) for r in no_skill),
        primary_only_right=sum(right(r) for r in scenarios if r.get("worker_read_skill") is False),
        wrong=sorted(wrong),
        errored=sorted({str(r["id"]) for r in rows if "error" in r} - set(kept)),
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _search(es: httpx.Client, index: str, body: Mapping[str, object]) -> list[dict[str, object]]:
    r = es.post(f"{EVAL_ES}/{index}/_search", json=body, timeout=30.0)
    r.raise_for_status()
    return [h["_source"] for h in r.json()["hits"]["hits"]]


def turn_events_query(trace_id: str, event_type: str) -> dict[str, object]:
    """Build the ``agent-logs-*`` search for one event type of one turn.

    The fields are those of the production ``agent-logs`` template, where ``trace_id`` and
    ``event_type`` are ``keyword``. ``make eval-infra-up`` applies that template to eval ES.

    Args:
        trace_id: The turn's trace id.
        event_type: The event type.

    Returns:
        The search body.
    """
    return {
        "size": 500,
        "sort": [{"@timestamp": "asc"}],
        "query": {
            "bool": {
                "filter": [
                    {"term": {"trace_id": trace_id}},
                    {"term": {"event_type": event_type}},
                ]
            }
        },
    }


def read_turn(es: httpx.Client, trace_id: str, settle_s: float = 20.0) -> dict[str, object]:
    """Read one turn's skill loads, read_skill calls and primary model from the eval ES.

    Args:
        es: A client for ``elasticsearch-eval``.
        trace_id: The turn's trace id.
        settle_s: Seconds to wait for late events after the turn returns.

    Returns:
        ``skills_loaded``, ``read_skill_calls`` (``tool_call_started`` count, a cross-check),
        ``worker_read_skill`` and ``primary_models``.

    Raises:
        ValueError: If the turn shows no ``model_call_completed`` event. Every completed turn has
            one, so the query or the index mapping is wrong, and an empty skill list would be
            scored as "no skill loaded".
    """
    time.sleep(settle_s)

    def events(event_type: str) -> list[dict[str, object]]:
        return _search(es, "agent-logs-*", turn_events_query(trace_id, event_type))

    completed = events("model_call_completed")
    if not completed:
        raise ValueError(f"no model_call_completed event for trace {trace_id}")
    invoked = events("read_skill_invoked")
    started = [e for e in events("tool_call_started") if e.get("tool_name") == "read_skill"]
    models = sorted({str(e.get("model")) for e in completed if e.get("role") == "primary"})
    subs = _search(
        es,
        "agent-captains-captures-subagents-*",
        {"size": 50, "query": {"term": {"trace_id": trace_id}}, "_source": ["tools_used"]},
    )
    worker = any("read_skill" in (s.get("tools_used") or []) for s in subs)  # type: ignore[operator]
    return {
        "skills_loaded": [str(e.get("skill_name")) for e in invoked],
        "skill_paths": [e.get("path") for e in invoked],
        "read_skill_calls": len(started),
        "worker_read_skill": worker,
        "sub_agents": len(subs),
        "primary_models": models,
    }


async def run(args: argparse.Namespace) -> int:
    """Run every item that has no completed row, then score.

    Args:
        args: Parsed arguments.

    Returns:
        0 when G2 passed, 1 when it failed.
    """
    from scripts.eval.eval_isolation import IsolatedArmRunner, create_eval_driver
    from scripts.eval.gateway_freshness import assert_gateway_fresh

    out = OUT_DIR / args.run_id / "g2.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {str(r["id"]) for r in _read_jsonl(out) if "error" not in r}
    items = [i for i in load_items() if i.id not in done]
    if args.limit:
        items = items[: args.limit]
    async with httpx.AsyncClient() as http, httpx.AsyncClient() as es_async:
        fresh = await assert_gateway_fresh(http, EVAL_CHAT, Path(args.build_root))
        print(f"eval gateway fresh: {fresh}", flush=True)
        driver = create_eval_driver()
        runner = IsolatedArmRunner(driver=driver)
        try:
            with httpx.Client() as es:
                for item in items:
                    row: dict[str, object] = {
                        "id": item.id,
                        "kind": item.kind,
                        "expected_skill": item.expected_skill,
                        "prompt": item.prompt,
                        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    t0 = time.monotonic()
                    try:
                        turn = await runner.run_turn(http, es_async, item.prompt, arm="treatment")
                        row.update(session_id=turn.session_id, trace_id=turn.trace_id)
                        row.update(read_turn(es, turn.trace_id))
                    except (httpx.HTTPError, KeyError, ValueError) as exc:
                        row["error"] = repr(exc)[:300]
                    row["wall_s"] = round(time.monotonic() - t0, 1)
                    with out.open("a") as fh:
                        fh.write(json.dumps(row) + "\n")
                    print(
                        item.id,
                        row.get("skills_loaded"),
                        row.get("wall_s"),
                        str(row.get("error", ""))[:80],
                        flush=True,
                    )
        finally:
            await driver.close()
    result = score(_read_jsonl(out))
    (out.parent / "report.txt").write_text(result.render())
    sys.stdout.write(result.render())
    return 0 if result.passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, then run or only score.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 when G2 passed, 1 when it failed.
    """
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run-id", required=True)
    p.add_argument(
        "--build-root", default="/opt/seshat", help="the tree the eval image was built from"
    )
    p.add_argument("--limit", type=int, default=0, help="run at most this many items (a dry run)")
    p.add_argument("--score-only", action="store_true")
    args = p.parse_args(argv)
    if args.score_only:
        result = score(_read_jsonl(OUT_DIR / args.run_id / "g2.jsonl"))
        sys.stdout.write(result.render())
        return 0 if result.passed else 1
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
