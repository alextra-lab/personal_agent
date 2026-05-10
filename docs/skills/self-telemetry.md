---
name: self-telemetry
description: Query agent self-telemetry — token usage, prompt-cache hit rate, LLM latency, cost breakdown, and interaction outcomes from Elasticsearch and Captain's Log. Uses bash+curl throughout.
when_to_use: When asked about my own token stats, cache hit rate, latency, cost, model usage, LLM call counts, interaction success rate, or anything involving my telemetry, performance, or introspection. Also use for latency breakdown of a specific trace_id.
tools: [bash]
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
  - "litellm_request_complete"
  - "model_call_completed"
  - "llm_step_completed"
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
    reason: "ES field is 'cache_read_tokens', not 'cache_read_input_tokens' (internal usage object name)."
    suggestion: "Use 'cache_read_tokens' in all ES queries."
  - pattern: "event_type == \"model_call_completed\" AND model"
    applies_to:
      tool: bash
      fields: [command]
    reason: "Cloud path emits 'litellm_request_complete' (not 'model_call_completed'). A single equality filter that mixes path assumptions returns nothing."
    suggestion: "Filter each path separately, or use: event_type IN (\"litellm_request_complete\", \"model_call_completed\")"
---

# self-telemetry — Agent self-introspection via ES + Captain's Log

**Primary path:** `bash curl` to `http://elasticsearch:9200`. **No `run_python` project imports.**

## Event types and what they cover

| `event_type` | Path | Key fields |
|---|---|---|
| `litellm_request_complete` | Cloud LLM (all cloud providers) | `model`, `endpoint`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `latency_ms`, `cost_usd`, `cache_read_tokens`, `cache_creation_input_tokens` |
| `model_call_completed` | Local LLM (llama.cpp / MLX) | `model_id`, `endpoint`, `api_type`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `latency_ms`, `cache_read_tokens` |
| `llm_step_completed` | Orchestrator step wrapper | `model_role`, `duration_ms`, `tokens` (total) |

Captain's Log:
- `agent-captains-captures-*` — per-request outcome: `outcome`, `total_tokens`, `duration_ms`, `task_type`, `user_message`, `timestamp`
- `agent-captains-reflections-*` — recurring pattern reflections: `rationale`, `seen_count`, `category`, `proposed_change_what`

---

## Pattern 1 — Token stats by model, last 2 hours

```bash
# Cloud path (litellm)
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"litellm_request_complete\" AND @timestamp > NOW()-2hours | STATS calls=COUNT(*), prompt=SUM(prompt_tokens), completion=SUM(completion_tokens), cost=SUM(cost_usd) BY model | SORT prompt DESC"}' \
  | jq '.columns[].name, .values'

# Local path (llama.cpp / MLX)
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"model_call_completed\" AND @timestamp > NOW()-2hours | STATS calls=COUNT(*), prompt=SUM(prompt_tokens), completion=SUM(completion_tokens) BY model_id, endpoint | SORT prompt DESC"}' \
  | jq '.columns[].name, .values'
```

---

## Pattern 2 — Prompt-cache hit rate

Cache read tokens (served from cache) versus prompt tokens written fresh. A higher ratio = more cache reuse.

```bash
# Cloud path cache stats (last 24 h)
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"litellm_request_complete\" AND @timestamp > NOW()-24hours AND prompt_tokens IS NOT NULL | STATS total_prompt=SUM(prompt_tokens), total_cached=SUM(cache_read_tokens), total_written=SUM(cache_creation_input_tokens), calls=COUNT(*)"}' \
  | jq '.values'

# Local path cache stats (last 24 h)
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"model_call_completed\" AND @timestamp > NOW()-24hours AND cache_read_tokens IS NOT NULL | STATS total_prompt=SUM(prompt_tokens), total_cached=SUM(cache_read_tokens), calls=COUNT(*)"}' \
  | jq '.values'
```

Hit rate ≈ `total_cached / (total_prompt + total_cached)`. A ratio near zero means caching is not firing; near 1 means heavy reuse.

---

## Pattern 3 — Cost breakdown by model role (last 24 h)

```bash
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"litellm_request_complete\" AND @timestamp > NOW()-24hours AND cost_usd IS NOT NULL | STATS total_cost=SUM(cost_usd), calls=COUNT(*), avg_latency=AVG(latency_ms) BY model_role | SORT total_cost DESC"}' \
  | jq '.values'
```

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
    "_source": ["event_type","@timestamp","latency_ms","duration_ms","model","model_id","prompt_tokens","completion_tokens","tool_name","model_role"]
  }' | jq '.hits.hits[]._source'

# LLM-only events for a trace (cloud + local + step)
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE trace_id == \"<trace_id>\" AND event_type IN (\"litellm_request_complete\", \"model_call_completed\", \"llm_step_completed\") | FIELDS @timestamp, event_type, latency_ms, duration_ms, model, model_id, prompt_tokens, completion_tokens, model_role | SORT @timestamp ASC"}' \
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

# LLM call count + avg latency last hour (both paths combined)
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type IN (\"litellm_request_complete\", \"model_call_completed\") AND @timestamp > NOW()-1hour | STATS calls=COUNT(*), avg_latency=AVG(latency_ms), p95_approx=PERCENTILE(latency_ms, 95)"}' \
  | jq '.values'
```

---

## Notes

- Always pipe large responses through `| head -c 50000` or use `LIMIT N` in ES|QL.
- `trace_id` requires `.keyword` suffix for exact-match `_search` queries but not for ES|QL `==`.
- Cloud and local paths use different `event_type` values — see table above. Never mix them in a single `==` filter without `IN (...)`.
- Reflections and captures use the Captain's Log indices (`agent-captains-*`), not `agent-logs-*`.

See also: [query-elasticsearch](query-elasticsearch.md) · [seshat-observations](seshat-observations.md)
