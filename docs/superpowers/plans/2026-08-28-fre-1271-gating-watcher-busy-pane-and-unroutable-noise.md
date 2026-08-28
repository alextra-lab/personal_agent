# FRE-1271 — gating watcher: defer master sends into a busy pane, quiet unroutable noise, expose consumed-ledger reads

**Ticket:** FRE-1271 (Approved, High, `stream:build2`, Tier-2:Sonnet)
**Class:** Standard — touches `scripts/dispatch/gating_watcher.py` and `scripts/dispatch/trigger_ledger.py`,
both part of the live `seshat-gating-watcher.service` actuation daemon. Codex plan-review required.
**Deploy class:** host systemd restart of `seshat-gating-watcher.service`. No gateway image, no schema.
**Backing history:** FRE-939 (`docs/superpowers/plans/2026-07-23-fre-939-gating-busy-pane-delivery.md`) built
the `queued`/unconfirmed-delivery machinery this ticket reuses. FRE-845 (`git log --grep FRE-845`) is why
master sends were made unconditional in the first place — that history is addressed in §2 below, not ignored.

---

## 1. Symptom 1 — stale re-gate into a busy master pane

### 1.1 Where the defect actually is

`send_to_session` (`scripts/dispatch/gating_watcher.py:837-907`), on the master path
(`require_idle=False`), already captures the pane and correctly detects busy
(`session_is_idle(pane.stdout)` is `False`). But it **still injects the keystrokes** on that branch:

```python
if not session_is_idle(pane.stdout):
    if require_idle:
        return "busy"
    outcome = "queued"
    if on_queued is not None:
        on_queued()
# falls through unconditionally:
runner(["tmux", "send-keys", ...])
runner(["tmux", "send-keys", ..., "Enter"])
return outcome
```

`mark_queued` durably records the busy read (correct, FRE-939), but the send happens anyway. That
send is what the ticket calls "firing into the buffer" — the command lands in Claude Code's input
queue and submits once master's current turn ends, which can be minutes later, by which point master
may have already gated the same PR through its own scan. `resolve_queued_triggers` (already built,
lines 1052-1210) checks PR-closed obsolescence and re-offers idle-gated **but only for entries that
reach it still unconsumed on a *later* tick** — a `queued` entry created and immediately injected in
the *same* tick never gets that check before delivery, which is exactly the gap the ticket names:

> "What is missing is holding or re-evaluating the send rather than firing into the buffer, so that
> the obsolescence check that already exists can actually suppress the trigger."

### 1.2 The fix

Move the `return` up: when `require_idle=False` and the pane reads busy, call `on_queued()` (still
writes `queued_at`, still ledger-before-any-action) and **return `"queued"` without injecting**.
Idle → unchanged (`send-keys` fires, returns `"sent"`). `require_idle=True` (worker path) is
untouched.

```python
pane = runner(["tmux", "capture-pane", "-t", exact_pane(session), "-p"])
if not session_is_idle(pane.stdout):
    if require_idle:
        return "busy"
    if on_queued is not None:
        on_queued()
    return "queued"
runner(["tmux", "send-keys", "-t", exact_pane(session), "-l", command])
runner(["tmux", "send-keys", "-t", exact_pane(session), "Enter"])
return "sent"
```

That's the entire behavioral change. Everything downstream — `resolve_queued_triggers`'s obsolescence
check, its idle-gated re-offer, its 30-minute age escalation, `reconcile`'s "skip a queued entry"
guard — is **pre-existing, already tested** machinery (`test_queued_reoffered_when_pane_goes_idle`,
`test_queued_busy_reoffer_sends_no_keys`, `test_queued_escalates_once_naming_pr`, etc.). Today it only
ever runs on an entry that was *first* injected unconditionally on some earlier tick (an old-shape
`queued` record predates this ticket only via a live crash mid-flight — the ordinary busy-pane path
never produced one that reached a second tick still unconsumed, because the send always "succeeded").
After this change, **every** busy-pane master trigger enters that exact same resolution loop, one tick
sooner, before any keystroke is ever sent. Symptom 1's root cause — decide → busy → inject anyway —
is closed by construction, not by adding new state machinery.

### 1.3 The FRE-845 question, addressed directly

FRE-845 (`git log --grep FRE-845`, `docs/architecture_decisions/ADR-0116…`) is why master delivery was
made unconditional: `session_is_idle`'s scrape once false-flagged a genuinely **idle** master as busy,
and gating the send on that reading dropped the dispatch outright — master sat idle three hours with
two PRs ready. FRE-939's plan doc explicitly declined to revisit that call: "the answer is not 'start
gating on the scrape' … that decision is not reverted here."

This ticket **does** make the first attempt idle-gated. Is that the same regression? No — for two
reasons that hold independently:

1. **FRE-939 already made this exact trade for every re-offer**, not just new ones. `_retry_pending`
   (`gating_watcher.py:1291-1292`) calls `send_to_session(entry.target_pane, entry.command, runner)`
   with the **default** `require_idle=True` — every queued entry's re-delivery has been idle-gated
   since FRE-939 shipped (2026-07-23, a month before this ticket was filed), and that design was
   accepted with its own explicit note: "That is strictly safer than FRE-845's failure, which lost the
   dispatch outright." This ticket applies the *same, already-shipped* trade-off to the first attempt
   instead of only the second-and-later ones — it is not a new risk being introduced, it is closing an
   inconsistency where attempt #1 uniquely bypassed a safety property every later attempt already had.
2. **The liveness backstop is unchanged and still fires.** `resolve_queued_triggers`'s age escalation
   (`DEFAULT_QUEUED_ESCALATION_S = 1800.0`, 30 min) already logs `gating_trigger_unconfirmed_too_long`
   naming the PR if a queued entry is never successfully re-offered — that is *strictly more visible*
   than FRE-845's original failure mode (zero signal for three hours; here, an actionable log within
   30 minutes, and the PR keeps being retried every idle-gated tick in the meantime, not blocked).
   Nothing about this change makes a false-busy read on master *worse* than what FRE-939 already
   shipped for every second-and-later attempt; it only removes the special case where the very first
   attempt injected blind.

**Codex plan-review caught a real gap in the above and it is fixed here, not waved off.** The escalation
branch (`resolve_queued_triggers` step 3, `gating_watcher.py:1198-1209`) *only logs* — it never forces
delivery, never changes ledger state. Point (1) above is true (re-offers were already idle-gated) but
insufficient on its own: **today**, the very first master attempt is unconditional, so a persistently
false-busy detector cannot block *initial* delivery — worst case it fails to *confirm* delivery, which
is what FRE-939 already handles. After naively idle-gating the first attempt too, a persistently
false-busy detector genuinely *can* block delivery indefinitely, with nothing but a once-only log line
as a backstop. That is a real regression risk for a trust-ladder gating path, not an acceptable one.

**The actual fix: bound the deferral with a forced-delivery fallback, not just a log.** Step 3 keeps its
existing one-shot warning (still valuable, still fires once) but now *also* attempts an unconditional
delivery — bypassing the idle check entirely, exactly like the pre-fix behavior — once an entry has
been unconfirmed past `escalation_s` (1800s / 30 min). Unlike the warning, this force-attempt is **not**
one-shot: it retries every tick past the threshold until it succeeds (session exists) or the entry
resolves via obsolescence. This preserves the never-permanently-drop guarantee exactly (bounded to a
30-minute worst case instead of immediate), while still giving the obsolescence check ~30 idle-gated
ticks — the entire point of this ticket — to catch the common case (a busy pane that frees within
minutes, or a PR master already merged through its own scan) before ever falling back to a blind send.

A new primitive, `_force_deliver`, mirrors `send_to_session`'s existing-session safety check
(`exact_session`/`exact_pane`, FRE-909 dead-seat guard) but skips the pane capture and idle check
entirely — it is the pre-FRE-939 unconditional-inject behavior, deliberately reserved for this one
call site:

```python
def _force_deliver(session: str, command: str, runner: CommandRunner) -> Literal["sent", "absent"]:
    if runner(["tmux", "has-session", "-t", exact_session(session)]).returncode != 0:
        return "absent"
    runner(["tmux", "send-keys", "-t", exact_pane(session), "-l", command])
    runner(["tmux", "send-keys", "-t", exact_pane(session), "Enter"])
    return "sent"
```

`resolve_queued_triggers` gains a `force_deliver: Callable[[LedgerEntry], Literal["sent", "absent"]]`
parameter (wired from `run_once` the same way `reoffer` already is). Step 3 becomes:

```python
if now - entry.created_at >= escalation_s:
    if event_id not in escalated:
        escalated.add(event_id)
        logger.warning("gating_trigger_unconfirmed_too_long", ...)  # unchanged, still one-shot
    force_outcome = force_deliver(entry)
    if force_outcome == "sent":
        ledger = trigger_ledger.mark_sent(ledger, event_id, now)
        ledger_persist(ledger)
        ledger = trigger_ledger.mark_consumed(ledger, event_id, now)
        ledger_persist(ledger)
        delivered.append(event_id)
        logger.info("gating_queued_forced_after_escalation", trace_id=trace_id, event_id=event_id,
                     pr=entry.ticket, session=entry.target_pane)
```

This only runs when step 1 (obsolescence) has *already* found the PR not (authoritatively) closed and
step 2 (idle-gated reoffer) has *already* failed this tick — so by construction, force-delivery is the
last resort after both safer paths were tried this tick, not a bypass of them.

This point (with the escalation-then-force design) goes in the PR body verbatim — master should not
have to re-derive it at gate time.

### 1.4 Files / call sites touched

- `scripts/dispatch/gating_watcher.py`
  - `send_to_session`: hold-not-inject on busy+`require_idle=False` (above).
  - `send_to_session` docstring: `require_idle=False` outcome table, `on_queued` timing note (no
    longer "before injection" — now "instead of injection").
  - `run_once`'s `outcome == "queued"` branch (~line 1421): rename log event
    `gating_send_unconfirmed` → `gating_send_deferred` (`reason="target-busy"` kept), update the
    comment — no longer "issued, receipt not observed", now "held, not injected, awaiting a safe
    idle re-offer".
  - Module docstring §"Unconfirmed delivery (FRE-939)" (lines 51-63): update to describe deferral,
    not unconfirmed receipt. Note FRE-1271 alongside FRE-939.
- `scripts/dispatch/trigger_ledger.py`
  - Module docstring §"Unconfirmed delivery — the fourth state (FRE-939)" (lines 29-43): same
    semantic update — `queued_at` now means "held, not yet injected" rather than "injected, receipt
    unknown".
  - `LedgerEntry.queued_at` docstring, `mark_queued` docstring: same.
- `docs/runbooks/dispatch-orchestrator.md` §"Unconfirmed delivery — the `queued` state (FRE-939)"
  (lines 325-355): update prose to match (a live runbook, not a point-in-time plan doc — this one
  gets updated).

### 1.5 Tests

Update (behavior inverts):
- `test_send_master_injects_regardless_of_busy_pane` → rename
  `test_send_master_holds_when_busy_pane_pending_resolution`; assert `== "queued"` **and zero**
  `send-keys` calls (was: asserting the two `send-keys` calls happened).
- `test_send_master_on_queued_hook_fires_before_any_keystroke` → simplify: hook fires exactly once,
  zero `send-keys` calls total (there's no longer a "before" to prove — there's no keystroke on this
  path at all). Keep as a distinct test from the reconcile-ordering one below since it pins a
  different invariant.
- `test_run_once_master_sends_even_on_busy_session` → rename
  `test_run_once_master_holds_when_busy_session_no_keys_sent`; replace the `sends` assertion with "no
  send-keys calls"; ledger entry has `queued_at` set, `sent_at`/`consumed_at` both `None`. Update the
  docstring — this is no longer an FRE-845 no-regression test for *this* call site (that guarantee now
  lives in §1.3's re-offer path + escalation, covered by the pre-existing FRE-939 tests).
- `test_run_once_master_busy_pane_leaves_entry_unconsumed` → drop the final "keystrokes still went in"
  assertion; add "no send-keys calls occurred"; update the docstring's closing sentence (no longer
  "the keys still go in").
- `test_queued_escalates_once_naming_pr` → the tick at `now=1900` (1800s past `created_at`) now *also*
  force-delivers (busy runner still has `has-session` return 0, so `_force_deliver` succeeds). Update
  to assert: the warning still fires exactly once (unchanged), **and** after that tick the entry is
  `sent_at`/`consumed_at`-set with exactly one `send-keys` pair recorded, **and** the third tick
  (`now=5000`) neither re-alerts nor re-sends (entry already consumed).
- `test_queued_age_clock_not_reset_by_reoffer` → the final tick in the loop (`now=1900`) now also
  force-delivers. Add an assertion that after the loop, the entry is `sent_at`/`consumed_at`-set and
  exactly one `send-keys` pair was recorded (on that last tick only) — `created_at` staying at `100.0`
  and the single-alert assertion are otherwise unchanged.

Add (proves the codex-flagged gap is actually closed):
- `test_queued_force_delivers_after_escalation_when_pane_never_goes_idle` — a queued entry whose pane
  reads busy on every tick, driven past `escalation_s`: asserts the entry is eventually `sent_at`/
  `consumed_at`-set via forced delivery (not left permanently unconsumed), directly refuting the
  "log a warning is not equivalent to the previous always-eventually-send guarantee" finding.
- `test_queued_not_force_delivered_before_escalation_threshold` — same busy-forever pane, ticks that
  stay under `escalation_s`: zero `send-keys` calls, entry stays unconsumed (regression pin so the
  bounded-fallback doesn't quietly become unconditional again).

Add:
- `test_run_once_master_busy_first_tick_then_pr_merged_next_tick_never_sends_keys` — the ticket's
  actual reproduction, end to end: tick 1 (`now=100`, busy pane, PR open+ready) → queued entry, zero
  `send-keys`. Tick 2 (`now=160`, still busy — irrelevant; the obsolescence read now returns
  `state=MERGED`, simulating master having merged it independently) → `resolve_queued_triggers`
  consumes the entry as `gating_queued_obsolete`, **zero `send-keys` calls across both ticks**. This is
  the proof that matters for master at the PR gate.
- `test_run_once_master_busy_first_tick_then_idle_next_tick_delivers` — same shape, tick 2 pane goes
  idle instead → exactly one `send-keys` pair, on tick 2, confirming the "no permanent drop" side
  holds through the full two-tick flow (not just the pre-seeded-ledger unit tests that already cover
  the resolution pass in isolation).

`make test-file FILE=tests/scripts/test_gating_watcher.py` must be green with these changes before
moving to §2.

---

## 2. Symptom 2 — unroutable decision re-logged every tick

### 2.1 The gap

`run_once`'s trigger loop (~line 1344):

```python
if trigger.session is None:
    logger.warning("gating_skip", trace_id=trace_id, reason="unroutable", pr=trigger.pr)
    continue
```

No ledger write, no dedup-state write — this branch is reached and logs *before* `record_pending` is
ever called. A master-authored docs PR (no owning worker stream) re-derives and re-logs the identical
`gating_decision` + `gating_skip reason=unroutable` pair every tick (60s) for as long as it stays open
— ~35 log lines for one 35-minute-lived PR, per the ticket's own journalctl evidence. The `gating_skip`
line itself is correct; only its *repetition* is the defect (it buried the signal that mattered for
diagnosing Symptom 1).

### 2.2 The fix

Reuse the same `sent`/`state` dedup dict and `_suppressed` helper the master/worker TTL suppression
already uses — sticky per `(pr, head_sha)`, self-healing the same way `master:*`/`worker:*` keys do:

```python
DEFAULT_UNROUTABLE_LOG_TTL_S: float = DEFAULT_MASTER_TTL_S  # 6h — "logged once" for a PR's normal lifetime
```

The suppression check has to run **before** `gating_decision` is logged, not just before
`gating_skip` — the ticket's own evidence shows both lines recurring every tick, and deduping only the
second one leaves half the noise in place. And the dedup-state write must be **conditional on
`execute`** (codex plan-review flagged this): this loop runs before the `if not execute: continue`
gate, so an unconditional `persist(state)` here would give dry-run ticks a real side effect, breaking
the "execute=False → zero side effects" invariant every other path in this module holds
(`test_run_once_dry_run_sends_nothing`, `test_queued_untouched_in_dry_run`).

```python
for trigger in triggers:
    if trigger.session is None:
        unroutable_key = f"unroutable:{trigger.pr}:{trigger.head_sha}"
        if execute and _suppressed(state, unroutable_key, now, unroutable_ttl_s):
            continue  # fully silent this tick — both lines already logged within this SHA's window
    logger.info("gating_decision", trace_id=trace_id, kind=trigger.kind, reason=trigger.reason,
                pr=trigger.pr, session=trigger.session, command=trigger.command)
    if trigger.session is None:
        logger.warning("gating_skip", trace_id=trace_id, reason="unroutable", pr=trigger.pr)
        if execute:
            state[f"unroutable:{trigger.pr}:{trigger.head_sha}"] = now
            persist(state)
        continue
    if not execute:
        continue
    ...  # existing send path, unchanged
```

Dry-run (`execute=False`) keeps its exact pre-existing behavior: both lines log every tick, nothing is
persisted — the suppression only ever activates in `execute=True` runs, which is the only mode that can
observe repeated noise across real ticks anyway.

`unroutable_ttl_s: float = DEFAULT_UNROUTABLE_LOG_TTL_S` becomes a new `run_once` parameter — no new
CLI flag, matching the existing precedent (`context_pressure_ttl_s` has no CLI override either, only
its threshold does). `prune_state`'s `max_ttl_s=max(...)` call at the bottom of `run_once` folds in
`unroutable_ttl_s` so these keys age out like every other dedup entry.

A new head SHA (a fresh push to the same unroutable PR) mints a different key and logs immediately —
"sticky per head SHA" per the ticket's own wording, not sticky per PR forever.

### 2.3 Tests

Add to `test_gating_watcher.py`:
- `test_run_once_unroutable_logs_once_per_head_sha_across_ticks` — two `run_once` calls, same PR,
  shared `state` dict threaded through via `persist`, `_CapturingLogger`. First tick: exactly one
  `gating_decision` info + one `gating_skip reason=unroutable` warning. Second tick (`now` within
  TTL): zero of either.
- `test_run_once_unroutable_logs_again_after_ttl` — third tick past `unroutable_ttl_s` → logs again.
- `test_run_once_unroutable_logs_again_for_new_head_sha` — second tick uses a PR fixture with a
  different `head_sha` → logs immediately despite the first key still being within TTL.
- `test_run_once_unroutable_dry_run_never_persists_state` — `execute=False`, two ticks, same PR: both
  ticks log (dry-run never suppresses), and the `state` dict passed to `persist` is never mutated —
  pins the codex-flagged dry-run-side-effect fix.
- `test_run_once_ledger_untouched_for_unroutable_worker` (existing) must stay green unmodified — this
  fix only touches the plain dedup `state` dict, never the durable ledger.

---

## 3. Symptom 3 — no way to tell a watcher-sent invocation from owner-typed input after the fact

### 3.1 The gap

`trigger_ledger.py`'s CLI only supports `--unconsumed` (`main()` hard-refuses anything else:
`parser.error("--unconsumed is required (the only supported read today)")`). A **consumed** entry —
i.e. one the watcher already successfully sent — is invisible to this read. That is exactly the
failure the ticket's "why this matters" section names: on the first FRE-1271 occurrence, master
checked `--unconsumed`, found nothing, and concluded the `/master 928` it had just processed was
manual owner input. It was not — the watcher had sent it and the entry was already consumed.

### 3.2 The fix

Add `--all` as a sibling read mode to `--unconsumed` (mutually exclusive group, one of the two
required — mirrors the `--once`/`--loop` group pattern already used in `gating_watcher.main()`):

```python
mode = parser.add_mutually_exclusive_group()
mode.add_argument("--unconsumed", action="store_true", help="...")
mode.add_argument(
    "--all", action="store_true",
    help="Print every entry, including consumed ones, so a receiving session can tell a "
    "watcher-sent invocation from owner-typed input after the fact.",
)
...
if not (args.unconsumed or args.all):
    parser.error("one of --unconsumed or --all is required")
entries = snapshot_unconsumed(ledger) if args.unconsumed else tuple(ledger.values())
```

`--unconsumed`'s existing output format (JSON and text) is untouched — don't risk the runbook's
documented grep patterns. `--all`'s text output additionally distinguishes **why** an entry closed:
add a small `_entry_state` helper used by both branches —

```python
def _entry_state(entry: LedgerEntry) -> str:
    if entry.surfaced_at is not None:
        return "surfaced"
    if entry.consumed_at is not None:
        return "sent" if entry.sent_at is not None else "abandoned"
    if entry.queued_at is not None:
        return "queued"
    return "pending"
```

(`--unconsumed`'s printer only ever sees `surfaced`/`queued`/`pending` today since consumed entries
are already filtered out by `snapshot_unconsumed` — swapping its inline if/elif for a call to this
helper is a behavior-preserving refactor, not a new branch, so `--unconsumed`'s existing tests are
unaffected.) `--all`'s line additionally prints `sent_at`/`consumed_at` so a receiving session can
line up a specific ledger entry against the moment it received a command:

```
{event_id} [{state}] ticket={ticket} target={target_pane} sent_at={sent_at} consumed_at={consumed_at}
```

Module docstring's `Callable by hand` example gains a second line:
`python -m scripts.dispatch.trigger_ledger --all --json`.

**Two gaps codex plan-review found, both fixed here:**

1. **`_entry_to_json` omits `consumed_at`** (`trigger_ledger.py:444-459`) — so `--all --json` cannot
   tell a "sent" entry from an "abandoned" one (both can read `sent_at: null`; only `consumed_at`
   distinguishes "never attempted"/"queued" from "closed out without ever sending"). Add
   `"consumed_at": entry.consumed_at` to the dict. This is additive to the JSON shape — no existing
   consumer parses this output positionally, and `--unconsumed --json` gains the field too (harmless:
   every entry it returns already has `consumed_at: null` by definition).

2. **`prune_ledger` evicts a consumed PR-ticketed entry the instant its PR closes, regardless of
   `retention_s`** (`trigger_ledger.py:398-435`, pinned by the existing
   `test_prune_drops_consumed_entry_for_closed_pr`) — and the daemon prunes every tick
   (`gating_watcher.py:1719-1729`), so a `/master N` entry can vanish within ~60 seconds of master
   merging N. **Deliberately not changed by this ticket.** The reported incident (`master concluded the
   invocation was manual`) happened *while master was still processing* the trigger — before the PR
   closed, so the entry was still on disk and `--all` would have shown it. Inverting the closed-PR
   eviction to honor `retention_s` instead would be a real, separately-reviewable retention-policy
   change (7 days of every consumed entry instead of near-zero for closed PRs) with its own disk/audit
   trade-offs, and it has its own dedicated existing test asserting the current behavior — widening this
   ticket to change it risks a regression nobody asked for. Documented as a known limitation in
   §3.4 below and in the runbook update; a follow-up ticket is warranted only if someone actually needs
   long-after-the-fact (not same-turn) attribution, which is not what FRE-1271 reports.

### 3.4 Documented limitation (runbook)

`docs/runbooks/dispatch-orchestrator.md`'s retention paragraph gains one sentence: `--all` shows
whatever the ledger currently holds; a consumed entry for a PR that has since closed is pruned on the
very next tick regardless of `--ledger-retention-days`, so this read is reliable *during and shortly
after* processing a trigger, not as a long-term audit log.

### 3.3 Tests

Add to `test_trigger_ledger.py`:
- `test_main_all_json_includes_consumed_sent_entry` — a fully sent+consumed entry appears under
  `--all --json` (it's excluded under `--unconsumed --json`, already covered by
  `test_main_json_consumed_only_emits_empty_list`).
- `test_main_all_text_labels_sent_entry` — `--all` text output contains `[sent]` for that entry.
- `test_main_all_text_labels_abandoned_entry` — a `consumed_at`-set/`sent_at`-`None` entry (busy/absent
  skip) is labeled `[abandoned]`, distinct from `[sent]`.
- `test_main_all_includes_every_state_in_one_ledger` — one ledger with a pending, a queued, a
  surfaced, a sent, and an abandoned entry → `--all --json` returns all five; `--unconsumed --json`
  returns only the first three (regression pin on the existing filter).
- `test_main_all_json_distinguishes_sent_from_abandoned_via_consumed_at` — a sent+consumed entry and an
  abandoned (`consumed_at` set, `sent_at` `None`) entry both appear under `--all --json`; asserts
  `consumed_at` is present and non-null on both, and is the only field that tells them apart (pins the
  `_entry_to_json` fix).
- `test_main_requires_a_mode_flag` (rename of `test_main_requires_unconsumed_flag`) — neither flag →
  `SystemExit`.
- `test_main_rejects_both_unconsumed_and_all` — both flags → `SystemExit` (argparse's mutually
  exclusive group enforces this; pin it with a test since it's part of the CLI contract now).

---

## 4. Steps (TDD, in order)

1. §1: write the two new end-to-end tests first against current behavior (confirm they fail), then
   the `send_to_session` change, then update the four existing tests to the new contract.
   → `make test-file FILE=tests/scripts/test_gating_watcher.py`
2. §1 docs: `gating_watcher.py` + `trigger_ledger.py` module docstrings, `docs/runbooks/dispatch-orchestrator.md`.
3. §2: write the four new tests, then the `state`-dedup change in `run_once`.
   → `make test-file FILE=tests/scripts/test_gating_watcher.py`
4. §3: write the six new/renamed tests, then the `--all` CLI addition in `trigger_ledger.py`.
   → `make test-file FILE=tests/scripts/test_trigger_ledger.py`
5. Full gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`.
6. Self-review at the Step-6 gate: `feature-dev:code-reviewer` scoped to
   `git diff origin/main...HEAD` (bugs + standards). `security-review` — this diff touches
   subprocess/tmux actuation, so it's in scope even though no new external input surface is added.
7. Diff class: **escalate** — production write path (this daemon actuates `/master <PR#>` sends
   against the live master session; a wrong send policy has already caused two live incidents this
   ticket exists to fix). Note in the PR body: "diff class: escalated — flagged for owner
   `/code-review ultra` before merge", per §1.3's FRE-845 analysis being the load-bearing argument the
   owner needs to see.

## 5. Acceptance criteria → proof

| AC | Test |
|---|---|
| A busy-pane master send no longer injects keystrokes; it defers to the existing resolution loop | `test_send_master_holds_when_busy_pane_pending_resolution`, `test_run_once_master_holds_when_busy_session_no_keys_sent` |
| A trigger deferred while busy, whose PR is authoritatively merged before the pane frees, is never sent | `test_run_once_master_busy_first_tick_then_pr_merged_next_tick_never_sends_keys` |
| A trigger deferred while busy is still delivered once the pane goes idle (no permanent drop) | `test_run_once_master_busy_first_tick_then_idle_next_tick_delivers` |
| A trigger whose pane is busy on *every* tick is still eventually delivered — bounded, not silent, not permanent | `test_queued_force_delivers_after_escalation_when_pane_never_goes_idle`, `test_queued_not_force_delivered_before_escalation_threshold` |
| An unroutable decision logs once per `(pr, head_sha)`, not every tick, and dry-run never persists the suppression state | `test_run_once_unroutable_logs_once_per_head_sha_across_ticks`, `test_run_once_unroutable_dry_run_never_persists_state` |
| An unroutable log re-arms after its TTL and for a fresh head SHA | `test_run_once_unroutable_logs_again_after_ttl`, `test_run_once_unroutable_logs_again_for_new_head_sha` |
| `trigger_ledger` can show consumed entries, distinguishing sent from abandoned | `test_main_all_text_labels_sent_entry`, `test_main_all_text_labels_abandoned_entry`, `test_main_all_includes_every_state_in_one_ledger`, `test_main_all_json_distinguishes_sent_from_abandoned_via_consumed_at` |

## 6. Scope boundary

FRE-1270 (a sibling ticket: "watcher redelivers a queued event without re-checking PR state, after an
API outage blinds the tick that would have retired it") is a related but distinct defect in the
obsolescence *read* itself under `gh` outage conditions — out of scope here; this ticket does not
touch `pr_is_closed`'s failure-mode handling. The "give trigger_ledger a read mode" remedy is scoped to
exactly what the ticket asks (consumed entries visible) — no `--ticket`/`--since` filtering, no
`prime-master` wiring change (that consumer already exists and can adopt `--all` in its own ticket if
it wants to; this PR only adds the capability).
