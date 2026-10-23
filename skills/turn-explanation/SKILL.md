---
name: turn-explanation
description: Explains one agent turn from its trace id or session id - the per-call timeline with the input_tokens of each call, which part of the context used the tokens, where the time went, the spans of the trace, what the browser received, and the inference-server request log. Use when the task is to explain one turn - why it was slow, why the context grew, or what it did step by step - to replay a trace, or to list the spans of a known trace id. Not for latency over a time window. The `model-latency` skill gives that.
metadata:
  provides-tools: search_logs
---

# turn-explanation — Explain one turn end to end

## Rules

- Pull the turn from live data. Never infer the timeline or a token count from priors.
- Input: a `trace_id` (best) or a `session_id`. If the task names only "the last turn" or "the slowest turn", find the trace first (Step 0).
- Start with the `input_tokens` ladder (Step 1). It answers "why did the context grow" and shows which call was slow.
- This skill explains one turn in depth. For a count or a breakdown over many turns use the `log-search` skill. For latency over a window use the `model-latency` skill.
- `turn_status.context_tokens` is not a field that `search_logs` can read. The tool refuses it. Read the context size from `input_tokens`.
- `trace_id` and `session_id` join every substrate.

## What each substrate answers

| Substrate | Answers | How to read it |
|---|---|---|
| Elasticsearch `agent-logs-*` | The calls, the tokens, the tool calls | `search_logs` (Steps 0 and 1) |
| Gateway log | The same events, complete, with time stamps | `call_timeline.py` (Step 1b) |
| Tempo | The duration of each span | curl on `tempo:3200` (Step 1c) |
| Postgres `session_events` | What the browser received (`RUN_ERROR`, `STATE_DELTA`) | psql (Step 2) |
| R2 artifact store | The bytes of an artifact | Step 3 |
| Inference-server log | The model server's view of each request | Step 4 |

## Step 0 — Find the trace

```python
# The traces of one session, newest first (a model call starts every one)
search_logs(action="search", index="agent-logs", size=20, since="24h",
                query={"bool": {"must": [{"term": {"session_id": "<session_id>"}},
                                         {"term": {"event_type": "model_call_started"}}]}},
                fields=["@timestamp", "trace_id", "role"])
```

A turn with a model call has one trace id. Take the newest `trace_id` for "the last turn".

## Step 1 — The `input_tokens` ladder: the per-call timeline and the context attribution

```python
search_logs(action="search", index="agent-logs", size=50, since="24h",
                query={"bool": {"must": [{"term": {"trace_id": "<trace_id>"}},
                                         {"terms": {"event_type": ["model_call_completed", "tool_call_started"]}}]}},
                fields=["@timestamp", "event_type", "role", "model", "input_tokens", "output_tokens", "tool_name"])
```

The reply is newest first. Sort it by `@timestamp`. Read `input_tokens` down the primary calls.
A big step up is context growth. The `tool_call_started` before it is the cause. A whole-file
`read` is the usual one: file bytes divided by 4 are about the tokens.

Past the tool's limits, and only with the `bash` tool: read `references/other-substrates.md`.

## Reading the result

- **A context jump after a `read`**: a large read. The default `read` returns the first 200 lines and at most 8 KiB. A jump means that the call named a large `max_bytes` or `limit`, or that the model paged through a file in many reads. The arguments of the `tool_call_started` before the jump show which.
- **A slow primary call with no tool before it**: a large prefill (the grown context is sent again at each step), or a degraded model server (multi-minute calls, HTTP 524). A restart of the model server fixes the second case.
- **A gap between the sub-agent end and the final reply**: the synthesis call on the grown context. The sub-agent was not slow.
- **A terminal tool failure**: look for `tool_terminal_short_circuit`. The turn must end in milliseconds with a `tool_failure` `RUN_ERROR` and no recovery call.
- `turn_status.context_tokens` is an estimate that the UI shows. `search_logs` does not list it, and `input_tokens` (the model's tokenizer) differs from it by 10 to 20 percent. Quote `input_tokens`.

## Success

Check the result before you answer:

- The answer names the trace id and lists the calls with `input_tokens` and the gap before each.
- A "why was it slow" answer names one step (a call or a tool) and gives its seconds, from Step 1b or Step 1c.
- A "why did the context grow" answer names the tool result before the jump and its size.
- The answer says which substrates were read. A substrate that was not reachable is named, not skipped.

## Recovery

| Failure | Cause | Next step |
|---|---|---|
| The tool reply holds no event for the trace | The `since` window is too short, or the id is wrong | Widen `since` to `7d` once. Check the id against Step 0 |
| The same failure twice | — | Stop. Tell the user what failed and what was tried |
