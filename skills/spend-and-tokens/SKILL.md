---
name: spend-and-tokens
description: Reads the agent's own token totals, prompt-cache hit rate for each provider, spend by provider, role and day, and budget use. Starts with the search_logs tool and uses curl for the budget snapshot. Use when the task is about token totals, cache hit rate, cost, spend by provider or by day, or budget caps. Not for how long model calls took, which the `model-latency` skill gives, or for how interactions ended, which the `interaction-outcomes` skill gives.
metadata:
  provides-tools: search_logs
---

# spend-and-tokens — The agent's own tokens, cost and budget

## Rules

- Cite real counts from a live query. Never describe what the telemetry probably shows.
- Start with `search_logs`. Use curl only for the budget snapshot (Pattern 3b).
- For total spend, use `api_cost_recorded`. `model_call_completed` is LLM-only: it leaves out embedding and rerank cost.
- How long model calls took is in the `model-latency` skill. How interactions ended is in the `interaction-outcomes` skill. Counts of other log events are in the `log-search` skill.
- A path of the form `<skill>/<file>` names a file of another skill: load it with `read_skill`, with `name` set to `<skill>` and `path` set to `<file>`.

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
search_logs(action="search", index="agent-logs", size=0, since="2h",
                query={"term": {"event_type": "model_call_completed"}},
                aggs={"by_model": {"terms": {"field": "model", "size": 10},
                                   "aggs": {"input": {"sum": {"field": "input_tokens"}},
                                            "output": {"sum": {"field": "output_tokens"}},
                                            "cost": {"sum": {"field": "cost_usd"}}}}})
```

## Pattern 2 — Prompt-cache hit rate

Cache read tokens are served from the cache. A higher share means more reuse.

```python
search_logs(action="search", index="agent-logs", size=0, since="24h",
            query={"term": {"event_type": "model_call_completed"}},
            aggs={"by_provider": {"terms": {"field": "provider", "size": 10},
                                  "aggs": {"input": {"sum": {"field": "input_tokens"}},
                                           "cached": {"sum": {"field": "cache_read_tokens"}}}}})
```

Give the rate for each provider, with the formula. `input_tokens` already includes the cached
tokens for every provider that the client records:

| Provider path | What `input_tokens` holds | Hit rate |
|---|---|---|
| Local server (llama.cpp) | `prompt_tokens`, which includes the cached tokens | `cached / input` |
| Anthropic | The client adds the cache reads to `prompt_tokens` | `cached / input` |
| OpenAI | `prompt_tokens`, which includes the cached tokens | `cached / input` |

Check the formula on the data. A provider whose `cached` sum is larger than its `input` sum
reports the input without the cache. For that provider the rate is `cached / (input + cached)`.
A rate above 1 means that the wrong formula was used. A rate near zero means caching does not
fire. Some calls hold no `cache_read_tokens`: the sum covers only those that do.

## Pattern 3 — Cost by role and by model, LLM only (last 24 hours)

`role` is the factory role: `primary`, `sub_agent`, `compressor`, `artifact_builder`. It is
not the budget cap (see Pattern 3b). This pattern is LLM-only and leaves out embedding and
rerank cost. For total spend use Pattern 3c.

```python
search_logs(action="search", index="agent-logs", size=0, since="24h",
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
search_logs(action="search", index="agent-logs", size=0, since="7d",
                query={"term": {"event_type": "api_cost_recorded"}},
                aggs={"by_day": {"date_histogram": {"field": "@timestamp", "calendar_interval": "day"},
                                 "aggs": {"cost": {"sum": {"field": "cost_usd"}}}}})
```

## Pattern 3b — Spend by budget cap

Several factory roles share one budget cap. `budget_counter_snapshot` already holds the grouped
`running_total` for each cap, so no join is needed. The tool does not list these fields, so this
pattern uses curl. The role in this event is the budget role from `config/governance/budget.yaml`,
not the factory role of Pattern 3.

Past the tool's limits, and only with the `bash` tool: read `references/budget-snapshot.md`.

## Pattern 3c — Total spend by provider (last 24 hours)

The total-spend source of truth. `api_cost_recorded` carries every priced call.
`model_call_completed` alone understates spend, because it leaves out the `embedding_generated`
(OVH) and `reranker_applied` (Voyage) cost.

```python
search_logs(action="search", index="agent-logs", size=0, since="24h",
                query={"term": {"event_type": "api_cost_recorded"}},
                aggs={"by_provider": {"terms": {"field": "provider", "size": 10},
                                      "aggs": {"cost": {"sum": {"field": "cost_usd"}}}}})
```

## Pattern 3d — Managed-inference cost: embedding and reranker (last 7 days)

These are the two non-LLM cost paths that Pattern 3c adds up without a split by purpose.

```python
# OVH embedding cost, by day
search_logs(action="search", index="agent-logs", size=0, since="7d",
                query={"term": {"event_type": "embedding_generated"}},
                aggs={"by_day": {"date_histogram": {"field": "@timestamp", "calendar_interval": "day"},
                                 "aggs": {"cost": {"sum": {"field": "cost_usd"}}}}})

# Voyage reranker cost, total. The free local fallback has no cost_usd, so it is left out
search_logs(action="search", index="agent-logs", size=0, since="7d",
                query={"bool": {"must": [{"term": {"event_type": "reranker_applied"}},
                                         {"term": {"provider": "voyage"}},
                                         {"exists": {"field": "cost_usd"}}]}},
                aggs={"total": {"sum": {"field": "cost_usd"}}})
```

## Pattern 5 — Am I running well right now?

```python
# Errors in the last hour, by event type
search_logs(action="search", index="agent-logs", size=0, since="1h",
                query={"term": {"level": "ERROR"}},
                aggs={"by_event": {"terms": {"field": "event_type", "size": 10}}})

# Model calls in the last hour
search_logs(action="count", index="agent-logs", since="1h",
                query={"term": {"event_type": "model_call_completed"}})
```

For how long those calls took, use the `model-latency` skill.

## Success

Check the result before you answer:

- The answer names the window (`since`) and says whether the figure is LLM-only or total spend.
- A total-spend figure comes from `api_cost_recorded`. It is higher than the LLM-only figure by the embedding and rerank cost. If the two are equal, check the filter.
- A cache hit rate states the provider that it covers, the formula, and a value between 0 and 1. A local provider and a cloud provider have different cache behaviour.
- The budget figure is the latest snapshot for each role and window, not a sum of snapshots.

## Recovery

| Failure | Cause | Next step |
|---|---|---|
| `refused: field 'x' is not available in agent-logs. Allowed fields: ...` | A field outside the allowlist (for example `time_window` or `running_total`) | For the budget fields use Pattern 3b. Otherwise pick a field from the list |
| An aggregation returns no buckets | The field does not exist for this event, or the window is empty | Run a terms aggregation on `event_type` with `exists` on the field (see the `log-search` skill). Widen the window once |
| A query on `cache_read_input_tokens` returns nothing on `model_call_completed` | That name is for `api_cost_recorded` | Use `cache_read_tokens` on `model_call_completed` |
| A hit rate above 1 | The input does not include the cache for that provider | Use `cached / (input + cached)` for it, and say so |
| `run_python` raises `ImportError` on `personal_agent` | The sandbox holds no project source | Use the tool, or curl. Never import project modules |
| `store_unavailable` or `store_timeout` | Elasticsearch is down or slow | Call once more. If it fails again, stop and tell the user |
| The same failure twice | — | Stop. Tell the user what failed and what was tried |
