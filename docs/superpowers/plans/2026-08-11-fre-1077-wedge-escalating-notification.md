# FRE-1077: A wedged seat alarms once and then goes quiet

**Ticket:** https://linear.app/frenchforest/issue/FRE-1077
**Branch:** `fre-1077-a-wedged-seat-alarms-once-and-then-goes-quiet-build2-blocked`
**Tier:** Standard (touches `scripts/dispatch/orchestrator.py` logic + CLI surface + persisted
state shape) — codex plan-review required.

## Problem (from the ticket)

`_note_wedge` (`scripts/dispatch/orchestrator.py:1163-1215`) counts consecutive suspected-wedge
ticks in the in-memory `wedge_counts: dict[str, int]` and pings master exactly once, on the tick
the count first exceeds `DEFAULT_WEDGE_TICKS` (2). Every tick after that logs a warning but never
notifies again. The FRE-1077 incident produced 186 further warned-but-silent ticks (15+ hours)
after the single ping. Additionally the counter is held in a plain in-memory dict created fresh in
`main()` (`orchestrator.py:1510`), so a dispatcher restart resets it to 0 and the one-shot ping can
re-arm — the alerting is "once per process lifetime," not a property anyone chose.

## Acceptance criteria (this ticket's own, not the backing incident's)

1. Simulating a seat that reports busy indefinitely, notifications continue past the crossing tick
   on a defined schedule rather than stopping after one. The test must run long enough to pass the
   *second* notification point (a test that stops at the crossing tick passes today — not a valid
   proof).
2. Restarting the dispatcher mid-wedge: the counter and the notification state both survive — the
   alarm neither resets (losing progress / re-arming from zero) nor double-fires (re-notifying
   immediately purely because of the restart).

Out of scope (ticket prose, no `## Open remedies` disposition obligation — lifecycle-rules §
Ticket state): the pytest-poller instance itself (fixed by hand, no code change) and "consider
whether the seat should detect this class itself" (a suggestion, not an ask — CLAUDE.md "consider ≠
asked for").

## Design

### Why a new persisted structure, not a bigger `wedge_counts`

The wedge counter is deliberately *not* anchored to `DispatchRecord` today: on a `seat-busy`
outcome, `_apply` writes a `"delivering"` record, executes the plan, then (since `seat-busy` maps to
`new_record = None` and there is no prior `"delivering"` record for this ticket) pops the stream
from `state` again in the same tick (`orchestrator.py:1003-1021`). So across a sustained wedge,
`DispatchRecord`/`dispatch_state.json` carries nothing — there is no existing persisted anchor to
piggyback on. The codebase's one established cross-restart mechanism is the atomic
load/mutate/save JSON file (`load_state`/`save_state`, `orchestrator.py:1375-1414`); this plan adds
a sibling of that pattern rather than a new storage technology, matching how every other durable
field in this file already persists (`stall_notified`, `attempts`).

### New dataclass — `WedgeState`

```python
@dataclasses.dataclass(frozen=True)
class WedgeState:
    """Persisted per-stream suspected-wedge tracking (FRE-1077).

    Attributes:
        count: Consecutive suspected-wedge ticks observed this episode.
        last_notified_count: The `count` value at which master was last
            pinged this episode (0 = never notified this episode). Persisted
            so a dispatcher restart resumes the re-notification schedule
            instead of losing it (silence) or restarting it (a duplicate
            ping the instant the process comes back).
    """

    count: int
    last_notified_count: int = 0
```

### New constant — `DEFAULT_WEDGE_RENOTIFY_TICKS`

A re-notification cadence past the crossing tick (documented with the same rationale style as the
existing threshold constants at `orchestrator.py:104-121`). Default `12` (~1 hour at the 300s poll
cadence): frequent enough that a multi-hour wedge stays unmissable, sparse enough to stay
actionable rather than spammy. Overridable via `--wedge-renotify-ticks`, mirroring `--wedge-ticks`.

### Rewritten `_note_wedge` — schedule test uses `>=`, not `==`

The **crossing-tick equality test** (`count == wedge_ticks + 1`) is exactly what made the *old*
design's rejected "persist the counter" idea unsafe (its own docstring: a restart/changed-threshold
could observe the count *first* above the crossing value and permanently skip the equality). The
fix is to stop relying on equality at all: notify whenever the count is past threshold **and**
either this episode has never notified (`last_notified_count == 0`) or enough ticks have elapsed
since the last notification (`count - last_notified_count >= wedge_renotify_ticks`). This is a `>=`
test, so a persisted count resumed at any value past a due notification point still fires — it
cannot be skipped by a restart, a changed `--wedge-ticks`/`--wedge-renotify-ticks`, or a crash
between notify and persist (in which case the worst case is one extra, survivable re-ping — the same
notify-then-persist tradeoff already accepted at `orchestrator.py:978-987` for delivery-exhausted).

```python
def _note_wedge(
    stream: str,
    ticket: str,
    wedge_state: dict[str, WedgeState],
    *,
    wedge_ticks: int,
    wedge_renotify_ticks: int,
    trace_id: str,
    notifier: Notifier,
    logger: Logger,
    persist_wedge: Callable[[dict[str, WedgeState]], None],
) -> None:
    prior = wedge_state.get(stream)
    count = (prior.count if prior is not None else 0) + 1
    last_notified = prior.last_notified_count if prior is not None else 0
    should_notify = False
    if count > wedge_ticks:
        logger.warning("dispatch_seat_wedged", trace_id=trace_id, stream=stream,
                        ticket=ticket, consecutive_ticks=count, detail="...")
        should_notify = last_notified == 0 or count - last_notified >= wedge_renotify_ticks
        if should_notify:
            notifier("dispatch_seat_wedged", trace_id=trace_id, stream=stream,
                      ticket=ticket, consecutive_ticks=count)
    wedge_state[stream] = WedgeState(count, count if should_notify else last_notified)
    persist_wedge(wedge_state)
```

One `persist_wedge` call per tick (after the notify decision, so ordering stays notify-then-persist)
— the count itself must persist every tick (not only on notify), since AC-2 requires the *counter*,
not just the notification state, to survive a restart.

### `_reset_wedge` gains persistence

```python
def _reset_wedge(
    stream: str,
    wedge_state: dict[str, WedgeState],
    persist_wedge: Callable[[dict[str, WedgeState]], None],
) -> None:
    if wedge_state.pop(stream, None) is not None:
        persist_wedge(wedge_state)
```

Guarding the persist call on an actual pop (mirrors the `"clear"` case at `orchestrator.py:1080-1082`)
avoids a redundant write every non-wedge tick for streams that were never wedged.

### Threading — `run_once` / `_apply`

- Param renamed `wedge_counts: dict[str, int] | None = None` → `wedge_state: dict[str, WedgeState] | None = None`.
- New param `wedge_renotify_ticks: int = DEFAULT_WEDGE_RENOTIFY_TICKS`.
- New param `persist_wedge: Callable[[dict[str, WedgeState]], None] = lambda _state: None` (no-op
  default so any caller that doesn't care about persistence, e.g. a dry-run, need not supply one).
- All three `_reset_wedge(stream, wedge_counts)` call sites (`orchestrator.py:890, 922, 1043`) become
  `_reset_wedge(stream, wedge_state, persist_wedge)`.
- The `_note_wedge(...)` call site (`orchestrator.py:1032-1041`) gains `wedge_renotify_ticks=` and
  `persist_wedge=`.

### File persistence — mirrors `load_state`/`save_state` exactly

```python
def load_wedge_state(path: Path) -> dict[str, WedgeState]: ...   # empty if absent/invalid
def save_wedge_state(path: Path, state: dict[str, WedgeState]) -> None: ...  # atomic tmp+os.replace
def _default_wedge_state_path() -> Path: ...  # telemetry/dispatch_wedge_state.json
```

**Codex review finding, incorporated:** `load_state`'s defensive check only rejects a non-int
`attempts`; mirroring *only* that for `WedgeState` is not enough, because `last_notified_count` and
`count` have a real relationship between them (`0 <= last_notified_count <= count`) that a corrupt or
hand-edited file can violate. A record with `last_notified_count > count` — e.g. surviving a lowered
`count` from a bug, or a hand-edit — would make `count - last_notified_count` permanently negative,
so `_note_wedge`'s `>=` schedule check never fires again: exactly the "silently suppressed forever"
failure this whole design exists to eliminate. `load_wedge_state` must drop a record (not merely
construct it) when any of: `count` is not a non-bool int, `count < 0`, `last_notified_count` is not a
non-bool int, `last_notified_count < 0`, or `last_notified_count > count`. Dropping loses that
stream's progress (same fail-safe direction `load_state` already takes for a corrupt `attempts`) but
never leaves an unfireable state in place.

Similarly, `_note_wedge` clamps `wedge_renotify_ticks = max(1, wedge_renotify_ticks)` at its top,
mirroring `_note_delivery_failure`'s existing `threshold = max(1, threshold)` clamp
(`orchestrator.py:1270`) and its stated rationale: a sub-1 value is meaningless but a plausible
operator shorthand for "notify every tick," so it's honored rather than rejected, and — critically —
clamping prevents a `<= 0` value from making `count - last_notified_count >= wedge_renotify_ticks`
trivially true forever (spamming) or, if computed unsafely, misbehaving at zero.

### `main()` — load once per tick, exactly like `DispatchRecord`'s `state`

`tick()` already reloads `dispatch_state.json` from disk on **every** tick (not just at process
start) — `state = load_state(state_path)` inside the `tick()` closure. Doing the identical thing for
`wedge_state` makes restart-safety fall out for free: the next tick after a restart reloads the last
persisted `WedgeState` from disk exactly as it would within one continuous run, no special-casing
needed.

```python
def tick() -> None:
    state = load_state(state_path)
    wedge_state = load_wedge_state(wedge_state_path)
    run_once(
        ...,
        wedge_state=wedge_state,
        wedge_ticks=args.wedge_ticks,
        wedge_renotify_ticks=args.wedge_renotify_ticks,
        persist_wedge=lambda st: save_wedge_state(wedge_state_path, st),
        ...
    )
```

Remove the now-dead `wedge_counts: dict[str, int] = {}` line before `tick()`
(`orchestrator.py:1508-1510`; `held_escalated`/`delivery_failures` stay in-memory, unchanged —
out of scope, a different ticket's problem per FRE-924/FRE-927).

New CLI flags (mirroring `--state-file`/`--wedge-ticks`):
- `--wedge-state-file` (default `_default_wedge_state_path()`)
- `--wedge-renotify-ticks` (default `DEFAULT_WEDGE_RENOTIFY_TICKS`)

### Non-code files touched

- `.gitignore`: add `telemetry/dispatch_wedge_state.json` next to the existing
  `telemetry/dispatch_state.json` line (139).
- `docs/runbooks/dispatch-orchestrator.md`: the "Surface and recover a stalled or failed run"
  section documents `dispatch_stall` / `dispatch_held_too_long` / `dispatch_seat_delivery_failing`
  but has no `dispatch_seat_wedged` entry at all (a pre-existing gap). Add one in the same style,
  describing the new re-notify-on-schedule + persisted-across-restart behavior and the two new
  flags.

## Test plan (`tests/scripts/test_orchestrator.py`)

Existing wedge tests (`test_wedge_is_surfaced_past_threshold_and_never_killed`,
`test_wedge_ping_is_once_per_episode_and_re_fires_on_a_new_episode`,
`test_genuinely_busy_seat_is_never_mistaken_for_a_wedge`,
`test_stale_wedge_count_is_reset_on_a_non_wedge_decision`,
`test_blocked_launch_tick_resets_a_stale_wedge_count`,
`test_duplicate_streams_do_not_double_increment_the_wedge_counter`) construct
`wedge_counts: dict[str, int]` literals and pass `wedge_counts=` to `run_once`/read
`wedge_counts["build1"]` back. These become `wedge_state: dict[str, WedgeState]` literals
(`{"build1": WedgeState(count=5)}` instead of `{"build1": 5}`) and `.count` reads instead of raw int
reads, and the kwarg renames to `wedge_state=`. The default `wedge_renotify_ticks` (12) means none of
these short (≤5-tick) tests cross a second notification point, so their notify-count assertions are
unchanged — a mechanical rename, not a behavior change to what they prove.

New tests, in the `# --- FRE-922: suspected-wedge detection ---` section (renamed/extended to
mention FRE-1077):

1. **`test_wedge_renotifies_on_a_schedule_past_the_crossing_tick`** (AC-1) — `wedge_ticks=2`,
   `wedge_renotify_ticks=3`; run 10 continuous-wedge ticks via the existing `_WedgeRunner`. Assert
   `notifier` fired at ticks 3, 6, 9 (three events, not one) — explicitly runs past the *second*
   notification point per the AC's own wording ("a test that stops at the crossing tick passes
   today").
2. **`test_wedge_state_persists_across_a_simulated_restart`** (AC-2) — using real
   `load_wedge_state`/`save_wedge_state` against a `tmp_path` file (not a bare in-memory dict),
   `wedge_ticks=2`, `wedge_renotify_ticks=3`. Run 4 ticks (count reaches 4, one notification already
   fired at count 3, so `last_notified_count == 3`). **Restart precisely mid-schedule** — between the
   first notification and the next due one, per the review finding that the restart point must land
   there to actually exercise the no-reset/no-double-fire property (a restart *at* a just-notified
   tick, or long after the next one is already due, doesn't distinguish correct from broken
   behavior). Discard the in-memory `wedge_state` dict entirely and call `load_wedge_state(path)`
   fresh (simulating the new process). Tick once more (count → 5, `5 - 3 = 2 < 3`): assert **no**
   notification fires — proves the restart itself doesn't spuriously re-arm/double-fire. Tick once
   more (count → 6, `6 - 3 = 3 >= 3`): assert a **second** notification fires now, at the correct
   scheduled count — proves the counter's progress and the notify schedule both survived the restart
   (neither reset to 0 nor lost track of when it last notified).
3. **`test_wedge_state_file_round_trips`** — direct `save_wedge_state`/`load_wedge_state` round trip
   on a `tmp_path`, mirroring `test_a_corrupt_attempts_value_is_dropped_not_crash_looped` /
   `test_an_unknown_phase_is_dropped`'s defensive-load style: a corrupt `count` (e.g. a string) is
   dropped; separately, a structurally-valid-but-invariant-violating record
   (`last_notified_count > count`, e.g. `{"count": 3, "last_notified_count": 9}`) is also dropped —
   the codex-review-flagged case that would otherwise silently suppress the schedule forever.
4. **`test_main_wires_the_new_wedge_flags_through`** — codex review flagged that no test proves
   `main()` actually threads `--wedge-state-file`/`--wedge-renotify-ticks` to `run_once`, only that
   `run_once` itself behaves correctly when called directly. Monkeypatch
   `orch.load_wedge_state`/`orch.save_wedge_state` to record the path they're called with (mirroring
   how `test_main_once_dry_run_no_launch` monkeypatches `fetch_board`), call
   `main(["--once", "--wedge-state-file", str(tmp_path / "w.json"), ...])`, and assert
   `load_wedge_state` was invoked with that exact path — proving the CLI flag reaches the loader
   rather than silently falling back to the default `telemetry/dispatch_wedge_state.json`.

Command: `make test-file FILE=tests/scripts/test_orchestrator.py`, then `make test` (full suite,
since `scripts/dispatch` is exercised by other suites indirectly — check for cross-imports) once the
targeted file is green.

## Codex plan-review disposition

Reviewed by `codex:rescue` (2026-08-11). Verdict: "the plan's core design is sound." Incorporated
into this plan: (1) `load_wedge_state` invariant validation (`last_notified_count <= count`, both
non-negative non-bool ints) — closes a "malformed state silently suppresses the schedule forever"
gap; (2) `_note_wedge` clamps `wedge_renotify_ticks = max(1, ...)`, mirroring the existing
`_note_delivery_failure` threshold clamp; (3) a `main()`-level wiring test for the two new flags;
(4) the restart test is now timed precisely mid-schedule so it actually distinguishes correct
behavior from both "reset" and "double-fire."

Flagged but consciously not changed (accepted, matching existing risk posture already present
elsewhere in this file, not a regression introduced by this ticket):
- **Notify-then-persist can duplicate on a crash between the two** — explicitly the same tradeoff
  already accepted for `dispatch_delivery_exhausted` (`orchestrator.py:978-987`) and `dispatch_stall`;
  at-least-once, never silent loss, which is the correct direction for an alert.
- **The two state files aren't written transactionally together** — a crash between persisting
  `dispatch_state.json` and `dispatch_wedge_state.json` can lose one tick's wedge increment; this
  delays, never loses, the eventual alert (the counter and log warning resume next tick regardless).
- **No file locking** — matches the pre-existing single-writer assumption `dispatch_state.json`
  already carries (one `systemd` daemon instance, `Restart=always`); not a new risk this ticket
  introduces.
- **A dry-run `launch` decision never touches wedge state either way** (`orchestrator.py:947-948`
  returns before the wedge codepath) — pre-existing behavior (dry-run = inspection-only, no side
  effects at all), unchanged by this design.

## Quality gates

`make test-file FILE=tests/scripts/test_orchestrator.py` → `make test` → `make mypy` →
`make ruff-check` + `make ruff-format` → `pre-commit run --all-files`.

## Risk / diff class (Step 8)

Self-serve. Not a production write path (dispatch orchestrator writes only its own local JSON state
files under `telemetry/`, never Neo4j/Postgres/ES/R2), not destructive, not a schema change to any
DB, not cost/governance code. `feature-dev:code-reviewer` review only; `security-review` not
warranted (no new input/subprocess/auth/network surface — the CLI flags are local, and
`load_wedge_state` reuses the same defensive-parse shape already reviewed for `load_state`).
