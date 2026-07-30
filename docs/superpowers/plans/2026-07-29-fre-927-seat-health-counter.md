# FRE-927 — a persistently broken seat escapes both dispatch reconcilers

**Ticket:** [FRE-927](https://linear.app/frenchforest/issue/FRE-927) · Tier-1:Opus · stream:build2
**Related shipped work:** FRE-922 (wedge counter), FRE-923 (bounded delivery retry), FRE-924 (held-too-long escalation)
**Files:** `scripts/dispatch/orchestrator.py`, `tests/scripts/test_orchestrator.py`, `docs/runbooks/dispatch-orchestrator.md`
**Deploy class:** host systemd restart of the dispatch daemons; no gateway image.

---

## 1. The hole, confirmed against merged `main` (not inferred)

Traced through `scripts/dispatch/orchestrator.py` at `46f8b71c`:

| Tick | State | Decision | Effect |
|------|-------|----------|--------|
| 1 | no record, NEXT=`FRE-A` | `launch` (`_decide_no_record`) | `_apply` line 861-864: `prior is None` → `attempts = 0 + 1 = 1`; `execute_plan` → `delivery-failed`; `_record_for_result` (1 < 3) → `delivering(A, attempts=1)` |
| 2 | board churns, NEXT=`FRE-B` | `_decide_delivering` line 508: `nxt.identifier == B != A` → `still_next` False → **`clear`** (`owner-acted`) | `state.pop("build1")` — the record, **and the attempt count with it**, is discarded |
| 3 | no record, NEXT=`FRE-B` | `launch` | `prior is None` → `attempts = 1` again; `delivery-failed` → `delivering(B, attempts=1)` |
| 4 | churn to `FRE-C` | `clear` | … repeats indefinitely |

`attempts` never exceeds 1, so `MAX_DELIVERY_ATTEMPTS` (3) is never reached and
`_decide_delivering` never emits `surface`. `launched_at` is rewritten on every
fresh record, so `_note_held`'s age never reaches `DEFAULT_HELD_ESCALATION_S`.
Both clocks are keyed to a ticket; the broken thing is the **seat**. It fails
silently and indefinitely.

Two independent reset paths cause it — line 863 (`prior.ticket == decision.ticket
else 0`) and line 513 (`clear` pops the record wholesale).

## 2. Design decision (the Tier-1 question the ticket poses)

The ticket asks me to consider fixing the **owner-action vs board-churn
ambiguity** directly, rather than adding a third counter.

**I considered it, and it cannot close the hole on its own.** Distinguishing the
two tells us *why* the record went away, but the stream must still advance to the
newly-outranking NEXT — holding the stale `delivering` record would block real,
higher-priority work. Whatever we learn from the distinction, the ticket-keyed
record is still discarded, so any signal stored *inside* it is still lost. The
signal that must survive is seat-scoped, so it has to live outside the record.
Migrating `attempts` across the churn instead (keeping it in the record but
carrying it to the new ticket) would silently change FRE-923's contract — ticket
`B` would get fewer than three attempts because `A` burned some — violating AC-3.

So: **a per-stream consecutive-delivery-failure counter**, exactly the shape the
ticket names. I *do* address the ambiguity, but as diagnostics (§3.3): the log
now says which of the two happened, which is what an operator needs to see churn
occurring. Naming it is worth doing; it is not the fix.

## 3. Changes

### 3.1 `scripts/dispatch/orchestrator.py` — the counter

New module constant, next to `MAX_DELIVERY_ATTEMPTS`:

```python
# Consecutive delivery failures on one SEAT before the stream is surfaced as
# unhealthy (FRE-927). The FRE-923 budget and the FRE-924 age clock are both
# keyed to a ticket, so a seat that drops every dispatch while the board churns
# resets both on every tick and fails silently forever. This counter is keyed to
# the STREAM and is reset by a genuine delivery success and by nothing else — in
# particular not by the ticket changing, which is the whole point.
#
# Threshold semantics differ deliberately from ``DEFAULT_WEDGE_TICKS`` (which
# counts ticks *tolerated*, surfacing past it): here the value is the failure
# count at which the seat is surfaced, so the default 3 means "the third
# consecutive dropped delivery surfaces the seat" — matching the ticket's AC and
# reading the same way as ``MAX_DELIVERY_ATTEMPTS``.
DEFAULT_SEAT_FAILURE_THRESHOLD: int = 3

# Outcomes that PROVE the seat delivered — the only reset for the counter above.
# Identical to the set ``_record_for_result`` maps to a ``launched`` record.
_DELIVERY_SUCCESS_OUTCOMES: frozenset[str] = frozenset(
    {"launch", "prepare", "reuse", "registration-unverified"}
)
```

`run_once` gains two keyword-only params, mirroring `wedge_counts`/`wedge_ticks`:

```python
delivery_failures: dict[str, int] | None = None,
seat_failure_threshold: int = DEFAULT_SEAT_FAILURE_THRESHOLD,
```

defaulted to a throwaway map (`if delivery_failures is None: delivery_failures = {}`)
and threaded into `_apply`.

**In-memory, not persisted** — the FRE-922 lesson, and it applies identically:
the master ping fires exactly once on the crossing tick, and a persisted count
first observed *above* the crossing (restart, changed threshold, crash between
persist and notify) would silently lose the alert forever. In-memory the count
only ever climbs by 1 from 0, so the crossing is hit exactly once. A broken seat
outliving a restart simply re-counts (~15 min at the 300 s cadence), and the
greppable warning still fires every post-threshold tick meanwhile. This also
keeps the persisted state-file shape unchanged — no migration, no new
`load_state` validation surface.

### 3.2 `_apply` — increment, reset, surface

At the end of the `launch` case, immediately after the existing FRE-922 wedge
block (so all post-execution seat-signal handling sits together):

```python
if result.outcome == "delivery-failed":
    _note_delivery_failure(
        stream, delivery_failures,
        threshold=seat_failure_threshold, trace_id=trace_id,
        notifier=notifier, logger=logger,
    )
elif result.outcome in _DELIVERY_SUCCESS_OUTCOMES:
    delivery_failures.pop(stream, None)
```

Everything else leaves the counter **untouched**, deliberately:

- `seat-busy` / `worktree-dirty` / `launch-failed` typed nothing into the seat —
  neither evidence of failure nor of health (this mirrors the FRE-923 rule that a
  transient tick never spends the retry budget).
- `manual-model-required` / `manual-continuation` attempted no delivery at all.
- `seat-unhealthy` is a broken-seat signal, not a working-seat one, so it must
  never *reset* the counter. It already surfaces immediately per-ticket, so it
  does not need to increment either — keeping the counter precisely about dropped
  deliveries.
- Ticket churn, `clear`, and every non-`launch` decision: untouched. This is the
  fix. (Contrast the wedge counter, which resets on every non-wedge decision —
  correct there because a wedge is a *current* condition, wrong here because seat
  health must survive exactly the churn that discards the record.)

New helper, modelled on `_note_wedge`:

```python
def _note_delivery_failure(
    stream: str,
    delivery_failures: dict[str, int],
    *,
    threshold: int,
    trace_id: str,
    notifier: Notifier,
    logger: Logger,
) -> None:
    """Count a dropped delivery and surface the SEAT past the threshold (FRE-927)."""
    count = delivery_failures.get(stream, 0) + 1
    delivery_failures[stream] = count
    if count < threshold:
        return
    logger.warning(
        "dispatch_seat_delivery_failing",
        trace_id=trace_id, stream=stream, consecutive_failures=count,
        detail="this seat has dropped N consecutive dispatch deliveries across "
               "any tickets — the SEAT is failing, not the ticket; dispatch into "
               "this stream is not landing",
    )
    if count == threshold:  # crossing: ping master exactly once per episode.
        notifier(
            "dispatch_seat_delivery_failing",
            trace_id=trace_id, stream=stream, consecutive_failures=count,
        )
```

**Surface only** — no record is written, no process terminated, no stream halted.
Same posture as FRE-922/FRE-924: master decides. The event name deliberately
avoids `seat-unhealthy`, which is already a launcher *outcome* meaning "the pane
is not running claude" — a different condition.

Gating on `execute` is automatic: this sits after the `if not execute: return`
early return in the `launch` case, so a dry-run tick stays side-effect-free.

`main()` gains `delivery_failures: dict[str, int] = {}` beside `wedge_counts` /
`held_escalated`, a `--seat-failure-threshold` arg, and both threaded into `tick()`.

### 3.3 The churn-vs-owner-action distinction (diagnostics)

In `_decide_delivering` and `_decide_surfaced`, split the single `clear` reason:

```python
if not still_next:
    reason = "owner-acted" if normalized != "approved" else "board-churn"
    return StreamDecision(stream, "clear", ticket=record.ticket, reason=reason)
```

`kind` is unchanged (`clear` either way) — behaviour is identical, only the log
reason is now honest. No existing test asserts this reason string (verified:
`grep 'reason ==' tests/scripts/test_orchestrator.py` → only `reconciled-terminal`
and `retry-delivery`).

### 3.4 Known interaction: two alerts on the same-ticket case

A seat failing the *same* ticket three times trips **both** reconcilers in one
tick: FRE-923's `dispatch_delivery_exhausted` (this ticket gave up) and FRE-927's
`dispatch_seat_delivery_failing` (this seat is broken). They say different things
with different remedies, so both firing is correct, not noise — and suppressing
one would mean the same-ticket case never reports seat unhealth, though it is
just as broken a seat.

**Resolved at implementation: NO existing test needed changing.** I had expected
to narrow `test_giving_up_on_delivery_is_announced_exactly_once` (`:1433`), which
asserts global notifier-event-list equality. It passes untouched — but for an
incidental reason worth recording rather than glossing: its `_run` helper does
not pass `delivery_failures`, so every `run_once` call gets a *fresh throwaway*
counter and nothing accumulates across its five ticks. The production
double-alert therefore still happens (`main()` holds the dict across ticks); that
behaviour is pinned instead by the new
`test_fre923_budget_semantics_are_untouched_by_seat_health`, which shares the
dict exactly as the daemon does and asserts both events fire.

**AC-3 is met with every FRE-922/FRE-923/FRE-924 test byte-unchanged, 92/92
green** — the change adds a counter and a notification and touches neither
`decide`'s kinds, `_record_for_result`, nor the persisted record shape.

## 4. Tests (TDD — failing first, `tests/scripts/test_orchestrator.py`)

New section `--- FRE-927: seat-scoped delivery health ---`, with a `_run_seat`
helper mirroring `_run_wedge` (shared `delivery_failures` map across ticks).

| # | Test | AC | Proves |
|---|------|----|--------|
| 1 | `test_a_seat_failing_across_changing_tickets_surfaces_as_unhealthy` | 1 | Three ticks, **a different Approved ticket each tick** (`FRE-A`/`FRE-B`/`FRE-C`, board churn), seat drops every delivery → exactly one `dispatch_seat_delivery_failing` ping naming the **stream**. **Fails on merged `main`** (no such event exists). |
| 2 | `test_ticket_churn_never_resets_the_seat_counter` | 1 | Same scenario asserts `delivery_failures["build1"] == 3` while `state["build1"].attempts` never exceeds 1 — the ticket-keyed budget provably reset while the seat counter did not. Names the exact mechanism. |
| 3 | `test_a_genuine_success_clears_the_seat_counter` | 2 | fail, fail, **succeed** (`reuse`) → counter absent; two further failures → no ping (count 2 < 3). |
| 4 | `test_a_transient_tick_neither_counts_nor_clears_seat_health` | 2 | fail, `seat-busy`, fail, fail → ping fires on the 3rd *failure*; a tick that typed nothing neither spent nor healed. |
| 5 | `test_the_seat_ping_fires_once_per_episode_and_re_fires_after_a_success` | 1,2 | 5 consecutive failures → one ping; then a success; then 3 more failures → a second ping. Episode boundary is the success, nothing else. |
| 6 | `test_seat_health_never_kills_and_writes_no_record` | — | `_no_termination_argv(runner)` holds; no `surfaced` record is written by this path (surface-only, master decides). |
| 7 | `test_seat_health_is_gated_on_execute` | — | `execute=False` → no ping, no counter mutation. |
| 8 | `test_churn_and_owner_action_are_distinguishable_in_the_log` | — | §3.3: `clear` reason is `board-churn` when the ticket is still Approved but outranked, `owner-acted` when it left Approved; `kind` is `clear` in both. |
| 9 | `test_fre923_budget_and_fre924_age_are_untouched_by_seat_health` | 3 | The whole existing FRE-923/924 suite passing is the primary proof; this asserts explicitly that a single failure followed by a success leaves `attempts`/record semantics exactly as before. |

## 5. Verification

```bash
uv run pytest tests/scripts/test_orchestrator.py -q     # module — all green, incl. FRE-922/923/924
make test                                               # full unit suite
make mypy && make ruff-check && make ruff-format
pre-commit run --all-files
```

Failing-first proof: tests 1–2 run against merged `main` before the source change
and must fail (`KeyError`/no such event), demonstrating the hole is real.

## 6. Acceptance criteria → evidence (for master's gate)

| AC | Criterion | Evidence |
|----|-----------|----------|
| 1 | A seat failing across three *different* tickets surfaces as an unhealthy stream; the test fails against current merged behaviour | Tests 1, 2, 5 — ticket identity varies per attempt; test 1 verified red on `main` first |
| 2 | A success genuinely clears the counter; one failure + a working dispatch never escalates | Tests 3, 4, 5 |
| 3 | FRE-923 / FRE-924 behaviours unchanged, proven by their current tests still passing | Full module green; every FRE-923/924 test unchanged except the one over-tight global-event-list assertion (§3.4), narrowed rather than weakened — plus test 9 |

## 7. Docs

`docs/runbooks/dispatch-orchestrator.md` § "Surface and recover a stalled or
failed run" — new bullet for `dispatch_seat_delivery_failing` alongside
`dispatch_held_too_long`: what it means (the seat, not the ticket), the
`--seat-failure-threshold` knob, that it surfaces only and never halts dispatch,
and that the in-memory counter re-counts after a daemon restart.

## 8. Codex plan-review verdict (pre-coding, 2026-07-29)

Five design questions put adversarially; **zero issues returned**, each answer
cited against the source. Two points folded back in:

- **§2 strengthened.** Codex supplied a mechanism I had not cited: keeping the
  ticket-keyed record across churn fails not only because the stream must
  advance, but because *any* existing `delivering`/`surfaced` record bypasses
  `_decide_no_record` (`orchestrator.py:409-439`) — the newly-outranking NEXT
  would never be resolved at all.
- **§3.4 constrained.** Narrowing the assertion at `test_orchestrator.py:1433` is
  legitimate **provided the `phase == "surfaced"` assertion at `:1432` is kept
  alongside it**. It is (only `:1433` is touched).
- **§9 sharpened** on the in-memory cost, per codex: repeated daemon restarts
  arriving before the third failure could defer the ping indefinitely — the same
  tradeoff FRE-922 already accepts, now stated explicitly rather than implied.

## 9. Risks

- **Alert volume.** A genuinely broken seat now emits a per-tick warning past the
  threshold plus one ping (and, on the same-ticket path, FRE-923's ping too).
  Deliberate — the failure this closes is *silence*.
- **In-memory counter.** A daemon restarting faster than the threshold
  (~15 min at the 300 s cadence) never surfaces — and *repeated* restarts
  arriving before the third failure could defer the ping indefinitely. Accepted,
  matching FRE-922 exactly; a restart loop that tight is itself a louder signal
  via systemd, and the per-tick warning still lands meanwhile.
- **Surface-only.** Dispatch keeps being attempted into a seat known to be
  failing. Matches FRE-922/FRE-924 posture and the AC's wording ("surfaces as an
  unhealthy stream"); halting a stream is a policy change this ticket does not ask
  for.
