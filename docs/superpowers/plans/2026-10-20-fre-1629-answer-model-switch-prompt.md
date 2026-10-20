# FRE-1629 — The dispatcher answers its own "Switch model?" prompt

Ticket: FRE-1629 (Approved, bugfix grant). Related: FRE-1504 (wedge alert), FRE-1457 (held-prompt detector).
Tier: Standard (the change types a key into a live `acceptEdits` seat). Codex plan-review: required.

## Problem

On a reuse launch the dispatcher types `/clear`, `/model <alias>`, `/build <id>` into the seat.
If the ticket Tier differs from the seat model, Claude Code opens a blocking "Switch model?" prompt.
`deliver_to_seat` waits 10 s for the `/model` command, then returns `delivery-failed`.
Nothing answers the prompt. The seat holds until master presses Enter.

## Design

Answer the prompt inside the existing `/model` confirmation poll in `deliver_to_seat`.

- `pane_state.model_switch_prompt_open(pane_text)` is a new pure function. It returns `True` only when no live spinner is present and the active region holds ONE dialog: the text `Switch model?`, then within 8 lines the selector line `❯ 1. Yes, switch to` (option 1 selected, so a bare Enter picks it). A single ordered regex ties the title to the selector. Two independent substring tests would not prove one dialog.
- In `deliver_to_seat`, the `processed` poll for a command that starts with `/model ` behaves as follows (codex plan-review fixes):
  - It captures the pane FIRST and checks the modal BEFORE the RC `busy` shortcut.
  - If the modal is open and not yet answered, it sends one bare `Enter`, sets `answered`, and returns `False` at once. The FRE-923 resubmit path in the same poll cannot also fire.
  - For a `/model` command, RC `busy` is not accepted as proof of processing. `/model` is a client command. A transient `idle → busy → waiting` reading must not let `/build` reach an unseen modal. The pane (changed and idle) is the proof, with the existing idle-unchanged fallback.
  - At most one answer per command. Bounded by the existing `_DELIVERY_TIMEOUT_S`.
  - No other command and no other dialog is matched.
- Residual risk: the pane capture and the `Enter` are two tmux calls. If the owner answers the model prompt between them, and a different dialog appears at once, the Enter lands on it. The window is milliseconds. The ticket accepts this design. State it in the handoff.
- If the prompt is still open after the answer, the poll times out. The result is `delivery-failed`, and the wedge alert (FRE-1504) stays the fallback.
- `deliver_to_seat` returns `(outcome, model_switch_answered)`. `LaunchResult` gets a defaulted final field `model_switch_answered: bool = False`.
- The orchestrator adds `model_switch_answered` to the existing `dispatch_execute` log, which carries `trace_id`, `stream` and `ticket`. There is no separate event.

Why `Yes` is correct: the dispatcher sent `/model <alias>` because the ticket Tier asks for that model.

## Files

| File | Change |
|------|--------|
| `scripts/dispatch/pane_state.py` | add `model_switch_prompt_open`, export it |
| `scripts/dispatch/launcher.py` | answer in `deliver_to_seat`; `LaunchResult.model_switch_answered`; `execute_plan` passes it through |
| `scripts/dispatch/orchestrator.py` | log the answer |
| `tests/scripts/test_launcher.py` | AC-1, AC-2, AC-3 tests and `pane_state` unit tests |
| `tests/scripts/test_orchestrator.py` | the log test |

## Steps

1. Write failing tests in `tests/scripts/test_launcher.py` (`_ModelSwitchSeat` fake):
   - AC-1: modal pane after `/model` → events are `/clear`, Enter, `/model sonnet`, Enter, Enter, `/build FRE-1629`, Enter. Outcome `reuse`. `model_switch_answered` is true.
   - AC-2: permission pane ("Do you want to proceed?" with `❯ 1. Yes`) after `/model` → only the command's own Enter is sent after `/model`. Outcome `delivery-failed`. `/build` is never sent.
   - AC-3: no prompt → exactly three Enters, one per command. `model_switch_answered` is false.
   - Still-open: modal never clears → exactly one answer, then `delivery-failed`.
   - Transient RC `busy` while the modal is held → `/build` is not sent before the answer.
   - `pane_state` unit cases: modal true (plain, indented, wrapped explanation, command echo above); permission false; prose containing "Switch model?" with an unrelated `❯ 1. Yes` selector far below false; `Switch model?` with no selector false; `❯ 2. No` selected false; spinner present false.
   Run: `make test-file FILE=tests/scripts/test_launcher.py` → expect the new tests to fail.
2. Implement `model_switch_prompt_open` in `pane_state.py`.
3. Implement the answer, the tuple return and the `LaunchResult` field in `launcher.py`.
4. Add the orchestrator log and its test in `tests/scripts/test_orchestrator.py`.
5. Run `make test-file FILE=tests/scripts/test_launcher.py`, `make test-file FILE=tests/scripts/test_orchestrator.py`, `make test-file FILE=tests/scripts/test_gating_watcher.py` → all pass.
6. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

## Acceptance criteria and proof

| AC | Proof |
|----|-------|
| AC-1 | Step 1 modal test asserts the exact event order |
| AC-2 | Step 1 permission test asserts zero answer keys and no `/build` |
| AC-3 | Step 1 no-prompt test asserts exactly three Enters |
| AC-4 | Post-deploy, master: `dispatch_execute` with `model_switch_answered=true` in the next cross-tier launch log and no `dispatch_seat_wedged` |

## Risks

- A wrong match types Enter into a foreign dialog. The match needs both the title text and the exact `Yes, switch to` selector on option 1, and only runs while a `/model` delivery is in flight.
- Real pane text may differ from the fixture. The fixture is the shape that FRE-1504 recorded from a live pane. The post-deploy check (AC-4) confirms it.
- Diff class: dispatcher keystroke path. No production write path, no deletion, no schema, no cost or governance code. Self-serve.
