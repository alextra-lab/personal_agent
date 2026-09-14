"""FRE-1498 study runner — drives fixtures through the isolated eval gateway (treatment,
:9003) via IsolatedArmRunner (FRE-1372) and reads every behavioural column back from the
eval substrate only (elasticsearch-eval :9202, postgres-eval via docker exec).

Committed under FRE-1503 (was left in the explore seat's scratchpad per its own commit-only-
the-document convention). The client-timeout and gateway-idle-wait workaround this runner
originally carried locally moved into ``IsolatedArmRunner.run_turn`` itself (FRE-1503) — this
runner now only calls the shared :func:`wait_for_gateway_idle` once up front, to keep
``generation_check``'s health read accurate, and lets ``run_turn`` handle the rest.

Run from the repo root:

    set -a; source /opt/seshat/.env; set +a
    PYTHONPATH=. uv run python scripts/eval/fre1498/runner.py --regime A --run-id <id> [--labels a,b]

One JSONL row per turn, appended as soon as the turn lands, so a crash keeps prior rows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from scripts.eval.eval_isolation import IsolatedArmRunner, create_eval_driver, wait_for_gateway_idle
from scripts.eval.gateway_freshness import assert_gateway_fresh, repo_root

from personal_agent.request_gateway.intent import classify_intent

EVAL_ES = "http://localhost:9202"
EVAL_CHAT = "http://localhost:9003"
CADDY_SLM = "http://localhost:8600"
SERVED_MODEL = "unsloth/qwen3.8-flash-next"
HERE = Path(__file__).parent


def _es(
    es: httpx.Client, body: dict[str, Any], index: str = "agent-logs-*"
) -> list[dict[str, Any]]:
    r = es.post(f"{EVAL_ES}/{index}/_search", json=body, timeout=30.0)
    r.raise_for_status()
    return [h["_source"] for h in r.json()["hits"]["hits"]]


def _events(
    es: httpx.Client, trace_id: str, event_type: str, size: int = 500
) -> list[dict[str, Any]]:
    return _es(
        es,
        {
            "size": size,
            "query": {
                "bool": {
                    "must": [{"term": {"trace_id": trace_id}}, {"term": {"event_type": event_type}}]
                }
            },
            "sort": [{"@timestamp": {"order": "asc"}}],
        },
    )


def _psql_json(sql: str) -> list[dict[str, Any]]:
    out = subprocess.run(
        [
            "docker",
            "exec",
            "cloud-sim-postgres-eval",
            "psql",
            "-U",
            "agent",
            "-d",
            "personal_agent",
            "-At",
            "-c",
            f"SELECT coalesce(json_agg(r), '[]') FROM ({sql}) r;",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return json.loads(out or "[]")


def generation_check(model: str = SERVED_MODEL, base_url: str = CADDY_SLM) -> dict[str, Any]:
    """FRE-1474: the SLM health probe lies; prove generation with a trivial completion."""
    t0 = time.monotonic()
    try:
        r = httpx.post(
            f"{base_url}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with the single word OK."}],
                "max_tokens": 8,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=120.0,
        )
        ok = r.status_code == 200 and bool(r.json()["choices"][0]["message"]["content"].strip())
        return {
            "ok": ok,
            "status": r.status_code,
            "secs": round(time.monotonic() - t0, 2),
            "text": r.json()["choices"][0]["message"]["content"][:40] if ok else r.text[:200],
        }
    except Exception as exc:  # noqa: BLE001 — a study runner reports, it does not recover
        return {
            "ok": False,
            "status": None,
            "secs": round(time.monotonic() - t0, 2),
            "text": repr(exc)[:200],
        }


def wait_route_trace(trace_uuid: str, timeout_s: float = 120.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = _psql_json(
            "SELECT task_type, complexity, decomposition_strategy, decomposition_reason, sub_agent_count, "
            "tool_iteration_count, tools_used, effective_tool_iteration_ceiling, latency_total_ms, "
            "input_tokens, output_tokens, error_type, error_class, fallback_triggered, channel, "
            "orchestration_event, final_reply_chars, expansion_strategy, mode "
            f"FROM route_traces WHERE trace_id='{trace_uuid}' AND task_type IS NOT NULL ORDER BY id DESC LIMIT 1"
        )
        if rows:
            return rows[0]
        time.sleep(3)
    return None


def wait_captures(
    es: httpx.Client, trace_id: str, timeout_s: float = 90.0
) -> tuple[list[dict], list[dict]]:
    """Primary capture + sub-agent captures for the trace (captains-log indices in eval ES)."""
    deadline = time.monotonic() + timeout_s
    primary: list[dict[str, Any]] = []
    while time.monotonic() < deadline and not primary:
        primary = _es(
            es,
            {
                "size": 5,
                "query": {"term": {"trace_id": trace_id}},
                "_source": {"excludes": ["context_messages", "assembled_context", "full_output"]},
            },
            index="agent-captains-captures-2*",
        )
        if not primary:
            time.sleep(5)
    subs = _es(
        es,
        {
            "size": 50,
            "query": {"term": {"trace_id": trace_id}},
            "_source": [
                "task_id",
                "rounds",
                "stop_reason",
                "report_kind",
                "finish_reason",
                "tool_iterations",
                "tools_used",
                "tools_granted",
                "elapsed_generation_ms",
                "tool_result_chars_absorbed",
                "success",
                "status",
                "input_tokens",
                "output_tokens",
                "mode",
                "model_role",
                "error",
            ],
        },
        index="agent-captains-captures-subagents-*",
    )
    return primary, subs


def read_back(es: httpx.Client, trace_id: str) -> dict[str, Any]:
    tool_calls = _events(es, trace_id, "tool_call_completed")
    web = _events(es, trace_id, "web_search_completed")
    model_calls = _events(es, trace_id, "model_call_completed")
    by_role: dict[str, list[dict[str, Any]]] = {}
    for m in model_calls:
        by_role.setdefault(m.get("role") or "?", []).append(m)
    primary_inputs = [int(m.get("input_tokens") or 0) for m in by_role.get("primary", [])]
    planner_raw = _events(es, trace_id, "fre1498_planner_raw")
    planner_done = _events(es, trace_id, "planner_completed")
    planner_failed = _events(es, trace_id, "planner_failed")
    declined = _events(es, trace_id, "fre1498_planner_declined")
    starts = _events(es, trace_id, "sub_agent_start")
    forced = _events(es, trace_id, "sub_agent_forced_synthesis")
    budget = _events(es, trace_id, "tool_budget_warning_injected")
    ctrl = _events(es, trace_id, "expansion_controller_complete")
    fallback = _events(es, trace_id, "fallback_planner_used")
    errors = _events(es, trace_id, "model_call_error")
    tools_passed = _events(es, trace_id, "tools_passed_to_llm", size=1)
    gateway_out = _events(es, trace_id, "gateway_output", size=1)
    rt_failed = _events(es, trace_id, "route_trace_write_failed", size=1)
    stamps = sorted(m["@timestamp"] for m in model_calls if m.get("@timestamp"))
    return {
        "tool_call_count": len(tool_calls),
        "tool_names": [t.get("tool_name") for t in tool_calls],
        "web_search_count": len(web),
        "web_search_result_counts": [int(w.get("result_count") or 0) for w in web],
        "fetch_url_count": sum(1 for t in tool_calls if t.get("tool_name") == "fetch_url"),
        "model_calls_by_role": {r: len(v) for r, v in by_role.items()},
        "model_ids": sorted({m.get("model") for m in model_calls if m.get("model")}),
        "primary_input_tokens": primary_inputs,
        "input_token_growth": (primary_inputs[-1] - primary_inputs[0])
        if len(primary_inputs) >= 2
        else 0,
        "input_token_max": max(primary_inputs) if primary_inputs else 0,
        "reasoning_chars_primary": [
            int(m.get("reasoning_content_chars") or 0) for m in by_role.get("primary", [])
        ],
        "model_call_span_s": (
            (
                datetime.fromisoformat(stamps[-1].replace("Z", "+00:00"))
                - datetime.fromisoformat(stamps[0].replace("Z", "+00:00"))
            ).total_seconds()
            if len(stamps) >= 2
            else 0.0
        ),
        "model_call_errors": len(errors),
        "tool_budget_exhausted": len(budget) > 0,
        "planner_raw": [
            {
                k: p.get(k)
                for k in (
                    "raw_task_count",
                    "raw_strategy",
                    "given_strategy",
                    "max_tasks",
                    "finish_reason",
                )
            }
            for p in planner_raw
        ],
        "planner_completed": [
            {
                k: p.get(k)
                for k in (
                    "plan_task_count",
                    "task_types",
                    "task_thoroughness",
                    "parse_success",
                    "fallback_used",
                )
            }
            for p in planner_done
        ],
        "planner_failed": [p.get("reason") for p in planner_failed],
        "planner_declined": len(declined) > 0,
        "fallback_planner_used": len(fallback) > 0,
        "sub_agents_started": [
            {
                k: s.get(k)
                for k in ("task_id", "worker_type", "thoroughness", "round_budget", "task")
            }
            for s in starts
        ],
        "sub_agent_forced_synthesis": [
            {k: s.get(k) for k in ("stop_reason", "tool_iterations", "tool_result_chars_absorbed")}
            for s in forced
        ],
        "tools_passed_to_llm": {
            k: v
            for k, v in (tools_passed[0].items() if tools_passed else [])
            if k in ("tool_count", "tool_names", "tools", "count", "names")
        },
        "gateway_output": {
            k: v
            for k, v in (gateway_out[0].items() if gateway_out else [])
            if k
            in (
                "expansion_budget",
                "expansion_permitted",
                "has_memory",
                "strategy",
                "task_type",
                "complexity",
                "mode",
            )
        },
        "route_trace_write_failed": len(rt_failed) > 0,
        "expansion_controller_complete": [
            {k: c.get(k) for k in ("sub_agent_count", "successful", "degraded", "plan_is_fallback")}
            for c in ctrl
        ],
    }


async def run(args: argparse.Namespace) -> int:
    fixtures = yaml.safe_load((HERE / "fixtures.yaml").read_text())["fixtures"]
    if args.labels:
        wanted = {s.strip() for s in args.labels.split(",")}
        fixtures = [f for f in fixtures if f["label"] in wanted]
        missing = wanted - {f["label"] for f in fixtures}
        if missing:
            print(f"unknown labels: {missing}", file=sys.stderr)
            return 2
    out_dir = HERE / "out"
    out_dir.mkdir(exist_ok=True)
    rows_path = out_dir / f"{args.run_id}.{args.regime}.jsonl"
    replies_dir = out_dir / f"{args.run_id}.{args.regime}.replies"
    replies_dir.mkdir(exist_ok=True)
    done = set()
    if rows_path.exists():
        done = {
            json.loads(line)["label"] for line in rows_path.read_text().splitlines() if line.strip()
        }
        print(f"resuming; {len(done)} rows already present")

    driver = create_eval_driver()
    runner = IsolatedArmRunner(driver=driver)
    es = httpx.Client()
    try:
        async with httpx.AsyncClient() as http, httpx.AsyncClient() as es_async:
            await assert_gateway_fresh(http, EVAL_CHAT, repo_root())
            health = (await http.get(f"{EVAL_CHAT}/health", timeout=10)).json()
            print("gateway fingerprint:", health.get("build_fingerprint"))
            chat_secs: dict[str, float] = {}
            last_chat: dict[str, str] = {}
            _orig_post = http.post

            async def _timed_post(url: str, *a: Any, **kw: Any) -> Any:
                t = time.monotonic()
                if url.endswith("/chat"):
                    last_chat.clear()
                if url.endswith("/chat") and args.model:
                    # explicit primary selection on the turn (sub_agent inherits it)
                    kw["params"] = dict(kw.get("params") or {}, model=args.model)
                try:
                    resp = await _orig_post(url, *a, **kw)
                    if url.endswith("/chat"):
                        try:
                            last_chat["response"] = resp.json().get("response") or ""
                        except Exception:  # noqa: BLE001
                            last_chat["response"] = ""
                    return resp
                finally:
                    if url.endswith("/chat"):
                        chat_secs["chat"] = round(time.monotonic() - t, 1)

            http.post = _timed_post  # type: ignore[method-assign]
            for fx in fixtures:
                if fx["label"] in done:
                    continue
                if args.deadline and datetime.now(UTC) >= datetime.fromisoformat(args.deadline):
                    print(
                        f"deadline {args.deadline} reached before {fx['label']}; stopping",
                        file=sys.stderr,
                    )
                    return 4
                # FRE-1503: wait here (not only inside run_turn) so generation_check reads
                # the backend's true idle state rather than one still finishing a prior turn.
                idle_wait = await wait_for_gateway_idle(es_async)
                gen = generation_check()
                print(
                    f"[{datetime.now(UTC).isoformat(timespec='seconds')}] idle-wait {idle_wait}s gen-check {gen}"
                )
                if not gen["ok"]:
                    print("backend not generating — stopping", file=sys.stderr)
                    return 3
                cls = classify_intent(fx["message"])
                t0 = time.monotonic()
                started_at = datetime.now(UTC).isoformat(timespec="seconds")
                chat_secs.clear()
                try:
                    turn = await runner.run_turn(http, es_async, fx["message"], arm="treatment")
                except Exception as exc:  # noqa: BLE001
                    row = {
                        "run_id": args.run_id,
                        "regime": args.regime,
                        "label": fx["label"],
                        "group": fx.get("group"),
                        "pair": fx.get("pair"),
                        "message": fx["message"],
                        "started_at": started_at,
                        "deterministic_task_type": cls.task_type.value,
                        "deterministic_complexity": cls.complexity.value,
                        "http_error": repr(exc)[:300],
                        "http_wall_s": round(time.monotonic() - t0, 1),
                    }
                    rows_path.open("a").write(json.dumps(row) + "\n")
                    print("TURN FAILED", fx["label"], repr(exc)[:200])
                    continue
                http_wall = round(time.monotonic() - t0, 1)
                import uuid as _uuid

                trace_uuid = str(_uuid.UUID(turn.trace_id))
                rt = wait_route_trace(trace_uuid)
                primary_cap, sub_caps = wait_captures(es, turn.trace_id)
                signals = read_back(es, turn.trace_id)
                reply = ""
                if primary_cap:
                    reply = primary_cap[0].get("assistant_response") or ""
                delivered = last_chat.get("response") or ""
                (replies_dir / f"{fx['label']}.md").write_text(reply or delivered)
                if delivered and delivered != reply:
                    (replies_dir / f"{fx['label']}.delivered.md").write_text(delivered)
                row = {
                    "run_id": args.run_id,
                    "regime": args.regime,
                    "label": fx["label"],
                    "group": fx.get("group"),
                    "pair": fx.get("pair"),
                    "message": fx["message"],
                    "started_at": started_at,
                    "model_selected": args.model or None,
                    "gen_check": gen,
                    "session_id": turn.session_id,
                    "trace_id": turn.trace_id,
                    "extraction_settled": turn.extraction_settled,
                    "http_wall_s": http_wall,
                    "chat_wall_s": chat_secs.get("chat"),
                    "deterministic_task_type": cls.task_type.value,
                    "deterministic_complexity": cls.complexity.value,
                    "deterministic_signals": list(cls.signals),
                    "route_trace": rt,
                    "signals": signals,
                    "primary_capture": {
                        k: primary_cap[0].get(k)
                        for k in (
                            "entry_id",
                            "duration_ms",
                            "tools_used",
                            "outcome",
                            "status",
                            "eval_mode",
                            "input_tokens",
                            "output_tokens",
                        )
                    }
                    if primary_cap
                    else None,
                    "sub_agent_captures": sub_caps,
                    "reply_chars": len(reply),
                    "delivered_chars": len(last_chat.get("response") or ""),
                    "reply_head": reply[:400],
                }
                rows_path.open("a").write(json.dumps(row, default=str) + "\n")
                print(
                    f"  {fx['label']:24s} det={cls.task_type.value}/{cls.complexity.value} "
                    f"strat={rt and rt.get('decomposition_strategy')}/{rt and rt.get('decomposition_reason')} "
                    f"subs={rt and rt.get('sub_agent_count')} tools={signals['tool_call_count']} "
                    f"web={signals['web_search_count']} growth={signals['input_token_growth']} "
                    f"wall={http_wall}s declined={signals['planner_declined']}"
                )
    finally:
        es.close()
        await driver.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--regime", required=True, choices=["A", "B", "D"])
    p.add_argument("--run-id", required=True)
    p.add_argument("--labels", default="")
    p.add_argument(
        "--model",
        default="",
        help="primary deployment key to select on every turn (e.g. qwen3.8-27b-ovh)",
    )
    p.add_argument(
        "--deadline", default="", help="ISO-8601 UTC; start no new fixture at or after this instant"
    )
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
