# FRE-1413 — planner max_tokens sized for a thinking primary

Ticket: https://linear.app/frenchforest/issue/FRE-1413
Backing context: FRE-1390 (moved the planner to `ModelRole.PRIMARY`), ADR-0141 D5 (on
llama.cpp the completion budget includes thinking).

## Root cause (confirmed by reading the code, not assumed)

Two separate defects compound:

1. `expansion_controller._run_planner` (`src/personal_agent/orchestrator/expansion_controller.py:450`)
   hard-codes `max_tokens=1024` on the planner call. That number was sized for `SUB_AGENT`
   (thinking hard-disabled). FRE-1390 moved the call to `PRIMARY` (thinking enabled,
   `thinking_budget_tokens: 32768`), and on llama.cpp the completion budget is shared between
   thinking and content — so a thinking-heavy model exhausts 1024 before emitting JSON.
2. `adapt_chat_completions_response` (`src/personal_agent/llm_client/adapters.py:349-429`) —
   the adapter every **local** call goes through — never reads `choice.get("finish_reason")`
   into the returned `LLMResponse`, even though `_aggregate_streaming_chunks` (same file,
   line 131) already puts it on the aggregated dict it hands this adapter. `LLMResponse.finish_reason`
   (`llm_client/types.py:124`) is documented "populated on cloud calls; absent elsewhere" —
   that "elsewhere" is this dropped field, not an inherent local-server limitation. This is why
   a truncated planner call and a genuinely malformed one both surface as the same
   `schema_validation_failed` today (AC-3's complaint).

## Evidence gathered (real ES telemetry, `agent-logs-2026-09`, query via `prompt_callsite: "role.primary"`
which uniquely identifies planner calls vs. the orchestrator's own turn calls, `orchestrator.primary`)

| timestamp | model | in | out | outcome |
|---|---|---|---|---|
| 2026-09-04 20:20:15 | qwen3.8-flash-next | 215 | 501 | planner_completed |
| 2026-09-05 05:13:16 | qwen3.8-flash-next | 239 | 765 | planner_completed |
| 2026-09-05 05:47:10 | qwen3.8-flash-next | 239 | 804 | planner_completed |
| 2026-09-05 09:12:35 | qwen3.8-flash-next | 239 | 713 | planner_completed |
| 2026-09-05 16:20:34 | qwen3.6-35-A3B | 239 | 1024 | **capped, truncated** |
| 2026-09-05 16:25:37 | qwen3.6-35-A3B | 239 | 1024 | **capped, truncated** |

**Gap, stated plainly:** these are the only two `role.primary` calls against qwen3.6-35-A3B in
the whole index (checked both `agent-logs-2026-08` and `-09`) and both are capped — neither
shows the model's true, un-truncated completion length. AC-2 asks for 3 real turns on the
failing model; only 2 truncated ones exist. This build session has no path to the SLM server
(Cloudflare Access fronts it; this worktree's `.env` carries no CF Access service-token
credentials — confirmed empty) and firing a live turn through the deployed service is outside
a build session's authority regardless. **AC-1, AC-2's third data point, and AC-2b's live
confirmation are therefore PR evidence gathered by master/owner post-deploy, not by this
session** — matching the project's own precedent (`TestPlannerRoleBinding` docstring in
`tests/personal_agent/orchestrator/test_expansion_controller.py:1666`: "AC-1's live-container
verification is evidence for the PR, not a unit test"). Exact runbook below.

## Fix

**Revised after codex plan-review** (first draft proposed a `thinking_budget_tokens +
headroom` resolver — codex correctly flagged it as unsafe: it would apply the same generous
cap to a thinking-*disabled* or cloud-selected primary too, exceeding their own catalog
ceilings, since an explicit per-call override always wins over the client's configured one
(`litellm_client.py:1499`)).

**Simpler and safer: stop overriding `max_tokens` on this call at all.** The main orchestrator
turn call (`executor.py:6190`) already omits `max_tokens` and lets the resolved client's own
`self.max_tokens` — built per-deployment by `llm_client/factory.py` from the catalog + role
binding — govern. That value is already correct for every deployment shape: local
thinking-capable primaries declare no `max_tokens` in `config/models.yaml`, which
`factory.py:111-115`'s own comment explains is deliberate ("on llama.cpp the completion budget
includes thinking, so applying [a cloud default] locally would cap a primary whose thinking
budget alone declares 32768") — so the request omits `max_tokens` entirely
(`litellm_client.py:1531-1535`) and the local server is free to use its full context. A
thinking-disabled or cloud-selected primary keeps its own smaller declared ceiling
untouched. The planner call was the only thing overriding this with a stale `1024`; removing
the override is a one-line change and needs no new resolver, constant, or model-awareness
logic — it defers entirely to sizing the catalog authors already did correctly.

**Companion defect, found via codex review, folded in (Step 5 — required for the token fix to
actually work): `settings.planner_timeout_seconds` defaults to 30.0s** — sized for the same
retired thinking-disabled sub-agent-bound call FRE-1390 moved off of. `qwen3.6-35b-thinking`'s
own catalog entry declares `default_timeout: 600` with the comment "128K prefill + up to 32K
thinking output" — the authors already reasoned about exactly this budget. But
`_run_planner`'s own `asyncio.wait_for(..., timeout=settings.planner_timeout_seconds)` wraps
the call independently of the client's internal timeout, so 30s still kills a long think
before the deployment's own 600s budget would. Removing the token cap without also raising
this would just convert token-truncation into a timeout — the identical silent-fallback
failure mode in a different guise. Raise the default to 600.0 to match the deployment's own
considered figure.

Both changes still need live confirmation (AC-2b runbook below) — removing the token override
and raising the timeout removes the "sized for the wrong role" defect on both axes, but there
is no live measurement that qwen3.6-35-A3B completes within either bound in practice. AC-3 is
what makes an insufficient bound safe rather than silent (next item) — it is the actual
backstop this uncertainty needs.

**Edge case, decided explicitly (codex flagged this as undecided):** a response that is both
schema-valid AND reports `finish_reason == "length"` is accepted as-is. The planner prompt
says "Output ONLY valid JSON" with nothing after it, so a `json.loads` success is strong
evidence the content is complete — rejecting a well-formed plan on `finish_reason` alone would
throw away good output on a coincidence (the model finishing its JSON exactly at a token
boundary). `finish_reason` is consulted only when validation already failed.

**AC-3 — distinguish truncation from a parse failure.**
- `adapters.py`: add `finish_reason=choice.get("finish_reason")` to
  `adapt_chat_completions_response`'s returned `LLMResponse` (mirrors what the cloud path
  already does at `litellm_client.py:1340`).
- `expansion_controller._run_planner`: when `_validate_plan_json` returns `None`, check
  `raw_response.get("finish_reason") == "length"` and log `planner_failed` with
  `reason="output_truncated"` instead of `"schema_validation_failed"` when true.

**AC-4 — fallback usage reaches the turn's own record.**
`executor.py` already reads `expansion_result.plan.is_fallback` at line 5056 but only logs it.
`ctx.steps` (`OrchestratorStep` list) is the existing turn-record mechanism — it is what
`OrchestratorResult["steps"]` returns to the caller (`executor.py:7086`), the same channel
already used for tool-call and error visibility. Add one step when `plan.is_fallback` is true.

**AC-5 — thinking stays enabled.** Untouched: `ModelRole.PRIMARY` binding and
`thinking_budget_tokens` are not modified; `disable_thinking` is never set on this path.

## Files touched

- `src/personal_agent/llm_client/adapters.py` — propagate `finish_reason` (1 line + docstring).
- `src/personal_agent/orchestrator/expansion_controller.py` — drop the `max_tokens=1024`
  override; truncation-aware `planner_failed` reason.
- `src/personal_agent/config/settings.py` — raise `planner_timeout_seconds` default 30.0 → 600.0.
- `src/personal_agent/orchestrator/executor.py` — one `ctx.steps.append(...)` for fallback
  visibility.
- Tests: `tests/test_llm_client/test_adapters.py`, `tests/test_llm_client/test_streaming_aggregation.py`,
  `tests/personal_agent/orchestrator/test_expansion_controller.py`,
  `tests/personal_agent/orchestrator/test_gateway_integration.py`.

## Tests (TDD — write failing first)

1. `test_adapters.py::TestAdaptChatCompletionsResponse::test_finish_reason_propagated` — a
   response with `finish_reason: "length"` on `choices[0]` comes back with
   `result["finish_reason"] == "length"`; a response with no `finish_reason` key leaves it
   absent (backward compatible — do not default it to a string that could be mistaken for a
   real value).
2. `test_streaming_aggregation.py::test_aggregated_shape_round_trips_through_response_adapter`
   — extend to assert `llm_response["finish_reason"] == "stop"` survives the full
   chunks → aggregate → adapt round trip (codex: this contract test currently checks
   content/tools/usage but not the field this ticket depends on).
3. `test_expansion_controller.py`:
   - `test_planner_call_does_not_override_max_tokens` — extends the existing
     `TestPlannerRoleBinding` class: assert `"max_tokens" not in call_kwargs`, proving the call
     defers to the client's own catalog ceiling rather than a hardcoded value.
   - New class `TestPlannerTruncationDistinguishedFromParseFailure`:
     - AC-3's own seeded check: mock `respond` to return `{"content": "<truncated garbage>",
       "finish_reason": "length", "cost_usd": 0.0}` → assert `caplog` shows
       `reason=output_truncated` (not `schema_validation_failed`).
     - Regression: `finish_reason: "stop"` + invalid JSON → still `schema_validation_failed`.
     - Edge case: `finish_reason: "length"` + a *valid* plan JSON → plan is still accepted
       (`result.plan.is_fallback is False`), documenting the deliberate decision above.
4. `test_gateway_integration.py` — new test in `TestExpansionCostRollup`'s style: an
   `ExpansionResult(plan=MagicMock(is_fallback=True), ...)` returned from the mocked
   controller → `ctx.steps` gains an entry with `metadata["planner_fallback"] is True`
   after `step_init`.
5. `test_settings.py` (or wherever a default-value smoke test already lives) — 
   `planner_timeout_seconds` default is `600.0`, not `30.0`.

## Post-deploy runbook (for master/owner — closes AC-1, AC-2's 3rd point, AC-2b)

1. Confirm the deployed cap: `grep _PLANNER_JSON_HEADROOM_TOKENS` on the deployed container's
   source, or check the next `model_call_started` planner event's `max_tokens` field.
2. Run the owner's own repro query through a HYBRID/DECOMPOSE-triggering turn: "Compare three
   different ways to compute the median of a large list of numbers, and evaluate which is
   fastest." — once with primary bound to `qwen3.6-35b-thinking`, once with
   `qwen3.8-flash-next` (AC-2b needs both).
3. Query ES for the result:
   ```
   GET agent-logs-2026-09/_search
   { "query": { "bool": { "filter": [
       {"match_phrase": {"message": "model_call_completed"}},
       {"term": {"prompt_callsite": "role.primary"}}
   ]}}, "sort": [{"@timestamp": "desc"}], "size": 5 }
   ```
   Expect `output_tokens` well under the new cap on both models, and a paired
   `planner_completed`/`fallback_used=False` log line (or the new `ctx.steps` fallback entry,
   if it degrades) rather than a repeat of the 1024-exact truncation.
4. If qwen3.6-35-A3B still truncates at the new cap: AC-3's `output_truncated` reason (not
   `schema_validation_failed`) will show in the same query — that is the signal to raise
   `_PLANNER_JSON_HEADROOM_TOKENS` further, now with real data instead of a guess.
