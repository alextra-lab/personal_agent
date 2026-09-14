# FRE-1507 — turn shape, settled and unsettled statements, per-shape declaration

**Ticket:** FRE-1507 (ADR-0151 T1). **ADR:** `docs/architecture_decisions/ADR-0151-a-turn-that-cannot-be-grounded-says-so.md` D1, D2, D4 (declaration half).
**Tier:** Standard (touches `src/` grounding and orchestrator logic). Codex plan-review required.

## Scope

- D1: classify each verified turn as shape `a`, `b`, `c`, or null.
- D2: split non-passed spans into settled and unsettled. The note counts settled failures only.
- D4 (declaration): the note gains a shape sentence and the unsettled count. It is computed in `observe` and `enforce`. It is still appended after the capture is written.
- Event: `grounding_verification_completed` gains `turn_shape`, `tool_rounds`, `sub_agents_dispatched`.
- Out of scope: `decide()`, the retry, `TERMINAL_NO_SOURCE`, heavy forcing (FRE-1509). `compliance.py`, the span extractor and the checks do not change.

## Design

### 1. `grounding/verification.py` (beside `classify_turn_evidence`)

```python
class TurnShape(StrEnum):
    A = "a"  # tool calls or workers ran, registry admitted none of their output
    B = "b"  # the registry holds one or more tool sources from this turn
    C = "c"  # no tool call and no worker this turn

def classify_turn_shape(
    verification: TurnVerification, *, tool_results_admitted: int, tool_rounds: int,
    sub_agents_dispatched: int,
) -> TurnShape | None:
    if not verification.available or not verification.spans:
        return None
    if tool_results_admitted >= 1:
        return TurnShape.B
    if tool_rounds == 0 and sub_agents_dispatched == 0:
        return TurnShape.C
    return TurnShape.A
```

`TurnVerification` gains `settled_failures`: every span with outcome not `passed` and not in `_MACHINE_UNDECIDED`. The unsettled set already exists as `TurnVerification.unverifiable` (exactly `_MACHINE_UNDECIDED`). `failures`, `compliant` and the checks do not change.

### 2. The dispatched-worker count

- `ExpansionResult.dispatched_count: int = 0`, a field. `_run_dispatch` and `_maybe_redispatch_on_gap` increment it on the result immediately before each `await run_sub_agent(...)`, inside the span. A skipped task never reaches the call. (Revised after codex review: `len(dispatch_intervals)` also counted a failed `phase_span` entry, and the intervals are assigned to the result only after the loop.)
- `ExecutionContext.sub_agents_dispatched: int = 0` (`orchestrator/types.py`).
- `step_init` sets `ctx.sub_agents_dispatched = expansion_result.dispatched_count` next to `ctx.sub_agent_results = ...` (executor.py ~5345).
- Why not `len(ctx.sub_agent_results)`: a worker that raised is not in that list.

### 3. `tool_rounds`

`ctx.tool_iteration_count`. `step_tool_execution` increments it (executor.py ~7035) before the malformed-JSON gate (~7250), the missing-name gate and the loop gate, so a malformed call gives `tool_rounds = 1` while `tool_results_offered` stays 0.

### 4. `_record_grounding` (executor.py)

- Computes `turn_shape = classify_turn_shape(verification, tool_results_admitted=..., tool_rounds=ctx.tool_iteration_count, sub_agents_dispatched=ctx.sub_agents_dispatched)`.
- Logs three new fields: `turn_shape` (value or None), `tool_rounds`, `sub_agents_dispatched`. The two counts are ungated (they describe the turn, not the span list), like `tool_results_offered`.
- Returns `turn_shape` so `step_synthesis` builds the note from the same value the event carries. Its only caller is `step_synthesis`.

### 5. `_unsourced_assertion_disclosure(verification, shape)` (executor.py)

Returns None when `shape is None` or `not verification.settled_failures`. Else:

```
{S} of {M} factual statements in this answer are not backed by a source Seshat verified this turn. {SHAPE}[ Seshat could not check {U} other statement(s).] Check them before you rely on them.
```

- S = `len(settled_failures)`, M = `len(spans)` (= `non_exempt_spans`), U = `len(unverifiable)`.
- SHAPE:
  - A: `Tools or sub-agents ran this turn, and Seshat cannot cite their output.`
  - B: `The sources this turn retrieved do not back these statements.`
  - C: `No tool and no sub-agent ran this turn.`
- The unsettled sentence appears only when U > 0. Singular "statement" when U = 1.

### 6. `step_synthesis` (executor.py ~7584)

- `ctx.grounding_disclosure = _unsourced_assertion_disclosure(verification, shape)` in both `observe` and `enforce` (no mode gate).
- In `enforce`, set it back to None on `RETRY_WITH_FORCED_RETRIEVAL` (the next attempt recomputes it) and on `TERMINAL_NO_SOURCE` (the refusal replaces the generation the verdict describes). Both branches are T2's to change. Under T1's `decide()`, an `enforce` turn with failures always retries or refuses, so the note becomes visible in `enforce` only after FRE-1509. This is recorded, not a gap in this ticket.
- `execute_task_safe` is unchanged: it appends `Note: {disclosure}` after `execute_task` wrote the capture.

## Tests (TDD — each written first and seen to fail)

File: `tests/personal_agent/orchestrator/test_executor_grounding.py` unless noted.

| AC | Test | Seed | Asserts |
|---|---|---|---|
| AC-1 | `test_unsettled_only_turn_carries_no_note` | observe, 3× `unverifiable_by_containment` | `_deliver` reply == bare reply, `grounding_disclosure is None` |
| AC-1 | `test_note_counts_settled_and_states_unsettled_apart` | observe, 2 settled (`uncited`, `not_contained`) + 3 unsettled (`unverifiable_by_containment` ×2, `entailment_unavailable`) | note starts `2 of 5 factual statements`, contains `Seshat could not check 3 other statements.` |
| AC-2 | `test_malformed_tool_call_turn_is_shape_a` | real `step_tool_execution` with `web_search` arguments `"{not json"`, then `step_synthesis` with an `uncited` verdict | `tool_rounds == 1`, `tool_results_offered == 0`, event `turn_shape == "a"`, note has the A sentence |
| AC-2 | `test_decompose_turn_whose_workers_all_raise_is_shape_a` | real `ExpansionController._run_dispatch` with `run_sub_agent` raising for both tasks → that `ExpansionResult` into `step_init` (DECOMPOSE gateway, controller patched as in `test_fanout_incomplete_pause.py`), then `step_synthesis` with an `uncited` verdict | `sub_agent_results == []`, `sub_agents_dispatched == 2`, `tool_rounds == 0`, `turn_shape == "a"`, A sentence |
| AC-2 | `test_image_attachment_turn_without_tool_call_is_shape_c` | user message with an `image_url` block, no tool round, no workers, `uncited` verdict | `turn_shape == "c"`, C sentence |
| AC-2 | `test_admitted_web_search_turn_is_shape_b` | real `step_tool_execution` with `web_search` returning content (admitted), then `uncited` verdict | `tool_results_admitted == 1`, `turn_shape == "b"`, B sentence |
| AC-3 | existing `test_observe_a_fully_cited_turn_carries_no_note` (kept) + `test_turn_with_no_shape_carries_no_note` | no spans → `turn_shape is None`, no note; unavailable → `turn_shape is None`, no note | |
| AC-4 | `test_declared_turn_note_stays_out_of_the_capture` | observe, settled failure; patch `write_capture` (the capture writer inside `execute_task`) or run the real `execute_task` capture path | captured `TaskCapture.assistant_response` lacks `Check them before you rely on them`; delivered reply has it |
| — | `tests/personal_agent/grounding/test_turn_shape.py` | unit table for `classify_turn_shape` + `settled_failures` over every `CheckOutcome` | |
| — | `test_expansion_controller.py::test_dispatched_count_includes_raised_workers` | all tasks raise | `dispatched_count == 3`, `sub_agent_results == []` |

**Why a registry-count-only implementation fails the first two AC-2 seeds.** A rule "C when `tool_results_offered == 0`" reads the malformed-call turn as C: the malformed gate calls `_record_undispatched_invocation`, never `register_tool_result`, so `tool_results_offered = 0`. It reads the all-raise DECOMPOSE turn as C: workers never touch the primary registry, and no primary tool round ran. Both tests assert `tool_results_offered == 0` to show the seed defeats that rule, and assert `turn_shape == "a"`. A second trap: `len(ctx.sub_agent_results)` is 0 on the all-raise seed, so the test asserts `sub_agent_results == []` beside `sub_agents_dispatched == 2`.

The suite, not one seed, defeats every count-only rule. A rule "B if admitted else A" passes the first two seeds, and the image seed (expected C) fails it.

**Existing tests to update (they encode the old rule):**
- `test_observe_a_zero_of_nine_turn_carries_the_unsourced_note` — exact text gains the C sentence.
- `test_observe_partial_compliance_counts_only_the_failures` — becomes `1 of 3` plus the unsettled sentence.

## Commands

```bash
make test-file FILE=tests/personal_agent/grounding/test_turn_shape.py
make test-file FILE=tests/personal_agent/orchestrator/test_executor_grounding.py
make test-k K=dispatched_count
make test && make mypy && make ruff-check && make ruff-format && pre-commit run --all-files
```

## Codex plan review — disposition

1. Interval count is not the dispatch count (blocking) — **accepted**, §2 revised.
2. A text-form tool call that the parser rejects leaves `tool_rounds = 0`, so the turn reads C (blocking) — **not accepted for this ticket.** ADR-0151's Implementation Notes define `tool_rounds` as `ctx.tool_iteration_count`, and AC-5 checks `turn_shape` against that field. A rejected text call never reaches the executor as a tool call. Recorded in the handoff as a known limit of the observable.
3. `tool_iteration_count` can be nonzero on an inconsistent direct entry (non-blocking) — no change. The state machine enters `TOOL_EXECUTION` only with a non-empty call list.
4. An admitted source with zero rounds — no defect.
5. `enforce` is not D2/D4-complete (blocking) — **not accepted.** The ticket scopes `decide()`, the retry and the refusal to FRE-1509. §6 records the split.
6. The first two seeds alone do not defeat a "B else A" rule (non-blocking) — **accepted**, the rationale above corrected.

## AC-5 (post-deploy, owner turn)

After the `seshat-gateway` rebuild, the owner runs one tool turn and one no-tool factual turn. For each `trace_id`: note S == count of settled members of `outcomes`, M == `non_exempt_spans`, shape sentence ↔ `turn_shape`, and `turn_shape` == D1 over `tool_results_admitted`, `tool_rounds`, `sub_agents_dispatched`.
