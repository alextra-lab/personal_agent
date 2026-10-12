---
name: self-telemetry
description: Reads the agent's own token use, prompt-cache hit rate, spend, budget use and interaction
  outcomes. Starts with the query_telemetry tool for event data and uses curl for the budget snapshot and
  the Captain's Log, which the tool does not read. Use when the task is about token totals, cache hit rate,
  cost, spend by provider, budget caps or how recent interactions ended. Not for how long model calls took,
  or for a general count of log events.
metadata:
  requires-tools: bash
---

# self-telemetry — The agent's own tokens, cost, budget and outcomes

## Rules

- Cite real counts from a live query. Never describe what the telemetry probably shows.
- Start with `query_telemetry`. Use curl only for the budget snapshot (Pattern 3b) and the Captain's Log (Pattern 4).
- For total spend, use `api_cost_recorded`. `model_call_completed` is LLM-only: it leaves out embedding and rerank cost.
- How long model calls took is in the `query-tempo` skill. The timeline of one turn is in the `turn-forensics` skill.
- Elasticsearch mechanics (schema, ES|QL, the `_search` template) are in the `query-elasticsearch` skill. A path of the form `<skill>/<file>` names a file of another skill: load it with `read_skill`, with `name` set to `<skill>` and `path` set to `<file>`.

## What the events hold

| `event_type` | Fields to read |
|---|---|
| `api_cost_recorded` | The total-spend ledger. Every priced call, by `provider` (`anthropic`, `openai`, `ovh`, `voyage`). `model`, `cost_usd`, `trace_id`, `session_id` |
| `model_call_completed` | LLM-completions only, cloud and local. `model`, `provider`, `role`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cost_usd` (priced calls) |
| `embedding_generated` | Managed embedding call. `provider`, `model`, `cost_usd` |
| `reranker_applied` | Reranker call. `provider` (`voyage`), `cost_usd` is empty for the free local fallback |
| `budget_counter_snapshot` | One document for each budget role and window, every 60 seconds. `role`, `time_window`, `running_total`, `cap_usd`, `utilization_ratio` |

Two fields have different names for the same meaning. On `model_call_completed` the cache field
is `cache_read_tokens`. On `api_cost_recorded` it is `cache_read_input_tokens`. A query for the
second name on `model_call_completed` returns nothing.

## Pattern 1 — Token stats by model

```python
query_telemetry(action="search", index="agent-logs", size=0, since="2h",
                query={"term": {"event_type": "model_call_completed"}},
                aggs={"by_model": {"terms": {"field": "model", "size": 10},
                                   "aggs": {"input": {"sum": {"field": "input_tokens"}},
                                            "output": {"sum": {"field": "output_tokens"}},
                                            "cost": {"sum": {"field": "cost_usd"}}}}})
```

## Pattern 2 — Prompt-cache hit rate

Cache read tokens are served from the cache. A higher share means more reuse.

```python
query_telemetry(action="search", index="agent-logs", size=0, since="24h",
                query={"term": {"event_type": "model_call_completed"}},
                aggs={"by_provider": {"terms": {"field": "provider", "size": 10},
                                      "aggs": {"input": {"sum": {"field": "input_tokens"}},
                                               "cached": {"sum": {"field": "cache_read_tokens"}}}}})
```

Hit rate is `cached / (input + cached)`. Say which providers the rate covers. A rate near zero
means caching does not fire. Some calls hold no `cache_read_tokens`: the sum covers only those
that do.

## Pattern 3 — Cost by role and by model, LLM only (last 24 hours)

`role` is the factory role: `primary`, `sub_agent`, `compressor`, `artifact_builder`. It is
not the budget cap (see Pattern 3b). This pattern is LLM-only and leaves out embedding and
rerank cost. For total spend use Pattern 3c.

```python
query_telemetry(action="search", index="agent-logs", size=0, since="24h",
                query={"bool": {"must": [{"term": {"event_type": "model_call_completed"}},
                                         {"exists": {"field": "cost_usd"}}]}},
                aggs={"by_role": {"terms": {"field": "role", "size": 10},
                                  "aggs": {"cost": {"sum": {"field": "cost_usd"}}}}})
```

For the split by model, change `"field": "role"` to `"field": "model"`.

## Pattern 3a — Daily spend trend (last 7 days)

Total spend across all providers (LLM, OVH embedding, Voyage rerank). It uses
`api_cost_recorded`, the unified per-call ledger.

```python
query_telemetry(action="search", index="agent-logs", size=0, since="7d",
                query={"term": {"event_type": "api_cost_recorded"}},
                aggs={"by_day": {"date_histogram": {"field": "@timestamp", "calendar_interval": "day"},
                                 "aggs": {"cost": {"sum": {"field": "cost_usd"}}}}})
```

## Pattern 3b — Spend by budget cap

Several factory roles share one budget cap. `budget_counter_snapshot` already holds the grouped
`running_total` for each cap, so no join is needed. The tool does not list these fields, so this
pattern uses curl. The role in this event is the budget role from `config/governance/budget.yaml`,
not the factory role of Pattern 3.

```bash
curl -fsS -X POST 'http://elasticsearch:9200/agent-logs-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 0,
    "query": {"bool": {"filter": [
      {"term": {"event_type": "budget_counter_snapshot"}},
      {"range": {"@timestamp": {"gte": "now-5m"}}}
    ]}},
    "aggs": {"by_window": {
      "terms": {"field": "time_window", "size": 5},
      "aggs": {"by_role": {
        "terms": {"field": "role", "size": 20},
        "aggs": {
          "running_total": {"max": {"field": "running_total"}},
          "cap_usd": {"max": {"field": "cap_usd"}},
          "utilization_ratio": {"max": {"field": "utilization_ratio"}}
        }
      }}
    }}
  }' | jq '.aggregations.by_window.buckets'
```

If the reply has no buckets, the last 5 minutes hold no snapshot. Widen `gte` to `now-30m` once.

## Pattern 3c — Total spend by provider (last 24 hours)

The total-spend source of truth. `api_cost_recorded` carries every priced call.
`model_call_completed` alone understates spend, because it leaves out the `embedding_generated`
(OVH) and `reranker_applied` (Voyage) cost.

```python
query_telemetry(action="search", index="agent-logs", size=0, since="24h",
                query={"term": {"event_type": "api_cost_recorded"}},
                aggs={"by_provider": {"terms": {"field": "provider", "size": 10},
                                      "aggs": {"cost": {"sum": {"field": "cost_usd"}}}}})
```

## Pattern 3d — Managed-inference cost: embedding and reranker (last 7 days)

These are the two non-LLM cost paths that Pattern 3c adds up without a split by purpose.

```python
# OVH embedding cost, by day
query_telemetry(action="search", index="agent-logs", size=0, since="7d",
                query={"term": {"event_type": "embedding_generated"}},
                aggs={"by_day": {"date_histogram": {"field": "@timestamp", "calendar_interval": "day"},
                                 "aggs": {"cost": {"sum": {"field": "cost_usd"}}}}})

# Voyage reranker cost, total. The free local fallback has no cost_usd, so it is left out
query_telemetry(action="search", index="agent-logs", size=0, since="7d",
                query={"bool": {"must": [{"term": {"event_type": "reranker_applied"}},
                                         {"term": {"provider": "voyage"}},
                                         {"exists": {"field": "cost_usd"}}]}},
                aggs={"total": {"sum": {"field": "cost_usd"}}})
```

## Pattern 4 — How recent interactions ended (Captain's Log)

The captures index is not one of the tool's families, so this pattern uses curl. A capture holds
`trace_id`, `outcome`, `total_tokens` and `timestamp`. The date field is `timestamp`, not
`@timestamp`: a range on `@timestamp` returns nothing and no error.

```bash
# The last 10 interactions, newest first
curl -fsS -X POST 'http://elasticsearch:9200/agent-captains-captures-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 10,
    "sort": [{"timestamp": "desc"}],
    "_source": ["trace_id", "outcome", "total_tokens", "timestamp"]
  }' | jq '.hits.hits[]._source'

# Outcomes over the last 24 hours
curl -fsS -X POST 'http://elasticsearch:9200/agent-captains-captures-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 0,
    "query": {"range": {"timestamp": {"gte": "now-24h"}}},
    "aggs": {"by_outcome": {"terms": {"field": "outcome", "size": 10}}}
  }' | jq '{total: .hits.total.value, outcomes: .aggregations.by_outcome.buckets}'
```

Reflections (recurring patterns) are in `agent-captains-reflections-*`. They hold `rationale`,
`seen_count`, `category` and `proposed_change_what`. Use the same `_search` form, with
`"sort": [{"seen_count": "desc"}]` and `"query": {"range": {"seen_count": {"gte": 2}}}`.

## Pattern 5 — Am I running well right now?

```python
# Errors in the last hour, by event type
query_telemetry(action="search", index="agent-logs", size=0, since="1h",
                query={"term": {"level": "ERROR"}},
                aggs={"by_event": {"terms": {"field": "event_type", "size": 10}}})

# Model calls in the last hour
query_telemetry(action="count", index="agent-logs", since="1h",
                query={"term": {"event_type": "model_call_completed"}})
```

For how long those calls took, use the `query-tempo` skill.

## Success

Check the result before you answer:

- The answer names the window (`since`) and says whether the figure is LLM-only or total spend.
- A total-spend figure comes from `api_cost_recorded`. It is higher than the LLM-only figure by the embedding and rerank cost. If the two are equal, check the filter.
- The buckets of an outcome count can sum to less than the total: a capture can hold no `outcome`. Say how many hold none.
- A cache hit rate states the providers that it covers. A local provider and a cloud provider have different cache behaviour.
- The budget figure is the latest snapshot for each role and window, not a sum of snapshots.

## Recovery

| Failure | Cause | Next step |
|---|---|---|
| `refused: field 'x' is not available in agent-logs. Allowed fields: ...` | A field outside the allowlist (for example `time_window` or `running_total`) | For the budget fields use Pattern 3b. Otherwise pick a field from the list |
| An aggregation returns no buckets | The field does not exist for this event, or the window is empty | Run a terms aggregation on `event_type` with `exists` on the field (see `query-elasticsearch`). Widen the window once |
| A query on `cache_read_input_tokens` returns nothing on `model_call_completed` | That name is for `api_cost_recorded` | Use `cache_read_tokens` on `model_call_completed` |
| `run_python` raises `ImportError` on `personal_agent` | The sandbox holds no project source | Use the tool, or curl. Never import project modules |
| A curl range on the captures index returns nothing | The range used `@timestamp` | Use `timestamp` |
| `store_unavailable` or `store_timeout` | Elasticsearch is down or slow | Call once more. If it fails again, stop and tell the user |
| The same failure twice | — | Stop. Tell the user what failed and what was tried |
