"""Build one row per tool call from the extracted events, joined by span_id."""

import collections
import json
import pathlib

HERE = pathlib.Path(__file__).parent
SESSIONS = ["fn_s2", "gm_s2", "gm_s3", "fn_s3", "gm4_s3", "gm4_s2"]
# stage 3 worker grants (ADR-0150): the tool name fixes the worker type
STAGE3_WORKER = {
    "web_search": "researcher",
    "run_python": "general",
    "search_memory": "general",
    "recall_personal_history": "general",
}


def short(v, n=400):
    s = str(v)
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n})"


for key in SESSIONS:
    ev = [json.loads(l) for l in open(HERE / "events" / f"{key}.jsonl")]
    rows = {r["turn"]: r for r in json.load(open(HERE / "events" / f"{key}.rows.json"))}
    by_span = collections.defaultdict(list)
    for e in ev:
        if e.get("span_id"):
            by_span[e["span_id"]].append(e)
    # sub-agent windows per trace
    windows = collections.defaultdict(list)
    starts = {}
    for e in ev:
        if e["event_type"] == "sub_agent_start":
            starts[e.get("task_id")] = (e["@timestamp"], e.get("worker_type"), e["_turn"])
        elif e["event_type"] == "sub_agent_complete" and e.get("task_id") in starts:
            s, wt, t = starts[e["task_id"]]
            windows[t].append((s, e["@timestamp"], wt, e.get("task_id")))
    out = []
    n = 0
    for e in ev:
        if e["event_type"] != "tool_call_started":
            continue
        n += 1
        sib = by_span[e["span_id"]]
        types = {x["event_type"]: x for x in sib}
        inside = [w for w in windows[e["_turn"]] if w[0] <= e["@timestamp"] <= w[1]]
        role = "sub_agent" if inside else "primary"
        worker = None
        if inside:
            wts = {w[2] for w in inside}
            worker = wts.pop() if len(wts) == 1 else STAGE3_WORKER.get(e["tool_name"], "multi")
        o = {}
        if "tool_call_completed" in types:
            o["success"] = types["tool_call_completed"].get("success")
        if "tool_call_failed" in types:
            o["error"] = short(types["tool_call_failed"].get("error"), 200)
        for t, fields in {
            "bash_completed": ["exit_code", "stdout_len", "stderr_len", "truncated"],
            "bash_allowlist_miss": ["bad_segment"],
            "approval_denied": ["decision"],
            "sandbox_finished": ["exit_code", "stdout_len", "stderr_len"],
            "run_python_finished": ["exit_code", "timed_out"],
            "sandbox_timed_out": ["timeout_seconds"],
            "web_search_started": ["query"],
            "web_search_completed": [
                "result_count",
                "degraded_retrieval",
                "unresponsive_engine_reasons",
            ],
            "web_search_timeout": ["error"],
            "fetch_url_started": ["url"],
            "fetch_url_completed": ["char_count", "truncated"],
            "fetch_url_http_error": ["status"],
            "fetch_url_empty_extraction": ["status"],
            "read_executor_success": ["path", "offset", "limit", "lines_returned", "total_lines"],
            "read_executor_error": ["error"],
            "write_executor_success": ["path", "bytes_written", "mode"],
            "search_memory_tool_completed": ["result_count", "query_path"],
            "personal_history_recalled": ["turn_count"],
            "query_telemetry_refused": ["reason"],
            "notes_search_completed": ["result_count"],
            "tool_call_missing_required_params": ["missing_params"],
        }.items():
            if t in types:
                o[t] = {
                    f: short(types[t].get(f), 160) for f in fields if types[t].get(f) is not None
                }
        gate = [
            x for x in sib if x["event_type"] == "tool_loop_gate" and x.get("decision") != "allow"
        ]
        if gate:
            o["loop_gate"] = [g.get("decision") + ":" + str(g.get("reason")) for g in gate]
        other = sorted(
            {x["event_type"] for x in sib}
            - set(types.keys() & {"tool_call_started"})
            - {
                "tool_call_started",
                "tool_call_completed",
                "tool_loop_gate",
                "tool_result_skill_hint_appended",
                "source_registry_tool_inadmissible",
                "sandbox_starting",
                "sandbox_container_removed",
                "run_python_started",
                "bash_started",
                "bash_auto_approved",
                "read_executor_called",
                "write_executor_called",
                "search_memory_tool_called",
                "fetch_url_novel_destination",
                "recall_personal_history_called",
                "notes_search_called",
            }
            - set(o)
        )
        out.append(
            {
                "id": f"{key}#{n}",
                "turn": e["_turn"],
                "ts": e["@timestamp"][11:19],
                "trace": e["trace_id"][:8],
                "role": role,
                "worker": worker,
                "tool": e["tool_name"],
                "args": short(e.get("arguments"), 600),
                "outcome": o,
                "other_events": other,
            }
        )
    # bash commands refused by the allowlist never emit tool_call_started: add them as attempts
    for e in ev:
        if e["event_type"] != "bash_allowlist_miss":
            continue
        n += 1
        denied = [x for x in by_span[e["span_id"]] if x["event_type"] == "approval_denied"]
        out.append(
            {
                "id": f"{key}#{n}",
                "turn": e["_turn"],
                "ts": e["@timestamp"][11:19],
                "trace": e["trace_id"][:8],
                "role": "primary",
                "worker": None,
                "tool": "bash",
                "args": "(not logged; refused segment) " + short(e.get("bad_segment"), 300),
                "outcome": {
                    "bash_allowlist_miss": {"bad_segment": short(e.get("bad_segment"), 160)},
                    "approval_denied": {"decision": denied[0].get("decision") if denied else None},
                },
                "other_events": [],
            }
        )
    out.sort(key=lambda c: (c["turn"], c["ts"]))
    with open(HERE / f"calls_{key}.jsonl", "w") as f:
        for c in out:
            f.write(json.dumps(c) + "\n")
    tc = collections.Counter(c["tool"] for c in out)
    rc = collections.Counter(c["role"] for c in out)
    print(key, len(out), dict(rc), dict(tc.most_common()))
