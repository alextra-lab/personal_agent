# FRE-1409 — pre-step lifetime check in the state-driver loop (ADR-0143 D4)

Tier: Standard (touches `src/` logic). Backing ADR: `docs/architecture_decisions/ADR-0143-the-request-task-is-the-unit-that-must-end.md` D4.
One phase, one PR.

## Defect

`orchestrator_turn_lifetime_seconds` binds three sites in `executor.py`: the in-flight primary call, a constraint pause, and the tool-iteration-limit gate. The driver loop in `execute_task` dispatches every step with no lifetime check. A turn whose tool round ran past the cap then makes another LLM call.

## Design

1. In `execute_task`, at the top of the `while` body, before the `STATE_TRANSITION` log and before the `step_func` lookup:

   ```python
   if state is not TaskState.SYNTHESIS and _turn_lifetime_remaining(ctx) <= 0:
       log.warning("turn_lifetime_cap_exhausted_before_step", trace_id=..., session_id=..., state=state.value, budget_seconds=...)
       _stop_turn_for_lifetime_cap(ctx)
       state = TaskState.SYNTHESIS
   ```

   - The `while` condition already excludes `COMPLETED` and `FAILED`.
   - `SYNTHESIS` is exempt. Without the exemption, an exhausted turn would re-run the check on the state it just set.
   - The check sits before the transition log, so the log shows `to_state=synthesis` and never a step that did not run.
   - A step span left open by a previous `TOOL_EXECUTION` return is closed by the existing `finally` after `step_synthesis`, because `COMPLETED != TOOL_EXECUTION`.
   - The check bounds the next step. It does not cancel a tool in flight.

2. In `config/settings.py`, replace the description of `orchestrator_turn_lifetime_seconds`. Name the four bind points: in-flight primary call, constraint pause, tool-iteration-limit gate, pre-step check. Point to `service_request_task_lifetime_seconds` for the whole-task bound. Remove the unqualified "cap on a turn".

3. Update the `_turn_lifetime_remaining` and `_stop_turn_for_lifetime_cap` docstrings to name the pre-step check.

4. Regenerate the config inventory doc (the FRE-1408 commit did the same for its settings).

## Tests — `tests/personal_agent/orchestrator/test_lifetime_cap_pre_step_check.py`

Harness: real `execute_task` and real `step_synthesis`. Recorders replace `step_init`, `step_planning`, `step_llm_call`, and `step_tool_execution`. The `step_llm_call` recorder calls a mocked LLM client. Post-completion side effects (`_write_task_capture`, event bus, reflection) are patched out. A fake `time` stub replaces `ex.time` and delegates everything except `monotonic`. A global patch of `time.monotonic` is not used, because the asyncio loop reads it.

| AC | Test |
|----|------|
| AC-1 | Parametrized over `INIT`, `PLANNING`, `LLM_CALL`, `TOOL_EXECUTION`. Clock past the cap before dispatch. Assert `COMPLETED`, `turn_stopped_early`, non-empty `final_reply`, dispatched states equal `[SYNTHESIS]`, zero LLM calls. |
| AC-1b | Chain: `INIT` recorder advances the clock and returns `LLM_CALL`. Assert `SYNTHESIS` runs once and the LLM client has zero calls. |
| AC-2 | Start in `SYNTHESIS` with the cap exhausted and a preset reply. Assert synthesis runs once, the turn completes, the reply is unchanged, `turn_stopped_early` is false, and `_stop_turn_for_lifetime_cap` is not called. |
| AC-3 | Real `step_tool_execution` with a 0.5 s fake tool. `turn_started_monotonic` is set so the cap expires 0.1 s into it. Assert the tool completed (not cancelled), its result is in `ctx.tool_results` and in `final_reply`, and the LLM client has zero calls. |
| AC-4 | Description names all four bind points and points to `service_request_task_lifetime_seconds`. The text does not contain "cap on a turn" without that qualifier. |
| AC-5 | Run AC-1, AC-1b, AC-3 on the merge-base source and on the branch. Record both runs in the PR. |

AC-5 note: AC-2 passes on `main` by construction. On `main`, `SYNTHESIS` already runs once. AC-2 guards against an implementation without the exemption. The PR records a mutant run (exemption removed) in place of a `main` failure for AC-2.

## Commands

- `make test-file FILE=tests/personal_agent/orchestrator/test_lifetime_cap_pre_step_check.py`
- `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`

## Risks

- `step_synthesis` with an empty `ctx.messages` calls `session_manager.update_session`. The tests use a mock session manager.
- The check fires at `INIT` for a turn that is already past the cap. This is the intended behavior.
