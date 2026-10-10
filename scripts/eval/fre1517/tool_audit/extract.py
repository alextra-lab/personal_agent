"""Pull every agent-logs event of each study turn, by trace id, into one JSONL per session."""

import glob
import json
import pathlib

import httpx

OUT = pathlib.Path(__file__).parent / "events"
OUT.mkdir(exist_ok=True)
RUNS = {
    "fn_s2": "stage3-flash-s2",
    "gm_s2": "stage3-gemma-s2",
    "gm_s3": "stage3-gemma-s3",
    "fn_s3": "stage3-flash-s3",
    "gm4_s3": "stage4-gemma-s3",
    "gm4_s2": "stage4-gemma-s2",
}
ROWS = pathlib.Path("/opt/seshat/.claude/worktrees/explore/scripts/eval/fre1517/out")
ES = "http://localhost:9200/agent-logs-*/_search"  # fre-375-allow: read-only _search of study traces, no write

for key, run in RUNS.items():
    rows = [json.loads(l) for l in open(glob.glob(str(ROWS / run / "*.jsonl"))[0]) if l.strip()]
    with open(OUT / f"{key}.jsonl", "w") as f, open(OUT / f"{key}.rows.json", "w") as rf:
        json.dump(
            [
                {
                    "turn": r["turn"],
                    "trace_id": r["trace_id"],
                    "message": r["message"],
                    "delivered": r["outcome"]["delivered"],
                    "reasons": r["outcome"]["reasons"],
                    "validity": r["outcome"].get("validity"),
                    "sub_agent_captures": r["trace_reads"].get("sub_agent_captures"),
                    "tools_completed": r["trace_reads"]["tools_completed"],
                    "reply": r["reply"],
                }
                for r in rows
            ],
            rf,
        )
        total = 0
        for r in rows:
            after = None
            while True:
                body = {
                    "size": 1000,
                    "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}],
                    "query": {"term": {"trace_id": r["trace_id"]}},
                }
                if after:
                    body["search_after"] = after
                hits = httpx.post(ES, json=body, timeout=60).json()["hits"]["hits"]
                if not hits:
                    break
                for h in hits:
                    s = h["_source"]
                    s["_turn"] = r["turn"]
                    f.write(json.dumps(s) + "\n")
                    total += 1
                after = hits[-1]["sort"]
        print(key, len(rows), "turns", total, "events")
