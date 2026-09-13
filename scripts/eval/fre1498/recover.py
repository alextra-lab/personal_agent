"""Recover rows whose HTTP call timed out at the client while the gateway finished the turn.

For every row carrying `http_error`, find the turn's capture in the eval ES by exact
user_message and a start-time window, then read every column back exactly as runner.py does.
The recovered row is marked `recovered: true` and keeps the original error text.

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. uv run python <scratch>/fre1498/recover.py --regime D --run-id 2026-09-12
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from runner import EVAL_ES, read_back, wait_captures, wait_route_trace  # noqa: E402

from personal_agent.request_gateway.intent import classify_intent  # noqa: E402

HERE = Path(__file__).parent


def find_capture(es: httpx.Client, message: str, started_at: str) -> dict | None:
    t0 = datetime.fromisoformat(started_at)
    body = {
        "size": 5,
        "query": {
            "bool": {
                "must": [
                    {"match_phrase": {"user_message": message}},
                    {
                        "range": {
                            "timestamp": {
                                "gte": (t0 - timedelta(minutes=1)).isoformat(),
                                "lte": (t0 + timedelta(minutes=90)).isoformat(),
                            }
                        }
                    },
                ]
            }
        },
        "_source": ["timestamp", "session_id", "trace_id", "user_message", "duration_ms"],
    }
    r = es.post(f"{EVAL_ES}/agent-captains-captures-2*/_search", json=body, timeout=30.0)
    r.raise_for_status()
    hits = [
        h["_source"]
        for h in r.json()["hits"]["hits"]
        if h["_source"].get("user_message") == message
    ]
    hits.sort(key=lambda h: h["timestamp"])
    return hits[0] if hits else None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--regime", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--map", default="", help="label=trace_uuid,... for turns with no capture (error path)")
    args = p.parse_args()
    trace_map = {kv.split("=")[0]: kv.split("=")[1] for kv in args.map.split(",") if "=" in kv}
    path = HERE / "out" / f"{args.run_id}.{args.regime}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    es = httpx.Client()
    changed = 0
    for i, row in enumerate(rows):
        if "http_error" not in row:
            continue
        cap = find_capture(es, row["message"], row["started_at"])
        if cap is None and row["label"] in trace_map:
            cap = {"trace_id": uuid.UUID(trace_map[row["label"]]).hex, "session_id": None, "timestamp": None}
        if cap is None:
            print(f"{row['label']}: no capture found (turn may still be running or never captured)")
            continue
        trace_id = cap["trace_id"]
        rt = wait_route_trace(str(uuid.UUID(trace_id)), timeout_s=5.0)
        primary_cap, sub_caps = wait_captures(es, trace_id, timeout_s=5.0)
        signals = read_back(es, trace_id)
        reply = primary_cap[0].get("assistant_response") or "" if primary_cap else ""
        cls = classify_intent(row["message"])
        new = {
            "run_id": args.run_id,
            "regime": args.regime,
            "label": row["label"],
            "group": row.get("group"),
            "pair": row.get("pair"),
            "message": row["message"],
            "started_at": row["started_at"],
            "recovered": True,
            "http_error": row["http_error"],
            "session_id": cap["session_id"],
            "trace_id": trace_id,
            "capture_timestamp": cap["timestamp"],
            "chat_wall_s": None,
            "http_wall_s": row.get("http_wall_s"),
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
            "reply_head": reply[:400],
        }
        (HERE / "out" / f"{args.run_id}.{args.regime}.replies" / f"{row['label']}.md").write_text(
            reply
        )
        rows[i] = new
        changed += 1
        print(
            f"{row['label']:24s} recovered trace={trace_id[:8]} strat={rt and rt.get('decomposition_strategy')} subs={rt and rt.get('sub_agent_count')} tools={signals['tool_call_count']} web={signals['web_search_count']} dur={primary_cap and primary_cap[0].get('duration_ms')}"
        )
    path.write_text("\n".join(json.dumps(r, default=str) for r in rows) + "\n")
    print(f"{changed} rows recovered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
