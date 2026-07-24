---
name: self-telemetry
description: Query agent self-telemetry — token usage, prompt-cache hit rate, LLM latency, cost breakdown, and interaction outcomes from Elasticsearch and Captain's Log. Uses bash+curl throughout.
when_to_use: When asked about my own token stats, cache hit rate, latency, cost, model usage, LLM call counts, interaction success rate, or anything involving my telemetry, performance, or introspection. Also use for latency breakdown of a specific trace_id.
tools: [bash]
nudge: "Cite real ES counts/traces in your answer — do not paraphrase what the telemetry 'probably' shows."
keywords:
  # Token / cost questions
  - token
  - token usage
  - token stats
  - prompt tokens
  - completion tokens
  - cache hit
  - cache rate
  - prompt cache
  - cache read
  - cache_creation
  - cost
  - spend
  - cost_usd
  # Latency / performance
  - latency
  - latency_ms
  - duration
  - how fast
  - how long
  - slow
  - p95
  - response time
  # Model / call counts
  - model usage
  - model calls
  - LLM calls
  - litellm
  - model_call
  - cloud path
  - local path
  - which model
  # Interaction outcomes
  - success rate
  - interaction
  - outcomes
  - captures
  - reflection
  # Meta / introspection
  - my logs
  - my stats
  - my telemetry
  - my performance
  - self-introspect
  - self telemetry
  - self-diagnosis
  - how am I doing
  - check myself
canonical_patterns:
  - "model_call_completed"
  - "agent-captains-captures-*"
  - "agent-captains-reflections-*"
known_bad_patterns:
  - pattern: "from personal_agent"
    applies_to:
      tool: bash
      fields: [command]
    reason: "run_python sandbox has no project source installed — 'from personal_agent import ...' raises ImportError."
    suggestion: "Use bash+curl to the ES REST API instead."
  - pattern: "cache_read_input_tokens"
    applies_to:
      tool: bash
      fields: [command]
    reason: "On 'model_call_completed' the ES field is 'cache_read_tokens', not 'cache_read_input_tokens' (internal usage object name)."
    suggestion: "Use 'cache_read_tokens' when querying 'model_call_completed'."
  - pattern: "litellm_request_complete"
    applies_to:
      tool: bash
      fields: [command]
    reason: "'litellm_request_complete' was removed in FRE-376 Phase 3 — it no longer exists in agent-logs-* (0 docs). Cost and token telemetry for BOTH cloud and local model calls is unified on 'model_call_completed'."
    suggestion: "Use event_type == \"model_call_completed\" for per-call cost/tokens, or \"api_cost_recorded\" for the parallel cost ledger."
---

# self-telemetry — Agent self-introspection via ES + Captain's Log

**Primary path:** `bash curl` to `http://elasticsearch:9200`. **No `run_python` project imports.**

## Event types and what they cover

| `event_type` | Path | Key fields |
|---|---|---|
| `model_call_completed` | **Unified** LLM call event — cloud (`LiteLLMClient`) AND local (`LocalLLMClient`) both emit this since FRE-376 Phase 3 (see `known_bad_patterns` above for the retired per-provider name) | `model`, `provider`, `role`, `endpoint`, `latency_ms`, `input_tokens`, `output_tokens`, `total_tokens`, `cache_read_tokens`, `cost_usd` (present on cloud/priced calls) |
| `api_cost_recorded` | Parallel cost ledger (Postgres `api_costs` mirror) — always carries `cost_usd`, no `role` | `provider`, `model`, `cost_usd`, `latency_ms`, `record_id`, `trace_id`, `session_id`, `cache_read_input_tokens`, `cache_creation_input_tokens` |
| `budget_counter_snapshot` | Cap-utilization gauge, one doc per configured budget-role cap every 60s (FRE-547) | `role` (the **budget-role** key, e.g. `main_inference`, `entity_extraction`, `artifact_builder` — already groups factory roles like `primary`/`sub_agent`/`compressor` via `cost_gate.budget_role_for()`), `time_window` (`daily`/`weekly`), `window_start`, `running_total`, `cap_usd`, `utilization_ratio` |
| `llm_step_completed` | Orchestrator step wrapper | `model_role`, `duration_ms`, `tokens` (total) |

Captain's Log:
- `agent-captains-captures-*` — per-request outcome: `outcome`, `total_tokens`, `duration_ms`, `task_type`, `user_message`, `timestamp`
- `agent-captains-reflections-*` — recurring pattern reflections: `rationale`, `seen_count`, `category`, `proposed_change_what`

---

## Pattern 1 — Token stats by model, last 2 hours

```bash
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"model_call_completed\" AND @timestamp > NOW()-2hours | STATS calls=COUNT(*), input=SUM(input_tokens), output=SUM(output_tokens), cost=SUM(cost_usd) BY model, provider | SORT input DESC"}' \
  | jq '.columns[].name, .values'
```

---

## Pattern 2 — Prompt-cache hit rate

Cache read tokens (served from cache) versus input tokens written fresh. A higher ratio = more cache reuse.

```bash
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"model_call_completed\" AND @timestamp > NOW()-24hours AND input_tokens IS NOT NULL | STATS total_input=SUM(input_tokens), total_cached=SUM(cache_read_tokens), calls=COUNT(*) BY provider"}' \
  | jq '.values'
```

Hit rate ≈ `total_cached / (total_input + total_cached)`. A ratio near zero means caching is not firing; near 1 means heavy reuse.

---

## Pattern 3 — Cost breakdown by role and by model (last 24 h)

`role` is the factory role (`primary`, `sub_agent`, `compressor`, `artifact_builder`, ...) —
not yet grouped into a budget cap. See Pattern 3b to group by budget cap instead.

```bash
# By role
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"model_call_completed\" AND @timestamp > NOW()-24hours AND cost_usd IS NOT NULL | STATS total_cost=SUM(cost_usd), calls=COUNT(*), avg_latency=AVG(latency_ms) BY role | SORT total_cost DESC"}' \
  | jq '.values'

# By model
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"model_call_completed\" AND @timestamp > NOW()-24hours AND cost_usd IS NOT NULL | STATS total_cost=SUM(cost_usd), calls=COUNT(*) BY model | SORT total_cost DESC"}' \
  | jq '.values'
```

---

## Pattern 3a — Daily spend trend (last 7 days)

```bash
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"model_call_completed\" AND @timestamp > NOW()-7days AND cost_usd IS NOT NULL | EVAL day = DATE_TRUNC(1 day, @timestamp) | STATS daily_cost=SUM(cost_usd), calls=COUNT(*) BY day | SORT day ASC"}' \
  | jq '.values'
```

---

## Pattern 3b — Spend by budget cap (grouped, not per-role)

Several factory roles share one budget cap (`cost_gate.budget_role_for()` maps them —
`primary`/`sub_agent`/`compressor` all count against the `main_inference` cap;
`artifact_builder` has its own lane). `budget_counter_snapshot` already carries the grouped
`running_total` per cap — no client-side join needed:

```bash
# Current utilization per budget cap (daily + weekly windows)
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"budget_counter_snapshot\" AND @timestamp > NOW()-5minutes | STATS running_total=MAX(running_total), cap_usd=MAX(cap_usd), utilization_ratio=MAX(utilization_ratio) BY role, time_window | SORT time_window, utilization_ratio DESC"}' \
  | jq '.values'
```

The cap definitions themselves live in `config/governance/budget.yaml`; the `role` value on
this event is the budget-role key from that file, not the factory `role` seen on
`model_call_completed`.

---

## Pattern 4 — Recent interaction outcomes

```bash
# Last 10 interactions (newest first)
curl -s 'http://elasticsearch:9200/agent-captains-captures-*/_search' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 10,
    "sort": [{"timestamp": "desc"}],
    "_source": ["trace_id","task_type","outcome","total_tokens","duration_ms","timestamp","user_message"]
  }' | jq '.hits.hits[]._source'

# Success rate over last 24 h (ES|QL on captures)
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-captains-captures-* | WHERE @timestamp > NOW()-24hours | STATS total=COUNT(*), successes=COUNT_IF(outcome == \"success\"), avg_tokens=AVG(total_tokens), avg_duration=AVG(duration_ms)"}' \
  | jq '.values'
```

---

## Pattern 5 — Latency breakdown for a specific trace

Replace `<trace_id>` with the actual ID.

```bash
# Full timeline for a trace
curl -s 'http://elasticsearch:9200/agent-logs-*/_search' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 100,
    "query": {"term": {"trace_id.keyword": "<trace_id>"}},
    "sort": [{"@timestamp": "asc"}],
    "_source": ["event_type","@timestamp","latency_ms","duration_ms","model","input_tokens","output_tokens","tool_name","role"]
  }' | jq '.hits.hits[]._source'

# LLM-only events for a trace (model call + step wrapper)
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE trace_id == \"<trace_id>\" AND event_type IN (\"model_call_completed\", \"llm_step_completed\") | FIELDS @timestamp, event_type, latency_ms, duration_ms, model, input_tokens, output_tokens, model_role | SORT @timestamp ASC"}' \
  | jq '.values'
```

---

## Quick health check — am I running well right now?

```bash
# Errors in the last hour
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE level == \"ERROR\" AND @timestamp > NOW()-1hour | STATS count=COUNT(*) BY message | SORT count DESC | LIMIT 10"}' \
  | jq '.values'

# LLM call count + avg latency last hour (cloud + local combined — one unified event)
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"model_call_completed\" AND @timestamp > NOW()-1hour | STATS calls=COUNT(*), avg_latency=AVG(latency_ms), p95_approx=PERCENTILE(latency_ms, 95)"}' \
  | jq '.values'
```

---

## Notes

- Always pipe large responses through `| head -c 50000` or use `LIMIT N` in ES|QL.
- `trace_id` requires `.keyword` suffix for exact-match `_search` queries but not for ES|QL `==`.
- Cloud and local paths emit the **same** `event_type` (`model_call_completed`) — filter by `provider` if you need to split them, not by a different event name.
- Reflections and captures use the Captain's Log indices (`agent-captains-*`), not `agent-logs-*`.

See also: [query-elasticsearch](query-elasticsearch.md) · [seshat-observations](seshat-observations.md)
