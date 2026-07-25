# FRE-976 — Dispatch daemon must reconcile in-flight ticket against Linear each tick

**Ticket:** FRE-976 (Approved, Tier-1:Opus, stream:build2) · **Refs:** ADR-0110/ADR-0113, lifecycle-rules § Dispatch
**Backing:** pipeline-reliability bug (no ADR acceptance table); reproducing test is the proof.

## Problem (confirmed by reading source)

`scripts/dispatch/next_resolver.fetch_board(stream, api_key)` queries Linear **server-side filtered by
the stream label** (`issues(filter: { labels: { name: { eq: $label } } })`). So each tick's board
snapshot passed to `run_once` contains **only issues that currently carry `stream:<mine>`**.

In `orchestrator._decide_launched`, `_state_of(issues, record.ticket)` returns:
- a real state when the launched ticket still carries the label (present in snapshot), OR
- **`None` when the ticket is absent from the label-filtered snapshot** — i.e. its stream label was
  removed.

The launched-decision ladder:
1. `normalized in _TERMINAL_STATES` → `clear` (works **only** when the label is kept, e.g. Done-with-label).
2. `in review` + PR → `run_complete`.
3. `in review`/`in progress` → `await`.
4. else (Approved/unknown, past timeout) → **`stall`** (notify-only, **never releases the slot**).

FRE-965 incident shape: merged → moved Done **directly** + **stream label removed** (PR-service outage,
no open PR). The ticket vanished from the label-filtered board → `_state_of` = `None` → not terminal,
not in-review/in-progress → fell to **stall** → notified once, then re-emitted `kind=stall
reason=no-pr-past-timeout` every tick **forever**, never advancing to the Urgent FRE-971. Manual
`dispatch_state.json` surgery was needed.

Root cause: **absence from the stream's label-filtered board is not treated as "no longer mine → release."**

## Fix (REVISED after codex review + live evidence — board-absence is unsafe)

The original board-absence approach (`state is None → clear`) was **rejected**: codex flagged that
absence is ambiguous, and live inspection proved it — `fetch_board` fetches `issues { nodes }` with **no
pagination**, and the stream label is **kept on Done tickets forever**, so the label-filtered board is
**already truncated at Linear's 50-node cap** (build1: 47 Done + 3 Awaiting = 50/50; build2: 42 Done).
A genuinely-In-Progress launched ticket can be paginated out → board-absence ≠ "label removed" → a false
release + double-dispatch. So the fix has two parts (owner approved folding the second into this PR).

### Part A — direct by-identifier reconciliation (core)
`next_resolver.fetch_issue_state(identifier, api_key)` — a DIRECT `issue(id:"FRE-965")` lookup (Linear
accepts the human identifier; verified), immune to the board's page cap and to label removal. Returns the
true state, or `None` (not-found / lookup-failure — inconclusive). `run_once` reconciles each launched
record's ticket via this (injected `reconcile` seam, `RuntimeError`→`None`+warn). `_decide_launched` now
drives off this `tracked_state`, not the board:
- confirmed terminal state → `clear` (reason `reconciled-terminal`) — the **only** release path; fires
  regardless of label presence, so it catches the FRE-965 shape (Done, label removed, no PR).
- `in review`+PR → `run_complete`; `in review`/`in progress` → `await`.
- `None` / Approved / other, past timeout → `stall` (notify) — **never released** on an inconclusive
  read (that is the double-dispatch risk codex named). `decide` stays pure (state injected).

### Part B — fix the board truncation (folded in, owner-approved)
`fetch_board` now (a) filters terminal state **types** server-side
(`state:{type:{nin:["completed","canceled","duplicate"]}}` — Awaiting Deploy is type `started`, KEPT)
and (b) paginates over `pageInfo.hasNextPage`. Result: build2 board 50→8, `hasNextPage:false`, and
completeness is guaranteed by construction, not assumed. `resolve_next` needs only Approved + In
Progress/In Review, all preserved; blocked-by states come from nested relations, unaffected.

**Release cadence:** `clear` pops the record; NEXT launches on the following tick via `_decide_no_record`
— identical to the proven normal-merge path. 5-min next-tick launch vs the hours-long wedge is the win.

**Deliberately out of scope (noted for master):** In Progress/In Review still `await` indefinitely
(codex's liveness observation) — pre-existing, not the wedge bug; a stall timer there risks false alarms
on long Opus builds. Follow-up candidate, not folded in.

## Tests (TDD — `tests/scripts/test_orchestrator.py`)

New failing test first (reproduces FRE-965 at the decision layer):
1. `test_decide_clear_when_launched_ticket_vanished_from_board` — launched record, board **omits** the
   ticket (label removed) → `kind == "clear"`, `reason == "label-removed"`. **Fails before the fix**
   (currently `stall`/`await`).
2. `test_decide_clear_on_terminal_with_label_kept` — Done and Canceled *with label kept* → `clear`
   (locks the other abnormal-lifecycle variants; complements existing Awaiting-Deploy test).

Regression (don't release too eagerly — pass before & after):
3. `test_decide_in_progress_no_pr_not_released_even_when_old` — launched record, board has ticket
   `In Progress` (label kept), `now` far past timeout, no PR → `await` (never `clear`).
4. Existing `test_decide_stall_on_silence_past_timeout` (Approved+label past timeout → `stall`) must
   still pass — proves the legit liveness-stall path is intact (normalized="approved" ≠ None, skips the
   new branch).

run_once level (incident reproduction end-to-end):
5. `test_run_once_releases_vanished_ticket_then_launches_next` — tick 1: launched record for the vanished
   ticket, board omits it → `state` releases (`"build1" not in state`). tick 2: board now = next Approved
   ticket → `run_once` launches it (asserts the slot advanced, not stall-looped).

## Verify
- `make test-file FILE=tests/scripts/test_orchestrator.py` (module, green)
- `make test` (full) · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`
- self-review (code-review `high` — orchestrator correctness) before PR.

## Acceptance criteria (Proof, from the ticket)
- **AC-1** Reproduce FRE-965 shape (launched ticket → terminal/label-removed, no open PR) → daemon
  **releases within one tick** and advances to the next eligible ticket, not stall-loop. → tests 1, 5.
- **AC-2** Regression: a genuinely-in-flight ticket (In Progress, no PR) still holds (does not release
  eagerly). → tests 3, 4.
