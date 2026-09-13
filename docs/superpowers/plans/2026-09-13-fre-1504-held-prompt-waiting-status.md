# FRE-1504 — the held-prompt wedge detector reads the wrong Remote Control status

Ticket: FRE-1504 (Tier-1, Urgent, Bug). Predecessor: FRE-1457 (PR #1106). Lesson source: FRE-1405.

## Root cause (AC-2), named before the fix

The link that suppressed the signal is `launcher.seat_is_busy`.

A seat held at an interactive prompt reports Remote Control `status: "waiting"`.
`seat_is_busy` maps only `busy` → True and `idle` → False. Every other status returns
`None`. `seat_wedge_reason` starts with `if seat_is_busy(...) is not True: return None`.
The orchestrator call site then calls `_reset_wedge`, which is a no-op on empty state.
`_note_wedge` is never called, so `dispatch_wedge_state.json` stays `{}`.

The dispatch itself still returns `seat-busy`. `deliver_to_seat` falls back to the pane
scrape when `seat_is_busy` is `None`, and a modal pane is not idle.

Observed values (2026-09-13, this worktree):

| Probe | Observed |
|-------|----------|
| Throwaway `claude --remote-control` seat, one turn of history, `/model sonnet` sent | pane shows `❯ 1. Yes, switch to Sonnet 5`; `claude agents --json --all` status = `waiting` on 6 reads over 18 s |
| Live `cc-adrs` seat held at a Bash permission prompt | status = `waiting`; `seat_is_busy` → `None`; `held_prompt_summary` → matches `❯ 1. Yes`; `seat_wedge_reason` → `None` |
| Real `run_once` tick × 3 against the held modal, unfixed main | `outcome=seat-busy` × 3, `wedge_state {}`, notify ledger empty |

Why FRE-1457's tests passed: every fixture seeds RC `status: "busy"` (`_WedgeRunner`,
`_agent("busy")`). Production never emits `busy` for a held prompt.

The same `waiting` value also explains the first `delivery-failed` attempt: `processed()`
reads `seat_is_busy` → `None`, the modal pane is not idle, and the poll times out.

## Design

1. `launcher.py`
   - Extract `_seat_rc_status(topology, runner) -> str | None`: the lowercased status of
     the single cwd-matched agent, else `None`. `seat_is_busy` uses it. Its behaviour
     does not change. `waiting` stays `None` there, because `deliver_to_seat.processed()`
     must never read a modal as "command accepted".
   - `seat_wedge_reason`:
     - `busy` → the existing logic (pane-idle, then held-prompt).
     - `waiting` → always `held-prompt`. RC's own status is the structured evidence. The
       summary is `held_prompt_summary(pane)`, or `pane_tail_summary(pane)` if no `❯ N.`
       selector matches (for example the unnumbered folder-trust dialog `❯ No, exit`).
     - any other status → `None`.
2. `pane_state.py` — add `pane_tail_summary(pane_text) -> str`: the last non-blank lines
   of the active region, capped at `_PROMPT_SUMMARY_MAX_CHARS`.
3. `orchestrator.py`
   - `WedgeState` gains `prompt_summary: str | None = None`. Persisted. `load_wedge_state`
     drops a record whose `prompt_summary` is not `str | None`. Old files still load.
   - `_note_wedge` (AC-3): past the threshold, a `held-prompt` tick whose prompt differs
     from the persisted one notifies at once. The ledger entry then names the prompt that
     blocks the seat now, not the one already answered. The count continues. The episode
     does not reset, so the seat never reads as recovered.
   - The comparison uses `_prompt_key`: the last `❯` selector line of the summary. Text
     around one static prompt can change between ticks, and a full-text comparison would
     notify on every tick. A stored record with no prompt (a file written before this
     change) counts as a first observation, not a change. (Codex plan-review, finding 4.)

`held-prompt` means any persistent state in which the seat waits for input. That
includes a question to the owner if it reaches the launch path. The detector only
reports after `wedge_ticks`, and it never answers or ends a seat, so this is accepted.
A tracked run does not reach this path: every non-`launch` decision resets wedge state
before FRE-1499's `dispatch_awaiting_owner` logic runs. (Codex plan-review, finding 2.)
   - `_WEDGE_DETAIL["held-prompt"]` text: "reports busy or waiting".

Out of scope: auto-answering prompts (ticket). Model aliases stay unpinned (owner, 19:02
UTC). FRE-1499's `seat_turn` false negative on the same panes is a separate classifier.

## Steps

1. Tests first — `tests/scripts/test_orchestrator.py`, `tests/scripts/test_launcher.py`.
   Run `uv run pytest tests/scripts/test_orchestrator.py tests/scripts/test_launcher.py -q`.
   Expect the new tests to fail on main.
2. Implement 1–3 above.
3. Re-run the same command. Expect all pass.
4. Re-run the live probe (`live_tick_probe.py`, scratchpad) against the held modal. Expect a
   `dispatch-notify:dispatch_seat_wedged:build1` entry that names `FRE-1504` and the prompt.
5. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`.

## Acceptance criteria

| AC | Proof |
|----|-------|
| AC-1 live path | Live probe: real `run_once`, real `claude agents`, real `tmux capture-pane`, a real held model-switch modal. Ledger entry names stream, ticket, prompt. Master repeats on the deployed daemon. |
| AC-2 root cause | This document, table above. |
| AC-3 prompt chain | `test_prompt_chain_second_prompt_is_detected_after_the_first_is_answered`, plus `test_drifting_text_around_one_static_prompt_does_not_renotify` and `test_pre_fre1504_wedge_record_does_not_renotify_off_its_missing_prompt` |
| `waiting` never confirms a delivery | `test_deliver_to_seat_never_types_into_a_waiting_seat` |
| AC-4 seeded negative | `test_advancing_spinner_writes_no_wedge_ledger_entry_across_ten_ticks` |
| AC-5 composition regression | `test_live_model_switch_modal_reaches_the_notify_ledger` — runs `run_once`; fails on main |
