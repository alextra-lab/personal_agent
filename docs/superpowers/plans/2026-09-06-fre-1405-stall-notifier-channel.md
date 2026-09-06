# FRE-1405 — route dispatch-daemon stall notifications through a durable ledger master reads

Ticket: https://linear.app/frenchforest/issue/FRE-1405
Backing design intent: `scripts/dispatch/trigger_ledger.py` module docstring (three-state crash
model) and `.claude/skills/prime-master/SKILL.md` step 5 (trigger ledger read at priming).

**Revision note:** this plan replaces a first draft that proposed writing directly into
`telemetry/trigger_ledger.json` (the file `gating_watcher.py` and `send_keys_whitelist.py` already
write). Codex plan-review found that adding the orchestrator daemon as a second continuous writer
to that same file is a genuine read-modify-write race — no locking exists anywhere in
`scripts/dispatch/`, and both daemons run as separate, always-on `systemd` services
(`seshat-gating-watcher.service`, `seshat-dispatch-orchestrator.service`). It also found the
first draft's claim that all non-wedge notify call sites are one-shot latches was false
(`dispatch_blocked` re-fires every tick, unthrottled), and that a fresh-UUID `event_id` per call
would grow the ledger without bound for a persisting condition. This revision fixes all three.

## Problem

`scripts/dispatch/orchestrator.py` defines a `Notifier` Protocol used at 7 call sites:
`dispatch_blocked` (:1016, unthrottled — fires every blocked tick), `dispatch_delivery_exhausted`
at `MAX_DELIVERY_ATTEMPTS` (:1093) and at `surface` (:1262), `dispatch_stall` (:1206, one-shot per
episode via `stall_notified`/`in_progress_stall_notified`), `dispatch_seat_wedged` via
`_note_wedge` (:1365, `>`/`>=` crossing-then-schedule), `dispatch_seat_delivery_failing` via
`_note_delivery_failure` (:1443, one-shot per episode), `dispatch_held_too_long` via `_note_held`
(:1519, one-shot per episode). The only implementation, `_structlog_notifier` (:1633), just calls
`logger.warning(...)`. Every call site already calls `logger.warning` itself too, so today the
notifier is a pure duplicate log line. Nothing reaches master.

## Design decisions

### 1. A new, orchestrator-owned ledger file — not `trigger_ledger.json`

Reuse `trigger_ledger.py`'s data model and CLI (`LedgerEntry`, `load_ledger`/`save_ledger`,
`snapshot_unconsumed`, `main() --unconsumed --json`) — it already has the exact shape master
needs, and `.claude/skills/prime-master/SKILL.md:19-21` already runs
`--unconsumed --json` at every priming and treats `surfaced` as "demands owner attention." But
point it at a **separate file**, `telemetry/dispatch_notify_ledger.json`, so the orchestrator
daemon is the sole writer of its own file — exactly the single-writer-per-file discipline
`trigger_ledger.json` already relies on today (only `gating_watcher.py` writes it in a loop;
`send_keys_whitelist.py`'s writes are a one-off per hook invocation, not a competing daemon loop).
This eliminates the race entirely, with no new locking code. `prime-master/SKILL.md` step 5 gets
one added line for the second file (a real change — the first draft's "zero changes to
prime-master" claim no longer holds under this design, and that is the honest tradeoff for
removing the race by construction instead of adding locking).

### 2. Stable, per-episode `event_id` — not a fresh UUID per call

`event_id = f"dispatch-notify:{event}:{stream}"`. Re-notifying the same condition **overwrites
the same ledger entry** (fresh `preconditions`/`surfaced_at`, same key) rather than adding a new
one. This satisfies AC-4 ("count < ticks") by construction, for *any* persisting condition,
independent of whichever throttle schedule (or lack of one, per `dispatch_blocked`) gates the
call itself. `trace_id` is tick identity, not episode identity, and does not belong in the key
(codex's finding).

### 3. Consume-on-resolve for all 7 call sites

Each event has an existing point in `_apply` where its condition provably ends — reuse it to
call `trigger_ledger.mark_consumed_if_present` (new: a `mark_consumed` that no-ops instead of
`KeyError`ing when the key is absent) on that event/stream's key. This turns "master sees this
forever once surfaced" (true of `trigger_ledger.py`'s own `reconcile()`-surfaced entries today —
confirmed unused/never-consumed by design, per `gating_watcher.py:1143`) into "master sees this
exactly while it is true," which is the actual point of the ticket — a channel that silently
accumulates stale alarms is as untrustworthy as one that stays silent.

**Crash-safety ordering (codex round 2 finding — the first draft of this table had this
backwards): always consume the ledger entry BEFORE persisting the state mutation that would make
the process forget to retry.** The existing codebase's own convention for the *opposite*
direction is notify-then-persist ("a crash between the two means the worst case is one extra,
survivable re-ping on the next tick, never a silently lost one" — `_note_wedge`'s docstring). The
same logic run in reverse gives the safe order for un-notifying: if the ledger consume happens
first and the process crashes before the state write, the next tick reloads the *stale*
(unresolved-looking) state, naturally re-derives the same decision, and safely re-attempts both
steps (`mark_consumed_if_present` no-ops harmlessly on the second attempt). If the state write
happened first and the crash lands before the consume, the state that would have driven the
retry is already gone — the ledger entry is orphaned, surfaced forever, un-consumable by
construction. Every row below persists the ledger consume strictly before the corresponding
state file write.

**Restart-safety for the two in-memory-only trackers.** `delivery_failures` and `held_escalated`
are deliberately in-memory, reset to empty on every daemon restart (pre-existing FRE-927/FRE-924
design — unlike `wedge_state`/`head_stall_state`, which are persisted). Gating a resolve call on
"the in-memory dict's `.pop()` returned a value" is broken across a restart: a fresh empty dict
never has the key, so a real post-restart recovery would never be recognized and the ledger entry
would stay stuck forever. Both rows below resolve **unconditionally** on the qualifying decision,
not conditioned on the pop's return value — `mark_consumed_if_present`'s absent-is-a-no-op
semantics make the extra calls free.

| Event | Resolves at |
|---|---|
| `dispatch_blocked` | top of `_apply`, alongside the existing wedge-reset guard: whenever `execute and decision.kind != "launch"` (**execute-gated** — codex round 3 caught this missing on the first draft of this row; a dry-run tick must not consume a real notification) — a stream not attempting to launch this tick was not blocked this tick, covering the case codex flagged, a stream falling to a non-launch decision right when the block clears — **and** inside the `"launch"` case once the blocked-check passes |
| `dispatch_stall` | top of `_apply`, whenever `decision.kind != "stall"` (codex round 3: simpler and more complete than the round-2 draft's `run_complete`-plus-`clear` pair — it also covers the pre-pickup-stall-notified-then-ticket-enters-`In Progress`-still-inside-its-own-longer-grace transition, which decides `"await"`, neither `run_complete` nor `clear`, and would otherwise stay surfaced through the whole grace window). Matches the existing `"stall"` case's own precedent of notifying **ungated** in dry-run (an existing, out-of-scope inconsistency this ticket does not touch) — this resolve call is likewise ungated. |
| `dispatch_delivery_exhausted` | `case "clear"` (same key/stream as `dispatch_stall`'s clear — both are terminal states of the same tracked record; codex confirmed this resolve point is reachable and correct, only the ordering needed fixing) |
| `dispatch_seat_wedged` | inside `_reset_wedge`, before `persist_wedge` |
| `dispatch_seat_delivery_failing` | unconditionally whenever `result.outcome in _DELIVERY_SUCCESS_OUTCOMES` — checked and resolved **before the `"launch"` case's `persist(state)`** (the code's existing dispatch-record write), not at the later `delivery_failures.pop(...)` line (codex round 3: that line runs *after* `persist(state)`, so a crash in between would persist a `launched` record whose next-tick decision is `"await"`, not another delivery outcome, orphaning the entry). The in-memory `delivery_failures.pop(stream, None)` bookkeeping itself is untouched, at its existing location — only the ledger-consume timing moves earlier; not gated on the pop's return value (restart-safety, above) |
| `dispatch_held_too_long` | unconditionally whenever `decision.kind != "hold"` and `execute` (not gated on the pop's return value — restart-safety, above) |
| `dispatch_head_stalled` (new, §5) | inside `_reset_head_stall`, before `persist_head_stall`, whenever **not** (`decision.kind == "skip"` and `decision.reason == "occupied-no-record"`) — codex found the originally-proposed `kind != "skip"` guard wrong: a healthy `"no-candidate"` skip right after an `"occupied-no-record"` streak is still `kind == "skip"` and would never reset/resolve under that guard |

### 4. AC-2(c)'s trigger must not fire on a healthy empty backlog

`decide()` → `_decide_no_record` (:544) returns `StreamDecision(stream, "skip",
reason="occupied-or-no-candidate")` whenever `resolve_next` returns `None` — which conflates two
different things: the stream's head ticket is stuck `In Progress`/`In Review` with **no**
orchestrator record tracking it (the actual FRE-1288 incident shape — anomalous), and the
backlog is simply empty (healthy, can persist indefinitely with zero alarm value). Counting every
`skip` (the naive AC-2(c) fix) would alert on every idle stream. Fix: split the reason at the
source — add a small public wrapper in `next_resolver.py`:

```python
def is_occupied(issues: Sequence[IssueSnapshot], stream: str) -> bool:
    """Return True if `stream`'s head ticket is In Progress or In Review."""
    return _is_occupied(issues, stream_label(stream))
```

and use it in `_decide_no_record` (orchestrator.py:544):

```python
def _decide_no_record(stream: str, issues: Sequence[IssueSnapshot]) -> StreamDecision:
    """Resolve NEXT for an untracked stream."""
    if is_occupied(issues, stream):
        return StreamDecision(stream, "skip", reason="occupied-no-record")
    nxt = resolve_next(issues, stream)
    if nxt is None:
        return StreamDecision(stream, "skip", reason="no-candidate")
    model = model_for_labels(nxt.labels)
    if model is None:
        return StreamDecision(stream, "skip", ticket=nxt.identifier, reason="no-tier-label")
    return StreamDecision(
        stream, "launch", ticket=nxt.identifier, model=model,
        context_keep="context:keep" in nxt.labels, reason="idle-with-next",
    )
```

Only `reason == "occupied-no-record"` drives the new head-stall counter — `"no-candidate"` (empty
backlog) and `"no-tier-label"` (a labeling gap, a different and immediately board-visible
problem) do not. Confirmed no existing test asserts the literal string
`"occupied-or-no-candidate"` (`grep` came back empty against `tests/scripts/test_orchestrator.py`),
so this is a safe, scoped rename-with-split. `resolve_next`'s own contract and every other caller
(`_decide_delivering`, `_decide_surfaced`) are untouched. Codex round 2 also flagged a stale
reference to the old string in `stream_label`'s own docstring (`next_resolver.py:128-137`,
"...reporting `occupied-or-no-candidate` forever") — update that prose to name the split reasons,
a documentation-only fold-in.

## Atomic steps

### Step 1 — `trigger_ledger.py`: `record_surfaced` + `mark_consumed_if_present`

Add near `mark_surfaced` (:237-241):

```python
def record_surfaced(
    ledger: Ledger,
    *,
    event_id: str,
    source: str,
    target_pane: str,
    ticket: str,
    preconditions: Mapping[str, str],
    now: float,
) -> Ledger:
    """Write (or overwrite) an entry that needs owner attention, no actuation attempted.

    Unlike ``record_pending``, this sets ``surfaced_at`` immediately: there is
    no command to send, so the entry must never enter ``reconcile()``'s retry
    path, which treats any non-terminal (``consumed_at is None and
    surfaced_at is None``) entry as a dropped actuation and calls
    ``execute_pending`` on it. ``command`` is always empty. Calling this again
    with the same ``event_id`` replaces the entry in place — the caller is
    expected to use a stable, per-episode key so a persisting condition
    updates one entry rather than accumulating one per call.
    """
    updated = dict(ledger)
    updated[event_id] = LedgerEntry(
        event_id=event_id,
        source=source,
        target_pane=target_pane,
        ticket=ticket,
        command="",
        preconditions=dict(preconditions),
        created_at=now,
        surfaced_at=now,
    )
    return updated


def mark_consumed_if_present(ledger: Ledger, event_id: str, now: float) -> Ledger:
    """Close out ``event_id`` if it exists; a no-op otherwise.

    ``mark_consumed`` raises on an absent key (by design — every other caller
    knows the entry it is closing exists). A resolve-on-episode-end caller
    does not: the episode may have ended before any entry was ever written
    (e.g. a condition that never crossed its own notify threshold), so
    "already absent" and "just closed" must be equally unremarkable.
    """
    if event_id not in ledger:
        return ledger
    return mark_consumed(ledger, event_id, now)
```

Tests, `tests/scripts/test_trigger_ledger.py`:
- `test_record_surfaced_writes_terminal_surfaced_entry` — `surfaced_at is not None`,
  `consumed_at is None`, `command == ""`.
- `test_record_surfaced_entry_skipped_by_reconcile` — a ledger with only a `record_surfaced`
  entry; `reconcile()` with an `execute_pending` stub that raises if called; assert it is never
  called and the ledger is unchanged (regression guard for the hazard `reconcile()`'s first check,
  :290, exists to prevent).
- `test_record_surfaced_same_event_id_overwrites` — call twice with the same `event_id`,
  different `preconditions`; assert the ledger has exactly one entry with the latest fields.
- `test_mark_consumed_if_present_noop_when_absent` — call on an empty ledger; assert no exception
  and an unchanged (empty) ledger.
- `test_mark_consumed_if_present_closes_existing_entry` — mirrors `mark_consumed`'s own test.

Verify: `make test-file FILE=tests/scripts/test_trigger_ledger.py`

### Step 2 — `next_resolver.py`: split `is_occupied` out, `orchestrator.py`: use it

Add `is_occupied()` (§Design 4) next to `_is_occupied` in `next_resolver.py`. Update
`_decide_no_record` in `orchestrator.py` (§Design 4). Import `is_occupied` alongside the existing
`from scripts.dispatch.next_resolver import (IssueSnapshot, fetch_board, fetch_issue_state,
resolve_next)` block (:67-72).

Tests, `tests/scripts/test_next_resolver.py`:
- `test_is_occupied_true_when_head_in_progress`
- `test_is_occupied_false_when_no_matching_issue`

Tests, `tests/scripts/test_orchestrator.py`:
- `test_decide_skip_reason_occupied_no_record_when_head_stuck` — an issue carrying the stream's
  label sits `In Progress`, no tracked record; assert `reason == "occupied-no-record"`.
- `test_decide_skip_reason_no_candidate_when_backlog_empty` — no issues at all; assert
  `reason == "no-candidate"`.

Verify: `make test-file FILE=tests/scripts/test_next_resolver.py` ·
`make test-file FILE=tests/scripts/test_orchestrator.py`

### Step 3 — `orchestrator.py`: the notifier itself, wired into `main()`

Add `from scripts.dispatch import trigger_ledger` (module import — avoids shadowing the existing
`reconcile` parameter name used throughout `run_once`/`_apply`). `Path`, `uuid`, `time` are
already imported.

```python
def _default_notify_ledger_path() -> Path:
    """Return the default path for the orchestrator's own notify ledger (FRE-1405).

    Deliberately separate from ``trigger_ledger.py``'s ``_default_ledger_path()``
    (``telemetry/trigger_ledger.json``, written by ``gating_watcher.py`` and
    ``send_keys_whitelist.py``) — a second continuous writer to that file would
    race it (no locking exists anywhere in this package). Single-writer-per-file
    is what keeps that ledger race-free today; this preserves it.
    """
    return Path("telemetry") / "dispatch_notify_ledger.json"


def _trigger_ledger_notifier(ledger_path: Path, logger: Logger) -> Notifier:
    """A notifier that surfaces the event as an entry in the orchestrator's own notify ledger.

    Master reads this the same way it reads ``trigger_ledger.json`` — see
    ``prime-master/SKILL.md`` step 5.
    """

    def notify(event: str, **fields: object) -> None:
        stream = str(fields.get("stream", ""))
        ticket = str(fields.get("ticket") or stream)
        ledger = trigger_ledger.load_ledger(ledger_path, logger)
        ledger = trigger_ledger.record_surfaced(
            ledger,
            event_id=f"dispatch-notify:{event}:{stream}",
            source=event,
            target_pane=stream,
            ticket=ticket,
            preconditions={k: str(v) for k, v in fields.items()},
            now=time.time(),
        )
        trigger_ledger.save_ledger(ledger_path, ledger)
        logger.info("dispatch_notify_surfaced", event=event, stream=stream, ticket=ticket)

    return notify
```

`fields` already carries `trace_id` + `stream` + `ticket` (or `stream` alone for
`dispatch_seat_delivery_failing`/`dispatch_held_too_long`, covered by the `or stream` fallback) at
every one of the 7 sites — satisfies AC-3 (stream, ticket, condition = `source`, evidence =
`preconditions`) with **no call-site changes**. The `Notifier` signature is untouched, so none of
the existing tests using the fake `_Notifier` capture in `tests/scripts/test_orchestrator.py`
change.

In `main()` (:1745), replace `notifier = _structlog_notifier(logger)` with:
```python
notifier = _trigger_ledger_notifier(_default_notify_ledger_path(), logger)
```

Test (AC-1, end to end — the proof the ticket demands):
- `test_trigger_ledger_notifier_writes_entry_readable_by_main_cli` — build the notifier against
  a `tmp_path` ledger file, call
  `notifier("dispatch_stall", trace_id="t1", stream="build1", ticket="FRE-1", reason="x")`, then
  invoke `trigger_ledger.main(["--ledger-file", str(path), "--unconsumed", "--json"])` and assert
  the printed JSON (via `capsys`) names the stream/ticket/reason — literally master's own command
  (pointed at the new file via `--ledger-file`).

Verify: `make test-file FILE=tests/scripts/test_orchestrator.py`

### Step 4 — AC-2(a)/(b): already-wired paths, sink-only proof + consume-on-resolve

These two conditions already call `notifier(...)` today (stall :1206, wedge via `_note_wedge`
:1365); only the sink was wrong.

Add the consume-on-resolve calls (§Design 3 table) using a small shared helper:

```python
def _resolve_dispatch_notify(ledger_path: Path, logger: Logger, event: str, stream: str) -> None:
    """Close out `event`'s notify-ledger entry for `stream`, if one is open (FRE-1405)."""
    ledger = trigger_ledger.load_ledger(ledger_path, logger)
    ledger = trigger_ledger.mark_consumed_if_present(
        ledger, f"dispatch-notify:{event}:{stream}", time.time()
    )
    trigger_ledger.save_ledger(ledger_path, ledger)
```

Thread `notify_ledger_path: Path` through `run_once`/`_apply` alongside `notifier` (both come from
the same `main()`-constructed value; simplest is a single new parameter, not a re-derivation, so
`main()` passes the same `Path` object used to build the notifier).

Call sites (all consume-before-persist — see Design §3's crash-safety ordering; `execute`-gated
wherever the code around it already is, so a dry-run tick stays side-effect-free, except
`dispatch_stall`'s own resolve, which matches that event's pre-existing ungated-in-dry-run
notify):
- Top of `_apply`, alongside the existing `if decision.kind != "launch": _reset_wedge(...)`
  (:993): add `if execute and decision.kind != "launch": _resolve_dispatch_notify(...,
  "dispatch_blocked", stream)` (**execute-gated** — codex round 3). This covers the case codex
  flagged — a stream that falls to any non-launch decision the very tick the block clears still
  gets `dispatch_blocked` resolved, because resolution no longer depends on ever reaching a
  "launch, not blocked" tick for that stream again.
- Same top-of-`_apply` location: add `if decision.kind != "stall":
  _resolve_dispatch_notify(..., "dispatch_stall", stream)`, **ungated** (codex round 3 — this
  single guard replaces the round-2 draft's separate `run_complete`/`clear` insertions; it
  subsumes both and additionally covers the pre-pickup-stall → in-progress-grace `"await"`
  transition, which is neither).
- The `"launch"` case, right after the blocked-check passes (blocked is `None`, still inside
  `if execute:`) — call `_resolve_dispatch_notify(..., "dispatch_blocked", stream)` too (belt and
  suspenders with the top-level guard above; harmless double-resolve, `mark_consumed_if_present`
  no-ops on the second call).
- Still inside the `"launch"` case, once `result = execute_plan(...)` is known (:1069) and
  **before** the existing `persist(state)` at :1126 (codex round 3: the delivery-failing resolve
  must land before this persist, not after, at the `delivery_failures.pop(...)` line further down
  — a crash between :1126 and that later line would persist a `launched` record whose next
  decision is `"await"`, orphaning the entry) — if `result.outcome in
  _DELIVERY_SUCCESS_OUTCOMES`, call `_resolve_dispatch_notify(..., "dispatch_seat_delivery_failing",
  stream)`. The existing in-memory `delivery_failures.pop(stream, None)` bookkeeping (:1181)
  stays exactly where it is — only the ledger-consume timing moves earlier.
- `case "clear":` (:1187) — before the existing `persist(state)`, call
  `_resolve_dispatch_notify(..., "dispatch_delivery_exhausted", stream)`.
- `_reset_wedge` (:1282) — before `persist_wedge`, call
  `_resolve_dispatch_notify(..., "dispatch_seat_wedged", stream)`.
- the top-of-`_apply` `if decision.kind != "hold": held_escalated.pop(stream, None)` (:1001) —
  add `if execute: _resolve_dispatch_notify(..., "dispatch_held_too_long", stream)` inside that
  same `if`, **unconditionally** (not gated on the pop's return value — restart-safety, §3),
  gated only on `execute` to match the `"hold"` case's own existing side-effect gate.

Tests:
- `test_stall_reaches_notify_ledger` — assert a `surfaced` entry lands via
  `trigger_ledger.load_ledger` + `snapshot_unconsumed`.
- `test_stall_notify_consumed_when_kind_leaves_stall` — seed a stall notify entry, then decide
  any non-`"stall"` kind (`run_complete`, `clear`, and separately `await` — the pre-pickup → 
  in-progress-grace transition codex flagged); assert each consumes it.
- `test_wedge_reaches_notify_ledger`
- `test_wedge_notify_consumed_on_reset`
- `test_delivery_failing_notify_consumed_after_restart_amnesia` — populate a surfaced
  `dispatch_seat_delivery_failing` entry, then call the success path with a **fresh, empty**
  `delivery_failures` dict (simulating a post-restart daemon that never saw the failure episode);
  assert the entry is still consumed. This is the regression test for codex's restart-amnesia
  finding.
- `test_held_notify_consumed_after_restart_amnesia` — same shape for `held_escalated`.
- `test_blocked_notify_consumed_when_stream_falls_to_non_launch_decision` — seed a
  `dispatch_blocked` entry, then decide a non-`launch` outcome for that stream (e.g. `"skip"`)
  without ever revisiting a not-blocked `"launch"` tick; assert it is consumed. Regression test
  for codex's other finding.

Verify: `make test-file FILE=tests/scripts/test_orchestrator.py`

### Step 5 — AC-2(c): new condition, head cannot start for N consecutive ticks

**Genuinely missing, not a bad sink.** `_apply`'s `match` (:1003) has no `case "skip":` — it falls
through to `case _:  # await / skip — no state change. return` (:1278-1279). No counter, no
notify.

Mirror `WedgeState`/`_note_wedge`/`_reset_wedge` (:429-447, 1282-1373) as a **new sibling** — do
not refactor the existing wedge code (tested, crash-safety-critical, out of this ticket's scope):

```python
@dataclasses.dataclass(frozen=True)
class HeadStallState:
    """Persisted per-stream consecutive-occupied-with-no-record tracking (FRE-1405)."""

    count: int
    last_notified_count: int = 0


DEFAULT_HEAD_STALL_TICKS: int = 3
DEFAULT_HEAD_STALL_RENOTIFY_TICKS: int = 12
```

`_note_head_stall(stream, ticket, head_stall_state, *, head_stall_ticks,
head_stall_renotify_ticks, trace_id, notifier, logger, persist_head_stall)` — **exact** algorithm
copy of `_note_wedge` including its `count > head_stall_ticks` crossing test (not `>=`) and
`count - last_notified >= head_stall_renotify_ticks` re-notify test — emitting
`dispatch_head_stalled`. `ticket` is the stream name (this condition, by construction from Step 2,
only fires when `decision.ticket is None` — `_decide_no_record`'s `occupied-no-record` branch
names no ticket, since the occupying ticket isn't the one this stream would dispatch next).

`_reset_head_stall(stream, head_stall_state, persist_head_stall)` mirrors `_reset_wedge`, calling
`_resolve_dispatch_notify(..., "dispatch_head_stalled", stream)` before `persist_head_stall`
(consume-before-persist, §3).

Wire into `_apply` with the **corrected** reset predicate (codex round 2 finding — resetting on
`kind != "skip"` alone is wrong: a healthy `"no-candidate"` skip right after an
`"occupied-no-record"` streak is still `kind == "skip"` and would never reset under that guard,
leaving the counter and the ledger entry stuck):
```python
head_stalled = decision.kind == "skip" and decision.reason == "occupied-no-record"
if not head_stalled:
    _reset_head_stall(stream, head_stall_state, persist_head_stall)
```
placed alongside the existing `if decision.kind != "launch": _reset_wedge(...)` (:993).
- Add before the `case _:` catch-all:
  ```python
  case "skip":
      if execute and decision.reason == "occupied-no-record":
          _note_head_stall(
              stream, decision.ticket, head_stall_state,
              head_stall_ticks=head_stall_ticks,
              head_stall_renotify_ticks=head_stall_renotify_ticks,
              trace_id=trace_id, notifier=notifier, logger=logger,
              persist_head_stall=persist_head_stall,
          )
  ```
  (the `reason` check is the AC-2(c)-vs-healthy-idle split from Step 2 — a `"no-candidate"` or
  `"no-tier-label"` skip never reaches `_note_head_stall`).
- Thread `head_stall_state: dict[str, HeadStallState] | None = None`, `head_stall_ticks: int =
  DEFAULT_HEAD_STALL_TICKS`, `head_stall_renotify_ticks: int =
  DEFAULT_HEAD_STALL_RENOTIFY_TICKS`, `persist_head_stall: Callable[[dict[str, HeadStallState]],
  None] = lambda _s: None` through `run_once` and `_apply`, exactly like `wedge_state` (:815-818,
  886-887, 949-952, 975-978).

Add `load_head_stall_state`/`save_head_stall_state`/`_default_head_stall_state_path()` mirroring
`load_wedge_state`/`save_wedge_state`/`_default_wedge_state_path` (:1580-1649) verbatim in shape
(same corrupt-record drop-not-construct invariant checks), pointed at
`telemetry/dispatch_head_stall_state.json`.

New CLI flags in `main()`, mirroring the wedge flags (:1694-1711): `--head-stall-ticks`,
`--head-stall-renotify-ticks`, `--head-stall-state-file`; wire into `tick()` like
`wedge_state`/`wedge_ticks`/`wedge_renotify_ticks`/`persist_wedge` (:1762, 1777-1780). `main()`
also threads the same `notify_ledger_path` (Step 4) into `run_once`.

Tests (TDD order):
1. `test_decide_skip_reason_occupied_no_record_when_head_stuck` (already added in Step 2, listed
   again here as the proof the gap is in `_apply`, not `decide()`).
2. `test_head_stall_no_notify_before_threshold`
3. `test_head_stall_notifies_on_crossing` — asserts the `>` (not `>=`) crossing test.
4. `test_head_stall_renotify_schedule` — mirrors `_note_wedge`'s own schedule test shape.
5. `test_head_stall_resets_on_launch`
6. `test_head_stall_ignores_no_candidate_skip` — a `"no-candidate"` skip streak never increments
   the counter or notifies (the regression guard for Step 2's whole point).
7. `test_head_stall_reaches_notify_ledger`
8. `test_head_stall_notify_consumed_on_reset`

Verify: `make test-file FILE=tests/scripts/test_orchestrator.py`

### Step 6 — AC-4: explicit bound tests

Stable `event_id` (Design 2) already makes entry-count independent of tick-count for every event.
Add explicit tests proving it for the two schedule-throttled paths:
- `test_wedge_renotify_schedule_bounded_over_ten_ticks` — hold a wedge condition 10+ ticks,
  assert exactly 1 ledger entry for that stream throughout, and its `preconditions` update on
  each renotify tick per the schedule.
- `test_head_stall_renotify_schedule_bounded_over_ten_ticks` — same shape.
- `test_dispatch_blocked_bounded_despite_unthrottled_calls` — hold a blocked condition 10+ ticks
  (unthrottled at the log/notify-call level, per the Problem section); assert exactly 1 ledger
  entry throughout — the stable key, not a new throttle, is what bounds this one.

### Step 7 — AC-5: seeded negative

`test_no_stall_no_ledger_entries_over_full_cycle` — run `run_once` for several ticks with a
healthy stream (launch succeeds each tick, no stall/wedge/skip/hold condition), using
`_trigger_ledger_notifier` as the sink; assert `trigger_ledger.load_ledger(path)` stays empty.
This is the test the ticket explicitly flags as easy to skip ("a notifier that fires on
everything passes AC-1 through AC-4").

### Step 8 — Docs

- `prime-master/SKILL.md` step 5: add the second `--ledger-file
  telemetry/dispatch_notify_ledger.json --unconsumed --json` read, naming it as the
  orchestrator's own stall/wedge/blocked notifications (distinct from `gating_watcher.py`'s
  actuation entries in `trigger_ledger.json`).
- `docs/runbooks/dispatch-orchestrator.md`: document the new file, its 7 event kinds, and that
  entries are consumed automatically when the underlying condition resolves (not by the owner).

## Acceptance criteria mapping

| AC | Covered by |
|----|-----------|
| AC-1 | Step 3's end-to-end test |
| AC-2(a) in-progress-past-timeout | Step 4 (already wired) |
| AC-2(b) seat-busy wedge | Step 4 (already wired) |
| AC-2(c) head cannot start | Step 5 (new code, gated on `occupied-no-record` only) |
| AC-3 | Step 3 (`preconditions` carries all `fields`) |
| AC-4 | Step 6, backed by Design 2's stable-key construction |
| AC-5 | Step 7 |

## Quality gates

`make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

## Risk tier

Standard — touches production dispatch-daemon logic (`scripts/dispatch/orchestrator.py`,
`trigger_ledger.py`, `next_resolver.py`) read by master at every priming, and changes a decision
reason string (`"occupied-or-no-candidate"` → split). Second codex plan-review pass requested on
this revision before implementation, focused on: the separate-file design fully eliminating the
race (vs. some subtler shared-state hazard between the two files), and whether consume-on-resolve
at 7 distinct call sites is correctly scoped (no event consumed before its condition has actually
ended, no event left un-consumable by a code path that can never reach its resolve point).
