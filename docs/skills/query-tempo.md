---
name: query-tempo
description: Query Tempo traces by ID, search for spans, and inspect trace-level span hierarchies. Use when investigating latency, span relationships, or turn-by-turn execution traces.
when_to_use: When you need to read a trace end-to-end with span timing and nesting, investigate latency patterns by span type, or examine root-span to leaf-span causality chains.
tools: [bash]
nudge: "These results must come from a live Tempo query — never answer from training-data priors about what the trace might contain."
keywords:
  # Natural user phrasing
  - trace
  - latency
  - span
  - timing
  - what took so long
  - root span
  - latency breakdown
  # Technical / operator phrasing
  - trace_id
  - tempo
  - trace retrieval
  - span breakdown
  - execution trace
  - query tempo
  - search traces
  - trace query
  - span hierarchy
  - trace analysis
  - duration by span
  - critical path
canonical_patterns:
  - "trace_id"
  - "span"
known_bad_patterns:
  - pattern: "api/v2/traces"
    applies_to:
      tool: bash
      fields: [command]
    reason: "Tempo API v2 traces endpoint does not exist. Our deployment runs Tempo v2.10.7, which only has a v1 API."
    suggestion: "Use 'api/v1/traces/{traceID}' for trace retrieval."
  - pattern: "api/v2/search"
    applies_to:
      tool: bash
      fields: [command]
    reason: "Tempo API v2 search endpoint does not exist."
    suggestion: "Use 'api/v1/search' with time-windowed queries."
  - pattern: "tempo-localhost"
    applies_to:
      tool: bash
      fields: [command]
    reason: "Tempo on the compose network answers as 'tempo', not 'tempo-localhost'. The latter times out (000 connection error)."
    suggestion: "Use 'http://tempo:3200' as the endpoint. Tempo is reachable ONLY from inside the compose network."
---

# query-tempo — Query Tempo traces, read span hierarchies, and investigate latency

**Status:** Primary path (FRE-1321, 2026-08-29). Tempo v2.10.7 deployed with OTLP ingress (ADR-0129).

**Category:** `system_read` · **Risk:** low · **Approval:** `bash curl` auto-approved (NORMAL); reachability limited to compose network

## Reachability

- **Tempo answers on the compose network as `http://tempo:3200`**
- Query API base: `http://tempo:3200/api/v1/`
- **NOT reachable as `tempo-localhost`** — this hostname times out (connection error 000)
- Available only from inside containers (compose network) or SSH tunnels with port forwarding

## Endpoints (verified live 2026-08-29)

### Trace retrieval by ID

```bash
curl -s 'http://tempo:3200/api/v1/traces/{traceID}' \
  | jq '.'
```

Returns:
```json
{
  "traceID": "f5d25f8b...",
  "batches": [
    {
      "instrumentationLibrarySpans": [
        {
          "spans": [
            {
              "traceID": "f5d25f8b...",
              "spanID": "abc123def456",
              "parentSpanID": "",
              "name": "root_span",
              "startTimeUnixNano": 1725014400000000000,
              "endTimeUnixNano": 1725014402500000000,
              "durationMs": 2500,
              "status": { "code": "STATUS_CODE_OK" },
              "attributes": {
                "span.kind": "INTERNAL",
                "service.name": "personal-agent"
              }
            },
            {
              "parentSpanID": "abc123def456",
              "name": "model_call",
              "startTimeUnixNano": 1725014400500000000,
              "endTimeUnixNano": 1725014401500000000,
              "durationMs": 1000
            }
          ]
        }
      ]
    }
  ]
}
```

**Key fields per span:**
- `traceID` — fully qualified trace identifier
- `spanID` — this span's unique identifier
- `parentSpanID` — empty string if this is a root; references parent's `spanID` otherwise
- `name` — semantic span name (e.g., `root_span`, `model_call`, `tool_execution`)
- `startTimeUnixNano`, `endTimeUnixNano` — Unix nanosecond timestamps
- `durationMs` — **computed duration** (endTime - startTime in milliseconds)
- `attributes` — key-value metadata (span type, service, model, tool name, etc.)
- `status.code` — `STATUS_CODE_OK`, `STATUS_CODE_ERROR`, or `STATUS_CODE_UNSET`

**Span hierarchy:** Parent-child relationships via `parentSpanID`. Root span has empty `parentSpanID`. All siblings share the same `parentSpanID`.

### Search for traces (by tags)

```bash
# Search with time window (required)
curl -s -X POST 'http://tempo:3200/api/v1/search?start=1725000000&end=1725100000' \
  -H 'Content-Type: application/json' \
  -d '{
    "tags": "service.name=personal-agent"
  }' | jq '.traces'
```

Returns:
```json
[
  {
    "traceID": "f5d25f8b...",
    "rootServiceName": "personal-agent",
    "rootTraceName": "root_span",
    "startTimeUnixNano": 1725014400000000000,
    "durationMs": 2500
  }
]
```

**Parameters:**
- `start`, `end` — Unix **seconds** (not nanos). Required.
- `tags` — filter by span attribute key=value pairs. Space-separated multiple filters.
  - Common: `service.name=personal-agent`, `span.kind=INTERNAL`, `trace_id=<value>` (if available as a span attribute)

**Time math:** Use explicit Unix seconds. `jq` can compute:
```bash
# Now minus 1 hour
end=$(date +%s); start=$((end - 3600))
curl -s -X POST "http://tempo:3200/api/v1/search?start=${start}&end=${end}" ...
```

### Service enumeration (tag keys)

```bash
curl -s 'http://tempo:3200/api/v1/services' | jq '.'
```

Returns:
```json
{
  "services": ["personal-agent", "opentelemetry-collector"]
}
```

Use this to discover what services have traces, then refine search with `service.name=<service>`.

### Status / version

```bash
curl -s 'http://tempo:3200/api/v1/status' | jq '.'
```

Returns:
```json
{
  "commit": "...",
  "version": "2.10.7"
}
```

---

## Common patterns

### Pattern 1: Read a complete trace end-to-end (with spans and timing)

```bash
trace_id="f5d25f8b..."
curl -s "http://tempo:3200/api/v1/traces/${trace_id}" | jq '.batches[0].instrumentationLibrarySpans[0].spans | sort_by(.startTimeUnixNano) | .[] | {name, spanID, parentSpanID, durationMs, status}'
```

Output:
```
{
  "name": "root_span",
  "spanID": "abc123...",
  "parentSpanID": "",
  "durationMs": 2500,
  "status": {
    "code": "STATUS_CODE_OK"
  }
}
{
  "name": "model_call",
  "spanID": "def456...",
  "parentSpanID": "abc123...",
  "durationMs": 1000,
  "status": {
    "code": "STATUS_CODE_OK"
  }
}
```

**Interpretation:** Root span took 2500ms total; model_call (child of root) took 1000ms of it.

### Pattern 2: Find longest-running trace in the last hour

```bash
end=$(date +%s)
start=$((end - 3600))

curl -s -X POST "http://tempo:3200/api/v1/search?start=${start}&end=${end}" \
  -H 'Content-Type: application/json' \
  -d '{"tags": "service.name=personal-agent"}' | \
  jq '.traces | sort_by(.durationMs | -.) | .[0:3] | .[] | {traceID, durationMs, startTimeUnixNano}'
```

### Pattern 3: Find traces with errors (HTTP 500, span status ERROR)

```bash
end=$(date +%s)
start=$((end - 3600))

curl -s -X POST "http://tempo:3200/api/v1/search?start=${start}&end=${end}" \
  -H 'Content-Type: application/json' \
  -d '{"tags": "span.status.code=ERROR"}' | \
  jq '.traces | .[] | {traceID, rootTraceName, durationMs}'
```

### Pattern 4: Breakdown latency by span type (how much time in model vs tool calls)

```bash
trace_id="f5d25f8b..."
curl -s "http://tempo:3200/api/v1/traces/${trace_id}" | jq '
  .batches[0].instrumentationLibrarySpans[0].spans 
  | group_by(.name) 
  | map({
      span_type: .[0].name,
      count: length,
      total_ms: (map(.durationMs) | add),
      avg_ms: (map(.durationMs) | add / length)
    })
  | sort_by(.total_ms | -.)
'
```

Output:
```json
[
  { "span_type": "root_span", "count": 1, "total_ms": 2500, "avg_ms": 2500 },
  { "span_type": "model_call", "count": 1, "total_ms": 1000, "avg_ms": 1000 },
  { "span_type": "tool_execution", "count": 3, "total_ms": 1200, "avg_ms": 400 }
]
```

---

## Known limitations

**Metrics endpoint not exposed.** TraceQL (the metrics query language, `/api/v1/query_range`) requires the metrics aggregator (`metrics_generator`), which is enabled in config but exposes no instrumented endpoint. The search and trace-retrieval APIs are the primary path.

**Query Frontend max_duration.** Tempo's `query_frontend.metrics.max_duration` is configured to 360 hours (15 days) to permit fortnight-long range queries. Queries exceeding that ceiling are rejected — this is a hard limit per AC-1 of ADR-0129, not advisory.

**Span attribute schema is dynamic.** There is no schema registry — attributes are whatever the instrumentation library emits. Common ones:
- `service.name` — originating service
- `span.kind` — `INTERNAL`, `SERVER`, `CLIENT`
- `span.status.code` — `OK`, `ERROR`, `UNSET`
- `gen_ai.operation.name` — semantic operation (per OTel spec)
- Any custom attributes the producer chose to include

---

## Data discipline

- **Never rely on attribute names** from training data — run `curl 'http://tempo:3200/api/v1/services'` first and enumerate what's actually indexed
- **Timestamps are nanoseconds.** Divide by 1e9 for seconds; use `jq '... / 1000000000'` for millisecond conversion
- **Search always requires time bounds.** A missing `start`/`end` returns an empty result silently, not an error
- **Traces are immutable.** Once written to Tempo, a trace cannot be updated; re-opening a span is a new span with a new ID

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `connection refused` or HTTP 000 | Using `tempo-localhost` instead of `tempo` | Change hostname to `tempo`. Compose network routing is one-way: `tempo-localhost` is an alias for localhost inside the Tempo container itself, not reachable from other containers. |
| Empty result `[]` on search | Query time bounds are wrong (past the retention window or swapped) | Check `start < end`, use `date +%s` to get current Unix time, verify Tempo is receiving spans (check `/api/v1/services`) |
| HTTP 404 on `/api/v2/...` endpoint | Attempting to use Tempo v2 API | Tempo v2.10.7 has only v1 API. Use `/api/v1/` paths. |
| Large trace (>10 MB) takes time to retrieve | Traces with many spans (1000+) are slower to deserialize | Use selective `jq` filtering to extract only needed span fields rather than rendering full docs |

---

## References

- [Tempo API docs](https://grafana.com/docs/tempo/latest/api_docs/) — official reference (this skill contains our deployment specifics)
- [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/) — standard span attribute names
- [ADR-0129](../architecture_decisions/ADR-0129-opentelemetry-instrumentation-and-trace-visibility.md) — Tempo deployment and design rationale
- `docs/skills/query-elasticsearch.md` — analogue for log queries (Elasticsearch backs logs, Tempo backs traces)
