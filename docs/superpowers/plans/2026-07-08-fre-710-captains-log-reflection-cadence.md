# FRE-710 — Captain's Log reflection fires per-turn — move to a coarser, higher-signal cadence

**Ticket:** FRE-710 (Approved, `stream:build2`, `Tier-2:Sonnet`) — Linear Async Feedback Channel
**Not backed by an ADR** — this is a standalone cost/signal-quality bug against the existing Captain's
Log pipeline (ADR-0040's promotion throttle and `dedup.py`'s fingerprint dedup stay unchanged per the
ticket's explicit instruction).

## 1. Problem (confirmed by research)

`_trigger_captains_log_reflection` (`src/personal_agent/orchestrator/executor.py:1754-1812`) is called
unconditionally at `executor.py:2058`, inside `execute_task`'s `if state == TaskState.COMPLETED:`
branch (`:1955`) — once per completed turn, fire-and-forget via `run_in_background`. Live: ~0.7
reflections per turn, 1864 `AWAITING_APPROVAL` vs 6 `Approved` all-time — the promotion funnel is
flooded with per-turn noise.

**Key finding that shapes the design:** there is **no session-end signal anywhere in the codebase.**
`SESSION_CLOSED` (`telemetry/events.py:108`) is defined but never emitted. `Session`
(`orchestrator/session.py:16-37`) has no turn counter or closed flag. WS disconnect handling
(`transport/agui/ws_endpoint.py`) only logs and breaks the receive loop — it triggers no
session-lifecycle event. So "once per session" cannot be implemented as a literal session-end hook
without building new infrastructure well beyond this ticket's "cheap win" framing. A related but
inexact precedent for "no session-end signal, so debounce instead" is `brainstem/scheduler.py`'s
`_should_consolidate` (`:260-329`): it *has* a `min_consolidation_interval_seconds` debounce path
(`:311-322`), but under the current cloud/remote-inference deployment
(`resource_gating_enabled=False`) that path is skipped entirely — it returns `True` unconditionally at
`:298-299` ("Consolidate on every captured event"), because consolidation there doesn't compete with
local GPU inference the way it does on a local-inference deployment. So this plan does **not** reuse
that debounce as running code; it borrows only the *shape* (a per-unit minimum-interval gate) for a
new, purpose-built gate, since reflection's cost/noise problem (LLM call + funnel flood) is a genuine
concern in the current deployment mode, unlike consolidation's.

## 2. Design — per-session min-interval debounce + a notable-turn bypass

Approximate "once per session" with a **per-`session_id` minimum-interval gate**: the first turn seen
for a session always reflects; subsequent turns for that session only reflect once
`captains_log_reflection_min_interval_seconds` has elapsed since the last one. This is process-local,
in-memory state (mirrors `SessionManager`'s own in-memory-only model — no new persistence layer).

**Notable-turn bypass (addresses AC-3 — "proposals that mattered under per-turn still surface"):** a
turn that hits the iteration limit (`hit_iteration_limit`) **always** reflects regardless of the
interval. Today this predicate is computed *inside* `_trigger_captains_log_reflection`
(`executor.py:1769-1770`) and logged as `captains_log_iteration_limit_reflected` — i.e. the code already
treats it as noteworthy, it's just not available at the call site yet. The new gate code recomputes the
same cheap, pure predicate (`ctx.tool_iteration_count > _resolve_max_iterations(ctx)`) once more at the
call site so the gate can see it before deciding whether to invoke the reflection call at all; I'm not
inventing a new cost/anomaly threshold that isn't already computed somewhere in this code path, to
avoid scope creep past the ticket's ask.

**Eval-mode carve-out:** `ctx.eval_mode` turns **bypass the cadence gate entirely** (always reflect,
same as today). FRE-523's comment at `executor.py:1964-1969` states eval turns must feed the cognitive
pipeline (capture + reflection) so consolidation can write eval-derived KG content; evals are a bounded,
intentional, already-higher-cost context (CLAUDE.md's `evaluation` marker = "100+ calls"), and
`test_eval_isolation.py::test_eval_mode_now_writes_capture_and_reflection` currently asserts
per-turn-for-eval as an isolation guarantee. Changing eval density is out of this ticket's scope.

**Rollback lever:** `captains_log_reflection_cadence_enabled` (default `True`) reverts to unconditional
per-turn reflection when `False`, no code change needed — matches the codebase's existing convention
of an `_enabled` flag on cadence/behavior changes (e.g. `insights_wiring_enabled`).

### New file: `src/personal_agent/captains_log/reflection_cadence.py`

```python
# How long a session's entry survives in the map with no reflected turn before it's evicted, as a
# multiple of min_interval_seconds. Generous enough that a session legitimately debouncing across
# the interval is never evicted mid-window; only sessions that have gone quiet for well past their
# own interval are pruned.
_EVICTION_MULTIPLIER = 4.0

@dataclasses.dataclass
class ReflectionCadenceGate:
    min_interval_seconds: float
    _last_reflected_at: dict[str, float] = dataclasses.field(default_factory=dict)

    def should_reflect(self, session_id: str, *, hit_iteration_limit: bool, now: float | None = None) -> bool:
        """True on a notable turn (hit_iteration_limit) or when >= min_interval_seconds have
        elapsed since the last reflected turn for this session_id (or none yet). Records `now`
        against session_id as a side effect whenever it returns True. Synchronous / no `await`
        anywhere in this method by design -- that (not a lock) is what makes the check-then-set
        against the shared dict safe under asyncio's single-threaded cooperative scheduling; a
        future edit must not make this a coroutine without re-establishing that safety some other
        way.

        Every call also opportunistically prunes entries older than
        min_interval_seconds * _EVICTION_MULTIPLIER, bounding the dict's size by recently-active
        sessions rather than growing unboundedly over the gateway process's lifetime."""
        current = time.time() if now is None else now
        eviction_after = self.min_interval_seconds * _EVICTION_MULTIPLIER
        self._last_reflected_at = {
            sid: t for sid, t in self._last_reflected_at.items() if current - t < eviction_after
        }
        ...  # decide + record as before, against the now-pruned dict

_gate: ReflectionCadenceGate | None = None

def get_reflection_cadence_gate() -> ReflectionCadenceGate: ...   # lazy process-global singleton,
                                                                    # mirrors events/bus.py's get_event_bus()
def reset_reflection_cadence_gate() -> None: ...                  # test isolation only
```

### `settings.py` — new fields (near the Captain's Log section, ~line 1572)

```python
captains_log_reflection_cadence_enabled: bool = Field(
    default=True,
    description="FRE-710: gate Captain's Log reflection to a per-session cadence instead of "
    "every turn. False reverts to unconditional per-turn reflection (pre-FRE-710 behavior).",
)
captains_log_reflection_min_interval_seconds: float = Field(
    default=1800.0,
    ge=0,
    description="FRE-710: minimum seconds between reflected turns for the same session_id, "
    "approximating 'once per session' (no durable session-end signal exists to trigger on "
    "literally). A turn that hits the iteration limit always bypasses this interval.",
)
```

### `executor.py` — the call site (replaces lines 2053-2058 only)

```python
# Trigger Captain's Log reflection (LLM-based, background), gated to a coarser
# per-session cadence (FRE-710) rather than every turn.
from personal_agent.captains_log.background import run_in_background
from personal_agent.captains_log.reflection_cadence import get_reflection_cadence_gate

hit_iteration_limit = ctx.tool_iteration_count > _resolve_max_iterations(ctx)
should_reflect = (
    ctx.eval_mode
    or not settings.captains_log_reflection_cadence_enabled
    or get_reflection_cadence_gate().should_reflect(
        ctx.session_id, hit_iteration_limit=hit_iteration_limit
    )
)
if should_reflect:
    run_in_background(_trigger_captains_log_reflection(ctx))
else:
    log.debug(
        "captains_log_reflection_skipped_cadence",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
    )
```

No change to `_trigger_captains_log_reflection` itself, `capture.py`'s fast capture write, the
`RequestCapturedEvent` publish, `dedup.py`, or `promotion.py` — all explicitly out of scope per the
ticket.

## 3. Tests (TDD)

- `tests/test_captains_log/test_reflection_cadence.py` (new, pure unit tests, no LLM/IO):
  - First turn for a fresh `session_id` → `should_reflect` returns `True`.
  - A second turn for the same session, `now` inside the interval → returns `False`.
  - A second turn for the same session, `now` at/after `min_interval_seconds` → returns `True`.
  - `hit_iteration_limit=True` always returns `True` regardless of interval (AC-3's bypass).
  - Two different `session_id`s are independent (one session's debounce doesn't starve another —
    the load-bearing property of per-session vs. global debounce).
  - `get_reflection_cadence_gate()` returns the same instance across calls (singleton); after
    `reset_reflection_cadence_gate()` a fresh instance with clean state is returned.
  - A stale entry (older than `min_interval_seconds * _EVICTION_MULTIPLIER`) is pruned from the
    internal map on the next `should_reflect` call for a *different* session (proves eviction without
    depending on internal attribute access from the test for the pruned session itself).
  - `should_reflect` is not a coroutine function (`inspect.iscoroutinefunction`) — pins the
    synchronous-by-design invariant the asyncio-safety argument depends on.
- `tests/test_orchestrator/test_eval_isolation.py` — update
  `test_eval_mode_now_writes_capture_and_reflection` if needed to confirm eval_mode still reflects
  unconditionally (bypasses the new gate) — verifying the carve-out rather than breaking on it.
- New test in `tests/test_orchestrator/` (or extend an existing executor test file) proving the
  **outcome** at the `execute_task` level for a non-eval session: two consecutive completed turns in
  the same session within the debounce interval trigger `_trigger_captains_log_reflection` **once**,
  not twice (mock/patch `_trigger_captains_log_reflection` and assert call count — mirrors the existing
  `test_eval_mode_now_writes_capture_and_reflection` pattern). This is the AC-1 proof ("reflection no
  longer fires on every turn").
- A test proving a turn with `hit_iteration_limit=True` still triggers `_trigger_captains_log_reflection`
  even immediately after a prior reflected turn in the same session (AC-3 proof at the `execute_task`
  level, not just the gate's unit test).
- A test proving a **first** completed, non-eval turn for a brand-new `session_id` schedules
  `_trigger_captains_log_reflection` (AC-2 proof at the `execute_task` level, not just the gate's
  unit-level first-turn property — closes the rollback-contract gap between "the gate says yes" and
  "the executor actually calls it").
- A test proving `captains_log_reflection_cadence_enabled=False` preserves unconditional per-turn
  reflection: two consecutive completed turns in the same session, within the debounce interval, both
  trigger `_trigger_captains_log_reflection` when the flag is off — the rollback lever's whole reason
  to exist.

## 4. Acceptance-criteria mapping

- **AC-1** ("no longer fires on every turn... roughly per-session, not ~0.7/turn"): proven by the
  `ReflectionCadenceGate` interval tests + the `execute_task`-level "two turns, one reflection" test.
- **AC-2** ("a session with real content still produces a reflection"): the first-turn-always-reflects
  property (no session goes un-reflected).
- **AC-3** ("proposals that mattered under per-turn still surface"): the `hit_iteration_limit` bypass,
  proven at both the gate-unit and `execute_task` level.

## 5. Quality gates

`make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
`pre-commit run --all-files`.

## 6. Files touched

- `src/personal_agent/captains_log/reflection_cadence.py` (new)
- `src/personal_agent/config/settings.py` (add 2 fields)
- `src/personal_agent/orchestrator/executor.py` (replace lines 2053-2058 only)
- `tests/test_captains_log/test_reflection_cadence.py` (new)
- `tests/test_orchestrator/test_eval_isolation.py` (verify/adjust eval-mode carve-out)
- A new or extended `tests/test_orchestrator/` executor-level test for the gated call site
