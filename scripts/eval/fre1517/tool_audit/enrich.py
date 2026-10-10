"""Attach the primary's tool results (from the archived captures) and web_search result URLs to each call row."""

import collections
import json
import pathlib

HERE = pathlib.Path(__file__).parent


def short(v, n):
    s = str(v)
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n})"


for key in ["fn_s2", "gm_s2", "gm_s3", "fn_s3", "gm4_s3", "gm4_s2"]:
    calls = [json.loads(l) for l in open(HERE / f"calls_{key}.jsonl")]
    rows = json.load(open(HERE / "events" / f"{key}.rows.json"))
    ev = [json.loads(l) for l in open(HERE / "events" / f"{key}.jsonl")]
    urls = {
        e["span_id"]: e.get("result_urls") for e in ev if e["event_type"] == "web_search_completed"
    }
    started_span = {}
    for e in ev:
        if e["event_type"] == "tool_call_started":
            started_span[
                (
                    e["trace_id"][:8],
                    e["@timestamp"][11:19],
                    e["tool_name"],
                    str(e.get("arguments"))[:200],
                )
            ] = e["span_id"]
    unmatched = 0
    for r in rows:
        cap = json.load(open(HERE / "caps" / f"{r['trace_id']}.json"))
        results = collections.deque(cap.get("tool_results") or [])
        prim = [c for c in calls if c["trace"] == r["trace_id"][:8] and c["role"] == "primary"]
        for c in prim:
            # captures list the primary's results in call order; match by tool name
            for i, tr in enumerate(results):
                if tr.get("tool_name") == c["tool"]:
                    c["result"] = short(
                        tr.get("output")
                        if tr.get("output") not in (None, "{}")
                        else tr.get("error"),
                        900,
                    )
                    if c["args"].startswith("(not logged"):
                        c["args"] = short(tr.get("arguments"), 600)
                    del results[i]
                    break
            else:
                unmatched += 1
    for c in calls:
        if c["tool"] == "web_search":
            for k, sp in started_span.items():
                if k[0] == c["trace"] and k[1] == c["ts"] and k[2] == "web_search" and sp in urls:
                    c["result_urls"] = short(urls[sp], 500)
                    break
    msgs = {
        r["turn"]: {
            "message": r["message"],
            "delivered": r["delivered"],
            "reasons": r["reasons"],
            "reply_head": short(r["reply"], 600),
        }
        for r in rows
    }
    with open(HERE / f"enriched_{key}.json", "w") as f:
        json.dump({"turns": msgs, "calls": calls}, f, indent=0)
    print(key, len(calls), "primary rows without a capture result:", unmatched)
