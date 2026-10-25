# FRE-1697 — A cloud answer that spends the whole output cap on reasoning must not end as completed

Ticket: FRE-1697 (Approved 2026-10-25). Tier: Standard (touches `src/` logic).
Related: FRE-1562 and FRE-1676 (the same stop on a local primary), FRE-1692, FRE-1699.
Codex plan-review: done 2026-10-25. Four findings accepted, shown below as "review".

## Defect

A cloud response with `finish_reason == "length"` takes the normal path in
`_finalize_llm_call_success` (`orchestrator/executor.py`). The local path already handles this stop
(`generation_bound`). The cloud path skips it on purpose, because `_local_generation_bound` returns
`None` for a cloud client.

- Reasoning only, no text: the turn ends `completed` with no answer (trace bff76b1d, 13:04:34Z).
- Tool call cut: the call arrives with no parameters. The tool result says only "missing required
  parameters" (13:00:22Z).

## Design

A cloud `length` stop with no tool call and some answer text keeps its old handling.

1. **New parameter** `output_cap: int | None = None` on `_finalize_llm_call_success`. A new helper
   `_cloud_output_cap(llm_client)` returns the client's `max_tokens` when the placement is cloud and
   the value is an `int`, else `None`. Both call sites in `step_llm_call` pass it.
2. **Cause**, decided from the raw response before the cite-only filter drops tool calls (review):
   - raw tool calls present, not a cite-only retry → `tool_call_cut`, whatever the text (review:
     a `length` stop with tool calls means the last call is cut).
   - no raw tool calls and empty `content` → `reasoning_only`.
   - cite-only retry with raw tool calls and empty `content` → `tool_call_cut`, and the turn fails.
   - otherwise (answer text, no tool call) → no change.
3. **AC-3 event** `primary_output_cap_reached` (`telemetry/events.py`, exported from
   `telemetry/__init__.py`). Fields: `trace_id`, `session_id`, `span_id`, `deployment`, `cause`,
   `output_cap`, `tokens_generated`, `reasoning_tokens`. One event per stop. No argument or
   reasoning text.
4. **AC-1, reasoning only** (and a second cut, and a cite-only cut): the turn ends `FAILED`.
   - New `LLMOutputCapReached(LLMClientError)` in `llm_client/types.py` with `cap` and `tokens`.
   - `classify_error` maps it to category `output_cap`. The reason names the output limit. The
     category is added to both `Literal` lists (`error_classification.py`, `transport/events.py`).
   - A helper in `executor.py` sets `ctx.error`, salvages gathered tool results with
     `_salvage_partial_reply`, sets `ctx.classified_error`, appends the warning step, logs
     `STEP_PLANNING_COMPLETED` with `status="error"`, and returns `TaskState.FAILED`. The cut
     response does not enter history. The existing failed-turn path writes the capture with
     `outcome="failed"`.
   - **Registry outcome (review, accepted).** `OrchestratorResult` gains `error_category`, set in
     `execute_task_safe` from the classified error. In `service/app.py`, after
     `handle_user_request`, `request_record.stamp_failed()` runs when `error_category == "output_cap"`.
     Other classified failures keep their current registry outcome. This is a deliberate narrow
     scope. It is noted in the handoff.
5. **AC-2, tool call cut.** Built inside the finalizer, so no governance return in
   `step_tool_execution` can bypass it (review). The first cut in a turn:
   - The assistant message with its tool calls enters history, so every call has a result.
   - One `role: tool` message per call, content from `undispatched_failure` with new `DispatchCode`
     `"output_cut"`. It says the output was cut at the output limit, so the call did not run, and to
     call again with shorter reasoning and smaller arguments.
   - `ctx.length_stop_retries` is incremented. The finalizer returns `TaskState.LLM_CALL`.
   - A second cut in the same turn ends the turn through item 4 (`tool_call_cut`). This reuses the
     FRE-1676 counter. No tool-iteration cap bounds this route, so the counter is the bound.
   - `step_tool_execution` is unchanged.

## Steps (TDD)

1. Write `tests/personal_agent/orchestrator/test_cloud_output_cap.py`. Confirm every AC test fails
   on `origin/main`.
2. Add `LLMOutputCapReached`, the `output_cap` category, the `output_cut` dispatch code, the event
   constant, `OrchestratorResult.error_category`.
3. Implement `_cloud_output_cap`, the finalize branch, the failure helper, the `app.py` stamp.
4. Add `classify_error` cases to `tests/personal_agent/test_error_classification.py`.
5. Run the file, then the gates.

## Acceptance criteria and proof

| AC | Test | Expected |
|----|------|----------|
| AC-1 | `test_the_turn_fails_and_the_cut_reply_does_not_enter_history` | state `FAILED`; `ctx.error` is `LLMOutputCapReached`; reason names `16,384` |
| AC-1 | `test_the_visible_reply_names_the_limit_and_the_capture_is_failed` | reply holds `output limit` and `16,384`; capture outcome `failed` |
| AC-1 | `test_the_result_carries_the_category_for_the_registry` and the `app.py` stamp test | `error_category == "output_cap"`; the record is stamped `failed` |
| AC-2 | `test_the_first_cut_answers_every_call_and_returns_to_the_model` | state `LLM_CALL`; each call has a tool result with code `output_cut`, "cut", "output limit"; no "missing" |
| AC-2 | `test_text_beside_a_cut_call_is_still_a_cut` | `length` + text + tool call → cut path |
| AC-2 | `test_a_second_cut_ends_the_turn` | `FAILED`, cause `tool_call_cut` |
| AC-3 | `test_the_event_records_the_cause` (3 cases) | one `primary_output_cap_reached`, `cause` per case, no text |
| scope | `test_a_partial_answer_at_the_cap_keeps_the_old_path` | `SYNTHESIS`, no event |
| scope | `test_a_cite_only_cut_is_not_misread_as_reasoning_only` | cause `tool_call_cut` |
| scope | FRE-1562 and FRE-1676 test files | still pass |

## Out of scope

GLM's default effort level (FRE-1699). Registry `failed` for other classified failures.
