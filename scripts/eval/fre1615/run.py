"""FRE-1615 Block 1: run each suite task as a full turn on each planner arm, then report per arm.

An arm is a name mapped to an eval gateway (``docker-compose.fre1615.yml`` sets what each gateway runs):

    phase A: --arms S=control,PL=treatment   (S: expansion off; PL: the production planner)
    phase B: --arms P2=treatment --ids <the tasks PL expanded>

Per (task, arm), like ``scripts/eval/fre1590/help.py``: wipe ``neo4j-eval``; for ``mr`` tasks write the
three memory facts (``memory_seed.py``, run inside the arm's gateway); wait until no local model call is in
flight; then the turn in a new session. A follow-up task (``history_user``) runs its first turn live on the
same arm, then the follow-up in that session; the row scores the follow-up and keeps the first turn's trace.
As in ``help.py``, this does not call ``IsolatedArmRunner.run_turn``: consolidation is off (no extraction to
settle), and its 150 s idle guard would add 2.5 minutes to every turn. The wipe runs before every task (once per follow-up pair).
The arm order alternates by task position. After the turn: the turn's ES events (``help.read_turn``), the
Stage 5 decision, the tool arguments, the llama.cpp per-call counts from Tempo (``slm-server`` spans of the
turn's trace) and the turn's FRE-1614 prompt captures (copied into the run directory).

Run from the repo root, with the eval passwords in the environment:

    export NEO4J_PASSWORD=...   # the eval Neo4j password only
    uv run python -m scripts.eval.fre1615.run run --run-dir <run> --arms S=control,PL=treatment --limit 1
    uv run python -m scripts.eval.fre1615.run report --run-dir <run>

Rows go to ``<run>/rows.jsonl``. A rerun skips each (task, arm) with a row without an error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess  # nosec B404 — fixed docker argv, no shell
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
from scripts.eval.eval_isolation import (
    _CLIENT_TIMEOUT_BUFFER_S,
    create_eval_driver,
    wait_for_local_model_calls,
)
from scripts.eval.fre1337_intent_probe.substrate import (
    EVAL_ARMS,
    EVAL_NEO4J_URI,
    assert_eval_chat_url,
    wipe_eval_graph,
)
from scripts.eval.fre1590 import help as part2
from scripts.eval.fre1615.capture import load_suite

from personal_agent.config import settings

if TYPE_CHECKING:
    from neo4j import AsyncDriver

HERE = Path(__file__).resolve().parent
SEED_SCRIPT = HERE / "memory_seed.py"
MODEL_KEY = "qwen3.8-flash-next"
TEMPO_URL = "http://172.25.0.11:3200"
CONTAINERS: dict[part2.Arm, str] = {
    "control": "cloud-sim-seshat-gateway-control",
    "treatment": "cloud-sim-seshat-gateway-treatment",
}
PROMPT_DEBUG_DIR = "/app/telemetry/prompt_debug"
_SLM_FIELDS = (
    "slm.prompt_n",
    "slm.cache_reuse",
    "slm.prefill_ms",
    "slm.decode_ms",
    "slm.predicted_n",
)

Row = dict[str, object]
Task = Mapping[str, object]


def parse_arms(text: str) -> tuple[tuple[str, part2.Arm], ...]:
    """Parse ``--arms`` (``NAME=gateway,...``).

    Args:
        text: Comma-separated ``name=gateway`` pairs; a gateway is ``control`` or ``treatment``.

    Returns:
        The (arm name, gateway) pairs, in the given order.

    Raises:
        ValueError: A pair is malformed, or an arm name or a gateway repeats.
    """
    pairs: list[tuple[str, part2.Arm]] = []
    for item in (p.strip() for p in text.split(",") if p.strip()):
        name, sep, gateway = item.partition("=")
        if not sep or not name or gateway not in part2.ARMS:
            raise ValueError(f"--arms wants NAME=control|treatment, got {item!r}")
        pairs.append((name, cast(part2.Arm, gateway)))
    if (
        not pairs
        or len({n for n, _ in pairs}) != len(pairs)
        or len({g for _, g in pairs}) != len(pairs)
    ):
        raise ValueError(f"--arms names each arm and each gateway at most once, got {text!r}")
    return tuple(pairs)


def select_tasks(tasks: Sequence[Task], ids: Sequence[str], limit: int) -> list[Task]:
    """Pick the tasks to run, in suite order.

    Raises:
        ValueError: An id is not a suite task.
    """
    picked = list(tasks)
    if ids:
        unknown = sorted(set(ids) - {str(t["id"]) for t in tasks})
        if unknown:
            raise ValueError(f"not suite tasks: {', '.join(unknown)}")
        picked = [t for t in tasks if t["id"] in set(ids)]
    return picked[:limit] if limit > 0 else picked


def tempo_hex(trace_id: str) -> str:
    """Return the Tempo trace id of an agent trace id (the UUID without dashes)."""
    return trace_id.replace("-", "").lower()


def slm_calls(trace: Mapping[str, object]) -> list[dict[str, float]]:
    """Return the llama.cpp counts of each ``slm-server`` span of a Tempo trace, in start order.

    Args:
        trace: The ``/api/traces/<id>`` reply (OTLP JSON ``batches``).

    Returns:
        One dict per span: ``start_ns`` and each ``slm.*`` field of :data:`_SLM_FIELDS` that the span has.
    """
    calls: list[dict[str, float]] = []
    for batch in cast(list[dict[str, object]], trace.get("batches") or []):
        resource = cast(dict[str, list[dict[str, object]]], batch.get("resource") or {})
        service = {
            str(cast(dict[str, object], a["value"]).get("stringValue"))
            for a in resource.get("attributes", [])
            if a.get("key") == "service.name"
        }
        if service != {"slm-server"}:
            continue
        for scope in cast(list[dict[str, object]], batch.get("scopeSpans") or []):
            for span in cast(list[dict[str, object]], scope.get("spans") or []):
                call = _span_call(span)
                if call:
                    calls.append(call)
    return sorted(calls, key=lambda c: c["start_ns"])


def _span_call(span: Mapping[str, object]) -> dict[str, float] | None:
    """Return one span's ``slm.*`` counts and ``start_ns``, or ``None`` without ``slm.prompt_n``."""
    attrs = {
        str(a["key"]): next(iter(cast(dict[str, object], a["value"]).values()))
        for a in cast(list[dict[str, object]], span.get("attributes") or [])
    }
    call = {k: float(cast(str, attrs[k])) for k in _SLM_FIELDS if k in attrs}
    if "slm.prompt_n" not in call:
        return None
    call["start_ns"] = float(cast(str, span["startTimeUnixNano"]))
    return call


def search_calls(reply: Mapping[str, object]) -> list[dict[str, float]]:
    """Return the llama.cpp counts of each span of a Tempo ``/api/search`` reply, in start order.

    A span that appears in several span sets counts once (by ``spanID``).
    """
    seen: dict[str, dict[str, float]] = {}
    for trace in cast(list[dict[str, object]], reply.get("traces") or []):
        for span_set in cast(list[dict[str, object]], trace.get("spanSets") or []):
            for span in cast(list[dict[str, object]], span_set.get("spans") or []):
                call = _span_call(span)
                if call:
                    seen[str(span.get("spanID"))] = call
    return sorted(seen.values(), key=lambda c: c["start_ns"])


async def tempo_calls(
    http: httpx.AsyncClient, trace_id: str, start_s: float, end_s: float
) -> list[dict[str, float]]:
    """Read the turn's llama.cpp calls from Tempo (production's Tempo; read only).

    First the agent's own trace (the gateway sends ``traceparent``). When that trace holds no
    ``slm-server`` span (an eval gateway exports no spans of its own), a TraceQL search on the
    ``slm.client.trace_id`` that slm_server records from the ``X-Trace-Id`` header.

    Raises:
        httpx.HTTPError: The search failed.
        ValueError: The search filled its page, so it may be truncated.
    """
    resp = await http.get(f"{TEMPO_URL}/api/traces/{tempo_hex(trace_id)}", timeout=30.0)
    if resp.status_code == 200:
        calls = slm_calls(resp.json())
        if calls:
            return calls
    ids = sorted({trace_id, tempo_hex(trace_id)})
    match = " || ".join(f'span.slm.client.trace_id="{i}"' for i in ids)
    query = f'{{resource.service.name="slm-server" && ({match})}} | select({", ".join(f"span.{k}" for k in _SLM_FIELDS)})'
    limit = 100
    resp = await http.get(
        f"{TEMPO_URL}/api/search",
        params={
            "q": query,
            "start": int(start_s) - 60,
            "end": int(end_s) + 60,
            "limit": limit,
            "spss": 100,
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    reply = resp.json()
    if len(reply.get("traces") or []) >= limit:
        raise ValueError(f"Tempo search for {trace_id} filled its page of {limit}")
    return search_calls(reply)


def seed_memory(gateway: part2.Arm) -> dict[str, object]:
    """Write the three memory facts through the gateway's own memory path (``memory_seed.py``).

    Raises:
        ValueError: The seed failed or did not write every fact.
    """
    with SEED_SCRIPT.open("rb") as script:
        out = subprocess.run(  # nosec B603 B607 — fixed argv; the script is a repo file
            ["docker", "exec", "-i", CONTAINERS[gateway], "/app/.venv/bin/python", "-"],
            stdin=script,
            capture_output=True,
            timeout=300,
            check=False,
        )
    lines = out.stdout.decode().strip().splitlines()
    if out.returncode != 0 or not lines:
        raise ValueError(f"memory seed failed ({out.returncode}): {out.stderr.decode()[-300:]}")
    result = cast(dict[str, object], json.loads(lines[-1]))
    if result != {"entities": 6, "turns": 3}:
        raise ValueError(f"memory seed wrote {result}, not 6 entities and 3 turns")
    return result


def copy_prompt_captures(gateway: part2.Arm, session_id: str, dest: Path) -> int:
    """Copy the session's FRE-1614 prompt captures out of the gateway; return the file count."""
    dest.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(  # nosec B603 B607 — fixed argv
        ["docker", "cp", f"{CONTAINERS[gateway]}:{PROMPT_DEBUG_DIR}/{session_id}", str(dest)],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if out.returncode != 0:
        return 0
    return sum(1 for p in (dest / session_id).rglob("*.json"))


async def stage5(es: httpx.AsyncClient, trace_id: str) -> dict[str, object]:
    """Return the turn's Stage 5 decision (``decomposition_assessed``, the pipeline's event)."""
    events = [
        e for e in await part2._events(es, trace_id, "decomposition_assessed") if e.get("trace_id")
    ]
    if not events:
        return {"strategy": None, "reason": None, "task_type": None}
    last = events[-1]
    return {k: last.get(k) for k in ("strategy", "reason", "task_type", "complexity")}


async def tool_arguments(es: httpx.AsyncClient, trace_id: str) -> dict[str, object]:
    """Return the turn's tool calls with their arguments, and the schema-validation failures."""
    started = await part2._events(es, trace_id, "tool_call_started")
    invalid = await part2._events(es, trace_id, "tool_schema_validation_failed")
    return {
        "tool_args": [
            {"tool": e.get("tool_name"), "arguments": e.get("arguments")} for e in started
        ],
        "tool_schema_failures": len(invalid),
    }


async def chat(
    http: httpx.AsyncClient, base_url: str, message: str, session_id: str | None
) -> tuple[dict[str, object], float]:
    """Send one ``/chat`` turn; return the reply and the wall seconds.

    Raises:
        httpx.HTTPError: Transport or HTTP failure.
        ValueError: The reply is the generic failure reply.
    """
    params = {"message": message, "channel": "EVAL", "model": MODEL_KEY}
    if session_id:
        params["session_id"] = session_id
    timeout = settings.orchestrator_turn_lifetime_seconds + _CLIENT_TIMEOUT_BUFFER_S
    t0 = time.monotonic()
    resp = await http.post(f"{base_url}/chat", params=params, timeout=timeout)
    wall = round(time.monotonic() - t0, 1)
    resp.raise_for_status()
    data = cast(dict[str, object], resp.json())
    if part2.GENERIC_FAILURE_REPLY in str(data.get("response")):
        raise ValueError(f"the reply is the generic failure reply: {data.get('response')!r}"[:200])
    return data, wall


@dataclass(frozen=True)
class RunConfig:
    """What one ``run`` does.

    Attributes:
        run_dir: The run directory.
        arms: The (arm name, gateway) pairs.
        settle_s: Seconds to wait after a turn before its events are read.
        gap_s: Seconds without a local model call before a turn starts.
    """

    run_dir: Path
    arms: tuple[tuple[str, part2.Arm], ...]
    settle_s: float = 20.0
    gap_s: float = 10.0


async def run_task(
    task: Task,
    arm: str,
    gateway: part2.Arm,
    position: int,
    cfg: RunConfig,
    *,
    http: httpx.AsyncClient,
    es: httpx.AsyncClient,
    driver: AsyncDriver,
) -> Row:
    """Run one task on one arm and read everything the row records.

    Returns:
        The row. A transport, HTTP, missing-key or value failure is an ``error`` row.
    """
    base_url = EVAL_ARMS[gateway]
    assert_eval_chat_url(base_url)
    row: Row = {
        "id": task["id"],
        "category": task["category"],
        "arm": arm,
        "gateway": gateway,
        "arm_position": position,
        "message": task["message"],
        "expects": task.get("expects", []),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    t_begin = time.time()
    try:
        await wipe_eval_graph(driver, uri=EVAL_NEO4J_URI)
        if str(task["id"]).startswith("mr"):
            row["memory_seed"] = await asyncio.to_thread(seed_memory, gateway)
        await wait_for_local_model_calls(es, gap_s=cfg.gap_s)
        session_id: str | None = None
        if task.get("history_user"):
            first, first_wall = await chat(http, base_url, str(task["history_user"]), None)
            session_id = str(first["session_id"])
            row["first_turn"] = {
                "trace_id": first["trace_id"],
                "reply": first["response"],
                "wall_s": first_wall,
            }
            await asyncio.sleep(cfg.settle_s)
            await wait_for_local_model_calls(es, gap_s=cfg.gap_s)
        data, wall = await chat(http, base_url, str(task["message"]), session_id)
        trace_id = str(data["trace_id"])
        row.update(
            trace_id=trace_id,
            session_id=str(data["session_id"]),
            reply=str(data["response"]),
            wall_s=wall,
        )
        await asyncio.sleep(cfg.settle_s)
        row.update(await part2.read_turn(es, trace_id, "treatment"))
        row["stage5"] = await stage5(es, trace_id)
        row.update(await tool_arguments(es, trace_id))
        t_end = time.time()
        row["slm_calls"] = await tempo_calls(http, trace_id, t_begin, t_end)
        if not row["slm_calls"] and int(cast(int, row.get("model_calls") or 0)) > 0:
            raise ValueError(f"Tempo holds no slm-server call for trace {trace_id}")
        if isinstance(row.get("first_turn"), dict):
            first_turn = cast(dict[str, object], row["first_turn"])
            first_turn["stage5"] = await stage5(es, str(first_turn["trace_id"]))
            first_turn["slm_calls"] = await tempo_calls(
                http, str(first_turn["trace_id"]), t_begin, t_end
            )
        row["prompt_captures"] = await asyncio.to_thread(
            copy_prompt_captures,
            gateway,
            str(row["session_id"]),
            cfg.run_dir / "prompt_debug" / arm,
        )
        row["graph_nodes_after"] = await part2.count_graph_nodes(driver)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        row["error"] = repr(exc)[:300]
    return row


def rows_path(run_dir: Path) -> Path:
    """Return the run's JSONL file."""
    return run_dir / "rows.jsonl"


async def run_block(
    cfg: RunConfig,
    tasks: Sequence[Task],
    *,
    http: httpx.AsyncClient,
    es: httpx.AsyncClient,
    driver: AsyncDriver,
) -> list[Row]:
    """Run each (task, arm) without a row without an error; append each row as it lands."""
    out = rows_path(cfg.run_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {(str(r["id"]), str(r["arm"])) for r in part2.read_rows(out) if "error" not in r}
    written: list[Row] = []
    for index, task in enumerate(tasks):
        order = cfg.arms if index % 2 == 0 else tuple(reversed(cfg.arms))
        for position, (arm, gateway) in enumerate(order):
            if (str(task["id"]), arm) in done:
                continue
            row = await run_task(task, arm, gateway, position, cfg, http=http, es=es, driver=driver)
            with out.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            written.append(row)
            stage = cast(dict[str, object], row.get("stage5") or {})
            print(
                task["id"],
                arm,
                stage.get("reason"),
                row.get("planner_decision"),
                row.get("model_calls"),
                row.get("wall_s"),
                str(row.get("error", ""))[:100],
                flush=True,
            )
    return written


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def summarize(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Summarize the last row without an error of each (task, arm), per arm.

    Returns:
        Per arm: tasks, errors, wall p50 and p95, model calls, the llama.cpp totals (prompt tokens
        prefilled, prefill and decode seconds, output tokens), cost, the Stage 5 reasons, the planner
        decisions, sub-agents, and the tasks the planner expanded (the P2 task list).
    """
    kept: dict[tuple[str, str], Mapping[str, object]] = {}
    errors: Counter[str] = Counter()
    for r in rows:
        if "error" in r:
            errors[str(r["arm"])] += 1
        else:
            kept[(str(r["id"]), str(r["arm"]))] = r
    arms = sorted({a for _, a in kept} | set(errors))
    out: dict[str, object] = {}
    for arm in arms:
        mine = [r for (_, a), r in kept.items() if a == arm]
        calls = [c for r in mine for c in cast(list[dict[str, float]], r.get("slm_calls") or [])]
        walls = [float(cast(float, r["wall_s"])) for r in mine]
        out[arm] = {
            "tasks": len(mine),
            "error_rows": errors[arm],
            "wall_p50_s": statistics.median(walls) if walls else None,
            "wall_p95_s": _pct(walls, 0.95),
            "wall_total_s": round(sum(walls), 1),
            "model_calls": sum(int(cast(int, r.get("model_calls") or 0)) for r in mine),
            "prompt_n": int(sum(c.get("slm.prompt_n", 0) for c in calls)),
            "prefill_s": round(sum(c.get("slm.prefill_ms", 0) for c in calls) / 1000, 1),
            "decode_s": round(sum(c.get("slm.decode_ms", 0) for c in calls) / 1000, 1),
            "predicted_n": int(sum(c.get("slm.predicted_n", 0) for c in calls)),
            "cost_usd": round(sum(float(cast(float, r.get("cost_usd") or 0)) for r in mine), 4),
            "stage5_reasons": dict(
                Counter(
                    str(cast(dict[str, object], r.get("stage5") or {}).get("reason")) for r in mine
                )
            ),
            "planner_decisions": dict(
                Counter(d for r in mine for d in cast(list[str], r.get("planner_decision") or []))
            ),
            "sub_agents": sum(int(cast(int, r.get("sub_agents") or 0)) for r in mine),
            "expanded": sorted(
                str(r["id"])
                for r in mine
                if any(d == "expanded" for d in cast(list[str], r.get("planner_decision") or []))
            ),
        }
    return out


async def _run(args: argparse.Namespace) -> int:
    from scripts.eval.gateway_freshness import assert_gateway_fresh

    cfg = RunConfig(run_dir=Path(args.run_dir), arms=parse_arms(args.arms), settle_s=args.settle_s)
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    tasks = select_tasks(load_suite(), ids, args.limit)
    async with httpx.AsyncClient() as http, httpx.AsyncClient() as es:
        for arm, gateway in cfg.arms:
            root = Path(
                args.build_root.get(arm, "/opt/seshat")
                if isinstance(args.build_root, dict)
                else args.build_root
            )
            print(
                f"{arm} ({gateway}) fresh: {await assert_gateway_fresh(http, EVAL_ARMS[gateway], root)}",
                flush=True,
            )
        driver = create_eval_driver()
        try:
            rows = await run_block(cfg, tasks, http=http, es=es, driver=driver)
        finally:
            await driver.close()
    errors = sum("error" in r for r in rows)
    print(f"{len(rows)} rows written, {errors} errors -> {rows_path(cfg.run_dir)}")
    return 1 if errors else 0


def _report(args: argparse.Namespace) -> int:
    summary = summarize(part2.read_rows(rows_path(Path(args.run_dir))))
    (Path(args.run_dir) / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


def _build_roots(text: str) -> dict[str, str] | str:
    """Parse ``--build-root``: one path for every arm, or ``ARM=path,...``."""
    if "=" not in text:
        return text
    return dict(p.split("=", 1) for p in text.split(",") if p.strip())


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, then run the turns or report.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        ``run``: 0 when no row has an error. ``report``: 0.
    """
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run each task on each arm")
    run.add_argument("--run-dir", required=True)
    run.add_argument("--arms", required=True, help="NAME=control|treatment, comma-separated")
    run.add_argument("--ids", default="", help="comma-separated task ids")
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--settle-s", type=float, default=20.0)
    run.add_argument(
        "--build-root",
        type=_build_roots,
        default="/opt/seshat",
        help="the tree each arm's image was built from: one path, or ARM=path,...",
    )
    report = sub.add_parser("report", help="summarize the rows per arm")
    report.add_argument("--run-dir", required=True)
    args = p.parse_args(argv)
    if args.command == "run":
        return asyncio.run(_run(args))
    return _report(args)


if __name__ == "__main__":
    sys.exit(main())
