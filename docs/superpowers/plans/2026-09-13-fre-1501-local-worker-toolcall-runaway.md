# FRE-1501 — a tool call cut at the token ceiling poisons the next request

Ticket: FRE-1501 (Approved 2026-09-13, stream:build2, Tier-1:Opus).
Evidence: `docs/research/2026-09-12-fre-1498-what-the-model-chooses-when-nothing-forces-it.md` D1, D2.
Design intent: ADR-0149 D3 (every terminal path declares a stop reason and attempts a landing), ADR-0150 D6 (`finish_reason == "length"` is never read as complete).

## Mechanism (verified, not inferred)

1. A worker round returns `tool_calls` with `finish_reason: "length"`. The argument string is cut.
2. `_run_tool_loop` (`src/personal_agent/orchestrator/sub_agent.py:1721`) handles `length` only when there are no tool calls. So the cut round falls through.
3. The loop appends the assistant message with the cut arguments to `state.messages` (line 1804). `json.loads` fails, and the loop feeds back a "retry" hint (lines 1876-1886).
4. The next round sends that history. llama.cpp parses every history tool-call argument as JSON (`common/chat.cpp:1043-1052`, `common_chat_msgs_parse_oaicompat`), throws `runtime_error`, and answers 500: "Failed to parse tool call arguments as JSON: … column 12014". The column is a position inside the cut argument.
5. litellm retries the same poisoned request (`num_retries`). The origin then answers 503 to every caller for about a minute.
6. The worker raises `LLMServerError`, and `run_sub_agent` labels it `stop_reason="error"`. `_run_dispatch` (serialized) then starts the next sibling at once, into the 503.

The 1,024 characters in the ledger are `_LEDGER_ARGS_CAP_CHARS` (1,000) plus the clip marker.

## Acceptance criteria (from the ticket)

| AC | Criterion | Proof |
|----|-----------|-------|
| AC-1 | A worker whose tool-call turn ends with `finish_reason` length stops with a distinct `stop_reason` and no litellm retry; shown on a seeded response. | `TestToolCallTruncated` in `tests/personal_agent/orchestrator/test_sub_agent.py`: the seeded round stops with `stop_reason="tool_call_truncated"`. No tool is dispatched. The next and last request carries no cut argument, so the llama.cpp parse that produced the 500 (and the litellm retries of it) cannot occur. |
| AC-2 | After one worker's 500, no sibling is dispatched while the origin returns 503; shown on a seeded sequence. | `TestOriginErrorStopsDispatch` in `tests/personal_agent/orchestrator/test_expansion_controller.py`: worker 1 ends `origin_error`, `run_sub_agent` is called once, the other tasks land in `skipped_tasks` with `skip_reason="origin_error"`. Plus `run_sub_agent` maps `LLMServerError`/`LLMConnectionError` to `origin_error`. |
| AC-3 | The origin behaviour is reported to slm_server with the two timelines, and the report is linked on the ticket. | GitHub issue on `alextra-lab/slm_server`, link posted on FRE-1501. No slm_server code change (master's pickup note). |

## Design

### D-A (AC-1): a cut tool-call round ends the loop

In `_run_tool_loop`, directly after the existing no-tool-call `length` branch:

```python
if raw_tool_calls and round_finish_reason == "length":
    logger.warning("sub_agent_tool_call_truncated", tool_names=[...], argument_chars=[...],
                   tool_iterations=state.tool_iterations, trace_id=trace_id, session_id=session_id)
    return await _forced_synthesis(..., stop_reason="tool_call_truncated")
```

- The cut calls are never dispatched and never appended to `state.messages`. The synthesis request therefore carries a valid history.
- `_forced_synthesis` already handles "no time left" (ledger) and a failed landing call (ledger). This keeps ADR-0149 D3: the worker keeps what it researched in rounds 1..N−1.
- `_forced_synthesis` picks its opening line by stop reason. Add `_SYNTHESIS_OPENING_TRUNCATED = "Your last tool call was cut off at the token limit and was not run."`.
- `SubAgentStopReason` gains `"tool_call_truncated"`, with its line in the type's comment block. The ES field is `keyword`, so no template change.

### D-B (fold-in): no invalid JSON argument ever enters history

The same 500 occurs when a model emits invalid JSON arguments without hitting the ceiling ("missing closing quote" in D2 can be either case). In `_normalize_tool_calls`, write `"{}"` into the history copy when the raw argument string does not parse as JSON. The loop still feeds back the existing "retry" hint. The ledger keeps the raw string (it reads `raw_arguments`, not the normalized call).

### D-C (AC-2): an origin failure stops the fan-out

Revised after codex plan review (see "Review disposition" below).

- `run_sub_agent`'s `except Exception` sets `stop_reason="origin_error"` when the exception is `LLMServerError`, else `"error"` as today. Only the local path's mapper (`litellm_client._map_local_dispatch_error`) raises it, after litellm's own retries are spent. `LLMConnectionError` stays `error`: a tunnel interruption does not show that the origin is down.
- `_write_landing_report`'s `except Exception` also labels its outcome `origin_error` on `LLMServerError`. Without this, a 5xx on the landing call keeps the triggering stop reason and the dispatcher cannot see it. The ledger keeps the triggering path.
- `_run_dispatch`: after a result with `stop_reason == "origin_error"`, the gap replacement is not attempted, and every task still queued is skipped. A gap replacement that ends `origin_error` trips the same skip. Skipped tasks go in `result.skipped_tasks`, and a `sub_agent_dispatch_skipped_origin_error` warning is logged. This is the same shape as the FRE-1397 turn-budget skip.
- The skip is for the rest of this fan-out, not a timed wait. A recovery wait needs a probe that sees a dead backend, and FRE-1474 records that the health probe cannot.
- The skipped-task prose today says "time budget exhausted" in three places. `SkipReason = Literal["turn_budget", "origin_error"]` and `SKIP_REASON_TEXT` go in `expansion_types.py`. `ExpansionResult` (in `expansion_controller.py`) gains `skip_reason: SkipReason | None`, set only when a task is skipped. `ExecutionContext` gains `expansion_skip_reason`. The three sites render the reason text:
  - `ExpansionController._build_synthesis_context`
  - `executor._fallback_reply_from_sub_agent_results` (the deadline fallback)
  - `executor._compose_fanout_stop_and_show`
  The trailer's `not_dispatched` pseudo reason stays.

### Review disposition (codex, 2026-09-13)

- D-B must canonicalize as execution does (`str(... or "{}")`): accepted.
- `LLMConnectionError` too broad for the skip: accepted.
- Landing-call 5xx swallowed by `_write_landing_report`: accepted.
- Origin check must precede the gap replacement, and cover the replacement: accepted.
- Typed `{task, reason}` skip records instead of one `skip_reason`: not taken. Both skips hold for every later task, so one fan-out has one reason. One field changes fewer call sites.
- Wrong names in the plan (`ExpansionResult` location, executor function): corrected.

Not in scope: the primary's own synthesis call also meets the 503. The ticket's criteria name siblings only.

## Steps

1. Tests first, confirm they fail: `make test-file FILE=tests/personal_agent/orchestrator/test_sub_agent.py` and `make test-file FILE=tests/personal_agent/orchestrator/test_expansion_controller.py`.
   - `TestToolCallTruncated`: (a) seeded `length` + cut tool call → `tool_call_truncated`, `dispatch_tool_call` not awaited, `success is False`. (b) the messages of every `respond` call (snapshotted per call) hold only JSON-parseable tool-call arguments, and none carries the cut string. (c) D-B: a round with invalid JSON and `finish_reason="tool_calls"` → the next call's history argument is `"{}"`, and the ledger still shows the raw string.
   - `TestOriginErrorStopReason`: `LLMServerError` → `origin_error`; `LLMConnectionError` → `origin_error`; `RuntimeError` → `error`.
   - `TestOriginErrorStopsDispatch`: 3 tasks, first ends `origin_error` → one dispatch, 2 skipped, `skip_reason == "origin_error"`, synthesis context names the origin reason. A plain `error` result → all 3 dispatched. The turn-budget skip → `skip_reason == "turn_budget"`.
2. Implement D-A, D-B, D-C.
3. Update existing tests that assert the "time budget exhausted" wording, if any.
4. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
5. File the slm_server issue (AC-3), link it on FRE-1501.
