---
name: query-tempo
description: Reads Tempo traces - how long model calls took by role or model over a time window, and the
  spans and durations of one trace id. Starts with the query_telemetry tool, action latency, and uses curl
  for a trace by id, a span search and the tag list. Use when the task is how long model calls took,
  latency over a window by role or model, or the spans and durations of a given trace id. Not for
  explaining a whole turn, which the turn-forensics skill does.
metadata:
  requires-tools: bash
---

# query-tempo — Model-call latency and the spans of one trace

## Rules

- Answer only from a live Tempo query. Never answer from what a trace probably holds.
- For latency over a window, start with `query_telemetry`, action `latency`. Elasticsearch holds no model-call timing (retired, ADR-0129).
- For the spans of one trace, use curl on `http://tempo:3200`. The tool does not read one trace.
- Write a trace id, a clock value and a number as a literal in the command. A shell variable or `$(...)` makes the command ask for approval.

## Step 1 — Latency over a window: use the tool

```python
# p50, p90 and max of model calls, by role, over the last day
query_telemetry(action="latency", since="24h", group_by=["role"])

# By role and model
query_telemetry(action="latency", since="24h", group_by=["role", "model"])

# One role only
query_telemetry(action="latency", since="24h", role="primary", group_by=["model"])

# The local server's side: prefill and decode time, by model, over the last week
query_telemetry(action="latency", since="7d", span="slm_chat", group_by=["model"])
```

`since` is from 1 minute to 7 days (Tempo refuses a longer window). `limit` is the number of
traces to read, 200 by default and 500 at most. The reply holds, for each group: `count`,
`errors`, `p50_ms`, `p90_ms`, `max_ms` and token sums.

**Read `complete` before you report a number.** `complete: false` means a cap was hit and the
numbers cover a sample. Name `incomplete_reasons` in the answer. An empty `groups` list means that
no model-call span matched in the window. It does not mean zero latency.

## Step 2 — One trace: use curl

Tempo answers on the compose network as `http://tempo:3200`. It is not reachable from the host
shell. Trace ids are written as literals.

```bash
# The spans of one trace (the id below is an example)
curl -fsS 'http://tempo:3200/api/v2/traces/1bc849bf94ebf80162af9c97b9ee91c3' | jq '.trace.resourceSpans[0].scopeSpans[0].spans'
```

```bash
# Each span with its duration in milliseconds, in time order
curl -fsS 'http://tempo:3200/api/v2/traces/1bc849bf94ebf80162af9c97b9ee91c3' | jq '[.trace.resourceSpans[].scopeSpans[].spans[] | {name, spanId, parentSpanId, start: (.startTimeUnixNano | tonumber), durationMs: (((.endTimeUnixNano | tonumber) - (.startTimeUnixNano | tonumber)) / 1000000)}] | sort_by(.start)'
```

Fields of a span: `name`, `spanId`, `parentSpanId` (absent for the root), `startTimeUnixNano`,
`endTimeUnixNano`, `kind`, `attributes` (a list of key and value pairs), `status`. Times are in
nanoseconds: divide by 1,000,000 for milliseconds. Span and trace ids are base64 in the reply and are
used as they are in a later query.

## Step 3 — Search and status: use curl

```bash
# Recent traces. limit is required: a search without it fails silently
curl -fsS 'http://tempo:3200/api/search?limit=10' | jq '.traces'

# Traces of a service in a window. start and end are Unix seconds, written as literals
curl -fsS 'http://tempo:3200/api/search?start=1788026891&end=1788030491&q=service.name%3Dseshat-vps&limit=20' | jq '.traces'

# The attribute names that a search can use
curl -fsS 'http://tempo:3200/api/search/tags' | jq '.tagNames'

# Status and version
curl -fsS 'http://tempo:3200/status'
```

To get the clock for `start` and `end`, run two plain commands and write the two numbers into
the search:

```bash
date +%s
date -d '1 hour ago' +%s
```

`q` takes TraceQL. A search with a pipeline returns only the attributes that `select(...)` names.
`spss` caps the spans returned for each trace and `limit` caps the traces. A reply that reaches
either cap is a sample. For a latency summary that the tool cannot give, read
`references/latency-recipe.md`.

## Success

Check the result before you answer:

- A latency answer states the window, the group keys and the value of `complete`.
- A percentile comes from `spans_used`. A role or model with few spans has a coarse p90: say how many spans.
- A trace answer lists the span names with durations and says which span is the root.
- A number that is in nanoseconds is converted: the answer states the unit.

## Recovery

| Failure | Cause | Next step |
|---|---|---|
| `refused: ...` from the tool | An argument is not allowed (a span kind, a group key, a window over 7 days) | Change the argument as the message states |
| `store_unavailable`, `store_timeout`, `store_blocked` | Tempo is down, slow or blocked | Call once more. If it fails again, stop and tell the user |
| `complete: false` | A trace or span cap was hit | Narrow `since`, or add `role` or `model`. Report the figure as a sample |
| `could not resolve host: tempo` | The command ran on the host shell | Run it through the `bash` tool, which runs in the gateway container |
| An empty `[]` from `/api/search` | `limit` is missing or the window is empty | Add `limit=10`. Try the search with no `start` and `end` |
| HTTP 404 on `/api/v1/...` | Tempo v1 paths do not exist in this deployment (v2.10.7) | Use `/api/v2/traces/<id>`, `/api/traces/<id>` or `/api/search` |
| HTTP 404 on `/api/status` | Wrong status path | Use `/status` or `/api/echo` |
| A very large trace is slow | More than 1000 spans | Select the span fields with `jq`. Do not print the whole trace |
| The same failure twice | — | Stop. Tell the user what failed and what was tried |

See also: ADR-0129 (`docs/architecture_decisions/ADR-0129-opentelemetry-instrumentation-and-trace-visibility.md`)
for the Tempo deployment, and the `query-elasticsearch` skill for log queries.
