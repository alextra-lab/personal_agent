"""FRE-1590 Part 2: does a skill help? Each scenario runs with the skill catalog and without it.

For each scenario of ``part2_scenarios.yaml`` and each arm, one full turn runs on an eval gateway:
``treatment`` (:9003) has the skill catalog, ``control`` (:9002) has none (see
``docker-compose.fre1590.yml``, which also turns consolidation off on both). The model is a
catalog key sent as the ``model`` parameter of ``/chat``. The rows carry what a judge needs to
score the reply against the scenario's ``expect`` line; the judging is not done here.

Isolation (FRE-375, FRE-1372). Each turn starts a new session on a freshly wiped ``neo4j-eval``
(``wipe_eval_graph``), after no local model call is in flight. This does not call
``IsolatedArmRunner.run_turn``: with consolidation off there is no extraction to settle, and its
150 s idle guard would add 2.5 minutes to every turn. The wipe still runs before every turn and
cannot be skipped. ``graph_nodes_after`` (a node count right after the turn) proves that the turn
wrote nothing to the graph. Every URL is an eval constant from ``eval_isolation`` and its
imports.

The two arms run in alternating order (control first on even scenarios, treatment first on odd
ones), so a warm model cache does not favour one arm.

Run from the repo root, after the override is up (see its header):

    export NEO4J_PASSWORD=...   # the eval Neo4j password only
    uv run python -m scripts.eval.fre1590.help run --run-dir <dir> --model qwen3.8-flash-next
    uv run python -m scripts.eval.fre1590.help report --run-dir <dir> --model qwen3.8-flash-next

Rows go to ``<dir>/rows/<model>/part2.jsonl``. A rerun skips each (id, arm) with a row without an
error. The report goes to ``<dir>/rows/<model>/part2_report.json``.
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
from typing import TYPE_CHECKING, Literal, cast, get_args

import httpx
import yaml  # type: ignore[import-untyped]
from scripts.eval.eval_isolation import (
    _CLIENT_TIMEOUT_BUFFER_S,
    create_eval_driver,
    wait_for_local_model_calls,
)
from scripts.eval.fre1337_intent_probe.behavioral import EVAL_ES_INDEX, EVAL_ES_URL
from scripts.eval.fre1337_intent_probe.substrate import (
    EVAL_ARMS,
    EVAL_NEO4J_URI,
    assert_eval_chat_url,
    wipe_eval_graph,
)
from scripts.eval.fre1581.g2_skill_choice import turn_events_query

from personal_agent.config import settings

if TYPE_CHECKING:
    from neo4j import AsyncDriver

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
SKILLS_DIR = REPO_ROOT / "docs" / "skills"
SCENARIOS_FILE = HERE / "part2_scenarios.yaml"

#: The models of FRE-1590, as catalog keys (config/models.yaml).
MODELS = ("qwen3.8-flash-next", "gemma-4-26b-a4b")

Arm = Literal["control", "treatment"]
ARMS: tuple[Arm, ...] = get_args(Arm)

GRAPH_COUNT_CYPHER = "MATCH (n) RETURN count(n) AS n"

#: Event types read for each turn, by row field.
_COUNTED_EVENTS = {
    "tool_failures": "tool_call_failed",
    "constraint_pauses": "constraint_pause_emitted",
    "force_synthesis": "force_synthesis_injected",
}

Row = dict[str, object]


@dataclass(frozen=True)
class Scenario:
    """One Part 2 scenario, with its prompt and expect line from the skill's evals.yaml.

    Attributes:
        id: ``<skill>#<n>``, n the 0-based position in the skill's ``scenarios`` list.
        skill: The skill the scenario belongs to.
        prompt: The user message.
        expect: What a right answer does, for the judge.
        reason: The knowledge only the skill has (from part2_scenarios.yaml).
    """

    id: str
    skill: str
    prompt: str
    expect: str
    reason: str


def _skill_scenarios(skills_dir: Path, skill: str) -> list[Mapping[str, object]]:
    path = skills_dir / skill / "evals.yaml"
    if not path.is_file():
        raise ValueError(f"no evals.yaml for skill {skill!r} ({path})")
    data = yaml.safe_load(path.read_text())
    scenarios = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(scenarios, list):
        raise ValueError(f"{path} has no scenarios list")
    return scenarios


def load_scenarios(path: Path = SCENARIOS_FILE, skills_dir: Path = SKILLS_DIR) -> list[Scenario]:
    """Load the Part 2 scenario list and resolve each id against its skill's evals.yaml.

    Args:
        path: The Part 2 scenario list.
        skills_dir: The directory of skill folders.

    Returns:
        The scenarios, in file order.

    Raises:
        ValueError: An id is malformed, repeated, also excluded, or names no scenario.
    """
    data = yaml.safe_load(path.read_text())
    excluded = {str(e["id"]) for e in data.get("excluded") or []}
    out: list[Scenario] = []
    seen: set[str] = set()
    for entry in data["scenarios"]:
        sid, reason = str(entry["id"]), str(entry["reason"])
        skill, sep, index = sid.partition("#")
        if not sep or not skill or not index.isdigit():
            raise ValueError(f"malformed scenario id {sid!r}: expected <skill>#<n>")
        if sid in seen:
            raise ValueError(f"scenario id {sid!r} is listed twice")
        if sid in excluded:
            raise ValueError(f"scenario id {sid!r} is both listed and excluded")
        seen.add(sid)
        rows = _skill_scenarios(skills_dir, skill)
        n = int(index)
        if n >= len(rows):
            raise ValueError(f"unknown scenario {sid!r}: {skill} has {len(rows)} scenarios")
        out.append(
            Scenario(
                sid, skill, str(rows[n]["prompt"]), " ".join(str(rows[n]["expect"]).split()), reason
            )
        )
    return out


def select(scenarios: Sequence[Scenario], ids: Sequence[str], limit: int) -> list[Scenario]:
    """Pick the scenarios to run.

    Args:
        scenarios: Every Part 2 scenario.
        ids: Only these ids, when given. Each must be a Part 2 scenario.
        limit: At most this many, when above 0.

    Returns:
        The picked scenarios, in list order.

    Raises:
        ValueError: An id is not a Part 2 scenario.
    """
    picked = list(scenarios)
    if ids:
        known = {s.id for s in scenarios}
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise ValueError(f"not Part 2 scenarios: {', '.join(unknown)}")
        wanted = set(ids)
        picked = [s for s in scenarios if s.id in wanted]
    return picked[:limit] if limit > 0 else picked


def parse_arms(text: str) -> tuple[Arm, ...]:
    """Parse ``--arms``.

    Args:
        text: Comma-separated arm names.

    Returns:
        The arms, in the given order.

    Raises:
        ValueError: An arm is unknown or repeated, or none is given.
    """
    names = [a.strip() for a in text.split(",") if a.strip()]
    bad = [a for a in names if a not in ARMS or a not in EVAL_ARMS]
    if bad or not names or len(set(names)) != len(names):
        raise ValueError(f"--arms must name each of {', '.join(ARMS)} at most once, got {text!r}")
    return tuple(cast(Arm, a) for a in names)


def arm_order(arms: Sequence[Arm], position: int) -> tuple[Arm, ...]:
    """Alternate the arm order by scenario position, so a warm cache favours neither arm.

    Args:
        arms: The selected arms.
        position: The scenario's position in the selection.

    Returns:
        The arms in the order to run.
    """
    return tuple(arms) if position % 2 == 0 else tuple(reversed(arms))


def rows_path(run_dir: Path, model: str) -> Path:
    """Return the JSONL file of one model's rows."""
    return run_dir / "rows" / model / "part2.jsonl"


def read_rows(path: Path) -> list[Row]:
    """Read the rows of a JSONL file; an absent file has none."""
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def done_keys(rows: Sequence[Mapping[str, object]]) -> set[tuple[str, str]]:
    """Return the (id, arm) pairs that have a row without an error."""
    return {(str(r["id"]), str(r["arm"])) for r in rows if "error" not in r}


def _num(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


async def _events(es: httpx.AsyncClient, trace_id: str, event_type: str) -> list[dict[str, object]]:
    body = turn_events_query(trace_id, event_type)
    resp = await es.post(f"{EVAL_ES_URL}/{EVAL_ES_INDEX}/_search", json=body, timeout=30.0)
    resp.raise_for_status()
    hits = resp.json()["hits"]["hits"]
    size = body.get("size")
    if isinstance(size, int) and len(hits) >= size:
        raise ValueError(f"{event_type} for trace {trace_id} filled its page of {size}")
    return [h["_source"] for h in hits]


async def read_turn(es: httpx.AsyncClient, trace_id: str, arm: Arm) -> Row:
    """Read one turn's skill loads, tool calls, failures, pauses, model calls and cost.

    Args:
        es: A client for ``elasticsearch-eval``.
        trace_id: The turn's trace id.
        arm: The arm that served the turn.

    Returns:
        The row fields read from ``agent-logs-*``.

    Raises:
        ValueError: The turn has no ``model_call_completed`` event (the query or the mapping is
            wrong, and empty lists would read as "nothing happened"), no ``skill_index_assembled``
            event, or a catalog that does not fit its arm (control must have none, treatment one).
    """
    completed = await _events(es, trace_id, "model_call_completed")
    if not completed:
        raise ValueError(f"no model_call_completed event for trace {trace_id}")
    catalog = [
        _num(e.get("index_chars")) for e in await _events(es, trace_id, "skill_index_assembled")
    ]
    if not catalog:
        raise ValueError(f"no skill_index_assembled event for trace {trace_id}")
    if arm == "control" and any(catalog):
        raise ValueError(f"control turn {trace_id} saw a skill catalog of {max(catalog):.0f} chars")
    if arm == "treatment" and not all(catalog):
        raise ValueError(f"treatment turn {trace_id} saw no skill catalog")
    invoked = await _events(es, trace_id, "read_skill_invoked")
    started = await _events(es, trace_id, "tool_call_started")
    permissions = await _events(es, trace_id, "permission_evaluated")
    costs = await _events(es, trace_id, "api_cost_recorded")
    row: Row = {
        "skills_loaded": [str(e.get("skill_name")) for e in invoked],
        "skill_paths": [str(e["path"]) for e in invoked if e.get("path")],
        "tool_calls": [str(e.get("tool_name")) for e in started],
        "permission_denials": sum(e.get("decision") != "allow" for e in permissions),
        "model_calls": len(completed),
        "primary_models": sorted(
            {str(e.get("model")) for e in completed if e.get("role") == "primary"}
        ),
        "cost_usd": round(sum(_num(e.get("cost_usd")) for e in costs), 6),
        "cost_events": len(costs),
        "skill_catalog_chars": max(catalog),
    }
    for field, event_type in _COUNTED_EVENTS.items():
        row[field] = len(await _events(es, trace_id, event_type))
    return row


async def count_graph_nodes(driver: AsyncDriver) -> int:
    """Count every node in ``neo4j-eval``.

    Args:
        driver: A driver from ``create_eval_driver`` (bound to ``EVAL_NEO4J_URI``).

    Returns:
        The node count.
    """
    async with driver.session() as session:
        result = await session.run(GRAPH_COUNT_CYPHER)
        record = await result.single()
    return int(record["n"]) if record is not None else 0


@dataclass(frozen=True)
class RunConfig:
    """What one ``run`` does.

    Attributes:
        run_dir: The run directory.
        model: The catalog key sent as ``/chat``'s ``model``.
        arms: The arms to run.
        settle_s: Seconds to wait after a turn before its events are read.
        gap_s: Seconds to wait once no local model call is in flight.
    """

    run_dir: Path
    model: str
    arms: tuple[Arm, ...]
    settle_s: float = 20.0
    gap_s: float = 10.0


async def run_one(
    scenario: Scenario,
    arm: Arm,
    position: int,
    cfg: RunConfig,
    *,
    http: httpx.AsyncClient,
    es: httpx.AsyncClient,
    driver: AsyncDriver,
) -> Row:
    """Wipe the eval graph, wait for the model, run one turn in a new session, read its events.

    Args:
        scenario: The scenario.
        arm: The arm.
        position: 0 or 1: whether this arm ran first or second for the scenario.
        cfg: The run configuration.
        http: A client for the eval gateway.
        es: A client for ``elasticsearch-eval``.
        driver: A driver for ``neo4j-eval``.

    Returns:
        The row. A transport, HTTP, missing-key or value failure is an ``error`` row.
    """
    base_url = EVAL_ARMS[arm]
    assert_eval_chat_url(base_url)
    row: Row = {
        "id": scenario.id,
        "skill": scenario.skill,
        "arm": arm,
        "arm_position": position,
        "model": cfg.model,
        "prompt": scenario.prompt,
        "expect": scenario.expect,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    t0: float | None = None
    try:
        await wipe_eval_graph(driver, uri=EVAL_NEO4J_URI)
        await wait_for_local_model_calls(es, gap_s=cfg.gap_s)
        params = {"message": scenario.prompt, "channel": "EVAL", "model": cfg.model}
        timeout = settings.orchestrator_turn_lifetime_seconds + _CLIENT_TIMEOUT_BUFFER_S
        t0 = time.monotonic()
        resp = await http.post(f"{base_url}/chat", params=params, timeout=timeout)
        row["wall_s"] = round(time.monotonic() - t0, 1)
        resp.raise_for_status()
        data = resp.json()
        trace_id = str(data["trace_id"])
        row.update(
            trace_id=trace_id,
            session_id=str(data["session_id"]),
            reply=str(data["response"]),
            primary_selection=data.get("primary_selection"),
        )
        await asyncio.sleep(cfg.settle_s)
        row.update(await read_turn(es, trace_id, arm))
        row["graph_nodes_after"] = await count_graph_nodes(driver)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        row["error"] = repr(exc)[:300]
        if t0 is not None and "wall_s" not in row:
            row["wall_s"] = round(time.monotonic() - t0, 1)
    return row


async def run_part2(
    cfg: RunConfig,
    scenarios: Sequence[Scenario],
    *,
    http: httpx.AsyncClient,
    es: httpx.AsyncClient,
    driver: AsyncDriver,
) -> list[Row]:
    """Run each (scenario, arm) that has no row without an error; append each row as it lands.

    Args:
        cfg: The run configuration.
        scenarios: The selected scenarios.
        http: A client for the eval gateways.
        es: A client for ``elasticsearch-eval``.
        driver: A driver for ``neo4j-eval``.

    Returns:
        The rows written by this call.
    """
    out = rows_path(cfg.run_dir, cfg.model)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = done_keys(read_rows(out))
    written: list[Row] = []
    for index, scenario in enumerate(scenarios):
        for position, arm in enumerate(arm_order(cfg.arms, index)):
            if (scenario.id, arm) in done:
                continue
            row = await run_one(scenario, arm, position, cfg, http=http, es=es, driver=driver)
            with out.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            written.append(row)
            print(
                scenario.id,
                arm,
                row.get("skills_loaded"),
                row.get("wall_s"),
                str(row.get("error", ""))[:80],
                flush=True,
            )
    return written


_SUMMARY_FIELDS = (
    "trace_id",
    "skills_loaded",
    "tool_calls",
    "tool_failures",
    "permission_denials",
    "constraint_pauses",
    "force_synthesis",
    "model_calls",
    "cost_usd",
    "wall_s",
    "graph_nodes_after",
    "primary_models",
)


def build_report(rows: Sequence[Mapping[str, object]], model: str) -> dict[str, object]:
    """Build the AC-3 skeleton: both arms of each scenario side by side.

    The last row without an error counts for each (id, arm).

    Args:
        rows: The rows of one model.
        model: The model key.

    Returns:
        The report: ``scenarios`` (each with its arms), ``one_arm`` (ids with one arm only, so
        AC-3 fails), ``errored`` (``id/arm`` with error rows only), ``graph_writes`` (``id/arm``
        that left nodes in the graph) and ``paired``.
    """
    kept: dict[tuple[str, str], Mapping[str, object]] = {}
    order: list[str] = []
    for r in rows:
        sid = str(r["id"])
        if sid not in order:
            order.append(sid)
        if "error" not in r:
            kept[(sid, str(r["arm"]))] = r
    errored = sorted(
        {f"{r['id']}/{r['arm']}" for r in rows if "error" in r}
        - {f"{sid}/{arm}" for sid, arm in kept}
    )
    scenarios: list[dict[str, object]] = []
    one_arm: list[str] = []
    paired = 0
    for sid in order:
        arms = {arm: kept[(sid, arm)] for arm in ARMS if (sid, arm) in kept}
        if len(arms) == 1:
            one_arm.append(sid)
        paired += len(arms) == len(ARMS)
        skill = sid.partition("#")[0]
        treatment = arms.get("treatment")
        loaded = treatment.get("skills_loaded") if treatment else None
        scenarios.append(
            {
                "id": sid,
                "skill": skill,
                "treatment_loaded_skill": isinstance(loaded, list) and skill in loaded,
                "arms": {a: {f: r.get(f) for f in _SUMMARY_FIELDS} for a, r in arms.items()},
            }
        )
    graph_writes = sorted(
        f"{sid}/{arm}" for (sid, arm), r in kept.items() if _num(r.get("graph_nodes_after")) > 0
    )
    return {
        "model": model,
        "scenarios_paired": paired,
        "one_arm": one_arm,
        "errored": errored,
        "graph_writes": graph_writes,
        "paired": bool(scenarios) and not one_arm and not errored,
        "scenarios": scenarios,
    }


def render_report(report: Mapping[str, object]) -> str:
    """Render the report as text.

    Args:
        report: The output of :func:`build_report`.

    Returns:
        The text.
    """

    def listed(key: str) -> str:
        value = report.get(key)
        return ", ".join(str(v) for v in value) if isinstance(value, list) and value else "none"

    lines = [f"FRE-1590 Part 2 — skill help, model {report['model']}"]
    scenarios = report.get("scenarios")
    for s in scenarios if isinstance(scenarios, list) else []:
        lines.append(f"{s['id']}  (treatment loaded {s['skill']}: {s['treatment_loaded_skill']})")
        for arm, a in s["arms"].items():
            lines.append(
                f"  {arm:<9} trace={a['trace_id']} skills={a['skills_loaded']} "
                f"tools={a['tool_calls']} failures={a['tool_failures']} "
                f"denials={a['permission_denials']} pauses={a['constraint_pauses']} "
                f"forced={a['force_synthesis']} cost={a['cost_usd']} wall={a['wall_s']}s "
                f"nodes_after={a['graph_nodes_after']}"
            )
    lines += [
        f"scenarios with both arms: {report['scenarios_paired']}",
        f"one arm only (AC-3 fails): {listed('one_arm')}",
        f"error rows only, not paired: {listed('errored')}",
        f"turns that left graph nodes: {listed('graph_writes')}",
        f"RESULT: {'PAIRED' if report['paired'] else 'NOT PAIRED'}",
    ]
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> int:
    from scripts.eval.gateway_freshness import assert_gateway_fresh

    cfg = RunConfig(
        run_dir=Path(args.run_dir),
        model=args.model,
        arms=parse_arms(args.arms),
        settle_s=args.settle_s,
    )
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    scenarios = select(load_scenarios(), ids, args.limit)
    async with httpx.AsyncClient() as http, httpx.AsyncClient() as es:
        for arm in cfg.arms:
            fresh = await assert_gateway_fresh(http, EVAL_ARMS[arm], Path(args.build_root))
            print(f"{arm} gateway fresh: {fresh}", flush=True)
        driver = create_eval_driver()
        try:
            rows = await run_part2(cfg, scenarios, http=http, es=es, driver=driver)
        finally:
            await driver.close()
    errors = sum("error" in r for r in rows)
    print(f"{len(rows)} rows written, {errors} errors -> {rows_path(cfg.run_dir, cfg.model)}")
    return 1 if errors else 0


def _report(args: argparse.Namespace) -> int:
    path = rows_path(Path(args.run_dir), args.model)
    report = build_report(read_rows(path), args.model)
    (path.parent / "part2_report.json").write_text(json.dumps(report, indent=2) + "\n")
    sys.stdout.write(render_report(report))
    return 0 if report["paired"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, then run turns or report.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        ``run``: 0 when no row has an error. ``report``: 0 when every scenario has both arms.
    """
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run each scenario on each arm")
    run.add_argument("--run-dir", required=True)
    run.add_argument("--model", required=True, choices=MODELS)
    run.add_argument("--arms", default=",".join(ARMS))
    run.add_argument("--limit", type=int, default=0, help="at most this many scenarios")
    run.add_argument("--ids", default="", help="comma-separated scenario ids")
    run.add_argument("--settle-s", type=float, default=20.0)
    run.add_argument(
        "--build-root", default="/opt/seshat", help="the tree the eval image was built from"
    )
    report = sub.add_parser("report", help="pair the arms of each scenario (AC-3)")
    report.add_argument("--run-dir", required=True)
    report.add_argument("--model", required=True, choices=MODELS)
    args = p.parse_args(argv)
    if args.command == "run":
        return asyncio.run(_run(args))
    return _report(args)


if __name__ == "__main__":
    sys.exit(main())
