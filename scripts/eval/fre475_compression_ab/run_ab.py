"""FRE-475 — intra-turn tool-result compression A/B harness (ADR-0085).

Drives one artifact-build turn through the live gateway `/chat` and extracts the
**per-round fresh-input curve** for that single trace, so a flag-on run can be
compared against the `a0a07227` baseline on the same prompt and stack.

This is the FRE-433 *measure-don't-assert* recipe applied intra-turn: the headline
metric is **total fresh (full-price) input tokens** summed over the turn's primary
`model_call_completed` events — the thing tool-result digestion is meant to reduce.

Protocol
--------
1. Deploy the gateway with the arm you want to measure:
   - baseline arm: `AGENT_TOOL_RESULT_COMPRESSION_ENABLED` unset/false (the `a0a07227` shape).
   - treatment arm: `AGENT_TOOL_RESULT_COMPRESSION_ENABLED=true` (+ rebuild).
2. Run this harness; it sends the prompt and, when the turn returns, prints the
   per-round table + totals + the comparison vs the recorded baseline.
3. PASS = total fresh input drops >= 30% vs baseline AND `artifact_committed == 1`
   AND no `task_failed` on the trace.

Reference traces
----------------
- Baseline (flag-off, SUCCEEDED): `a0a07227-121b-4ccb-871b-45072a32ccb0` — 768,484 fresh, artifact built.
- First treatment (keep-deferred, FAILED): `5f2d1277-0d26-420b-811f-719d5b15bd6e` —
  1,036,347 fresh (WORSE), artifact NOT built; case-(b) cache churn. See README.

Usage::

    uv run python scripts/eval/fre475_compression_ab/run_ab.py \
        --email <owner-cf-access-email> --profile cloud
    # extract-only against an existing trace (no /chat traffic):
    uv run python scripts/eval/fre475_compression_ab/run_ab.py --extract-trace <trace_id>

Identity note: pass the deployment owner's own CF-Access email. NEVER the injected
Claude Code `userEmail` (that leaks a private address into prod substrate).
"""

from __future__ import annotations

import argparse
import json
import time

import httpx

ES = "http://localhost:9200"  # fre-375-allow: A/B measures real cloud-sim turns; must read live ES
CHAT = "http://localhost:9001/chat"
BASELINE_FRESH = 768_484  # a0a07227 total fresh (full-price) input tokens
BASELINE_TRACE = "a0a07227-121b-4ccb-871b-45072a32ccb0"

# Faithful replay of the original a0a07227 user message.
PROMPT = (
    "Create an interactive html artifact to help me understand how the prompt "
    "construction, prompt cache, and prompt compression works in this harness.\n"
    "This document in the codebase should help - but you should also look at the "
    "code. I would like to better understand the math as well. Im not very strong "
    "in math.  \n2026-06-02-cache-aware-prompt-layout-and-compaction.md"
)


def send_turn(email: str, profile: str) -> dict[str, str]:
    """POST the artifact-build prompt to /chat and return trace/session ids."""
    params = {"message": PROMPT, "profile": profile, "channel": "EVAL"}
    headers = {"Cf-Access-Authenticated-User-Email": email}
    with httpx.Client() as c:
        r = c.post(CHAT, params=params, headers=headers, timeout=1800.0)
        r.raise_for_status()
        data = r.json()
    return {
        "trace_id": str(data.get("trace_id")),
        "session_id": str(data.get("session_id")),
        "response": str(data.get("response", "")),
    }


def _agg_count(c: httpx.Client, trace_id: str, event_type: str) -> int:
    body = {
        "size": 0,
        "query": {
            "bool": {
                "must": [
                    {"term": {"trace_id": trace_id}},
                    {"term": {"event_type": event_type}},
                ]
            }
        },
    }
    r = c.post(f"{ES}/agent-logs-*/_search", json=body, timeout=30.0)
    r.raise_for_status()
    return int(r.json()["hits"]["total"]["value"])


def extract(trace_id: str) -> None:
    """Print the per-round fresh-input curve, totals, and the baseline comparison."""
    with httpx.Client() as c:
        body = {
            "size": 200,
            "_source": [
                "@timestamp",
                "input_tokens",
                "cache_read_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
                "latency_ms",
            ],
            "query": {
                "bool": {
                    "must": [
                        {"term": {"trace_id": trace_id}},
                        {"term": {"event_type": "model_call_completed"}},
                        {"term": {"role": "primary"}},
                    ]
                }
            },
            "sort": [{"@timestamp": "asc"}],
        }
        r = c.post(f"{ES}/agent-logs-*/_search", json=body, timeout=30.0)
        r.raise_for_status()
        hits = [h["_source"] for h in r.json()["hits"]["hits"]]

        dig = _agg_count(c, trace_id, "tool_result_digest_recorded")
        reexpand = _agg_count(c, trace_id, "tool_result_digest_reexpanded")
        artifact = _agg_count(c, trace_id, "artifact_write_committed")
        failed = _agg_count(c, trace_id, "task_failed")

    def g(s: dict, k: str) -> int:
        return int(s.get(k) or 0)

    total_fresh = sum(g(s, "input_tokens") for s in hits)
    total_cache = sum(g(s, "cache_read_tokens") for s in hits)
    total_out = sum(g(s, "output_tokens") for s in hits)

    print(f"\ntrace={trace_id}  rounds={len(hits)}")
    print(f"{'#':>3} {'fresh_in':>9} {'cache_rd':>9} {'out':>7} {'lat_s':>6}")
    for i, s in enumerate(hits, 1):
        print(
            f"{i:>3} {g(s, 'input_tokens'):>9} {g(s, 'cache_read_tokens'):>9} "
            f"{g(s, 'output_tokens'):>7} {g(s, 'latency_ms') / 1000:>6.1f}"
        )

    print("\n--- totals ---")
    print(f"fresh input : {total_fresh:>10,}")
    print(f"cache_read  : {total_cache:>10,}")
    print(f"output      : {total_out:>10,}")
    print("\n--- vs baseline a0a07227 ---")
    print(f"baseline fresh : {BASELINE_FRESH:>10,}")
    print(f"this run fresh : {total_fresh:>10,}")
    if BASELINE_FRESH:
        red = (BASELINE_FRESH - total_fresh) / BASELINE_FRESH * 100
        print(f"reduction      : {red:>9.1f}%   (PASS gate: >= 30%)")
    print("\n--- digest / quality ---")
    print(f"digests recorded   : {dig}")
    print(f"expand calls       : {reexpand}")
    print(f"artifact committed : {artifact}  (need 1)")
    print(f"task_failed events : {failed}  (need 0)")
    verdict = (
        "PASS"
        if (total_fresh <= BASELINE_FRESH * 0.7 and artifact >= 1 and failed == 0)
        else "FAIL"
    )
    print(f"\nVERDICT: {verdict}")


def main() -> int:
    p = argparse.ArgumentParser(description="FRE-475 intra-turn compression A/B harness")
    p.add_argument("--email", help="Owner CF-Access email (never the injected CC userEmail).")
    p.add_argument("--profile", default="cloud", choices=["local", "cloud"])
    p.add_argument("--extract-trace", default=None, help="Extract-only for an existing trace id.")
    args = p.parse_args()

    if args.extract_trace:
        extract(args.extract_trace)
        return 0
    if not args.email:
        p.error("--email is required to send a turn")
    print("sending artifact-build turn (this takes minutes)...")
    res = send_turn(args.email, args.profile)
    print(json.dumps({k: v if k != "response" else v[:300] for k, v in res.items()}, indent=2))
    time.sleep(5)  # let the last events index
    extract(res["trace_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
