# FRE-1562 — A bound on every local primary generation

> Ticket: FRE-1562 (Approved, High). Source: FRE-1517 stage 3 (PR #1234), finding F3, proposal 3.
> Design intent: ADR-0141 D5 ("any future cap is a deliberate catalog edit, never a constructor default").

## Problem

A local primary call has no output bound except the server `--n-predict` (49,152) and the 600 s
client budget. Gemma s2 turns 7 and 13 (traces 8c347797, b42dbd6e) generated 17.5k to 18k tokens
at about 30 tok/s. The 600 s budget ended each turn with `LLMTimeout`, and the user read
"The model timed out — the request was large".

## AC-1 — Measurement (read-only, `agent-logs-*` on the VPS ES, 2026-10-10)

Query (all primary calls on local deployments, completed calls only):

```
filter: event_type = model_call_completed, role = primary, provider = slm_local
        [@timestamp >= now-30d for the 30-day table]
agg:    percentiles(output_tokens, 50/90/95/99/99.9), max(output_tokens), by model
```

Field notes: `event_type`, `role`, `provider`, `model` are `keyword` fields (a `.keyword`
sub-field returns zero hits). `output_tokens` is the `completion_tokens` of the stream, which
includes thinking on llama.cpp, so it is the quantity `max_tokens` bounds.

| Window | Deployment | n | p50 | p95 | p99 | p99.9 | max |
|---|---|---|---|---|---|---|---|
| 30 d | all local | 604 | 423 | 3,130 | 6,230 | 8,389 | **8,654** |
| 30 d | qwen3.8-flash-next | 339 | 401 | 3,786 | 6,821 | 8,506 | 8,654 |
| 30 d | gemma-4-26B-A4B | 141 | 693 | 3,316 | 4,359 | 5,032 | 5,127 |
| 30 d | mtplx flash-next | 81 | 302 | 2,221 | 2,730 | 2,906 | 2,926 |
| 30 d | mtplx 27B | 43 | 293 | 1,333 | 2,594 | 2,994 | 3,038 |
| since 2026-07-30 | all local (incl. retired qwen3.6) | 1,236 | 322 | 2,530 | 5,013 | 8,906 | **10,263** |

The largest normal call (8,654 tokens, trace e1c5d200) ended with `tool_calls: 1`, so a large tool
argument is a normal shape. The 10,263 maximum is `qwen3.6-35-A3B` (2026-08-31), still a catalog
entry.

Caveats the bound must respect:

1. `model_call_completed` records only calls that finished. A runaway ends as `model_call_error`
   (`LLMTimeout`), so these figures describe normal calls, which is what the bound must clear.
   There were 19 local primary `model_call_error` events, 4 of them 600 s timeouts.
2. ES drops events episodically, so each maximum is a lower bound.

Decode rate (call duration from `model_call_started` to `model_call_completed`, calls over 2,500
tokens, prefill included): Flash-Next median 26 tok/s (min 5.1, max 43.5); Gemma median 44 tok/s
(min 30.5).

## Decision

`max_tokens: 12288` on every local `kind: llm` entry in `config/models.yaml` (qwen3.6-35b-thinking,
qwen3.8-flash-next, qwen3.8-27b-mtplx, qwen3.8-flash-next-mtplx, gemma-4-26b-a4b).

- 1.42 x the 30-day normal maximum (8,654). 1.20 x the all-time maximum (10,263). Above the
  p99.9 of every deployment.
- At the Flash-Next median rate, 12,288 tokens take about 470 s, so the bound fires before the
  600 s budget. At the 5 tok/s tail the 600 s budget still wins, and no token bound can change that.
- A higher bound (16,384) would not fire before 600 s on Flash-Next. A lower bound (10,240) would
  cut the one retired-model normal call at 10,263.
- The catalog lever applies to the PRIMARY role's three call sites: the main turn loop, forced
  synthesis, and the expansion planner (`role=PRIMARY`, FRE-1413 already defers to the catalog).
  `sub_agent` keeps its own binding value, 8192.

Side effect to state in the PR: `effective_artifact_builder_max_tokens` reads `model_def.max_tokens`.
A local deployment chosen as artifact builder now drafts to `min(12288, 32768)` instead of 32768.
ES shows no local artifact-builder call in 70 days, and `artifact_tools.py:1664` already warns
when a draft reaches its budget.

## Design

1. `config/models.yaml`: add `max_tokens: 12288` (one comment block cites this plan).
2. `telemetry/events.py` + `telemetry/__init__.py`: add `PRIMARY_GENERATION_HIT_BOUND`.
3. `orchestrator/executor.py`:
   - `_local_generation_bound(llm_client) -> int | None`: `llm_client.max_tokens` when
     `llm_client.placement is Placement.LOCAL`, else `None`.
   - `_stop_turn_for_length_bound(ctx, *, deployment_key, bound, tokens_generated)`: sets
     `ctx.final_reply` (honest message, with the tool-results lead when results exist), appends a
     `warning` step, sets `ctx.turn_stopped_early = True` (skips grounding verification, so no
     retry LLM call), and logs `PRIMARY_GENERATION_HIT_BOUND` once.
   - `_finalize_llm_call_success(..., generation_bound: int | None = None)`: after the step record
     and before the assistant message is appended, if `generation_bound is not None` and
     `response.get("finish_reason") == "length"`, call the stop helper, log
     `STEP_PLANNING_COMPLETED` with `status="length_bound"`, and return `TaskState.SYNTHESIS`.
     The truncated content and any partial tool calls are dropped, so they never enter history.
   - `step_llm_call`: pass `generation_bound=_local_generation_bound(llm_client)` at both call
     sites (the call and its context-window retry).
4. Cloud: `generation_bound` is `None`, so `_finalize_llm_call_success` and every request is
   unchanged.

Message text: "Stopped — the model's reply ran too long. It reached the limit of 12,288 tokens
before it finished. Ask for a shorter answer or a narrower question." No "timed out" and no
"request was large".

## Tests (written first; each must fail before the code exists)

| AC | Test | File |
|---|---|---|
| AC-1 | The catalog bound exceeds the recorded normal maximum (constants 8,654 and 10,263 in the test, with the query in its docstring) | `tests/personal_agent/config/test_local_primary_generation_bound.py` |
| AC-2 | A stub stream that emits until the request's `max_tokens`, through the real `_respond_local`, ends at the bound with `finish_reason: length`; then `_finalize_llm_call_success` ends the turn with the honest message, one `primary_generation_hit_bound` event naming deployment, bound and tokens, no appended assistant message, and no tool execution | `tests/personal_agent/orchestrator/test_local_generation_bound.py` |
| AC-3 | A stub answer of 8,654 tokens with `finish_reason: stop` completes unchanged (content intact, normal final reply, no event) | same file |
| AC-4 | Local primary request carries `max_tokens` 12288. Cloud client requests carry the same `max_tokens` as before. `_local_generation_bound` returns `None` for a cloud client. A cloud response with `finish_reason: length` follows the old path | same file + golden |
| AC-5 | Live, master: first 20 local primary `model_call_started` events carry `max_tokens` 12288, none ends `length` below the bound | post-deploy runbook |

Golden: `tests/personal_agent/config/catalog_snapshot_golden.json` changes by exactly the five
local `max_tokens` cells. Review the diff before regenerating.

## Commands

```
uv run pytest tests/personal_agent/config/test_local_primary_generation_bound.py tests/personal_agent/orchestrator/test_local_generation_bound.py -x
make test-file FILE=tests/personal_agent/config/test_catalog_snapshot.py
make test && make mypy && make ruff-check && make ruff-format && pre-commit run --all-files
```

## Diff class

Not a production write path, not destructive, no schema, no cost or governance code. Self-serve.
Deploy class: `seshat-gateway` rebuild (master).
