# FRE-1499 — the dispatch machinery checks whether its own actuation worked

**Ticket:** FRE-1499 (Approved, Tier-1:Opus, stream:build2)
**Related:** FRE-1457 (`SeatWedgeSignal`), FRE-1245 (in-progress stall), FRE-1405 (notify ledger), ADR-0110, ADR-0116
**Class:** Standard/Complex. Touches `scripts/dispatch/` logic in two daemons. Codex plan-review required.

## 1. What the incident transcripts show

I read the seat transcripts under `~/.claude/projects/` before I designed anything.

**Fault A is real, and its cause is visible.** The build2 transcript
`0b7e28a2-….jsonl` holds 42 poke rows `PR #1144 failed CI checks - correct them`.
The first poke (05:54 UTC) got real work. From 12:03 UTC to 15:37 UTC, every poke
got one assistant row with the text `(No response.)` and `stop_reason: end_turn`,
about 1.5 s later. The model on those rows is `claude-haiku-4-5-20251001`.
The seat answered each poke with an empty turn.

**Fault B's causal claim does not hold.** Every poke row carries local
`sessionId: 0b7e28a2-…`, which is the FRE-1496 conversation. The id
`session_014Rrr8vHuBH4vaYSrKJynvZ` is the Remote Control bridge id of the seat.
That id does not change across `/clear`. This build2 seat carries the same bridge
id now, on FRE-1499, after a `/clear`. So the pokes did not land in the FRE-1493
conversation. The bridge id cannot identify a ticket's conversation. The local
transcript id can: `/clear` starts a new `*.jsonl` file.

**Fault C is real.** The adr transcript `a9e2d22e-….jsonl` ends a turn at
06:49:04 UTC with three numbered questions to the owner. The owner answered at
16:06 UTC. Its last line ("Tell me where you land, and push back on the framing
if it is wrong.") has no question mark. The questions are in the list above it.
A rule that reads only the last line misses this real case.

## 2. Design decisions

The ticket leaves four questions open. These are the answers this PR implements.

### D1 — proof that a poke worked (AC-1)

The watcher already sends a worker poke only into an idle seat. It re-arms the
poke after the 15 min lease. So a re-poke that becomes due proves three facts:
the seat is idle again, the PR is still red at the **same head SHA**, and the
seat left no ack marker. That is the definition of an ineffective poke. It needs
no transcript read, and it works for both transports (send-keys and channel).

The watcher records each delivered worker poke as a dedup-store entry
`poke:<pr>:<sha>:<n>` (value: the send epoch). The existing `prune_state`
already drops these entries by PR closure and TTL, because `_pr_of_key` reads
field 1. When a re-poke is due and `n ≥ 1` pokes exist at that SHA, the watcher
logs `gating_poke_ineffective` with the count. A new commit changes the SHA, so
the count restarts at zero.

### D2 — what happens after N ineffective pokes (AC-2)

`DEFAULT_WORKER_POKE_ESCALATION` = 2. When a worker candidate is due and the
seat has already received 2 pokes at that SHA, `classify_pr` returns a master
candidate with reason `worker-poke-ineffective` and dedup key
`escalate:<pr>:<sha>` (master TTL, 6 h). The command is a distinct prose message
to `cc-master`. It names the PR, the short SHA, the poke count, and the seat.
It states that the watcher stopped poking. No third identical poke is sent.

The escalation rides the existing master path: ledger-before-send, a busy
master pane defers (`queued`), and `resolve_queued_triggers` re-offers it.
Before the escalation is recorded, `run_once` reads the **worker** pane. If the
worker pane is busy, the seat is working. The watcher then skips this tick and
writes nothing. While the `escalate:` key is inside its TTL, worker pokes at
that SHA stay suppressed. After 6 h the escalation re-arms.

### D3 — idleness classification (AC-3)

New module `scripts/dispatch/seat_turn.py`. It reads the seat's **current**
transcript (`context_probe.resolve_jsonl`, newest `*.jsonl` for the pane's cwd)
and returns a frozen `SeatTurn`:

- `session_id` — the transcript file stem.
- `state` — one of:
  - `awaiting-owner` — the last main-chain assistant turn ended (`end_turn`) and
    its final text contains a question mark in its last 1 500 characters, or
    the last tool call is an unanswered `AskUserQuestion`.
  - `empty-response` — the turn ended with no text or with `(No response.)`.
  - `ended` — the turn ended with prose and no question.
  - `mid-turn` — the last main-chain message is a tool call, a tool result, or a
    user prompt with no reply.
  - `unknown` — no message rows.
- `question` — for `awaiting-owner`, the last line that contains `?` (200 chars max).
- `dispatched_tickets` — every ticket id in a `<command-args>FRE-…</command-args>` user row.

Sidechain rows (`isSidechain`) are skipped.

In the orchestrator `stall` case, for reason `in-progress-past-timeout`, the
orchestrator reads the seat. If the state is `awaiting-owner`, it emits
`dispatch_awaiting_owner` (with the question excerpt) **instead of**
`dispatch_stall`. For every other state it emits `dispatch_stall` as today, and
adds `seat_turn=<state>` to the fields so master sees `empty-response` or
`mid-turn` directly. The latch `in_progress_stall_notified` covers both events.
The top-of-`_apply` resolve also resolves `dispatch_awaiting_owner`.

### D4 — launch confirms which conversation it reached (AC-4)

The reinterpretation, stated plainly: the AC names a "session id". The only id
that identifies one ticket's conversation is the local transcript id (§1). The
PR therefore binds `DispatchRecord.session_id` to that id. Today the field is
always `None`.

For each `launched` record, on an `--execute` tick with a seat reader wired:

1. `session_id is None` and the seat's current transcript names the record's
   ticket in `dispatched_tickets` → bind `session_id`, persist, log `dispatch_session_confirmed`.
2. `session_id` is bound, the current transcript differs, and the new transcript
   names the ticket → rebind (a legitimate re-seed), log `dispatch_session_rebound`.
3. `session_id` is bound, the current transcript differs, and it does **not**
   name the ticket → notify `dispatch_session_mismatch` once (new record field
   `session_mismatch_notified`), warn every such tick.
4. The transcript matches again → clear the latch and resolve the ledger entry.

`clear` resolves `dispatch_session_mismatch`. The stall notifications gain a
`session_id` field, so an unbound session is visible in the stall itself.

### D5 — seams and wiring

- `orchestrator.run_once` and `_apply` gain `seat_turn_reader: Callable[[str], SeatTurn | None] | None = None`.
  `None` keeps today's behaviour, so existing tests never shell out to tmux.
  `main()` wires the production reader. A test proves the wiring.
- `gating_watcher.classify_pr`/`decide`/`run_once` gain `poke_escalation: int`
  with the module default, and `main()` gains `--worker-poke-escalation`.
- `load_state` accepts the new `session_mismatch_notified` field with a default,
  so a state file written before this PR still loads.

## 3. Acceptance criteria → proof

| AC | Test (outcome asserted) |
|----|-------------------------|
| AC-1 | `test_run_once_repoke_at_same_sha_is_recorded_ineffective` — a seat that never commits; tick 2 logs `gating_poke_ineffective` with `consecutive_pokes=1`, not only `gating_send`. |
| AC-2 | `test_run_once_escalates_to_master_after_ineffective_pokes` — tick 3 sends a message to `cc-master` whose text differs from the poke, and sends nothing to the worker. `test_run_once_escalation_skips_while_worker_busy` and `test_new_head_sha_restarts_poke_count`. |
| AC-3 | `test_run_once_in_progress_stall_awaiting_owner_is_a_distinct_signal` uses the real adr turn shape (questions in a numbered list, no `?` on the last line); a `mid-turn` seat still emits `dispatch_stall`. Parser tests in `tests/scripts/test_seat_turn.py`. |
| AC-4 | `test_run_once_binds_session_to_dispatched_transcript`, `test_run_once_surfaces_session_mismatch_once`, `test_run_once_rebinds_on_reseed_of_same_ticket`. |
| AC-5 | `test_seeded_negative_without_effectiveness_check_ac1_fails` (monkeypatch the poke count to 0 → the AC-1 assertion raises) and `test_seeded_negative_without_idle_classifier_ac3_fails` (a reader that always returns `None` → the AC-3 assertion raises). |

## 4. Steps

1. Write `tests/scripts/test_seat_turn.py` (fixtures built from the real row shapes in §1) → run it, confirm import failure.
2. Write `scripts/dispatch/seat_turn.py` → `make test-file FILE=tests/scripts/test_seat_turn.py` passes.
3. Add AC-1/AC-2 watcher tests to `tests/scripts/test_gating_watcher.py` → confirm they fail.
4. Implement D1/D2 in `scripts/dispatch/gating_watcher.py` → `make test-file FILE=tests/scripts/test_gating_watcher.py` passes.
5. Add AC-3/AC-4 orchestrator tests to `tests/scripts/test_orchestrator.py` → confirm they fail.
6. Implement D3/D4/D5 in `scripts/dispatch/orchestrator.py` → `make test-file FILE=tests/scripts/test_orchestrator.py` passes.
7. Add the AC-5 seeded negatives → pass.
8. Update `docs/runbooks/dispatch-orchestrator.md` (new signals and what master does with each) and `.claude/skills/prime-master/SKILL.md` step 5b if it lists event names.
9. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## 5. Codex plan-review (round 1) — disposition

1. **Channel delivery is not idle-gated (high) — accepted.** Before any re-poke or escalation (one or more pokes exist at the SHA), `run_once` reads the worker pane for every transport. A busy pane skips the tick. Tests: `test_repoke_to_busy_channel_seat_is_skipped`, `test_channel_pokes_are_counted`.
2. **Crash between `mark_sent` and the poke write (high) — accepted.** The `poke:` entry is written in the same `persist(state)` as the dedup key, so it adds no new crash window. The pre-existing window between `mark_sent` and that persist is unchanged.
3. **`_is_superseded` can retire a queued escalation (high) — rejected, with reason.** The only other entry with the same PR and target pane (`cc-master`) is `/master <pr>`. That trigger exists only when CI is green, and a green PR makes the escalation moot. Retiring the older entry is the correct outcome.
4. **Shared latch hides `awaiting-owner` (high) — accepted.** New record field `awaiting_owner_notified`. Each event has its own latch. Emitting one resolves the other's ledger entry.
5. **An unbound record is never surfaced (medium) — accepted.** Past `stall_timeout_s` (1 h) with `session_id` still `None`, the orchestrator emits `dispatch_session_mismatch` with `bound=false` and the seat's current session id and tickets. The mismatch latch covers both the unbound and the bound case.
6. **The question heuristic is noisy (medium) — partly accepted.** The regex needs a sentence-ending `?` (a URL query does not match). The residual noise is acceptable: the signal is informational, and a wedged seat reads `mid-turn`, never `awaiting-owner`. A missed question still emits `dispatch_stall` with `seat_turn=ended`.
7. **Dry-run side effects (medium) — accepted.** The transcript read, the binding, and the new notifications run only on `--execute` ticks.

## 6. Out of scope

- The Haiku empty-response behaviour itself. This PR makes it visible and escalated. The model choice for a seat is a dispatch-label matter.
- Killing or resetting a seat. Every new path detects and surfaces only (the FRE-922 posture).
