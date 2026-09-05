# FRE-1326 — PWA context counter: fix the numerator's data source

**Ticket:** FRE-1326 (Approved, `stream:build1`, `Tier-2:Sonnet`)
**Backing reference:** ADR-0092 (Context-Compaction Observability and Surfacing) — D3 defines
`session_context_tokens` as "current working-window occupancy," carried across turns. This plan
keeps that contract; it only corrects what value satisfies it.

## What is established (Step "What to establish first")

Read from the code, not guessed:

- **Numerator bug — confirmed as a wrong source.** `turn_status.context_tokens` /
  `session_context_tokens` are populated by `_report_turn_progress`
  (`src/personal_agent/orchestrator/executor.py:310`) from
  `estimate_messages_tokens(ctx.messages)` — a pre-call, local heuristic over the in-memory
  conversation list only. It excludes the assembled `system_prompt` (grounding contract,
  operator stanza, skill index block, tool prompt/awareness, injected memory — built up
  separately across `executor.py:5245-5750` and prepended only at `build_wire_messages`,
  `executor.py:1302-1303`, immediately before the wire call). The real, provider-reported input
  token count already flows through the system — `response["usage"]["prompt_tokens"]`
  (`executor.py:6064`, inside `step_llm_call`), matching the `gen_ai.usage.input_tokens` span
  attribute set in `src/personal_agent/llm_client/litellm_client.py:1653,1662` — but that number
  is read locally and discarded; it never reaches `turn_status`.
- **Denominator — already correct, not a hardcoded constant.** `_resolve_context_max`
  (`executor.py:264-280`) resolves through
  `resolve_active_context_length` (`src/personal_agent/config/model_loader.py:386-416`) →
  `resolve_role_target("primary", ...)` → the bound deployment's `context_length` from
  `config/models.yaml`. **No change needed here.** codex plan-review flagged that the existing
  `test_context_max_differs_cloud_vs_local` only proves the mechanism generalizes (Sonnet 200000
  ≠ the *retired* `qwen3.6-35b-thinking` 131072 key) — it does not exercise today's actual bound
  primary (`config/model_roles.yaml:122`, `qwen3.8-flash-next`, `context_length: 262144`,
  `config/models.yaml:163`). Added a new test (below) that resolves with **no explicit
  selection override** (the live default path) and asserts it against that current binding —
  concrete AC-2 evidence, not just the general mechanism.
- **Frontend — a pure function of the two numbers above.** `seshat-pwa/src/components/TurnStatusBar.tsx:56-57`
  computes `ctxPct = Math.round((sessionCtxTokens / ctxMax) * 100)` client-side from
  `session_context_tokens` / `context_max` alone. Once the backend numerator is correct, the
  percentage is automatically correct — **no PWA code change needed** (AC-3 scoped out with this
  reason). Session cost (`session_cost_usd`) already sources from real usage
  (`cost_authoritative_usd` = `SUM(api_costs)`, itself computed from real `input_tokens`/
  `output_tokens` × per-token price) — already correct, no fix needed (AC-3, second half).
- **The projector needs no change.** `observability/topology/projector.py` already just relays
  whatever `context_tokens` a `TurnProgressEvent` carries (line 339, 342) into `turn_status`
  (line 463, 476) and carries it across turns per ADR-0092 §D3. The bug is entirely in what
  value `_report_turn_progress` is given, not in how it's carried or surfaced.

## The fix

**Single source of truth, added once, read where it is known:** capture the real
`prompt_tokens` the moment `step_llm_call` learns it, hold it on `ExecutionContext` (mirrors the
existing `turn_cost_usd` accumulation pattern one line below it), and have
`_report_turn_progress` prefer it over the heuristic estimate.

This makes the reading **sticky** for the rest of the turn: between tool-loop iterations (before
the next model call resolves), the meter shows the last real count rather than reverting to a
smaller pre-call estimate — no flicker, and the number shown at turn-end is exactly the last
model call's actual input tokens (matches the ticket's own framing: "the final model call sent
41,520 input tokens").

**Precise semantics (per codex plan-review), stated so the ADR-0092 language is reconciled, not
contradicted:** the value is *the input-token count of the latest completed primary
`step_llm_call`, when the provider reported positive usage; otherwise the pre-call estimate.*
It is a measurement of the last request actually sent, not a live prediction of the next one —
newly appended assistant/tool messages after that call are not reflected until the *next*
`step_llm_call` resolves. This is still the right reading for ADR-0092's "current working-window
occupancy": between calls, the last measured occupancy is the best available proxy, and it is
corrected the moment the next call completes. Two edge cases follow from this, both acceptable
and left as-is (no code change needed to handle them):
- **HYBRID/DECOMPOSE expansion** (`executor.py:4810-4864`) builds independent message state for
  the planner/sub-agent calls (`expansion_controller.py`, `sub_agent.py`) — those calls never
  touch `ctx.last_prompt_tokens` on the parent context, so the pre-expansion progress report at
  `executor.py:4813` and the post-expansion one at `:4860` both correctly still read the
  estimate until the primary's own synthesis `step_llm_call` (`:4863-4864`, transitioning to
  `TaskState.LLM_CALL`) completes.
- **A turn whose first model call errors before completing** (timeout at `executor.py:6030`,
  general exception at `:6143`) never reaches the `prompt_tokens` read at `:6064`, so no
  `TurnProgressEvent` carrying a corrected value is published for that turn; the session lane
  (`sess.context_tokens`, ADR-0092 §D3) simply keeps showing whatever the prior turn last set —
  the existing carry-across-turns behavior, unchanged.

### 1. `src/personal_agent/orchestrator/types.py`

Add one field to `ExecutionContext`, next to `turn_cost_usd` (~line 344):

```python
# FRE-1326: the real, provider-reported input-token count from the most recent model
# call this turn (step_llm_call sets this from response["usage"]["prompt_tokens"]).
# Sticky across tool-loop iterations within a turn; _report_turn_progress prefers this
# over the pre-call estimate once a model call has resolved.
last_prompt_tokens: int = 0
```

### 2. `src/personal_agent/orchestrator/executor.py`

**a. Capture the real figure where it is already read** — right after the existing line
(~6064) `prompt_tokens = response.get("usage", {}).get("prompt_tokens", 0)`, before the adjacent
`ctx.turn_cost_usd += ...` / `await _report_turn_progress(ctx)` pair (~6070-6071), add:

```python
if prompt_tokens:
    ctx.last_prompt_tokens = prompt_tokens
```

(Guarded on truthy — a response that genuinely omits `usage` must not clobber a good prior
reading with 0.)

**b. Prefer it in `_report_turn_progress`** (~line 310), change:

```python
context_tokens=estimate_messages_tokens(ctx.messages),
```
to
```python
context_tokens=ctx.last_prompt_tokens or estimate_messages_tokens(ctx.messages),
```

Add one line to the function's docstring noting the preference order (real wire count once
known this turn; heuristic estimate only before the first model call resolves).

**c. Update the stale doc contract** (codex plan-review finding) — `TurnProgressEvent.context_tokens`
in `src/personal_agent/events/models.py` (~line 1044-1047) is documented as "Estimated tokens."
Update it to describe the corrected semantics: the latest completed primary call's real
input-token count when the provider reported positive usage, else the pre-call estimate.

## Tests (TDD — write failing first)

1. **`tests/test_orchestrator/test_turn_progress_report.py`** — the two existing
   `SimpleNamespace` ctx stubs gain `last_prompt_tokens=0` (keeps them a faithful
   `ExecutionContext` shape). Add:
   - `test_report_turn_progress_prefers_last_prompt_tokens_over_estimate` — stub with
     `last_prompt_tokens=41520` (the ticket's own trace number) and a tiny `messages` list;
     assert the published `TurnProgressEvent.context_tokens == 41520`.
   - `test_report_turn_progress_falls_back_to_estimate_before_first_model_call` — stub with
     `last_prompt_tokens=0` and a known `messages` list; assert
     `TurnProgressEvent.context_tokens == estimate_messages_tokens(messages)`.
2. **`tests/test_orchestrator/test_executor.py`** — two new end-to-end tests alongside
   `test_execute_simple_task` (codex plan-review: a single-call test proves the capture point
   wires up but not the sticky/overwrite contract, which is the actually-new behavior):
   - `test_last_prompt_tokens_captured_from_response_usage`: mock `mock_client.respond` to
     return `"usage": {"prompt_tokens": 41520, ...}`, run `execute_task_safe(ctx, ...)`, assert
     `ctx.last_prompt_tokens == 41520` afterward.
   - `test_last_prompt_tokens_sticky_across_zero_usage_response`: a two-call `side_effect`
     sequence — first response `"usage": {"prompt_tokens": 41520, ...}` with a tool call
     (forcing a second iteration), second response `"usage": {}` (or `"prompt_tokens": 0`) with
     no tool calls (turn ends); assert `ctx.last_prompt_tokens == 41520` still after the second
     call — proves a response with missing/zero usage does not clobber the last good reading.
     A third variant response (positive-then-different-positive, e.g. 41520 then 55000) asserts
     the later value **does** overwrite — proves correct-overwrite, not just stickiness.
3. **Regression:**
   - Re-run `tests/test_orchestrator/test_turn_status_context_max.py` (existing AC-2 evidence)
     and add `test_context_max_resolves_current_default_primary_binding`: call
     `_resolve_context_max()` with **no** `set_current_selection` override (the live default
     path), assert it equals `load_model_config().models["qwen3.8-flash-next"].context_length`
     (`config/model_roles.yaml:122`'s current binding) — concrete evidence against today's real
     default, not just the general mechanism (codex plan-review finding).
   - Re-run `tests/observability/topology/test_projector.py` (confirms the projector carries the
     corrected numerator through unchanged, since nothing there is touched).

## Acceptance criteria mapping

- **AC-1** (numerator matches `gen_ai.usage.input_tokens`) — satisfied exactly (same integer,
  same source read: `response["usage"]["prompt_tokens"]`), not "within a tolerance" against a
  separate estimate.
- **AC-2** (denominator resolves from the bound deployment) — already true; evidenced by the
  existing `test_context_max_differs_cloud_vs_local` plus the new
  `test_context_max_resolves_current_default_primary_binding` (today's actual bound primary),
  no code change.
- **AC-3** (percentage/cost verified or scoped out) — both verified: percentage is a pure
  frontend function of the two now-correct numbers; cost already sources from real usage.
- **AC-4** (a high-context turn shows a high reading) — the new
  `test_report_turn_progress_prefers_last_prompt_tokens_over_estimate` test uses the ticket's own
  41,520 figure directly as the asserted output.

## Test commands

```bash
make test-file FILE=tests/test_orchestrator/test_turn_progress_report.py
make test-file FILE=tests/test_orchestrator/test_executor.py
make test-file FILE=tests/test_orchestrator/test_turn_status_context_max.py
make test-file FILE=tests/observability/topology/test_projector.py
make test
make mypy
make ruff-check
make ruff-format
```

## Diff class

Not escalated — read-path observability fix (a value shown on a status line), no schema,
security, cost-governance, or destructive-write-path change. Standard self-review
(`feature-dev:code-reviewer`) applies per the build skill; `security-review` not triggered (no
input/subprocess/auth/secrets/network surface touched).
