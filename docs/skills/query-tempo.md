---
name: query-tempo
description: Query Tempo traces by ID, search for spans, and inspect trace-level span hierarchies. Use when investigating latency (model-call p50/p90 by role and model, see Pattern 5), span relationships, or turn-by-turn execution traces.
when_to_use: When you need model-call latency by role or model over a window, to read a trace end-to-end with span timing and nesting, investigate latency patterns by span type, or examine root-span to leaf-span causality chains.
tools: [bash]
nudge: "These results must come from a live Tempo query — never answer from training-data priors about what the trace might contain."
keywords:
  # Natural user phrasing
  - trace
  - latency
  - latencies
  - p50
  - p90
  - p95
  - percentile
  - response time
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
known_bad_patterns:
  - pattern: "api/v1/"
    applies_to:
      tool: bash
      fields: [command]
    reason: "Tempo v1 API endpoints do not exist in our deployment. Documented Tempo API (upstream) uses v1, but our deployment (Tempo v2.10.7) has only the unprefixed and v2 APIs."
    suggestion: "Use '/api/traces/{traceID}' or '/api/v2/traces/{traceID}' for trace retrieval, '/api/search' for search. Never '/api/v1/*'."
  - pattern: "api/status"
    applies_to:
      tool: bash
      fields: [command]
    reason: "/api/status returns 404. The correct endpoint for Tempo status is /status (not /api/status)."
    suggestion: "Use '/status' or '/api/echo' for status checks."
---

# query-tempo — Query Tempo traces, read span hierarchies, and investigate latency

**Status:** Primary path (FRE-1321, 2026-08-29). Tempo v2.10.7 deployed with OTLP ingress (ADR-0129).

**Category:** `system_read` · **Risk:** low · **Approval:** `bash curl` auto-approved (NORMAL); reachability limited to compose network

## Reachability

- **Tempo answers on the compose network as `http://tempo:3200`**
- Query API base: `http://tempo:3200/api/`
- Available only from inside containers (compose network) or SSH tunnels with port forwarding
- Test connectivity: `curl -s 'http://tempo:3200/status'` returns 200

## Endpoints (verified live 2026-08-29)

### Trace retrieval by ID

**Preferred: `/api/v2/traces/{traceID}` (standard OpenTelemetry proto format)**

```bash
curl -s 'http://tempo:3200/api/v2/traces/{traceID}' | jq '.trace.resourceSpans[0].scopeSpans[0].spans'
```

Returns standard OTel resourceSpans format:
```json
{
  "trace": {
    "resourceSpans": [
      {
        "resource": {
          "attributes": [
            {
              "key": "service.name",
              "value": {"stringValue": "seshat-vps"}
            }
          ]
        },
        "scopeSpans": [
          {
            "scope": {"name": "personal_agent.telemetry.otel_middleware"},
            "spans": [
              {
                "traceId": "G8hJv5Tr+AFir5yXue6Rww==",
                "spanId": "KPV6tY/gbzU=",
                "name": "GET /health",
                "kind": "SPAN_KIND_INTERNAL",
                "startTimeUnixNano": "1788030491849226974",
                "endTimeUnixNano": "1788030491854866238",
                "attributes": [
                  {
                    "key": "http.method",
                    "value": {"stringValue": "GET"}
                  }
                ],
                "status": {}
              }
            ]
          }
        ]
      }
    ]
  },
  "metrics": {
    "inspectedBytes": "72504"
  }
}
```

**Alternative: `/api/traces/{traceID}` (Tempo-native batches format)**

```bash
curl -s 'http://tempo:3200/api/traces/{traceID}' | jq '.batches[0].scopeSpans[0].spans'
```

Returns Tempo's batches format (similar structure, different root key). Both endpoints work; v2 is the standard.

**Key fields per span:**
- `traceId`, `spanId` — base64-encoded trace and span identifiers
- `parentSpanId` — parent span ID (if set); empty/absent for root spans
- `name` — semantic span name (e.g., `GET /health`, `model_call`)
- `startTimeUnixNano`, `endTimeUnixNano` — Unix nanosecond timestamps
- `kind` — `SPAN_KIND_INTERNAL`, `SPAN_KIND_SERVER`, `SPAN_KIND_CLIENT`, etc.
- `attributes` — key-value metadata (HTTP method, status, service name, etc.)
- `status` — `{}` for success; `{"code": "STATUS_CODE_ERROR"}` for errors

### Search for traces (by service or attributes)

```bash
# Search with limit (required parameter)
curl -s 'http://tempo:3200/api/search?limit=10' | jq '.traces'

# Search for a specific service
curl -s 'http://tempo:3200/api/search?q=service.name%3Dseshat-vps&limit=5' | jq '.traces'

# Search for recent traces with time window
curl -s 'http://tempo:3200/api/search?start=<unix_seconds>&end=<unix_seconds>&q=<query>&limit=10' | jq '.traces'
```

Returns:
```json
{
  "traces": [
    {
      "traceID": "1bc849bf94ebf80162af9c97b9ee91c3",
      "rootServiceName": "seshat-vps",
      "rootTraceName": "GET /health",
      "startTime": "1788030491849226974",
      "duration": "5639264"
    }
  ],
  "metrics": {
    "inspectedBytes": "...",
    "inspectedTraces": "..."
  }
}
```

**Parameters:**
- `limit` — required, max traces to return
- `start`, `end` — Unix seconds (optional; if omitted, searches recent traces only)
- `q` — query string with attribute filters (optional)

### Tag enumeration (search attributes)

```bash
curl -s 'http://tempo:3200/api/search/tags' | jq '.tagNames'
```

Returns available tag/attribute names that can be searched:
```json
{
  "tagNames": ["service.name", "http.method", "http.status_code", ...]
}
```

### Status / version

```bash
curl -s 'http://tempo:3200/status' | jq .
```

Returns:
```json
{
  "version": "2.10.7",
  "buildDate": "...",
  "gitRevision": "..."
}
```

### Echo endpoint (simple connectivity check)

```bash
curl -s 'http://tempo:3200/api/echo' | jq .
```

Returns:
```json
{}
```

---

## Common patterns

### Pattern 1: Read a complete trace end-to-end (with spans and timing)

Replace the trace id in the URL with the one you need. Write it as a literal: a shell variable or `$(…)` makes the command ask for approval.

```bash
curl -s "http://tempo:3200/api/v2/traces/1bc849bf94ebf80162af9c97b9ee91c3" | jq '.trace.resourceSpans[0].scopeSpans[0].spans | 
  map({
    name,
    spanId,
    startTimeUnixNano: (.startTimeUnixNano | tonumber),
    endTimeUnixNano: (.endTimeUnixNano | tonumber),
    durationNano: ((.endTimeUnixNano | tonumber) - (.startTimeUnixNano | tonumber)),
    attributes: ((.attributes // []) | map({(.key): (.value.stringValue // .value.intValue)}) | add)
  }) | 
  sort_by(.startTimeUnixNano)'
```

Output from real trace:
```json
[
  {
    "name": "GET /health",
    "spanId": "KPV6tY/gbzU=",
    "startTimeUnixNano": 1788030491849226974,
    "endTimeUnixNano": 1788030491854866238,
    "durationNano": 5639264,
    "attributes": {
      "http.method": "GET",
      "http.target": "/health",
      "http.status_code": 200
    }
  }
]
```

**Interpretation:** Single span representing an HTTP GET to /health that took ~5.6ms and returned 200.

### Pattern 2: Find recently active services

```bash
curl -s 'http://tempo:3200/api/search?limit=100' | jq '.traces | unique_by(.rootServiceName) | .[] | {service: .rootServiceName, recentTrace: .traceID}'
```

### Pattern 3: Calculate span duration in milliseconds

```bash
curl -s "http://tempo:3200/api/v2/traces/1bc849bf94ebf80162af9c97b9ee91c3" | jq '.trace.resourceSpans[0].scopeSpans[0].spans[] | 
  {
    name,
    durationMs: (((.endTimeUnixNano | tonumber) - (.startTimeUnixNano | tonumber)) / 1000000)
  }'
```

### Pattern 4: Find traces by service name and time window

Read the clock with two plain commands, then write the two numbers into the search. The numbers below are example values.

```bash
date -d '1 hour ago' +%s
date +%s
curl -s 'http://tempo:3200/api/search?start=1788026891&end=1788030491&q=service.name%3Dseshat-vps&limit=20' | jq '.traces[]'
```

### Pattern 5: Model-call latency by role and model (p50, p90, max)

Use this for "how slow are model calls", "p50 and p90 by role" and any latency over a window.
Elasticsearch holds no model-call timing (ADR-0129). Tempo does: one `model_call <model>` span
per model call, with the role in `gen_ai.operation.name` and the model in
`gen_ai.request.model`. The `query_telemetry` tool, action `latency`, returns the same kind of summary
when that tool is available. This is the bash path.

1. Read the clock: `date +%s` is the end, `date -d '24 hours ago' +%s` is the start. Tempo
   refuses a window over 168 hours.
2. Write both numbers as literals in `start` and `end`. A `$(…)` makes the command ask for approval.
3. Keep `limit` (traces) and `--argjson limit` equal. `spss` is the spans returned per trace.

```bash
date +%s
date -d '24 hours ago' +%s
curl -s -G 'http://tempo:3200/api/search' \
  --data-urlencode 'q={ span:name =~ "model_call .*" } | select(span.gen_ai.operation.name, span.gen_ai.request.model)' \
  --data-urlencode 'start=1788000000' \
  --data-urlencode 'end=1788086400' \
  --data-urlencode 'limit=500' \
  --data-urlencode 'spss=100' \
| jq --argjson limit 500 'def pct($p): sort | .[((($p * length) | ceil) - 1)];
def num: if type == "number" then . elif type == "string" then (tonumber? // null) else null end;
def attr($k): ([.attributes[]? | select(.key == $k) | .value.stringValue] | first) // "unknown";
[.traces[]? | (.spanSets // [.spanSet])[]? | select(. != null)] as $sets
| (.metrics.totalJobs | num) as $total
| (if $total != null then ((.metrics.completedJobs | num) // 0) else null end) as $done
| [$sets[] | .spans[]? | select((.durationNanos | num) != null)] as $spans
| (.traces | length) as $returned
| ([.traces[]? | select((.spanSets // .spanSet) == null)] | length) as $noset
| ([$sets[] | select((.matched | num) != null) | ((.matched | num) - ((.spans // []) | length)) | select(. > 0)] | add // 0) as $cut
| ([$sets[] | select((.matched | num) == null)] | length) as $nomatch
| ([$sets[] | .spans[]? | select((.durationNanos | num) == null)] | length) as $nodur
| (if $done != null and $total != null and $done < $total then $total - $done else 0 end) as $unread
| {
    traces_returned: $returned,
    trace_limit: $limit,
    spans_used: ($spans | length),
    spans_cut: $cut,
    blocks_unread: $unread,
    sets_without_match: $nomatch,
    spans_without_duration: $nodur,
    traces_without_span_set: $noset,
    complete: ($returned < $limit and $cut == 0 and $unread == 0 and $nomatch == 0 and $nodur == 0 and $noset == 0),
    groups: ($spans | group_by([attr("gen_ai.operation.name"), attr("gen_ai.request.model")])
      | map({
          role: (.[0] | attr("gen_ai.operation.name")),
          model: (.[0] | attr("gen_ai.request.model")),
          spans: length,
          p50_ms: (map((.durationNanos | num) / 1000000) | pct(0.5)),
          p90_ms: (map((.durationNanos | num) / 1000000) | pct(0.9)),
          max_ms: (map((.durationNanos | num) / 1000000) | max)
        })
      | sort_by(-.spans))
  }'
```

**Read `complete` before you report a number.** It is `false` when any of these holds:

| Field | Meaning when not zero (or `traces_returned` equals `trace_limit`) |
|-------|------------------------------------------------------------------|
| `traces_returned` = `trace_limit` | The `limit` cut the search. More traces can match. Narrow the window or add a filter to the TraceQL. |
| `spans_cut` | Tempo matched more spans in a trace than `spss` returned. |
| `blocks_unread` | Tempo stopped before it read every block. |
| `sets_without_match` | A result had no match count, so its completeness is unknown. |
| `spans_without_duration` | A span had no duration. It is left out of the percentiles. |
| `traces_without_span_set` | A trace came back with no span set. Its spans are not counted. |

When `complete` is `false`, say the percentiles come from a sample and name the field. An empty
`groups` means no model-call span matched in the window. It does not mean zero latency.
Percentiles are nearest-rank over `spans_used`. A role or model with few spans gives a
coarse p90.

---

## Known limitations

**Search requires a limit parameter.** Unlike Elasticsearch, Tempo search always requires `?limit=N`. Without it, requests fail silently or return empty results.

**Time format is Unix seconds only.** Unlike Elasticsearch's date-math (`now-1h`), Tempo requires explicit Unix-second timestamps for `start` and `end` parameters.

**`q` takes TraceQL.** `{ span:name =~ "model_call .*" && span.gen_ai.operation.name = "primary" } | select(span.gen_ai.request.model)` works (verified live, FRE-1567). A search with a pipeline returns only the attributes named in `select(...)`. `spss` caps the spans returned per trace, and `limit` caps the traces. A reply that hits either cap is a sample, not the whole window.

**Base64 encoding on IDs.** Trace and span IDs are base64-encoded in the response. To use them in subsequent queries, use them as-is (no decoding needed for another query).

---

## Data discipline

- **Run `curl` from `bash` as shown.** The bash tool runs inside the gateway container, where `http://tempo:3200` resolves. `docker exec` is not an allowlisted command and asks for approval
- **Search requires a limit.** Always include `?limit=N` in search queries; omitting it causes silent failures
- **Timestamps are nanoseconds.** Divide by 1e9 for seconds; divide by 1e6 for milliseconds
- **v1 API does not exist here.** Documentation says v1, but our Tempo has only the unprefixed and v2 paths
- **Traces are immutable.** Once written to Tempo, a trace cannot be updated

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `could not resolve host: tempo` | Running from the host instead of inside the gateway container | Run the command through the `bash` tool, which runs in the gateway container |
| Empty `[]` on `/api/search` | Missing or empty `limit` parameter | Add `?limit=10` (or any positive integer) to the query |
| `null` results on search | Trace time window is outside recent history | Use `/api/search?limit=10` with no time filter to get recent traces |
| HTTP 404 on `/api/v1/traces` | Attempting to use the documented Tempo v1 API | Use `/api/v2/traces/{traceID}` or `/api/traces/{traceID}` instead |
| HTTP 404 on `/api/status` | Using the wrong status endpoint | Use `/status` (not `/api/status`) |
| Large trace (>10 MB) takes time to retrieve | Traces with many spans (1000+) are slower to deserialize | Use selective `jq` filtering to extract only needed span fields |

---

## References

- [Tempo upstream API docs](https://grafana.com/docs/tempo/latest/api_docs/) — official reference (our deployment differs; use this skill for our specifics)
- [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/) — standard span attribute names
- [ADR-0129](../architecture_decisions/ADR-0129-opentelemetry-instrumentation-and-trace-visibility.md) — Tempo deployment and design rationale
- `docs/skills/query-elasticsearch.md` — analogue for log queries (Elasticsearch backs logs, Tempo backs traces)
