"""FRE-1517 production session runner — one scripted session on the PRODUCTION gateway.

Owner direction 2026-09-14: the study leaves the eval stack and runs arms 2 and 3 on production,
one replicate each. The owner approved it directly in the explore session at about 20:21 UTC.
This runner exists because ``session_runner.py`` is built for the eval stack: it wipes
``neo4j-eval`` and archives captures, and neither may ever touch production. So this file has
no wipe, no archive and no isolation gate. It reads production telemetry only, and it stops on
any growth in a read-only Neo4j node count.

What it does per session:

- **Before the session:** requires the arm's served id in ``/v1/models`` (A4). Reads the
  production node counts, and refuses to start when they exceed the baseline.
- **Per turn:** waits until no ``slm_local`` model call is in flight on production, plus 10 s
  (production carries the owner's traffic, so it never waits for global quiet). POSTs ``/chat``
  with ``channel=EVAL`` and the owner-approved synthetic identity header. Sends ``model`` on turn
  1 and checks that every ``primary`` call names the arm's ``telemetry_model``. Writes the
  pre-registered outcome (``session_runner.turn_outcome``) into one JSONL row.
- **Graph checks:** after turn 1 and after the session, re-reads the counts. Any growth stops
  the runner with exit 7.

Run from the repo root, one session per invocation, only with the owner's approval:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. uv run python -u scripts/eval/fre1517/prod_session_runner.py --arm mtplx_27b --script s1_trip --replicate 1 --run-id prod-arm2 --baseline 13222,2687,9483
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from scripts.eval.fre1517.session_runner import (
    HERE,
    apply_session_facts,
    load_arm,
    load_script,
    resume_state,
    tbd_fields,
    turn_outcome,
)

#: Production, by the owner's direct approval of 2026-09-14 (FRE-1517 leaves the eval stack).
PROD_CHAT = "http://localhost:9001"
PROD_ES = "http://localhost:9200"  # fre-375-allow: read-only telemetry reads, owner-approved production run
#: The owner-approved synthetic study identity (fre<ticket>-live@ convention), approved 2026-09-14.
#: Read from the environment, never committed: a real deployment domain must not enter this public
#: repository (the check-no-deployment-identifier hook). Set FRE1517_STUDY_IDENTITY before a run.
STUDY_IDENTITY_ENV = "FRE1517_STUDY_IDENTITY"
IN_SESSION_GAP_S = 10.0
POLL_S = 5.0


def _es_search(es: httpx.Client, body: dict[str, Any], index: str) -> dict[str, Any]:
    resp = es.post(f"{PROD_ES}/{index}/_search", json=body, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def _events(es: httpx.Client, trace_id: str, event_type: str) -> list[dict[str, Any]]:
    body = {
        "size": 500,
        "query": {
            "bool": {
                "must": [{"term": {"trace_id": trace_id}}, {"term": {"event_type": event_type}}]
            }
        },
        "sort": [{"@timestamp": {"order": "asc"}}],
    }
    return [h["_source"] for h in _es_search(es, body, "agent-logs-*")["hits"]["hits"]]


def _psql_json(sql: str) -> list[dict[str, Any]]:
    """Run one SELECT against production Postgres inside a read-only transaction."""
    out = (
        subprocess.run(
            [
                "docker",
                "exec",
                "cloud-sim-postgres",
                "psql",
                "-U",
                "agent",
                "-d",
                "personal_agent",
                "-At",
                "-c",
                "SET default_transaction_read_only = on",
                "-c",
                f"SELECT coalesce(json_agg(r), '[]') FROM ({sql}) r;",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .splitlines()
    )
    # The first line is the SET acknowledgement. json_agg output can span several lines, so parse
    # everything after it (reading only the last line crashed on a multi-row api_costs result).
    body = "\n".join(line for line in out if line != "SET")
    return json.loads(body or "[]")


def graph_counts() -> dict[str, int]:
    """Read production node, Turn and Entity counts. Read-only; credentials stay in the container.

    Returns:
        ``{"nodes": n, "turns": n, "entities": n}``.
    """
    query = (
        "CALL { MATCH (n) RETURN count(n) AS nodes } "
        "CALL { MATCH (t:Turn) RETURN count(t) AS turns } "
        "CALL { MATCH (e:Entity) RETURN count(e) AS entities } "
        "RETURN nodes, turns, entities"
    )
    out = (
        subprocess.run(
            [
                "docker",
                "exec",
                "cloud-sim-neo4j",
                "sh",
                "-c",
                f'cypher-shell -u neo4j -p "${{NEO4J_AUTH#neo4j/}}" --format plain --access-mode read "{query}"',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .splitlines()
    )
    nodes, turns, entities = (int(v.strip()) for v in out[-1].split(","))
    return {"nodes": nodes, "turns": turns, "entities": entities}


def grown(counts: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    """The counts that exceed the baseline, with their growth.

    Args:
        counts: The current counts.
        baseline: Master's baseline counts.

    Returns:
        ``{name: growth}`` for every count above its baseline; empty when nothing grew.
    """
    return {k: counts[k] - baseline[k] for k in baseline if counts[k] > baseline[k]}


@functools.cache
def _gateway_started_at() -> str:
    """The production gateway container's start time.

    A model call that started before the current container started cannot still be in flight.
    On 2026-09-15 a gateway recreate at 02:46 left a 02:45:32 worker call without an end event,
    and the pre-turn wait held on it.
    """
    return subprocess.run(
        ["docker", "inspect", "cloud-sim-seshat-gateway", "--format", "{{.State.StartedAt}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _local_in_flight(es: httpx.Client) -> int:
    body = {
        "size": 2000,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"provider": "slm_local"}},
                    {
                        "terms": {
                            "event_type": [
                                "model_call_started",
                                "model_call_completed",
                                "model_call_error",
                            ]
                        }
                    },
                    {"range": {"@timestamp": {"gte": "now-1h"}}},
                    {"range": {"@timestamp": {"gte": _gateway_started_at()}}},
                ]
            }
        },
        "_source": ["event_type", "span_id"],
    }
    hits = _es_search(es, body, "agent-logs-*")["hits"]["hits"]
    started = {
        h["_source"].get("span_id")
        for h in hits
        if h["_source"].get("event_type") == "model_call_started"
    }
    ended = {
        h["_source"].get("span_id")
        for h in hits
        if h["_source"].get("event_type") != "model_call_started"
    }
    return len(started - ended - {None})


def _wait_local_quiet(es: httpx.Client, max_wait_s: float = 1800.0) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_wait_s and _local_in_flight(es):
        time.sleep(POLL_S)
    time.sleep(IN_SESSION_GAP_S)
    return round(time.monotonic() - t0, 1)


def _trace_reads(es: httpx.Client, trace_id: str) -> dict[str, Any]:
    completed = _events(es, trace_id, "model_call_completed")
    errors = _events(es, trace_id, "model_call_error")
    by_role: dict[str, list[str]] = {}
    for m in completed:
        by_role.setdefault(m.get("role") or "?", []).append(m.get("model") or "?")
    errors_by_role: dict[str, int] = {}
    for e in errors:
        errors_by_role[e.get("role") or "?"] = errors_by_role.get(e.get("role") or "?", 0) + 1
    calls = [
        {
            k: m.get(k)
            for k in (
                "role",
                "model",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "tool_calls",
                "reasoning_content_chars",
                "@timestamp",
            )
        }
        for m in completed
    ]
    tools_done = _events(es, trace_id, "tool_call_completed")
    tools_failed = _events(es, trace_id, "tool_call_failed")
    subs = _es_search(
        es,
        {
            "size": 50,
            "query": {"term": {"trace_id": trace_id}},
            "_source": {"excludes": ["full_output", "context_messages"]},
        },
        "agent-captains-captures-subagents-*",
    )["hits"]["hits"]
    trace_uuid = str(uuid.UUID(trace_id))
    return {
        "model_calls": calls,
        "model_ids_by_role": {r: sorted(set(v)) for r, v in by_role.items()},
        "primary_models": by_role.get("primary", []),
        "errors_by_role": errors_by_role,
        "error_messages": [(e.get("role"), str(e.get("error"))[:300]) for e in errors],
        "tools_completed": [t.get("tool_name") for t in tools_done],
        "tools_failed": [(t.get("tool_name"), str(t.get("error"))[:200]) for t in tools_failed],
        "primary_invalid_tool_arguments": len(_events(es, trace_id, "tool_call_invalid_arguments")),
        # FRE-1521: the planner's own record (brief mode, history seen, constraints per task) and
        # the task text each worker received, for brief carry-through and invented-place review.
        "planner_completed": _events(es, trace_id, "planner_completed"),
        # FRE-1522 AC-5: the worker context reserve and the bounded landing.
        "sub_agent_landing_reserved": _events(es, trace_id, "sub_agent_landing_reserved"),
        "sub_agent_landing_trimmed": _events(es, trace_id, "sub_agent_landing_trimmed"),
        "sub_agent_starts": [
            {
                k: s.get(k)
                for k in ("task_id", "worker_type", "thoroughness", "round_budget", "task")
            }
            for s in _events(es, trace_id, "sub_agent_start")
        ],
        "sub_agent_captures": [h["_source"] for h in subs],
        "route_trace": (
            _psql_json(
                "SELECT task_type, complexity, decomposition_strategy, decomposition_reason, sub_agent_count, "
                "tool_iteration_count, latency_total_ms, input_tokens, output_tokens, error_type "
                f"FROM route_traces WHERE trace_id='{trace_uuid}' ORDER BY id DESC LIMIT 1"
            )
            or [None]
        )[0],
        "api_costs": _psql_json(
            "SELECT model, purpose, latency_ms, input_tokens, output_tokens, cost_usd::float AS cost_usd "
            f"FROM api_costs WHERE trace_id='{trace_uuid}' ORDER BY id"
        ),
    }


def run(args: argparse.Namespace) -> int:
    """Drive one scripted session on production.

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on completion, 2 on a refused start, 3 on a failed turn, 5 on an arm mismatch,
        7 on graph growth.
    """
    arm = apply_session_facts(load_arm(args.arm), args.session_fact)
    script = load_script(args.script)
    baseline = dict(
        zip(("nodes", "turns", "entities"), (int(v) for v in args.baseline.split(",")), strict=True)
    )
    out_dir = HERE / "out" / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / f"{args.arm}.{args.script}.r{args.replicate}.jsonl"
    rows = (
        [json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
        if rows_path.exists()
        else []
    )
    try:
        session_id, next_turn = resume_state(rows)
        if tbd_fields(arm):
            raise RuntimeError(f"arm {args.arm} still has TBD fields: {tbd_fields(arm)}")
        served = (
            [
                m.get("id")
                for m in httpx.get(arm["models_endpoint"], timeout=30).json().get("data", [])
            ]
            if arm["models_endpoint"]
            else None
        )
        if served is not None and arm["served_model_id"] not in served:
            raise RuntimeError(f"served ids {served} do not include {arm['served_model_id']}")
        counts = graph_counts()
        if grown(counts, baseline):
            raise RuntimeError(f"graph above baseline before the session: {counts} vs {baseline}")
    except (RuntimeError, httpx.HTTPError, subprocess.CalledProcessError) as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    preflight = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "served_ids": served,
        "graph_counts": counts,
    }
    print(f"preflight: {preflight}", flush=True)
    turns = [
        t
        for t in script["turns"]
        if t["n"] >= next_turn and (not args.stop_after or t["n"] <= args.stop_after)
    ]
    identity = os.environ.get(STUDY_IDENTITY_ENV, "")
    if not identity:
        sys.stderr.write(
            f"refused: set {STUDY_IDENTITY_ENV} to the owner-approved study identity\n"
        )
        return 2
    headers = {"Cf-Access-Authenticated-User-Email": identity}

    with httpx.Client() as http, httpx.Client() as es:
        for turn in turns:
            # A cloud arm calls no local engine, so it never waits behind local calls.
            if arm["models_endpoint"]:
                waited = _wait_local_quiet(es)
            else:
                time.sleep(IN_SESSION_GAP_S)
                waited = IN_SESSION_GAP_S
            params = {"message": turn["message"], "channel": "EVAL"}
            if session_id is None:
                params["model"] = arm["deployment_key"]
            else:
                params["session_id"] = session_id
            base_row = {
                "run_id": args.run_id,
                "target": "production",
                "arm": args.arm,
                "arm_manifest": arm,
                "script": args.script,
                "replicate": args.replicate,
                "turn": turn["n"],
                "message": turn["message"],
                "expect_route": turn["expect_route"],
                "back_refs": turn.get("back_refs", []),
                "holds": turn.get("holds", []),
                "preflight": preflight,
                "waited_s": waited,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            t0 = time.monotonic()
            try:
                resp = http.post(
                    f"{PROD_CHAT}/chat", params=params, headers=headers, timeout=3700.0
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as exc:
                row = {
                    **base_row,
                    "session_id": session_id,
                    "http_error": repr(exc)[:300],
                    "http_wall_s": round(time.monotonic() - t0, 1),
                }
                rows_path.open("a").write(json.dumps(row, default=str) + "\n")
                sys.stderr.write(f"turn {turn['n']} failed: {exc!r}\n")
                return 3
            http_wall = round(time.monotonic() - t0, 1)
            if session_id is not None and str(data["session_id"]) != session_id:
                sys.stderr.write(
                    f"gateway returned session {data['session_id']}, expected {session_id}\n"
                )
                return 3
            session_id, trace_id = str(data["session_id"]), str(data["trace_id"])
            time.sleep(15)  # let the trace's events reach Elasticsearch
            reads = _trace_reads(es, trace_id)
            brief = None
            if args.expect_brief_mode:
                # FRE-1521 Phase B: every planner call must run in the expected brief mode and
                # see conversation history. Turn 2 is a HYBRID turn in s1_trip, so it must plan.
                plans = _events(es, trace_id, "planner_completed")
                brief = {
                    "planner_events": len(plans),
                    "brief_mode": [p.get("brief_mode") for p in plans],
                    "history_chars": [p.get("history_chars") for p in plans],
                    "task_constraints_count": [p.get("task_constraints_count") for p in plans],
                }
                brief["holds"] = bool(plans) and all(
                    p.get("brief_mode") == args.expect_brief_mode
                    and (p.get("history_chars") or 0) > 0
                    for p in plans
                )
            ac4 = None
            if args.expect_expansion_disabled:
                # FRE-1520 AC-4: with expansion switched off, every turn routes SINGLE with
                # reason expansion_disabled and dispatches no worker.
                if not reads["route_trace"]:
                    time.sleep(20)
                    reads = _trace_reads(es, trace_id)
                rt4 = reads["route_trace"] or {}
                ac4 = {
                    "decomposition_strategy": rt4.get("decomposition_strategy"),
                    "decomposition_reason": rt4.get("decomposition_reason"),
                    "sub_agent_captures": len(reads["sub_agent_captures"]),
                }
                ac4["holds"] = (
                    str(ac4["decomposition_strategy"]).lower() == "single"
                    and ac4["decomposition_reason"] == "expansion_disabled"
                    and ac4["sub_agent_captures"] == 0
                )
            reply = data.get("response") or ""
            outcome = turn_outcome(
                reply=reply,
                errors_by_role=reads["errors_by_role"],
                primary_models=reads["primary_models"],
                telemetry_model=arm["telemetry_model"],
            )
            row = {
                **base_row,
                "session_id": session_id,
                "trace_id": trace_id,
                "http_wall_s": http_wall,
                "primary_selection": data.get("primary_selection"),
                "outcome": outcome,
                "trace_reads": reads,
                "reply": reply,
            }
            if turn["n"] == 1 or turn is turns[-1]:
                row["graph_counts_after"] = graph_counts()
                row["graph_growth"] = grown(row["graph_counts_after"], baseline)
            rows_path.open("a").write(json.dumps(row, default=str) + "\n")
            rt = reads["route_trace"] or {}
            print(
                f"  t{turn['n']:02d} session={session_id[:8]} trace={trace_id[:8]} delivered={outcome['delivered']} "
                f"{outcome['reasons']} route={rt.get('decomposition_strategy')} workers={len(reads['sub_agent_captures'])} "
                f"wall={http_wall}s growth={row.get('graph_growth')}",
                flush=True,
            )
            if outcome["attribution"] == "mismatch":
                sys.stderr.write(
                    f"arm mismatch on turn {turn['n']}: {reads['model_ids_by_role']}\n"
                )
                return 5
            if args.max_session_usd > 0:
                spent = _psql_json(
                    "SELECT coalesce(sum(cost_usd), 0)::float AS usd FROM api_costs "
                    f"WHERE session_id='{session_id}'"
                )[0]["usd"]
                print(f"  session cost after t{turn['n']:02d}: USD {spent:.4f}", flush=True)
                if spent > args.max_session_usd:
                    sys.stderr.write(
                        f"STOP: session cost USD {spent:.4f} > {args.max_session_usd}\n"
                    )
                    return 8
            if brief is not None:
                print(f"  BRIEF t{turn['n']:02d}: {brief}", flush=True)
                if turn["n"] == 2 and not brief["holds"]:
                    sys.stderr.write(f"STOP: planner brief check fails on turn 2: {brief}\n")
                    return 10
            if ac4 is not None:
                row_ac4 = f"  AC-4 t{turn['n']:02d}: {ac4}"
                print(row_ac4, flush=True)
                if not ac4["holds"] and turn["n"] == 1:
                    sys.stderr.write(f"STOP: FRE-1520 AC-4 fails on turn 1: {ac4}\n")
                    return 9
            if row.get("graph_growth"):
                sys.stderr.write(f"STOP: production graph grew: {row['graph_growth']}\n")
                return 7
    return 0


def main() -> int:
    """Parse arguments and run one production session.

    Returns:
        The exit code from :func:`run`.
    """
    p = argparse.ArgumentParser()
    p.add_argument("--arm", required=True)
    p.add_argument("--script", required=True)
    p.add_argument("--replicate", type=int, required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument(
        "--baseline", required=True, help="nodes,turns,entities — master's read-only baseline"
    )
    p.add_argument("--session-fact", action="append", default=[])
    p.add_argument("--stop-after", type=int, default=0)
    p.add_argument(
        "--expect-brief-mode",
        default="",
        help="FRE-1521 Phase B: require planner_completed brief_mode and history_chars>0; stop (exit 10) if turn 2 fails",
    )
    p.add_argument(
        "--expect-expansion-disabled",
        action="store_true",
        help="FRE-1520 AC-4: record route and workers per turn; stop (exit 9) if turn 1 fails",
    )
    p.add_argument(
        "--max-session-usd",
        type=float,
        default=0.0,
        help="stop (exit 8) when api_costs for the session pass this; 0 disables",
    )
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
