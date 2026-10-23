---
name: model-latency
description: Measures how slow model calls are over a time window - p50, p90 and max by role or by model, and the prefill and decode time of the local model server. Starts with the measure_model_latency tool and uses a curl recipe for a filter that the tool does not list. Use when the task asks for latency numbers over a window, by role or by model, or for prefill and decode time. Not for explaining one turn or reading the spans of one trace id. The `turn-explanation` skill does those.
metadata:
  provides-tools: measure_model_latency
---

# model-latency — How slow the model calls are over a window

## Rules

- Answer only from a live Tempo query. Never answer from what a trace probably holds.
- Start with the `measure_model_latency` tool. Elasticsearch holds no model-call timing.
- `since` is from 1 minute to 7 days. Tempo refuses a longer window: say so, and give the 7-day figure with its window.
- Use the curl recipe only for a filter or a span attribute that the tool does not list (Step 2).
- Write a clock value and a number as a literal in a command. A shell variable or `$(...)` makes the command ask for approval.

## Step 1 — Use the tool

```python
# p50, p90 and max of model calls, by role, over the last day
measure_model_latency(since="24h", group_by=["role"])

# By role and model
measure_model_latency(since="24h", group_by=["role", "model"])

# One role only
measure_model_latency(since="24h", role="primary", group_by=["model"])

# The local server's side: prefill and decode time, by model, over the last week
measure_model_latency(since="7d", span="slm_chat", group_by=["model"])
```

`limit` is the number of traces to read, 200 by default and 500 at most. The reply holds, for each
group: `count`, `errors`, `p50_ms`, `p90_ms`, `max_ms` and token sums. The `slm_chat` span adds
prefill and decode percentiles.

**Read `complete` before you report a number.** `complete: false` means a cap was hit and the
numbers cover a sample. Name `incomplete_reasons` in the answer. An empty `groups` list means that no
model-call span matched in the window. It does not mean zero latency.

## Step 2 — A filter that the tool does not list

Past the tool's limits, and only with the `bash` tool: read `references/latency-recipe.md`. It holds a curl and jq recipe. Change the TraceQL
condition in `q`, and the recipe returns the same summary, with `complete` computed from the reply. Tempo answers on
the compose network as `http://tempo:3200`. It is not reachable from the host shell.

## Success

Check the result before you answer:

- The answer states the window, the group keys and the value of `complete`.
- A percentile comes from `spans_used`. A role or model with few spans has a coarse p90: say how many spans.
- A figure for a window over 7 days is not given. The answer says that Tempo holds at most 7 days.
- A number in nanoseconds is converted: the answer states the unit.

## Recovery

| Failure | Cause | Next step |
|---|---|---|
| `refused: ...` from the tool | An argument is not allowed (a span kind, a group key, a window over 7 days) | Change the argument as the message states |
| `store_unavailable`, `store_timeout`, `store_blocked` | Tempo is down, slow or blocked | Call once more. If it fails again, stop and tell the user. The `service-health` skill reads the Tempo status |
| `complete: false` | A trace or span cap was hit | Narrow `since`, or add `role` or `model`. Report the figure as a sample |
| `could not resolve host: tempo` | The curl recipe ran on the host shell | Run it through the `bash` tool, which runs in the gateway container |
| An empty `groups` list | No model-call span in the window | Widen `since` once. Say that the window held no call |
| The same failure twice | — | Stop. Tell the user what failed and what was tried |
