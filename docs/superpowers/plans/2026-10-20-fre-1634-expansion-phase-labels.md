# FRE-1634 — Expansion planner phase and post-expansion synthesis label

Ticket: FRE-1634 (bugfix, `seshat-gateway` rebuild). Design intent: ADR-0123 §1-§2 (transport
phases). No event schema change. No new phase name.

## Defects

1. `ExpansionController.execute` runs `_run_planner` with no transport phase. The wait is
   invisible in the PWA.
2. `step_llm_call` (`orchestrator/executor.py`, `_inference_phase`) labels the primary call
   `planning` when `ctx.tool_iteration_count == 0`. After an expansion that call is the
   synthesis over helper digests, still with count 0.

## Changes

1. `src/personal_agent/orchestrator/expansion_controller.py`, `execute`: wrap the
   `_run_planner(...)` call in `async with phase_span(session_id=session_id, phase=Phase.PLANNING)`.
   Import both names locally, as `_run_dispatch` does. The span closes before `_run_dispatch`
   opens `EXPANSION`, so the two never nest. `phase_span` is a no-op without a `session_id`.
2. `src/personal_agent/orchestrator/executor.py`, `step_llm_call` (codex plan review, finding 2):
   - A call is `SYNTHESIS` when `tool_iteration_count > 0`, or `synthesis_appended`, or
     `sub_agents_dispatched > 0`. The last term covers a fan-out whose workers all raised. That
     fan-out appends no synthesis message.
   - Detail: `round N` when `tool_iteration_count > 0`. `helpers` when `synthesis_appended`.
     Otherwise `None`.
3. (Finding 3) Correct a stale comment in `seshat-pwa/src/__tests__/phase-labels.test.ts`: a turn can
   now show `planning` twice. Update the `Phase` docstring in `transport/events.py` so `PLANNING` also names the expansion
   planner call and `SYNTHESIS` also names the call over helper results. No enum change.

## Tests (write first, confirm they fail)

- `tests/personal_agent/orchestrator/test_fre1634_expansion_phases.py` (finding 4: events pair by `phase_id`)
  - AC-1: drive `step_init` then `step_llm_call` for a HYBRID turn with a fake planner and fake
    `run_sub_agent`. Assert the start order, that planning ends before expansion starts, that
    synthesis starts after expansion ends, that each `sub_agent` has the expansion as parent and
    ends first, and that no `planning` starts after the expansion ends.
  - Planner timeout: the fallback plan runs, `planning` is paired, the last start is `synthesis`.
  - All workers raise: `synthesis_appended` is `False`, the last start is `synthesis` with no detail.
  - Decline (`planner_asked`): the primary call stays `planning`.
  - AC-2: existing `TestInferencePhaseEvents` in `tests/test_orchestrator/test_executor.py`, unchanged.
- Command: `make test-file FILE=tests/personal_agent/orchestrator/test_fre1634_expansion_phases.py`

## Existing tests to run

- `make test-file FILE=tests/test_orchestrator/test_executor.py`
- `make test-file FILE=tests/personal_agent/orchestrator/test_expansion_controller.py`
- `make test-file FILE=tests/personal_agent/orchestrator/test_fre1515_decline_and_failure.py`
- `make test-file FILE=tests/personal_agent/transport/test_phase_events.py`
- Then the Step-6 gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
  `pre-commit run --all-files`.

## Out of scope

- PWA code. The PWA renders phase names from the events.
- Any change to `ExpansionPhase` (internal `PhaseResult`).

## Post-deploy (master)

AC-3: on the owner's next HYBRID turn, `session_events` has `PHASE_START planning` before
`expansion`, and no `planning` start after the `expansion` end.
