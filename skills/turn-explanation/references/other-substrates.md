# turn-explanation — Steps 1b to 4: the other substrates, with bash

### Step 1b — The same timeline from the gateway log, with the time gaps

Use this when the tool reply holds fewer events than expected, or when you need the gap in
seconds between events. The script strips colour codes, keeps the trace, and prints one line for
each model call, tool call and sub-agent end:

```bash
docker logs cloud-sim-seshat-gateway --since=40m 2>&1 \
  | python3 skills/turn-explanation/scripts/call_timeline.py <trace_id>
```

Each line reads `HH:MM:SS  +Ns  CALL in=<tokens> role=<role>`, or `TOOL <name> <path>`. The
`+Ns` is the gap since the previous line. The script exits 1 with one `error:` line when no event
of the trace is in the window: widen `--since`, or use Step 1.

### Step 1c — The spans of one trace (Tempo)

Tempo answers on the compose network as `tempo:3200`. It is not reachable from the host shell.
Write a trace id, a clock value and a number as a literal in the command. A shell variable or
`$(...)` makes the command ask for approval.

```bash
# Each span with its duration in milliseconds, in time order (the id below is an example)
curl -fsS 'http://tempo:3200/api/v2/traces/1bc849bf94ebf80162af9c97b9ee91c3' | jq '[.trace.resourceSpans[].scopeSpans[].spans[] | {name, spanId, parentSpanId, start: (.startTimeUnixNano | tonumber), durationMs: (((.endTimeUnixNano | tonumber) - (.startTimeUnixNano | tonumber)) / 1000000)}] | sort_by(.start)'
```

A span holds `name`, `spanId`, `parentSpanId` (absent for the root), `startTimeUnixNano`,
`endTimeUnixNano`, `kind`, `attributes` and `status`. Times are in nanoseconds: divide by
1,000,000 for milliseconds. A very large trace (more than 1000 spans) is slow: select the span
fields with `jq` and do not print the whole trace.

Recent traces. `limit` is required: a search without it fails silently.

```bash
curl -fsS 'http://tempo:3200/api/search?limit=10' | jq '.traces'
curl -fsS 'http://tempo:3200/api/search/tags' | jq '.tagNames'
```

`q` takes TraceQL, with braces and quotes: `{ resource.service.name = "seshat-vps" }`. A bare
`key=value` in `q` is refused with HTTP 400. `start` and `end` are Unix seconds, written as
literals (`date +%s` gives the clock). Tempo status and version belong to the `service-health` skill.

## Step 2 — What the browser received (Postgres `session_events`)

```bash
# ask: no allow rule covers docker exec, so this asks for approval.
docker exec cloud-sim-postgres psql -U agent -d personal_agent -c \
 "select seq, event_type, payload->'data'->>'category' as cat, created_at \
  from session_events where session_id='<session_id>' order by seq desc limit 20;"
```

## Step 3 — The bytes of an artifact, if the turn wrote one

```bash
# ask: no allow rule covers docker exec, so this asks for approval.
docker exec cloud-sim-postgres psql -U agent -d personal_agent -t -c \
 "select r2_key from artifacts where id='<artifact_id>';"
docker exec cloud-sim-seshat-gateway sh -c 'cd /app && /app/.venv/bin/python -c "
import asyncio; from personal_agent.storage.artifact_store import get_artifact_store
print(len(asyncio.run(get_artifact_store().get(\"<r2_key>\"))))"'
```

The first command gives the storage key. The second prints the size of the stored bytes.

## Step 4 — Join the inference-server request log

The model server writes a `request_complete` event for each request to `slm-requests-*`. It is
tagged with the `X-Span-Id` and `X-Trace-Id` headers of the harness. Join on `span_id`
for one call, or on `trace_id` for the turn:

```bash
# ask: the shell expands $CF_ACCESS_CLIENT_ID and $CF_ACCESS_CLIENT_SECRET here, so this is not a plain command and asks for approval.
curl -fsS -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
        -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  "https://es.example.com/slm-requests-*/_search?q=trace_id:<trace_id>&size=20" \
  | jq -r '.hits.hits[]._source | [.ts, (.span_id // "?")[0:8], .prompt_tokens, .completion_tokens, .prefill_ms, .decode_ms, .total_ms, .cache_reuse] | @tsv'
```

Columns: time, span, prompt tokens, completion tokens, prefill ms, decode ms, total ms, cache
reuse. A `cache_reuse` above 0 means the server reused a KV-cache prefix: the prompt was cheaper in
compute than the token count shows. All times are UTC ISO-8601.

## Recovery of these steps

| Failure | Cause | Next step |
|---|---|---|
| `error: no model call or tool call of trace ...` from the script | The window of `--since` is too short, or `docker logs` failed | Use `--since=2h`, or use Step 1 |
| `docker logs` prints nothing | The container name is wrong | List the names with `docker ps` and use the gateway container |
| Step 4 returns HTTP 401 or 403 | The access headers are missing | Say that the model server log is not reachable. Do not guess its content |
| Step 4 cannot resolve the host | `es.example.com` is not reachable from here | Use Steps 1 to 3. They are self-contained on the gateway host |
| A `span_id` is absent | An older turn | Match by `ts` within 5 seconds of `model_call_started` in the gateway log |
| `could not resolve host: tempo` | The command ran on the host shell | Run it through the `bash` tool, which runs in the gateway container |
