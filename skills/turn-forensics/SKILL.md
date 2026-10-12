---
name: turn-forensics
description: Explains one agent turn from its trace id or session id across the substrates - the per-call
  timeline, which part of the context used the tokens, where the time went, what the browser received, and
  the inference-server request log when present. Use when the task is to explain one turn - why it was
  slow, why the context grew, or what it did step by step - or to replay a trace. Not for latency numbers
  over a time window or the raw span list of a trace id - the query-tempo skill does those.
metadata:
  requires-tools: bash
---

# turn-forensics — Reconstruct one turn end to end

## Rules

- Pull the turn from live data. Never infer the timeline or a token count from priors.
- Input: a `trace_id` (best) or a `session_id`. If the task names only "the last turn" or "the slowest turn", find the trace first (Step 0).
- This skill explains one turn in depth. For a count or a breakdown over many turns use `query-elasticsearch`. For latency over a window use `query-tempo`.
- `trace_id` and `session_id` join every substrate.

## What each substrate answers

| Substrate | Answers | How to read it |
|---|---|---|
| Elasticsearch `agent-logs-*` | The calls, the tokens, the tool calls | `query_telemetry` (Steps 0 and 1) |
| Gateway log | The same events, complete, with time stamps | `call_timeline.py` (Step 1b) |
| Tempo | The duration of each span | `query-tempo` Step 2 |
| Postgres `session_events` | What the browser received (`RUN_ERROR`, `STATE_DELTA`) | `psql` (Step 2) |
| R2 artifact store | The bytes of an artifact | Step 3 |
| Inference-server log | The model server's view of each request | Step 4 |

## Step 0 — Find the trace

```python
# The traces of one session, newest first (a model call starts every one)
query_telemetry(action="search", index="agent-logs", size=20, since="24h",
                query={"bool": {"must": [{"term": {"session_id": "<session_id>"}},
                                         {"term": {"event_type": "model_call_started"}}]}},
                fields=["@timestamp", "trace_id", "role"])
```

A turn with a model call has one trace id. Take the newest `trace_id` for "the last turn".

## Step 1 — The per-call timeline and the context attribution

```python
query_telemetry(action="search", index="agent-logs", size=50, since="24h",
                query={"bool": {"must": [{"term": {"trace_id": "<trace_id>"}},
                                         {"terms": {"event_type": ["model_call_completed", "tool_call_started"]}}]}},
                fields=["@timestamp", "event_type", "role", "model", "input_tokens", "output_tokens", "tool_name"])
```

The reply is newest first. Sort it by `@timestamp`. Read `input_tokens` down the primary calls.
A big step up is context growth. The `tool_call_started` before it is the cause. A whole-file
`read` is the usual one: file bytes divided by 4 are about the tokens (`wc -c <path>` gives the bytes).

### Step 1b — The same timeline from the gateway log, with the time gaps

Use this when the tool reply holds fewer events than expected, or when you need the gap in
seconds between events. The script strips colour codes, keeps the trace, and prints one line for
each model call, tool call and sub-agent end:

```bash
docker logs cloud-sim-seshat-gateway --since=40m 2>&1 \
  | python3 skills/turn-forensics/scripts/call_timeline.py <trace_id>
```

Each line reads `HH:MM:SS  +Ns  CALL in=<tokens> role=<role>`, or `TOOL <name> <path>`. The
`+Ns` is the gap since the previous line. The script exits 1 with one `error:` line when no event
of the trace is in the window: widen `--since`, or use Step 1.

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

## Reading the result

- **A context jump after a `read`**: a large read. The default `read` returns the first 200 lines and at most 8 KiB. A jump means that the call named a large `max_bytes` or `limit`, or that the model paged through a file in many reads. The arguments of the `tool_call_started` before the jump show which.
- **A slow primary call with no tool before it**: a large prefill (the grown context is sent again at each step), or a degraded model server (multi-minute calls, HTTP 524). A restart of the model server fixes the second case.
- **A gap between the sub-agent end and the final reply**: the synthesis call on the grown context. The sub-agent was not slow.
- **A terminal tool failure**: look for `tool_terminal_short_circuit`. The turn must end in milliseconds with a `tool_failure` `RUN_ERROR` and no recovery call.
- `input_tokens` (the model's tokenizer) and `turn_status.context_tokens` (an estimate) differ by 10 to 20 percent. The trend is the same.

## Success

Check the result before you answer:

- The answer names the trace id and lists the calls with `input_tokens` and the gap before each.
- A "why was it slow" answer names one step (a call or a tool) and gives its seconds, from Step 1b or from Tempo.
- A "why did the context grow" answer names the tool result before the jump and its size.
- The answer says which substrates were read. A substrate that was not reachable is named, not skipped.

## Recovery

| Failure | Cause | Next step |
|---|---|---|
| The tool reply holds no event for the trace | The `since` window is too short, or the id is wrong | Widen `since` to `7d` once. Check the id against Step 0 |
| `error: no model call or tool call of trace ...` from the script | The window of `--since` is too short, or `docker logs` failed | Use `--since=2h`, or use Step 1 |
| `docker logs` prints nothing | The container name is wrong | List the names with `docker ps` and use the gateway container |
| Step 4 returns HTTP 401 or 403 | The access headers are missing | Say that the model server log is not reachable. Do not guess its content |
| Step 4 cannot resolve the host | `es.example.com` is not reachable from here | Use Steps 1 to 3. They are self-contained on the gateway host |
| A `span_id` is absent | An older turn | Match by `ts` within 5 seconds of `model_call_started` in the gateway log |
| The same failure twice | — | Stop. Tell the user what failed and what was tried |
