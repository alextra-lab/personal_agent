---
name: log-search
description: Counts, breaks down or finds agent log events over a time window, lists indices and fields, and runs ES|QL. Starts with the search_logs tool and uses curl past its limits - more than 50 hits, a window over 30 days, an index family outside its three, a field outside its list. Use when the task is a count, a breakdown, an error search, an index listing, an ES|QL query or a field lookup over agent-logs, agent-topology or the monitor indices. Not for model-call timing, which the `model-latency` skill gives, or for spend and token totals, which the `spend-and-tokens` skill gives.
metadata:
  requires-tools: bash
  provides-tools: search_logs
---

# log-search — Count, break down and find agent log events

## Rules

- Answer only from a live query. Never answer from what the logs probably hold.
- Start with the `search_logs` tool. It knows the fields, a refusal lists the allowed fields, and the result is bounded.
- Use curl only for what the tool cannot do. The table below says which.
- Use only the index families in this file. Never write a guessed index name.
- A monitor index records what a monitor saw at its last run. Read its newest document before you say that a pipeline is healthy.
- Model-call timing is not in Elasticsearch. Use the `model-latency` skill.
- Spend and token totals are in the `spend-and-tokens` skill. How interactions ended is in the `interaction-outcomes` skill.

## Step 1 — Use the tool

The tool description lists the fields of each family. The tool sorts hits newest first and
returns at most 50. `since` is at most `30d`.

```python
# Count the errors of the last 24 hours
search_logs(action="count", index="agent-logs", query={"term": {"level": "ERROR"}}, since="24h")

# The most common event types of the last hour
search_logs(action="search", index="agent-logs", size=0, since="1h",
                aggs={"by_event": {"terms": {"field": "event_type", "size": 20}}})

# The last 20 errors, with the fields that name them
search_logs(action="search", index="agent-logs", query={"term": {"level": "ERROR"}}, size=20,
                since="3h", fields=["@timestamp", "event_type", "message", "tool_name", "trace_id", "error"])

# The newest probe of the local model server (a monitor index)
search_logs(action="search", index="agent-monitors-slm-health", size=1, since="1h")

# The events of one trace
search_logs(action="search", index="agent-logs", query={"term": {"trace_id": "<trace_id>"}},
                size=50, since="7d", fields=["@timestamp", "event_type", "role", "tool_name", "input_tokens"])

# Which events carry a field (find the event before you aggregate on the field)
search_logs(action="search", index="agent-logs", size=0, since="24h",
                query={"exists": {"field": "input_tokens"}},
                aggs={"by_event": {"terms": {"field": "event_type", "size": 10}}})
```

What the events carry:

| `event_type` | Fields to read |
|---|---|
| `model_call_completed` | `model`, `provider`, `role`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cost_usd`. LLM-only: it leaves out embedding and rerank cost |
| `api_cost_recorded` | `provider`, `model`, `cost_usd`. Every priced call. This is the total-spend event |
| `tool_call_started` | `tool_name` |
| `tool_call_completed` | `tool_name`, `success` |
| `tool_loop_gate` | `decision`: `warn_consecutive`, `block_consecutive`, `block_identity` |
| `embedding_generated`, `reranker_applied` | `provider`, `cost_usd`. Part of total spend, not of `model_call_completed` |

Other values that had events in the last 7 days: `session_created`, `state_transition`,
`model_call_started`, `history_sanitised`, `budget_counter_snapshot`. Do not guess a value:
run the last call above and read the buckets.

## Step 2 — When the tool cannot do it, use curl

| Need | Why the tool cannot | Use |
|---|---|---|
| A family that is not `agent-logs`, `agent-topology` or `agent-monitors-slm-health` (captures, reflections, insights, other monitors) | `index` is a fixed list | `_search` with curl: see `references/curl-recipes.md` |
| A field that the tool refuses and that exists | The allowlist is closed | Check the field first (below). Fields that hold conversation text are left out on purpose |
| More than 50 hits, oldest first, or a window over 30 days | The caps | `_search` with curl |
| ES\|QL | Not offered | `references/esql.md` |
| The list of indices, or the fields of an index | Not offered | The two commands below |

**Check a field before a curl aggregation.** Elasticsearch gives empty buckets, not an
error, for a field that does not exist. A field missing from `fields` in this reply does not
exist:

```bash
curl -fsS 'http://elasticsearch:9200/agent-logs-*/_field_caps?fields=event_type,level'
curl -fsS 'http://elasticsearch:9200/_cat/indices?format=json&h=index,docs.count'
```

## Index families

Use `<family>-*`. The tool takes the family name without `-*`.

| Pattern | Holds | Date field |
|---|---|---|
| `agent-logs-*` | All structured log events | `@timestamp` |
| `agent-topology-*` | Per-turn routing and planning facts | `@timestamp` |
| `agent-captains-captures-*` | Captain's Log, one document for each request | `timestamp` |
| `agent-captains-reflections-*` | DSPy self-reflection output | `timestamp` |
| `agent-insights-*` | Cross-session insights and anomalies | `timestamp` |
| `agent-monitors-slm-health-*` | Probes of the local model server | `probed_at` |
| `agent-monitors-cache-reset-cadence-*`, `agent-monitors-projector-health-*` | Monitors | `@timestamp` |
| `agent-monitors-cache-erosion-*`, `agent-monitors-delivery-ratio-*` | Monitors | `run_at` |
| `agent-monitors-joinability-*` | Monitor | `started_at` |

A time bound must use the date field of its own family. A range on the wrong field returns
nothing and no error.

A family holds daily indices (`YYYY.MM.DD`), monthly indices (`YYYY-MM`), or both. Never cut a
date off a name: a cut name such as `agent-logs-2026` looks real and is not. To classify a
family, use the tested script:

```bash
curl -fsS 'http://elasticsearch:9200/_cat/indices?h=index' \
  | grep '^agent-monitors-joinability-' \
  | python3 skills/log-search/scripts/es_index_granularity.py agent-monitors-joinability
```

The script prints the daily and monthly counts, flags `MIXED`, and lists every name that it
does not recognise. It exits 1 if its input is empty, so a failed `curl` is never read as
"no indices".

## Field notes

- `level`, `event_type`, `trace_id`, `session_id`, `tool_name`, `model` and `provider` are plain `keyword`: use `term`, with no `.keyword` suffix.
- `message` is `text`. Use `match` on it. Do not aggregate on it.
- `error` is `text` with a `.keyword` subfield. Aggregate on `error.keyword`.
- Today is ambiguous at midnight UTC. Use a rolling window (`since="24h"`).

## Success

Check the result before you answer:

- The answer names the index family and the window that the numbers cover.
- A reply with `shards_failed`, `timed_out` or `truncated` is partial. Say so.
- A `total` of 0 for a window in which the agent ran means the window or the filter is wrong.
- A terms aggregation with no buckets and a `total` above 0 means the field is empty for these events. Find the event that carries it (the last call in Step 1).
- A count that you quote is the count that the reply holds, with its window. Do not round it up.
- A monitor answer gives the time of the newest document. A document older than the monitor's interval is reported as stale.

## Recovery

| Failure | Cause | Next step |
|---|---|---|
| `refused: field 'x' is not available in agent-logs. Allowed fields: ...` | The field is not in the allowlist, or the name is wrong | Pick a field from the list in the message. If none fits, check the field with `_field_caps` and use curl |
| `refused: index must be one of ...` | The family is not one of the three | Use the curl path in Step 2 |
| `refused: since ...` | The window is malformed or over 30 days | Write a number and `m`, `h` or `d` |
| `store_unavailable` or `store_timeout` | Elasticsearch is down or slow | Call once more. If it fails again, stop and tell the user. The `service-health` skill checks the service |
| `store_rejected` (HTTP 400) | The query shape is wrong | Change the query as the message states |
| HTTP 404 `index_not_found_exception` with curl | A guessed index name. The names `agent-events-*`, `agent-traces-*`, `agent-telemetry-*` and `logs-*` are retired or never existed | Use a family from the table. Events of every kind are in `agent-logs-*` |
| A curl reply is `{"hits": {"total": {"value": 0}}}` and the agent ran | Wrong date field, or `since` too short | Check the date field of the family. Widen the window once |
| The same failure twice | — | Stop. Tell the user what failed and what was tried |
