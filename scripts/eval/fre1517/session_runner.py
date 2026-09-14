"""FRE-1517 session runner — one scripted session, on one arm, through IsolatedArmRunner.

Extends the FRE-1498 runner (``scripts/eval/fre1498/runner.py``) from one-turn fixtures to a
scripted session of about 20 turns that reuses one ``session_id``, and reuses that runner's
read-back so that both studies read the eval substrate the same way.

What each amendment on FRE-1517 changes here:

- **A1** — ``IsolatedArmRunner.run_turn`` wipes ``neo4j-eval`` only on a session's first turn.
  Later turns pass ``session_id``, so the memory the session's own turns wrote stays readable.
- **Waiting (master, 2026-09-14)** — a session's first turn waits for full gateway quiet
  (150 s), so no session or arm inherits background work. A later turn waits only until no
  ``slm_local`` model call is in flight, plus 10 s. It does not wait for the previous turn's
  extraction or consolidation: that is the production condition. Each row records whether the
  previous turn's consolidation had completed before this turn's first event. The runner
  settles every turn of the session at the end, before the next session's wipe.
- **A3** — every row carries the pre-registered per-turn outcome (:func:`turn_outcome`).
  ``--replicate`` names the draw, and each replicate is a fresh session.
- **A4** — before the session, a local arm's ``/v1/models`` must list the arm's served id
  (manual MTPLX answers any model name, so the request id proves nothing). After each turn,
  every ``primary`` model call on the trace must name the arm's ``telemetry_model``. A positive
  mismatch stops the session. The whole arm manifest (``arms.yaml``) goes into every row.
- **A8** — the trailing-7-day spend on the eval ``api_costs`` table is read before the session,
  and the session refuses to start below ``--min-budget-usd``. Errors are counted per role.
- **A6 / A10** — each row keeps the raw inputs: per-worker ``finish_reason`` and
  ``stop_reason`` from the sub-agent captures, the primary's ``tool_call_invalid_arguments``
  events, and the ``api_costs`` rows for the trace (model, purpose, latency, tokens).

``http_wall_s`` includes the runner's wait before the turn. Read turn wall clock from
``route_trace.latency_total_ms``.

The generation check (FRE-1474) runs once before the session, not before each turn. A probe
completion between turns can take the slot that holds the session's cached prefix, and cache
reuse is one of the things this study reads.

An HTTP failure ends the session. The gateway may still finish that turn, so continuing would
put the script out of step with the session. ``rubric.md`` decides whether the session is re-run.
A runner crash with no failed row can be resumed: run the same command again.

Run from the repo root, one session per invocation:

    set -a; source /opt/seshat/.env; set +a
    PYTHONPATH=. uv run python scripts/eval/fre1517/session_runner.py --arm mtplx_27b --script s1_trip --replicate 1 --run-id <id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from scripts.eval.eval_isolation import (
    IsolatedArmRunner,
    create_eval_driver,
    local_model_calls_in_flight,
)
from scripts.eval.fre1337_intent_probe.behavioral import (
    CONSOLIDATION_SETTLE_TIMEOUT_S,
    EXTRACTION_SETTLE_TIMEOUT_S,
    fetch_latest_event,
    wait_for_event_settle,
)
from scripts.eval.fre1498.runner import (
    EVAL_CHAT,
    _es,
    _events,
    _psql_json,
    generation_check,
    read_back,
    wait_captures,
    wait_route_trace,
)
from scripts.eval.gateway_freshness import assert_gateway_fresh, repo_root

HERE = Path(__file__).parent
GATEWAY_ARM = "treatment"
WEEKLY_BUDGET_USD = 50.0  # docker-compose.eval.yml AGENT_CLOUD_WEEKLY_BUDGET_USD
#: The fan-out failure trailer's fixed opening (orchestrator/executor.py, _fanout_trailer_text).
TRAILER_MARKER = "— Research note:"
#: The owner's approval, 2026-09-14: only errors on the roles the arm serves fail a turn. Errors
#: on every other role (entailment, extraction, captains_log, summaries) are reported per role.
#: The planner call logs role "primary" (orchestrator/expansion_controller.py, role=ModelRole.PRIMARY),
#: so "planner" is kept only as the approval names it; the primary entry already covers it.
ARM_BOUND_ROLES = frozenset({"primary", "planner", "sub_agent"})
IN_FLIGHT_SAMPLE_S = 5.0


def load_arm(name: str) -> dict[str, Any]:
    """Load one arm's manifest entry.

    Args:
        name: A key under ``arms`` in ``arms.yaml``.

    Returns:
        The entry, with its key added as ``arm``.

    Raises:
        KeyError: If the arm is not in the manifest.
    """
    arms = yaml.safe_load((HERE / "arms.yaml").read_text())["arms"]
    return {"arm": name, **arms[name]}


def tbd_fields(arm: dict[str, Any]) -> list[str]:
    """Name the manifest fields that are still unrecorded.

    Args:
        arm: One arm manifest entry.

    Returns:
        The field names whose value is ``TBD``.
    """
    return [key for key, value in arm.items() if value == "TBD"]


def load_script(name: str) -> dict[str, Any]:
    """Load one session script.

    Args:
        name: The script file stem under ``scripts/``.

    Returns:
        The parsed script.
    """
    return yaml.safe_load((HERE / "scripts" / f"{name}.yaml").read_text())


def resume_state(rows: list[dict[str, Any]]) -> tuple[str | None, int]:
    """Decide where a session continues from its rows so far.

    Args:
        rows: The JSONL rows already written for this session, in order.

    Returns:
        (session_id, next turn number). ``(None, 1)`` for a session not yet started.

    Raises:
        RuntimeError: If a row records a failed turn, because a failed turn ends the session.
    """
    if not rows:
        return None, 1
    failed = [r["turn"] for r in rows if r.get("http_error")]
    if failed:
        raise RuntimeError(
            f"turn {failed[0]} failed; a failed turn ends the session (see rubric.md, Lost sessions)"
        )
    return rows[-1]["session_id"], rows[-1]["turn"] + 1


def turn_outcome(
    *,
    reply: str,
    errors_by_role: dict[str, int],
    primary_models: list[str],
    telemetry_model: str,
) -> dict[str, Any]:
    """The pre-registered per-turn outcome (A3), and the arm attribution (A4).

    Args:
        reply: The reply ``/chat`` delivered. Empty when the call returned none.
        errors_by_role: ``model_call_error`` counts on the trace, per role. Only the roles in
            :data:`ARM_BOUND_ROLES` fail the turn.
        primary_models: The ``model`` of every ``primary`` ``model_call_completed`` on the trace.
        telemetry_model: The arm's expected ``model`` value.

    Returns:
        ``delivered`` and the reason for each failed condition, plus ``attribution``:
        ``match`` (every primary call names the arm), ``mismatch`` (one names another
        model) or ``unverified`` (no primary call found — Elasticsearch counts are
        provisional, FRE-1051, so an absence is not a mismatch).
    """
    reasons = []
    if not reply.strip():
        reasons.append("empty_reply")
    if sum(count for role, count in errors_by_role.items() if role in ARM_BOUND_ROLES):
        reasons.append("model_call_error")
    if TRAILER_MARKER in reply:
        reasons.append("fanout_trailer")
    if not primary_models:
        attribution = "unverified"
    elif set(primary_models) == {telemetry_model}:
        attribution = "match"
    else:
        attribution = "mismatch"
        reasons.append("arm_mismatch")
    return {"delivered": not reasons, "reasons": reasons, "attribution": attribution}


def completed_before(consolidation_at: str | None, turn_first_event_at: str | None) -> bool | None:
    """Whether a consolidation completed before a turn's first event.

    Args:
        consolidation_at: ``@timestamp`` of the previous turn's ``consolidation_completed``.
        turn_first_event_at: ``@timestamp`` of this turn's earliest event.

    Returns:
        ``True`` or ``False``, or ``None`` when either event was not observed — Elasticsearch
        counts are provisional (FRE-1051), so a missing event does not decide the answer.
    """
    if not consolidation_at or not turn_first_event_at:
        return None
    parse = lambda ts: datetime.fromisoformat(ts.replace("Z", "+00:00"))  # noqa: E731
    return parse(consolidation_at) <= parse(turn_first_event_at)


def _served_ids(models_endpoint: str) -> list[str]:
    resp = httpx.get(models_endpoint, timeout=30.0)
    resp.raise_for_status()
    return [m.get("id") for m in resp.json().get("data", [])]


def _weekly_spend_usd() -> float:
    rows = _psql_json(
        "SELECT coalesce(sum(cost_usd), 0)::float AS usd FROM api_costs "
        "WHERE timestamp > now() - interval '7 days'"
    )
    return float(rows[0]["usd"]) if rows else 0.0


def _trace_reads(es: httpx.Client, trace_id: str) -> dict[str, Any]:
    completed = _events(es, trace_id, "model_call_completed")
    errors = _events(es, trace_id, "model_call_error")
    by_role: dict[str, list[str]] = {}
    for m in completed:
        by_role.setdefault(m.get("role") or "?", []).append(m.get("model") or "?")
    errors_by_role: dict[str, int] = {}
    for e in errors:
        role = e.get("role") or "?"
        errors_by_role[role] = errors_by_role.get(role, 0) + 1
    invalid_args = _events(es, trace_id, "tool_call_invalid_arguments")
    trace_uuid = str(uuid.UUID(trace_id))
    costs = _psql_json(
        "SELECT model, purpose, latency_ms, input_tokens, output_tokens, "
        "cache_read_input_tokens, cost_usd::float AS cost_usd "
        f"FROM api_costs WHERE trace_id='{trace_uuid}' ORDER BY id"
    )
    return {
        "model_ids_by_role": {role: sorted(set(ids)) for role, ids in by_role.items()},
        "primary_models": by_role.get("primary", []),
        "errors_by_role": errors_by_role,
        "error_messages": [(e.get("role"), str(e.get("error"))[:300]) for e in errors],
        "primary_invalid_tool_arguments": len(invalid_args),
        "api_costs": costs,
    }


async def _previous_consolidation(
    es: httpx.Client, es_async: httpx.AsyncClient, previous_trace_id: str, trace_id: str
) -> dict[str, Any]:
    extraction = await fetch_latest_event(
        es_async, previous_trace_id, "entity_extraction_completed", field="capture_trace_id"
    )
    consolidation = (
        await fetch_latest_event(es_async, str(extraction["trace_id"]), "consolidation_completed")
        if extraction
        else None
    )
    first = _es(
        es,
        {
            "size": 1,
            "query": {"term": {"trace_id": trace_id}},
            "sort": [{"@timestamp": {"order": "asc"}}],
            "_source": ["@timestamp"],
        },
    )
    consolidation_at = consolidation.get("@timestamp") if consolidation else None
    first_at = first[0].get("@timestamp") if first else None
    return {
        "previous_trace_id": previous_trace_id,
        "consolidation_completed_at": consolidation_at,
        "turn_first_event_at": first_at,
        "completed_before_turn": completed_before(consolidation_at, first_at),
    }


async def _settle_trace(es_async: httpx.AsyncClient, trace_id: str) -> dict[str, bool]:
    extraction = await wait_for_event_settle(
        es_async,
        trace_id,
        "entity_extraction_completed",
        timeout_s=EXTRACTION_SETTLE_TIMEOUT_S,
        require_nonzero=True,
        field="capture_trace_id",
    )
    consolidation = False
    if extraction:
        event = await fetch_latest_event(
            es_async, trace_id, "entity_extraction_completed", field="capture_trace_id"
        )
        if event:
            consolidation = await wait_for_event_settle(
                es_async,
                str(event["trace_id"]),
                "consolidation_completed",
                timeout_s=CONSOLIDATION_SETTLE_TIMEOUT_S,
                require_nonzero=True,
            )
    return {"extraction": extraction, "consolidation": consolidation}


async def _settle_session(
    es_async: httpx.AsyncClient, rows_path: Path, trace_ids: list[str]
) -> None:
    """Wait for every turn's consolidation, so the next session's wipe cannot race it."""
    if not trace_ids:
        return
    results = await asyncio.gather(*(_settle_trace(es_async, t) for t in trace_ids))
    settle = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "traces": dict(zip(trace_ids, results, strict=True)),
    }
    rows_path.with_suffix(".settle.json").write_text(json.dumps(settle, indent=2))
    unsettled = [t for t, r in settle["traces"].items() if not r["consolidation"]]
    sys.stdout.write(
        f"session settle: {len(trace_ids) - len(unsettled)}/{len(trace_ids)} consolidated\n"
    )


async def _sample_in_flight(es_async: httpx.AsyncClient, peak: dict[str, int]) -> None:
    """Record the most local model calls seen in flight at once during a turn.

    The owner's decision of 2026-09-14 asks for the peak per arm. The sample runs every
    :data:`IN_FLIGHT_SAMPLE_S` seconds, so the value is a lower bound: a burst shorter than the
    interval can pass unseen. A failed sample is counted, and it does not stop the turn.
    """
    while True:
        try:
            in_flight = await local_model_calls_in_flight(es_async)
            peak["in_flight"] = max(peak["in_flight"], len(in_flight))
        except (httpx.HTTPError, RuntimeError):
            peak["sample_errors"] += 1
        await asyncio.sleep(IN_FLIGHT_SAMPLE_S)


def _preflight(arm: dict[str, Any], min_budget_usd: float) -> dict[str, Any]:
    missing = tbd_fields(arm)
    if missing:
        raise RuntimeError(f"arm {arm['arm']} still has TBD fields: {missing}")
    spend = _weekly_spend_usd()
    remaining = WEEKLY_BUDGET_USD - spend
    if remaining < min_budget_usd:
        raise RuntimeError(f"eval cloud budget: {remaining:.2f} USD left, needs {min_budget_usd}")
    pre: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "trailing_7d_spend_usd": round(spend, 4),
        "budget_remaining_usd": round(remaining, 4),
        "served_ids": None,
        "gen_check": None,
    }
    if arm["models_endpoint"]:
        served = _served_ids(arm["models_endpoint"])
        pre["served_ids"] = served
        if arm["served_model_id"] not in served:
            raise RuntimeError(f"served ids {served} do not include {arm['served_model_id']}")
        base = arm["models_endpoint"].removesuffix("/v1/models")
        gen = generation_check(model=arm["served_model_id"], base_url=base)
        pre["gen_check"] = gen
        if not gen["ok"]:
            raise RuntimeError(f"backend not generating: {gen}")
    return pre


async def run(args: argparse.Namespace) -> int:
    """Drive one scripted session and append one JSONL row per turn.

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 when the session completes, 2 on a refused start, 3 on a failed turn, 4 at the
        deadline, 5 on an arm mismatch.
    """
    arm = load_arm(args.arm)
    script = load_script(args.script)
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
        preflight = _preflight(arm, args.min_budget_usd)
    except (RuntimeError, httpx.HTTPError) as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    turns = [
        t
        for t in script["turns"]
        if t["n"] >= next_turn and (not args.stop_after or t["n"] <= args.stop_after)
    ]
    if not turns:
        sys.stdout.write("session already complete\n")
        return 0

    trace_ids = [r["trace_id"] for r in rows if r.get("trace_id")]
    driver = create_eval_driver()
    runner = IsolatedArmRunner(driver=driver)
    es = httpx.Client()
    es_async = httpx.AsyncClient()
    try:
        async with httpx.AsyncClient() as http:
            await assert_gateway_fresh(http, EVAL_CHAT, repo_root())
            fingerprint = (
                (await http.get(f"{EVAL_CHAT}/health", timeout=10)).json().get("build_fingerprint")
            )
            for turn in turns:
                if args.deadline and datetime.now(UTC) >= datetime.fromisoformat(args.deadline):
                    sys.stderr.write(f"deadline reached before turn {turn['n']}\n")
                    return 4
                base_row = {
                    "run_id": args.run_id,
                    "arm": args.arm,
                    "arm_manifest": arm,
                    "script": args.script,
                    "replicate": args.replicate,
                    "turn": turn["n"],
                    "message": turn["message"],
                    "expect_route": turn["expect_route"],
                    "back_refs": turn.get("back_refs", []),
                    "holds": turn.get("holds", []),
                    "gateway_fingerprint": fingerprint,
                    "preflight": preflight,
                    "resumed": bool(rows),
                    "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
                t0 = time.monotonic()
                peak = {"in_flight": 0, "sample_errors": 0}
                sampler = asyncio.create_task(_sample_in_flight(es_async, peak))
                try:
                    result = await runner.run_turn(
                        http,
                        es_async,
                        turn["message"],
                        arm=GATEWAY_ARM,
                        session_id=session_id,
                        model=arm["deployment_key"] if session_id is None else None,
                        wait_settle=False,
                    )
                except httpx.HTTPError as exc:
                    row = {**base_row, "session_id": session_id, "http_error": repr(exc)[:300]}
                    row["http_wall_s"] = round(time.monotonic() - t0, 1)
                    row["peak_local_in_flight"] = dict(peak)
                    with rows_path.open("a") as f:
                        f.write(json.dumps(row, default=str) + "\n")
                    sys.stderr.write(f"turn {turn['n']} failed: {exc!r}\n")
                    return 3
                finally:
                    sampler.cancel()
                http_wall = round(time.monotonic() - t0, 1)
                if session_id is not None and result.session_id != session_id:
                    raise RuntimeError(
                        f"gateway returned session {result.session_id}, expected {session_id}"
                    )
                session_id = result.session_id
                previous = (
                    await _previous_consolidation(es, es_async, trace_ids[-1], result.trace_id)
                    if trace_ids
                    else None
                )
                trace_ids.append(result.trace_id)
                route = wait_route_trace(str(uuid.UUID(result.trace_id)))
                primary_cap, sub_caps = wait_captures(es, result.trace_id)
                signals = read_back(es, result.trace_id)
                reads = _trace_reads(es, result.trace_id)
                reply = (primary_cap[0].get("assistant_response") or "") if primary_cap else ""
                outcome = turn_outcome(
                    reply=reply,
                    errors_by_role=reads["errors_by_role"],
                    primary_models=reads["primary_models"],
                    telemetry_model=arm["telemetry_model"],
                )
                row = {
                    **base_row,
                    "session_id": session_id,
                    "trace_id": result.trace_id,
                    "http_wall_s": http_wall,
                    "previous_turn_consolidation": previous,
                    "outcome": outcome,
                    "route_trace": route,
                    "route_matches_script": bool(route)
                    and str(route.get("decomposition_strategy") or "").upper()
                    == turn["expect_route"],
                    "signals": signals,
                    "trace_reads": reads,
                    "primary_capture": {
                        k: primary_cap[0].get(k)
                        for k in ("entry_id", "duration_ms", "tools_used", "outcome", "status")
                    }
                    if primary_cap
                    else None,
                    "sub_agent_captures": sub_caps,
                    "peak_local_in_flight": dict(peak),
                    "reply": reply,
                }
                with rows_path.open("a") as f:
                    f.write(json.dumps(row, default=str) + "\n")
                sys.stdout.write(
                    f"  t{turn['n']:02d} delivered={outcome['delivered']} {outcome['reasons']} "
                    f"route={route and route.get('decomposition_strategy')} "
                    f"workers={len(sub_caps)} wall={http_wall}s\n"
                )
                if outcome["attribution"] == "mismatch":
                    sys.stderr.write(
                        f"arm mismatch on turn {turn['n']}: {reads['model_ids_by_role']}\n"
                    )
                    return 5
    finally:
        # Every exit path settles the session's turns, so the next session's wipe cannot race
        # a consolidation still writing to neo4j-eval.
        await _settle_session(es_async, rows_path, trace_ids)
        es.close()
        await es_async.aclose()
        await driver.close()
    return 0


def main() -> int:
    """Parse arguments and run one session.

    Returns:
        The process exit code from :func:`run`.
    """
    p = argparse.ArgumentParser()
    p.add_argument("--arm", required=True, help="a key under arms in arms.yaml")
    p.add_argument("--script", required=True, help="a file stem under scripts/, e.g. s1_trip")
    p.add_argument("--replicate", required=True, type=int)
    p.add_argument("--run-id", required=True)
    p.add_argument("--min-budget-usd", type=float, default=5.0)
    p.add_argument("--deadline", default="", help="ISO-8601 UTC; start no new turn at or after it")
    p.add_argument(
        "--stop-after",
        type=int,
        default=0,
        help="last turn to run; the Phase 1 s2 smoke check uses 5 with --replicate 0. 0 runs all",
    )
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
