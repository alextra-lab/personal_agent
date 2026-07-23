# FRE-939 — a gating send into a busy master pane must not be recorded as delivered

**Ticket:** FRE-939 (Approved, Urgent, `stream:build2`, Tier-1:Opus)
**Class:** standalone bug on the durable-actuation path. No backing ADR of its own; ambient design is
ADR-0113 §2 (durable trigger ledger) and ADR-0116 (event-driven dispatch actuation). The reproducing
test stands in for ADR provenance.
**Deploy class:** host systemd restart of the dispatch daemons. No gateway image, no schema.

---

## 1. The defect, located

`gating_watcher.send_to_session(session, command, runner, require_idle=False)`
(`scripts/dispatch/gating_watcher.py:755-792`) — the **master** trigger path
(`require_idle = trigger.kind != "master"`, line 1070) — checks only `tmux has-session` and then
injects. It **never captures the pane**, so it returns `"sent"` whenever the session merely exists,
whether master is at an idle prompt or mid-turn.

`run_once` (lines 1072-1085) treats that `"sent"` as confirmed delivery:

```
mark_sent → state[dedup_key] = now (arms the 6 h master TTL) → mark_consumed
```

Consequences, all three observed live on PR 602 (2026-07-21 12:33:17):

- The ledger holds **no unconsumed entry**, so `snapshot_unconsumed` / `trigger_ledger --unconsumed`
  (the surfacing read `prime-master` and the owner use) reports nothing wrong.
- The dedup store suppresses the same `master:<pr>:<sha>` key for 6 h, so the PR is not re-offered.
- Nothing escalates by age. The only detector left is a human noticing a still PR — which is the
  dependency every reconciler built this week exists to remove.

`require_idle=False` was deliberate (FRE-845/ADR-0116 §Context): the scrape once false-flagged an
**idle** master as busy and *dropped* the dispatch, so master delivery was made unconditional. That
decision is **not** reverted here — see §2, the send stays unconditional.

## 2. The fix — send unconditionally, but *record* what the pane said

The ticket asks first whether a busy pane is detectable at send time. It is: `session_is_idle` over
`capture-pane` is the same heuristic the worker path already trusts, and `pane_state.py` is
fail-safe-to-busy. Today the master path simply **never asks**. So the answer is not "start gating on
the scrape" (that is FRE-845's regression) but "**ask, still send, and stop claiming delivery we did
not observe**".

Three parts.

### Part 1 — a fourth send outcome, `queued`

`SendOutcome` becomes `Literal["sent", "absent", "busy", "queued"]`.

`send_to_session` with `require_idle=False` now captures the pane and:

| pane reads | keystrokes injected | returns |
|---|---|---|
| idle | yes | `"sent"` — delivery observed |
| busy | **yes** (unchanged) | `"queued"` — issued, delivery **not** observed |

`require_idle=True` (worker + context-pressure) is untouched: idle → `"sent"`, busy → `"busy"`, no
keystrokes. `absent` is unchanged on both.

The only new IO on the master path is one read-only `capture-pane`. No extra keystrokes on any path —
AC-4's "no additional keystrokes" is asserted directly on the recorded argv list.

**Failure-mode note (the FRE-845 tradeoff, deliberately taken).** A false-busy reading on a genuinely
idle master no longer drops anything: the keys still go in, exactly as today. It only leaves the entry
unconsumed, which costs a duplicate `/master N` on a later tick (the ticket states a duplicate gate
command is not harmful) and a possible age escalation. That is strictly safer than FRE-845's failure,
which lost the dispatch outright.

### Part 2 — a `queued` send does not consume the entry and does not arm the TTL

`LedgerEntry` gains `queued_at: float | None = None`, set when the pane read busy on a
`require_idle=False` send.

On `"queued"` in `run_once`, relative to the `"sent"` branch: **no** `mark_sent`, **no**
`state[dedup_key] = now`, **no** `mark_consumed`. The entry keeps `send_started_at` set,
`sent_at is None`, `consumed_at is None` — it is unconsumed, and therefore already visible through the
existing surfacing read (`snapshot_unconsumed`, `python -m scripts.dispatch.trigger_ledger
--unconsumed`) with no new plumbing. That alone satisfies AC-1.

`queued_at` exists because `reconcile`'s three-state crash model would otherwise misread this entry.
`send_started_at` set + `sent_at is None` currently means "crashed *during* the send call — ambiguous,
surface, never retry" (`trigger_ledger.py:255-260`). A queued entry is not a crash artifact: the send
demonstrably completed, only *receipt* is unknown, and it must stay re-offerable. So `reconcile` gains
one guard — `queued_at is not None and sent_at is None` → skip, the pass in Part 3 owns it — placed
**before** the `send_started_at` ambiguity branch. The crash model is otherwise untouched.

**`queued_at` must be persisted BEFORE the keystrokes, not after the send returns** (codex review #2).
Writing it after injection leaves an ordinary crash window — keys injected, `queued_at` not yet
durable — in which the entry reads as `send_started_at` set / everything else unset, i.e. exactly the
ambiguous-mid-send state `reconcile` marks terminally `surfaced`. That would permanently disable
delivery for that PR: a fresh variant of the very bug being closed. So `send_to_session` takes an
`on_queued: Callable[[], None] | None` hook, invoked **once, after the busy determination and before
injection**, on the `require_idle=False` busy path only. Putting the ordering inside `send_to_session`
— rather than trusting the caller to sequence it — is what makes the invariant unforgettable. The
caller's hook does `mark_queued` + `ledger_persist`.

Resulting crash windows on the master path:

| crash point | entry state on restart | reconcile verdict |
|---|---|---|
| before the pane capture | `send_started_at` only | ambiguous → `surfaced` (pre-existing, unchanged) |
| pane busy, after `mark_queued` persisted, any time around injection | `queued_at` set | skipped → Part 3 re-offers |
| pane idle, between injection and `mark_sent` | `send_started_at` only | ambiguous → `surfaced` (pre-existing, unchanged) |

### Part 3 — resolve unconfirmed entries each tick

New module-level helper in `gating_watcher.py`:

```python
def resolve_queued_triggers(
    ledger, *, now, open_pr_numbers, reoffer, escalation_s, escalated,
    ledger_persist, logger, trace_id,
) -> tuple[trigger_ledger.Ledger, tuple[str, ...]]
```

Policy lives in the watcher, not `trigger_ledger`: it needs PR openness and the tmux runner. The
ledger module keeps only durable-state primitives plus the crash model. Returns the updated ledger and
the event ids confirmed delivered this pass (so the caller arms its own dedup store — a return value,
not a callback, because it is a pure data need).

Called from `run_once` **after** `prs = board_fetcher()` and **before** the trigger loop, under
`execute`. For each entry with `queued_at is not None and sent_at is None and consumed_at is None and
surfaced_at is None`, in order:

1. **Obsolete → consume, on an authoritative read only.** A PR-ticketed entry (`ticket.isdigit()`,
   mirroring `prune_ledger`'s existing guard) whose PR is genuinely closed or merged is moot:
   `mark_consumed`, log `gating_queued_obsolete`.

   **Absence from `prs` is NOT sufficient evidence of closure** (codex review #1). `fetch_open_prs`
   caps enumeration at 50 (`_OPEN_PR_LIMIT`) and silently omits any PR whose `gh pr view` detail read
   fails (`_fetch_pr_detail` returns `None` → skipped). Consuming on absence would let one transient
   `gh` failure destroy a never-received trigger — and, because `prune_ledger` evicts a consumed
   numeric entry whose PR is not in the open list, destroy it *permanently and silently*. So:
   - present in `prs` → definitively open, not obsolete, **no extra IO**;
   - absent from `prs` → one authoritative `gh pr view <n> --json state` (`pr_is_closed`);
     `CLOSED`/`MERGED` → obsolete; `OPEN` → keep; **any read failure or unparseable state → keep**.
     Fail-safe direction is always *keep the entry*, never destroy it.

   The extra read is bounded by the number of unconfirmed entries, normally zero.

   A non-numeric ticket (a context-pressure entry, `ticket="cc-master"`) has no PR to close against
   and is never judged obsolete here — it resolves by re-offer or escalation. Running this step
   **first** is what keeps the healthy-but-slow case quiet: once master merges, the entry closes on
   the next tick.
2. **Re-offer, idle-gated (AC-3).** `reoffer(entry)` = `send_to_session(..., require_idle=True)`.
   - `"sent"` → delivery observed → `mark_sent` + `mark_consumed`, return the id so the caller arms
     the dedup TTL. Log `gating_queued_redelivered`.
   - `"busy"` → **zero keystrokes**, entry left unconsumed. This is the "do not add an unconditional
     retry / never a send loop into a busy pane" constraint: there is no per-tick re-injection and no
     backoff timer to mistune. (It is idle-*gated*, not race-free: the pane can turn busy between the
     capture and the send-keys, a narrow pre-existing window the worker path has always carried —
     codex review #3. The guarantee is "no send loop", not "no keystroke ever reaches a busy pane".)
   - `"absent"` → seat gone; left unconsumed for the age path.
3. **Escalate by age, once (AC-2).** `now - entry.created_at >= escalation_s` and `event_id not in
   escalated` → one distinct `logger.warning("gating_trigger_unconfirmed_too_long", pr=…, …)` naming
   the PR, then latch the id.

**The age clock cannot be reset — by construction, not by discipline (AC-2 / FRE-927).** Age is
measured from `created_at`, written exactly once by `record_pending`. The re-offer path never calls
`record_pending` and never rewrites any timestamp on the entry, so a repeated attempt has no way to
touch the clock. This is asserted directly by a test that re-offers twice and still escalates on the
original clock. Note the clock is only unresettable *while the entry survives*: a wrongly-consumed
entry would be pruned and later re-created fresh at `now` (codex review #4). That is one more reason
step 1 above never consumes on unauthoritative evidence.

**Latch is in-memory** (`escalated: set[str]`, allocated in `main()` outside `tick()`, passed in),
matching `orchestrator.held_escalated` (FRE-924) and its documented FRE-922 rationale: a *persisted*
crossing state can be first-observed already past its trigger after a restart and silently lose the
single alert forever. In-memory gives exactly-once per daemon run and at-least-once across restarts,
which is the correct direction to fail for an alert. `mark_surfaced` is deliberately **not** used —
it is documented terminal-pending/never-auto-retried, which would kill AC-3's re-offer.

**Threshold:** `DEFAULT_QUEUED_ESCALATION_S = 1800.0` (30 min), the same value and reasoning as
`orchestrator.DEFAULT_HELD_ESCALATION_S` — a fair window for a gate to complete without a premature
alarm, far short of the 9 h this ticket records. Exposed as `--queued-escalation-timeout`.

### Interaction with the existing trigger loop (no double-send)

While an entry is unconsumed, `decide` still yields the trigger (its dedup key was never armed), and
`record_pending` returns `"duplicate"` → the existing `gating_skip reason=ledger-duplicate` warning,
no send. So the resolve pass is the *only* thing that re-attempts. If a re-offer succeeds in a tick,
the caller arms `state[key]` before the trigger loop runs, so `classify_pr` suppresses it in the same
tick. Per-tick `ledger-duplicate` warnings while a PR sits unconfirmed are accurate and greppable, and
match the orchestrator's per-tick `card-already-surfaced` house style.

## 3. Scope boundary

The ticket's 2026-07-22 comment records a second occurrence of the same *class* on the dispatch
orchestrator's **reuse** path (FRE-926/build1), explicitly as "evidence, not a scope change request".
FRE-939's acceptance criteria are all on the gating path, so this PR fixes the gating path only. The
orchestrator reuse path is called out in the handoff as a live, unfixed sibling deserving its own
ticket — folding a second daemon's delivery semantics into an Urgent bugfix would bundle two
independent behaviour changes into one PR.

## 4. Files

| File | Change |
|---|---|
| `scripts/dispatch/trigger_ledger.py` | `SendOutcome` += `"queued"`; `LedgerEntry.queued_at`; `mark_queued`; `reconcile` skip guard; `load_ledger` / `_entry_to_json` carry the field; docstring updates |
| `scripts/dispatch/gating_watcher.py` | `send_to_session` returns `"queued"` + `on_queued` pre-injection hook; `run_once` `queued` branch; `resolve_queued_triggers`; `pr_is_closed`; `DEFAULT_QUEUED_ESCALATION_S`; CLI flag + `main()` latch; module docstring |
| `tests/scripts/test_trigger_ledger.py` | queued-vs-crash reconcile tests |
| `tests/scripts/test_gating_watcher.py` | AC-1..4 tests; **update** `test_send_master_injects_regardless_of_busy_pane` (it asserts the defective contract) |
| `docs/architecture_decisions/ADR-0116-…md` | one-line note that master delivery stays unconditional but is now *recorded* as unconfirmed |
| `docs/runbooks/dispatch-orchestrator.md` | how to read an unconfirmed gating trigger |

## 5. Steps (TDD)

1. Write the AC-1 test (busy master pane → unconsumed entry). Confirm it **fails** on current
   behaviour. → verify: `make test-file FILE=tests/scripts/test_gating_watcher.py` shows the failure.
2. `trigger_ledger`: `queued_at` + `mark_queued` + `SendOutcome` + reconcile guard + persistence.
   → verify: `make test-file FILE=tests/scripts/test_trigger_ledger.py` green.
3. `send_to_session` returns `"queued"`; update the stale master-injection test to the new contract.
4. `run_once` queued branch → AC-1 test green.
5. `resolve_queued_triggers` + wiring → AC-2 / AC-3 tests green.
6. AC-4 regression test: idle pane → exactly two send-keys calls, one consumed entry.
7. `main()` CLI flag + latch; docs.
8. Quality gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`; then `code-review` (high — live actuation daemon) and
   `security-review` (touches subprocess/tmux).

## 6. Acceptance criteria → proof

| AC | Test |
|---|---|
| 1. Busy-pane gating send produces **no** consumed ledger entry (must fail on current behaviour) | `test_run_once_master_busy_pane_leaves_entry_unconsumed` |
| 2. Unconsumed trigger past threshold surfaced **once**, naming the PR; clock from first attempt, unresettable by repeat attempts | `test_queued_escalates_once_naming_pr`, `test_queued_age_clock_not_reset_by_reoffer` |
| 3. Master-ready PR with no consumed trigger is eventually re-offered, not dropped | `test_queued_reoffered_when_pane_goes_idle`, `test_queued_busy_reoffer_sends_no_keys` |
| 4. Healthy path unchanged: idle pane → one delivery, one consumed entry, no extra keystrokes | `test_run_once_master_idle_pane_single_delivery_and_consume` |

Codex-review regressions, additionally proven:

| Hole | Test |
|---|---|
| Crash after injection, before `queued_at` persisted, must stay re-offerable (not `surfaced`) | `test_queued_marked_before_injection`, `test_reconcile_skips_queued_entry` |
| A `gh` read failure must never consume/destroy a queued entry | `test_queued_absent_from_list_but_pr_read_fails_keeps_entry`, `test_queued_absent_from_list_and_pr_open_keeps_entry` |
| A genuinely merged PR does consume | `test_queued_consumed_when_pr_authoritatively_merged` |
