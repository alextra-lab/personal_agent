# FRE-1515 — the four register types reach the planner; a decline or a failure returns to the tool loop

**Ticket:** FRE-1515 · **Design:** ADR-0154 D2, D3, Amendment 1 (1.1, 1.3) · **Tier:** Complex (codex plan review)

## Scope

- The gateway resolves the session's primary deployment and gives `_apply_matrix` one boolean: does that deployment declare a `planner` mode.
- With the mode, `CONVERSATIONAL`, `TOOL_USE`, `ANALYSIS` and `PLANNING` route `HYBRID` / `planner_asked` at every complexity. Without it, today's routing, plus `planner_gate_reason = planner_mode_absent`.
- `planner_may_decline` (true exactly when the reason is `planner_asked`) turns on the decline rule, a `SINGLE`-admitting schema, and the new validation. A decline or a failure returns to `LLM_CALL` with no worker, no synthesis message and no fallback planner.
- A `DECOMPOSE` answer on a `planner_asked` turn runs as `HYBRID` under min(3, `expansion_budget`).
- Every planner run writes `ctx.planner_run` (nothing writes it today), and `ctx.planner_gate_reason` is stamped.

## Design points

1. **The gate helper.** `config/model_loader.py::primary_declares_planner_mode(config=None) -> bool`. It resolves the primary with `resolve_inherited_deployment(config)` (the same selection guardrail and binding default that `get_llm_client("primary")` uses), then checks `"planner" in definition.modes` (the same test as `with_requested_mode`). Any exception logs `planner_gate_resolve_failed` and returns `False` (fail closed, same pattern as `resolve_active_context_length`). Both chat paths set the selection contextvar before `run_gateway_pipeline`, so the gate sees the turn's primary.
2. **The matrix.** `assess_decomposition(..., planner_mode_declared: bool = False)` → `_apply_matrix(task_type, complexity, delegation_enabled, planner_mode_declared)`. The forcings (`expansion_denied`, `zero_budget`, `expansion_disabled`) run first and are unchanged. `DecompositionResult` gains `planner_gate_reason: Literal["planner_mode_absent"] | None = None`, set only when the matrix ran on one of the four types without the mode.
3. **The prompt.** `_build_planner_system_prompt(surface, *, admit_single=False)`. With `admit_single`, it applies exactly the transformation that `scripts/eval/fre1537/render.py::render_system_prompt` applies (schema anchor `"strategy": "HYBRID|DECOMPOSE"` → `"strategy": "SINGLE|HYBRID|DECOMPOSE"`, and the FRE-1498 decline rule inserted after `Rules:\n`). A test asserts byte equality with the probe's rendering, so the prompt hash that FRE-1581's D7 run qualifies is the hash that ships (AC-10). The probe file is not changed.
4. **Validation with the flag.** JSON parse fails → `invalid`. `strategy == "SINGLE"` and `tasks` missing or `[]` → decline. `SINGLE` with tasks → `invalid`. `strategy` not in `HYBRID`/`DECOMPOSE` → `invalid`. Else `_validate_plan_json(raw, "HYBRID", max_tasks=budget)`, and the plan's strategy is set to `HYBRID` (D3.2). Without the flag, validation is unchanged.
5. **The controller outcome.** `ExpansionResult.planner_run: PlannerRunRecord | None`, set on every run. `PlannerRunRecord` gains `is_fallback: bool = False`. With the flag, a decline or a failure emits its one `planner_outcome` event (`is_fallback=False`) and returns no plan. `execute()` then returns at once: a decline reports nothing, a failure reports one `warning` degradation. The fallback planner runs only without the flag.
6. **The executor.** Stamps `ctx.planner_gate_reason` next to `conversation_history_chars`. Passes `planner_may_decline=(gw.decomposition.reason == "planner_asked")`. Calls `record_planner_outcome(ctx, result.planner_run)`. `record_planner_outcome` clears `ctx.expansion_strategy` for `declined`/`failed` only when the run is not a fallback (a fallback plan still dispatched workers). With no plan, no synthesis message is appended (already true), and the state is `LLM_CALL`.

## Steps

1. Matrix + gate (AC-1) — failing tests first.
   - `tests/personal_agent/request_gateway/test_fre1515_planner_gate.py`: every (four types × `Complexity` × gate) case, the unchanged types, the three forcings, and `primary_declares_planner_mode` with a catalog fixture (selection with mode, selection without, no selection → binding default, broken config → `False`).
   - Implement in `config/model_loader.py`, `request_gateway/decomposition.py`, `request_gateway/types.py`, `request_gateway/pipeline.py`.
   - Verify: `make test-file FILE=tests/personal_agent/request_gateway/test_fre1515_planner_gate.py`.
2. Prompt parity (AC-10 support).
   - `tests/personal_agent/orchestrator/test_fre1515_decline_prompt.py`: production `admit_single=True` prompt == probe `render_system_prompt`; `admit_single=False` unchanged from today.
3. Controller (AC-2, AC-3, AC-4).
   - `tests/personal_agent/orchestrator/test_fre1515_decline_and_failure.py`: decline; four failures (invalid JSON, timeout, exception, `input_too_large`) each with zero `run_sub_agent` calls, no `fallback_planner_used`, the matching `failure_reason`; the fifth (flag false → fallback runs); `SINGLE` with tasks → failure; five-task `DECOMPOSE`, budget 2 → 2 workers, plan strategy `HYBRID`.
   - Executor level in the same file: `step_init` with the real controller and a stub planner client — decline → `LLM_CALL`, no synthesis message, `ctx.expansion_strategy is None`, `ctx.planner_run.decision == "declined"`; failure → same with `failed`; AC-4 records `HYBRID`.
4. Executor wiring — implement until steps 3's executor tests pass.
5. AC-5 precondition — `tests/personal_agent/orchestrator/test_fre1515_primary_request_parity.py`.
   - One fixed session id, trace id and `turn_started_at`. Real `get_llm_client` on the real catalog (local primary `qwen3.8-flash-next`), `litellm.acompletion` patched (streaming fake), cost gate / tracker / skill catalog / tool registry patched to fixed values.
   - Route A: gateway `SINGLE`. Route B: gateway `HYBRID` / `planner_asked`, the fake answers the first call with `{"strategy": "SINGLE", "tasks": []}`.
   - Drive `step_init` then `step_llm_call`. Assert route A call 0 kwargs == route B call 1 kwargs, except `client` and the trace/span values inside `extra_headers`. Also assert route B call 0 is the planner call (decline-rule system prompt).
   - If a real difference shows, fix it in production, or report it before merge (Amendment 1, 1.3).
6. Fix the existing tests that the routing change moves (pipeline tests on the real catalog, whose primary declares a `planner` mode).
7. Docs: the controller's module docstring ("does not decide whether to expand"), the `input_too_large` comment, the `planner_gate_reason` comment in `orchestrator/types.py`.
8. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`. Commit, then self-review on `git diff origin/main...HEAD`.

## Acceptance criteria → proof

| AC | Proof |
|---|---|
| AC-1 | `test_fre1515_planner_gate.py` (matrix, unchanged types, forcings, gate helper) |
| AC-2 | `test_fre1515_decline_and_failure.py::…decline…` (controller + executor) |
| AC-3 | the four failure tests + the fallback test in the same file |
| AC-4 | the five-task `DECOMPOSE` test, controller + executor |
| AC-5 | the precondition test in this PR. The D7 scorer report with the paired arm comes from FRE-1581, posted before merge |
| Qualification | not decided by this build: the Gemma `planner` mode (FRE-1517 study) is an owner decision, recorded on the ticket before merge |
| AC-6 | live, after deploy (runbook in the handoff) |

## Out of scope

- No change to `config/models.yaml` (the Gemma decision is the owner's).
- No change to round budgets (ADR-0154 AC-9).
- No change to the probe under `scripts/eval/fre1537/`.
