# FRE-923 — Dispatch delivery is not atomic: a partial CLEAR send strands the ticket

**Ticket:** FRE-923 (Urgent, Tier-1) · **Related:** FRE-920 (the live incident), FRE-922, FRE-924
**Files:** `scripts/dispatch/launcher.py`, `scripts/dispatch/orchestrator.py`,
`tests/scripts/test_launcher.py`, `tests/scripts/test_orchestrator.py`
**Codex plan-review:** run 2026-07-20; diagnosis + retry design confirmed, three gaps found and folded in (§Revisions).

## Diagnosis (What-item 3, resolved by code reading; confirmed by codex)

The ticket's framing — "the delivery is not verified end-to-end" — is **half wrong**, and getting this
right changes the fix.

`deliver_to_seat` (launcher.py:964) already verifies every command before sending the next, and already
fails closed. Critically, `session_is_idle` (pane_state.py:27) requires a **bare caret alone on its
line** (`^\s*❯\s*$`), so a command echoed into the input box renders `❯ /model sonnet` and reads as
**not idle**. Mere echo therefore cannot satisfy the non-final predicate.

So the observed end-state — `/clear` executed, model set, `/model sonnet` sitting **unsent** in the
input box, `/build` never sent — decodes to: the literal text landed but its **`Enter` was swallowed**.
`deliver_to_seat` polled 10s, saw a non-idle pane, correctly returned `delivery-failed`, and correctly
never sent `/build`. The launcher behaved as designed.

The leading mechanism is the ticket's own hypothesis — a settle race: `/clear` re-initializes the TUI
input widget, and `session_is_idle` can read idle before the widget is fully interactive again, so the
next command's Enter is consumed by the re-initializing widget while its characters buffer into the box.

**Therefore AC-2 already holds** (`execute_plan` returns `launched=False` on `delivery-failed`;
launcher.py:1124-1132) and needs a regression test, not a fix. **The genuine defect is AC-1**: the
orchestrator treats `delivery-failed` as terminal.

## Revisions after codex review

Codex confirmed Q1 (diagnosis, AC-2 already holds) and Q2 (retry bounded, no stream wedge, no
double-dispatch, wedge counter unaffected). It found three gaps, all folded in below:

1. **No durable in-flight marker (codex Q5).** `execute_plan` is strict within one process, but the
   orchestrator only persists *after* it returns (orchestrator.py:676-690). A daemon crash between
   typing `/model` and the poll leaves **no record that a delivery was in flight** — the next tick
   re-dispatches with a fresh attempt budget, and the ticket's own title ("delivery is not atomic") is
   only half-addressed. → **Pre-write a `delivering` record before `execute_plan`** (§A).
2. **`_pending_in_box` regex soundness unproven (codex Q3)** over real `capture-pane` output (ANSI,
   wrapping, truncation). → Verify `capture-pane -p` is ANSI-free empirically, and use a **tolerant**
   match rather than a strict full-line anchor (§B).
3. **`C-u` semantics on a Claude Code TUI unproven (codex Q4).** → **Do not send it unconditionally.**
   Send it only when stale text is actually observed, so the normal path is byte-identical to today
   (§B) — this also makes AC-3's "unchanged" claim exact rather than approximate.

## Changes

### A. Orchestrator — durable in-flight marker + bounded retry (AC-1)

`delivery-failed` is currently mapped to `surfaced` (orchestrator.py:493-499), and `_decide_surfaced`
(orchestrator.py:425) then returns `hold`/`card-already-surfaced` every tick until a human clears the
state file. That is what cost FRE-920 ~2.5 hours. But the seat in this failure is **idle and ready** —
the delivery dropped, the seat is fine — so it is retryable, unlike `seat-unhealthy`.

A single new phase covers both the crash case and the delivery-failed case, because on the next tick
they mean the same thing: *an attempt was consumed; re-attempt if budget remains.*

1. `DispatchRecord` gains `attempts: int = 0`. The default keeps `load_state`'s `DispatchRecord(**value)`
   (orchestrator.py:818) backward-compatible with existing on-disk state files.
2. New phase literal `"delivering"` = an attempt is in flight or has failed retryably.
3. **Pre-write**: in `_apply`'s launch case, *after* the `if not execute: return` guard (so a dry run
   still persists nothing) and immediately before `execute_plan`, write
   `DispatchRecord(stream, ticket, "delivering", now, attempts=prior+1)` and `persist`. `prior` is the
   existing record's `attempts` **only when it tracks the same ticket**; a different ticket starts a
   fresh budget.
4. **Post-reconcile**: `_record_for_result` maps the real outcome over the pre-written record —
   - `reuse`/`launch`/`prepare`/`registration-unverified` → `launched`, `attempts=0` (budget resets on success);
   - `delivery-failed` → stays `delivering` when `attempts < MAX_DELIVERY_ATTEMPTS` (3), else escalates
     to `surfaced` (today's terminal card, two ticks later);
   - `seat-unhealthy`/`manual-*` → `surfaced` immediately (genuinely needs a human);
   - `seat-busy`/`worktree-dirty`/`launch-failed` → `None`, which `_apply` already turns into
     `state.pop(stream)` (orchestrator.py:688-689) — **this is what un-does the pre-write** and
     preserves today's transient semantics exactly.
5. `decide` routes `phase == "delivering"` to `_decide_delivering`: if `attempts >= MAX_DELIVERY_ATTEMPTS`
   → `surface`; else if the ticket is still `Approved` and still this stream's NEXT → the same `launch`
   decision `_decide_no_record` would return, with `reason="retry-delivery"`; else `clear` (owner acted).

### B. Launcher — swallowed-Enter repair, conditioned on observed evidence (What-items 2, 3)

Both sub-changes fire **only on positive evidence of the broken state**, so a healthy delivery executes
exactly today's instruction sequence (AC-3 holds byte-for-byte).

1. `_pending_in_box(pane_text, command)` — tolerant match over the pane's trailing active region: find
   the last caret line, and treat the box as holding our command when its remainder is a prefix of, or
   prefixed by, `command` (tolerating truncation/wrapping rather than demanding an exact full-line
   anchor). Verified first that `tmux capture-pane -p` (no `-e`) emits no ANSI — the existing
   `session_is_idle` already depends on this live, and the plan step below confirms it empirically.
2. During the per-command poll, when `_pending_in_box` holds, **re-send `Enter`**, bounded to
   `_MAX_ENTER_RESUBMITS = 2` per command. Safe because it fires only when our own text is observably
   unsubmitted; if the Enter landed, the box no longer holds it.
3. Send `C-u` **only** when the box is non-idle and holds text that is *not* our command (genuine stale
   input, the `/model sonnet` residue), immediately before typing. Never on the healthy path.

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 — partial delivery retries, bounded, not an immediate terminal card | `test_delivery_failure_retries_before_surfacing` (→ `delivering`, attempts increments); `test_delivering_record_redispatches_next_tick` (next tick decides `launch`/`retry-delivery`); `test_delivery_failure_surfaces_after_max_attempts` (3rd → `surfaced`); `test_crash_mid_delivery_leaves_a_durable_in_flight_record` (pre-write persisted before `execute_plan`) |
| AC-2 — a partial send never records success | `test_partial_send_never_records_launched` — 2 of 3 commands land → `delivery-failed`, `launched is False` |
| AC-3 (regression) — a normal CLEAR is unchanged, one tick, no extra retries | existing `test_clear_reuse_delivers_clear_then_model_then_build_in_order`; new `test_successful_delivery_records_launched_with_no_attempts`; `test_healthy_delivery_sends_no_ctrl_u_and_no_extra_enter` (exact instruction-sequence equality) |
| B (repair) | `test_swallowed_enter_is_resubmitted`; `test_enter_is_not_resubmitted_when_command_was_accepted`; `test_stale_foreign_input_is_cleared_before_typing` |
| Transient outcomes unchanged | `test_seat_busy_pops_the_prewritten_record` (the pre-write must not wedge a transient outcome) |

## Steps

1. Empirically confirm `tmux capture-pane -p` emits no ANSI on a live pane (one read-only capture on a
   scratch pane — **not** a worker seat); record the finding in the handoff.
2. Write failing tests for AC-1/AC-2/AC-3 and B → confirm they fail.
3. Implement A (record + pre-write + retry decision).
4. Implement B (pending-in-box detection, Enter re-submit, conditional C-u).
5. Update `test_non_self_clearing_seat_failures_are_surfaced` — it currently parametrizes
   `delivery-failed` as immediately-surfaced, which AC-1 deliberately changes. `seat-unhealthy` keeps
   the old assertion.
6. Quality gates: `make test-file FILE=tests/scripts/test_launcher.py`,
   `make test-file FILE=tests/scripts/test_orchestrator.py`, `make test`, `make mypy`,
   `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`; then code-review (high — src
   logic, live dispatch path) + security-review (subprocess/tmux surface).

## Risks

- **Retry loop.** Bounded by `MAX_DELIVERY_ATTEMPTS=3`, escalating to the existing `surfaced` card, so
  the worst case is today's behaviour delayed by two ticks.
- **Pre-write wedging a transient outcome.** The `None` → `state.pop` path un-does it; covered by
  `test_seat_busy_pops_the_prewritten_record`.
- **Enter re-submit double-submitting.** Fires only on observed pending-in-box; bounded to 2.
- **Collision with FRE-924** (`build2`), which touches the same orchestrator record/decision surface for
  surfaced-dispatch age escalation. Whichever merges second rebases; flagged in the handoff.

## Out of scope

No change to `seat-unhealthy`, the FRE-922 wedge counter, or stall detection.
