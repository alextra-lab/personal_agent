"""Assign every non-useful call to one fix bucket. Rules first, then hand assignments by id."""

import collections
import json
import pathlib
import re

HERE = pathlib.Path(__file__).parent
SESS = ["fn_s2", "fn_s3", "gm_s2", "gm_s3", "gm4_s2", "gm4_s3"]
OVERRIDE_LABEL = {
    "gm_s3#46": ("useful", None),
    "gm_s3#47": ("repeated", "model"),
    "gm_s2#35": ("failed", "instruction"),
}
# two refused attempts that never emitted tool_call_started (found in the captures)
ADDED = [("gm_s2", "relative_path"), ("gm_s3", "model_other")]
HAND = {  # the rows no rule matched, assigned by reading each reason
    "fn_s2#53": "researcher_no_fetch", "fn_s2#63": "model_other", "fn_s2#65": "model_other",
    "fn_s3#15": "worker_no_datetime", "fn_s3#34": "model_other", "fn_s3#126": "model_other",
    "fn_s3#131": "model_other", "fn_s3#112": "search_backend", "fn_s3#113": "search_backend",
    "gm_s2#36": "recall_paused",
    "fn_s2#55": "researcher_no_fetch",
    "fn_s2#100": "researcher_no_fetch",
    "fn_s2#60": "model_other",
    "fn_s2#121": "model_other",
    "fn_s3#86": "model_other",
    "fn_s3#87": "model_other",
    "gm_s2#32": "model_other",
    "gm_s3#47": "model_other",
    "fn_s2#67": "sandbox_state",
    "fn_s3#93": "sandbox_state",
    "fn_s3#91": "sandbox_state",
    "fn_s2#77": "read_via_bash",
    "fn_s2#84": "read_via_bash",
    "gm_s2#33": "read_via_bash",
    "gm4_s2#22": "read_via_bash",
    "fn_s2#89": "worker_files",
    "fn_s2#94": "worker_files",
    "fn_s2#131": "worker_files",
    "fn_s2#134": "worker_files",
    "fn_s2#139": "recall_paused",
    "fn_s2#141": "recall_paused",
    "fn_s3#134": "recall_paused",
    "gm_s2#37": "recall_paused",
    "fn_s3#23": "web_environment",
    "fn_s3#110": "web_environment",
    "gm4_s2#16": "web_environment",
    "fn_s3#53": "search_backend",
    "gm4_s2#20": "relative_path",
}
ES = r"elasticsearch:9200|localhost:9200|agent-logs|captains-captures|/_mapping|/_cat|esql|ES\|QL"


def rule(c, l):
    a, r, t, role = c["args"], l["reason"], c["tool"], c["role"]
    if t == "query_telemetry" or (
        role == "sub_agent"
        and t == "run_python"
        and re.search(r"latenc|telemetry|fetch_latency", a + r, re.I)
    ):
        return "telemetry_worker_tool"
    if (
        t == "web_search"
        and role == "sub_agent"
        and re.search(ES + r"|latenc|telemetry|p50|p90", a + r, re.I)
    ):
        return "telemetry_routed_to_web"
    if t == "bash" and re.search(ES, a + r, re.I):
        return "telemetry_primary_curl"
    if "bash_allowlist_miss" in c["outcome"] or (
        t == "bash" and re.search(r"allowlist|after the refusal", r, re.I)
    ):
        return "allowlist"
    if re.search(r"categories", a + r) and re.search(r"'it'|MDN|Docker", r):
        return "search_categories"
    if (
        t in ("bash", "write", "run_python", "read")
        and re.search(r"patch|heredoc|no edit tool|edit applied|sed -i", r + a, re.I)
        and role == "primary"
    ):
        return "edit_tool"
    if role == "sub_agent" and re.search(
        r"/app|nfl_picks|not mounted|from memory|rebuil|recreat", r + a, re.I
    ):
        return "worker_files"
    if (
        role == "sub_agent"
        and t == "run_python"
        and re.search(r"requests|wikipedia|duckduckgo|scrap", r + a, re.I)
    ):
        return "worker_web_via_python"
    if t == "web_search" and l["cause"] == "environment":
        return "search_backend"
    if t == "fetch_url" and re.search(r"50000|FRE-1569|same page|re-fetch|scoreboard", r + a, re.I):
        return "fetch_big"
    if re.search(r"sibling|never received", r, re.I):
        return "sibling_dependency"
    if t in ("web_search", "perplexity_query") and l["class"] == "repeated":
        return "dup_search"
    if t in ("read", "bash") and l["class"] == "repeated":
        return "reread"
    if l["cause"] == "guessed_url" or (t == "fetch_url" and re.search(r"404|guess", r, re.I)):
        return "guessed_url"
    if re.search(r"relative path|allowed paths", r, re.I):
        return "relative_path"
    if t in ("search_memory", "recall_personal_history") and re.search(
        r"0 (turns|matches|results)|returned 0|result_count 0|no record|older", r, re.I
    ):
        return "recall_paused"
    return None


B = collections.defaultdict(collections.Counter)
rows_out = []
unmatched = []
for k in SESS:
    d = json.load(open(HERE / f"enriched_{k}.json"))
    calls = {c["id"]: c for c in d["calls"]}
    for line in open(HERE / f"labels_{k}.jsonl"):
        l = json.loads(line)
        l["class"], l["cause"] = OVERRIDE_LABEL.get(l["id"], (l["class"], l["cause"]))
        if l["class"] == "useful":
            continue
        b = HAND.get(l["id"]) or rule(calls[l["id"]], l)
        if b is None:
            unmatched.append(l["id"])
            continue
        B[b][k] += 1
        rows_out.append({"id": l["id"], "bucket": b, "class": l["class"], "cause": l["cause"]})
for k, b in ADDED:
    B[b][k] += 1
json.dump(rows_out, open(HERE / "buckets.json", "w"))
for name, cnt in sorted(B.items(), key=lambda x: -sum(x[1].values())):
    fn = cnt["fn_s2"] + cnt["fn_s3"]
    gm3 = cnt["gm_s2"] + cnt["gm_s3"]
    gm4 = cnt["gm4_s2"] + cnt["gm4_s3"]
    print(
        f"{name:26s} total={sum(cnt.values()):3d}  FN(st3)={fn:3d}  GM(st3)={gm3:3d}  GM(st4)={gm4:3d}  {dict(cnt)}"
    )
print("unmatched", unmatched)
