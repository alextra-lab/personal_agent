---
name: log-search
description: Counts, breaks down or finds agent log events over a time window, lists indices and fields, and runs ES|QL. Starts with the search_logs tool and uses curl past its limits - more than 50 hits, over 30 days, an index family outside its three. Use when the task is a count, an error search, an index listing, an ES|QL query or a field lookup over agent-logs, agent-topology or the monitor indices. Not for model-call timing (`model-latency`), spend and token totals (`spend-and-tokens`) or the monitor of the local model server (`service-health`).
metadata:
  provides-tools: search_logs
---

# log-search — Count, break down and find agent log events

## Rules

- Answer only from a live query. Never answer from what the logs probably hold.
- Start with the `search_logs` tool. It knows the fields, a refusal lists the allowed fields, and the result is bounded.
- Use curl only for what the tool cannot do.
- Use only the index families in this file. Never write a guessed index name.
- A scheduled monitor index records what a monitor saw at its last run. Read its newest document before you say that a pipeline is healthy.
- The monitor of the local model server (`agent-monitors-slm-health`) is in the `service-health` skill.
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

The tool takes only the index families `agent-logs`, `agent-topology` and
`agent-monitors-slm-health`. It reads only the fields in its allowlist, and it leaves out the
fields that hold conversation text on purpose. It returns at most 50 hits, newest first, and
reads at most 30 days. It does not run ES|QL, and it does not list indices or fields.

Past the tool's limits, and only with the `bash` tool: read `references/curl-steps.md`.

## Index families

Use `<family>-*`. The tool takes the family name without `-*`.

| Pattern | Holds | Date field |
|---|---|---|
| `agent-logs-*` | All structured log events | `@timestamp` |
| `agent-topology-*` | Per-turn routing and planning facts | `@timestamp` |
| `agent-captains-captures-*` | Captain's Log, one document for each request | `timestamp` |
| `agent-captains-reflections-*` | DSPy self-reflection output | `timestamp` |
| `agent-insights-*` | Cross-session insights and anomalies | `timestamp` |
| `agent-monitors-slm-health-*` | Probes of the local model server (the `service-health` skill reads them) | `probed_at` |
| `agent-monitors-cache-reset-cadence-*`, `agent-monitors-projector-health-*` | Monitors | `@timestamp` |
| `agent-monitors-cache-erosion-*`, `agent-monitors-delivery-ratio-*` | Monitors | `run_at` |
| `agent-monitors-joinability-*` | Monitor | `started_at` |

A time bound must use the date field of its own family. A range on the wrong field returns
nothing and no error.

A family holds daily indices (`YYYY.MM.DD`), monthly indices (`YYYY-MM`), or both. Never cut a
date off a name: a cut name such as `agent-logs-2026` looks real and is not.

Past the tool's limits, and only with the `bash` tool: read `references/index-granularity.md`.

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
- A monitor answer gives the time of the newest document.
- Three monitors run on a schedule. At the default interval they write every hour (`agent-monitors-cache-erosion`, `agent-monitors-joinability`) or every day (`agent-monitors-delivery-ratio`). A newest document older than its interval is reported as stale.
- `agent-monitors-cache-reset-cadence` and `agent-monitors-projector-health` write on events, so they have no interval. A quiet period is not stale.

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
