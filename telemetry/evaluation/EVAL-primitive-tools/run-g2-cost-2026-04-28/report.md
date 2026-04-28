# FRE-262 PIVOT-3 Evaluation Report

Generated: 2026-04-28T17:21:02.133594+00:00
Control URL: http://localhost:9002
Treatment URL: http://localhost:9003

## Results

| # | ID | Category | Prompt | Ctrl response (truncated) | Trt response (truncated) | Ctrl tok | Trt tok | Ctrl turns | Trt turns | Cache hit % | Quality |
|---|----|-----------|--------------------|--------------------------|--------------------------|---------|---------|------------|-----------|-------------|---------|
| 1 | es-02 | query-elasticsearch | How many times did the agent call query_elasticsearch today? | Based on the telemetry results, the agent has called **`query_elasticsearch` 0 times** today — no events matching that tool call were found in today's logs. | The agent called **`query_elasticsearch` 79 times** in the last 24 hours. | 10628 | 17246 | 2 | 2 | ctrl 100% / trt 55% | <!-- ✅/⚠️/❌ --> |
| 2 | es-04 | query-elasticsearch | Find a trace from the past week where the agent hit the cons... | Here's a full breakdown of what the logs reveal:

---

## 🔁 Consecutive Loop Gate — Trace Found

**Trace ID:** `8dc7f1e6-0ee5-4f64-9953-17a2ef3e0d7b`
**Date:** April 28, 2026 (~17:22 UTC)
**Source:** ... | Here's what was found across the past week's loop gate events:

---

## 🔁 Consecutive Loop Gate Hits — Top Traces (Last 7 Days)

&#124; Rank &#124; Tool &#124; Max Consecutive Calls &#124; Trace ID &#124;
&#124;------&#124;------&#124;-----... | 57012 | 104460 | 10 | 12 | ctrl 98% / trt 96% | <!-- ✅/⚠️/❌ --> |
| 3 | fetch-03 | fetch-url | What's the current Anthropic pricing? Check https://www.anth... | Here's the current Anthropic pricing based on the search results:

---

## 🧑‍💻 Claude Subscription Plans (claude.ai)

&#124; Plan &#124; Price &#124;
&#124;------&#124;-------&#124;
&#124; **Free** &#124; $0 — basic access, ~10–15 messages/... | Here's the current Anthropic Claude pricing based on the search results (as of mid-2025):

---

## 🤖 Claude API Pricing (Pay-as-you-go, per million tokens)

&#124; Model &#124; Input &#124; Output &#124;
&#124;---&#124;---&#124;---&#124;
&#124; ... | 34472 | 45550 | 6 | 6 | ctrl 99% / trt 92% | <!-- ✅/⚠️/❌ --> |
| 4 | ls-03 | list-directory | How many YAML files are under /app/config? | There are **12 YAML files** under `/app/config`:

&#124; # &#124; Path &#124;
&#124;---&#124;------&#124;
&#124; 1 &#124; `/app/config/models.eval.yaml` &#124;
&#124; 2 &#124; `/app/config/models-baseline.yaml` &#124;
&#124; 3 &#124; `/app/config/governance/safety.yaml`... | There are **12 YAML files** under `/app/config`. | 11027 | 15194 | 2 | 2 | ctrl 100% / trt 83% | <!-- ✅/⚠️/❌ --> |
| 5 | metrics-02 | system-metrics | How much memory is the agent service using right now? | Here's a snapshot of the current memory usage on the system hosting the agent:

&#124; Metric &#124; Value &#124;
&#124;---&#124;---&#124;
&#124; **Memory Used** &#124; **33.4%** &#124;
&#124; **Total RAM** &#124; ~22.9 GB &#124;
&#124; **Available RAM** &#124; ~15.3 GB... | Here's the current memory usage for the agent service:

&#124; PID &#124; RSS (MB) &#124; % Mem &#124; Process &#124;
&#124;-----&#124;----------&#124;-------&#124;---------&#124;
&#124; 19 &#124; **338.9 MB** &#124; 1.4% &#124; `uvicorn` (main worker — Python app) &#124;
&#124; ... | 27265 | 14244 | 5 | 2 | ctrl 99% / trt 85% | <!-- ✅/⚠️/❌ --> |
| 6 | diag-01 | system-diagnostics | List the top 10 processes by memory usage. | Here are the **top 10 processes by memory usage** on this host:

&#124; Rank &#124; PID &#124; USER &#124; %CPU &#124; %MEM &#124; VSZ (KB) &#124; RSS (KB) &#124; Command &#124;
&#124;------&#124;-----&#124;------&#124;------&#124;------&#124;----------&#124;----------&#124;---------&#124;... | Here are the **top 10 processes by memory usage** on this system:

&#124; Rank &#124; PID &#124; User &#124; %CPU &#124; %MEM &#124; VSZ (KB) &#124; RSS (KB) &#124; Command &#124;
&#124;------&#124;-----&#124;------&#124;------&#124;------&#124;----------&#124;----------&#124;--------... | 11082 | 13154 | 2 | 2 | ctrl 100% / trt 69% | <!-- ✅/⚠️/❌ --> |

## Gate Criteria

- [ ] Primitive success rate >= curated success rate on >= 17/20 prompts
- [ ] Zero "could not find primitive equivalent" failures (dead-end failures)

## Token + Turns Summary

| Metric | Control | Treatment |
|--------|---------|-----------|
| Total tokens | 151,486 | 209,848 |
| Mean turns / prompt | 4.5 | 4.3 |
| Cache hit % | 99% | 89% |
| Total wall clock | 173s | 145s |

## Per-Category Gate

If primitive < curated for a specific category, move that category's tools to PIVOT-4 keep list.

## Grading Key

Fill in the Quality column:  ✅ = correct/complete  ⚠️ = partial/extra turns  ❌ = wrong/missing

Session IDs and full token breakdowns are in results.json — use them to look up traces in Kibana.
