# FRE-560 — Consolidation never triggers; KG write pipeline stalled

> **Date:** 2026-06-13 · **Ticket:** FRE-560 (Tier-1:Opus, Bug, Urgent) · **Project:** Memory Recall Quality
> **Refs:** ADR-0041 (event-driven consolidation / `request.captured`) · FRE-523 (eval pipeline writes captures) · FRE-435/ADR-0087 (KG write-completeness)

## Root cause — CONFIRMED (code logic + measured facts, no live DEBUG needed)

Consolidation is now **purely event-driven**: `_should_consolidate` has exactly one caller,
`on_request_captured` (scheduler.py:240). There is no polling fallback.

`_should_consolidate` (scheduler.py:243–342) has three gate groups:
1. **active-request** (line 271): `if self._active_request_count > 0: return False`
2. **min-interval** (line 280): `if self.last_consolidation: if time_since_last < min: return False`
3. **resource gates** (idle/CPU/mem), only reached when `resource_gating_enabled`.

Measured on the running cloud-sim container: `resource_gating_enabled=False`,
`enable_second_brain=True`, `event_request_captured_received=32`, `consolidation_triggered=0`.

**Decisive elimination:**
- `last_consolidation` is initialised `None` (line 93) and is **only** assigned inside
  `_trigger_consolidation` (line 378), which runs **only** when `_should_consolidate` returns True.
  Since `consolidation_triggered=0`, `_trigger_consolidation` never ran → `last_consolidation` is
  still `None` → the min-interval gate's body (line 281–283) **never executes**. It is *vacuous* and
  **cannot** be the declining gate. (Refutes the "min-interval" half of the hypothesis.)
- `resource_gating_enabled=False` → gates in group 3 are never reached (line 285 returns True first).
- ∴ The **only** statement that can return False is **line 271 (active-request)**. 32 receives →
  0 triggers means **all 32** calls observed `_active_request_count > 0`. QED.

**Why count is always > 0 at consume time (structural, not flaky):** `request.captured` is published
from `executor.py:1526` — *inside* `orchestrator.handle_user_request(...)`, right after
`write_capture()` (1509, capture already on disk) and `TASK_COMPLETED` (heavy inference done). That
call is wrapped by `notify_request_start()` (count +1) at entry and `notify_request_end()` (count −1)
only in the `finally` **after it returns** (app.py:279/310 and 1669/1703). The Redis-Streams
consolidator consumer (`on_request_captured`) runs concurrently and dequeues the event while the
publishing request is still between publish and the `finally` → count ≥ 1 → gate returns False. The
event-driven path structurally races its own publisher's in-flight window and loses every time.

**Compounding:** even if the pre-gate passed, `_trigger_consolidation` builds
`should_pause=lambda: self._active_request_count > 0` (line 370), so the consolidator would
immediately pause on the same triggering request.

## Fix — the active-request gate is resource-gating, not a universal guard (owner direction, 2026-06-13)

The active-request check exists **only** to keep background consolidation from competing with
**on-device** inference in the local-MLX deployment (its docstring says exactly this). In the
**cloud / remote-inference** deployment (`resource_gating_enabled=False`), consolidation runs on the
gateway VPS and does not compete with inference (a different machine) for any resource — so there is
no reason to defer on in-flight request count, and **no reason to wait for idle**. The bug is that the
active-request check was placed as a *universal* guard ahead of the `resource_gating_enabled` switch,
so it structurally blocked event-driven consolidation in cloud. Fix: move it (and the consolidator's
`should_pause`) **inside** the resource-gated section. The only universal guard left is the
min-interval rate-limit (desirable: don't run a full pass on every turn).

This is simpler than an idle-drain mechanism and matches the deployment reality the owner flagged:
in cloud, consolidate on the `request.captured` event immediately, regardless of `_active_request_count`
(also robust to a long-lived batch endpoint holding `count>0`). Local behaviour is preserved exactly.

### `src/personal_agent/brainstem/scheduler.py`
- `__init__`: add observability state — `_consolidations_run = 0`, `_consolidation_skips_active = 0`,
  `_consolidation_skips_min_interval = 0`, `_consolidation_in_progress = False`,
  `_last_request_captured_at: datetime | None = None`, `_started_at: datetime | None = None`,
  `_last_health_emit: tuple[int, int, int] | None = None`.
- `start()`: set `self._started_at = datetime.now(timezone.utc)`.
- `_should_consolidate(self, *, trace_id=None) -> bool` — the **resource-gating short-circuit moves
  to the top**: in cloud/remote-inference, none of the host guards (active-request, min-interval,
  idle/CPU/mem) apply — consolidate on every captured event; `_trigger_consolidation`'s single-flight
  guard coalesces bursts (owner direction: throttle dropped in cloud; entity extraction is a cloud
  API — `gpt-5.4-nano` — not the local GPU, so there is nothing to defer for). All guards remain for
  the local-MLX deployment, unchanged:
  ```python
  # Cloud / remote-inference: no GPU contention, no idle to wait for → consolidate
  # on every event. Single-flight in _trigger_consolidation coalesces bursts. (FRE-560)
  if not self.resource_gating_enabled:
      return True

  # --- Local-inference deployment only: defer to protect the on-device GPU. ---
  if self.last_consolidation:
      time_since_last = (now - self.last_consolidation).total_seconds()
      if time_since_last < self.min_consolidation_interval_seconds:
          self._consolidation_skips_min_interval += 1
          log.debug("consolidation_skipped_min_interval",
                    seconds_since_last=time_since_last,
                    min_interval=self.min_consolidation_interval_seconds, trace_id=trace_id)
          return False
  if self._active_request_count > 0:
      self._consolidation_skips_active += 1
      log.debug("consolidation_skipped_active_requests",
                active_request_count=self._active_request_count, trace_id=trace_id)
      return False
  # ... idle / CPU / memory guards unchanged ...
  ```
- `_trigger_consolidation(self, *, trace_id=None)` — `should_pause` is resource-gated (None in
  cloud → never pauses; the local lambda is unchanged), plus a single-flight guard + run counter:
  ```python
  if self._consolidation_in_progress:
      log.debug("consolidation_already_in_progress", trace_id=trace_id)
      return
  self._consolidation_in_progress = True
  try:
      should_pause = (lambda: self._active_request_count > 0) if self.resource_gating_enabled else None
      result = await self.consolidator.consolidate_recent_captures(
          days=7, limit=50, should_pause=should_pause)
      if result.get("captures_processed", 0) > 0:
          self.last_consolidation = datetime.now(timezone.utc)
      self._consolidations_run += 1
      ...  # existing consolidation_completed log + _publish_consolidation_completed
  finally:
      self._consolidation_in_progress = False
  ```
- `on_request_captured`: unchanged control flow; add `self._last_request_captured_at = now` for the
  health line. (Still: `if await self._should_consolidate(...): await self._trigger_consolidation(...)`.)
- `_lifecycle_loop`: emit `consolidation_health` at INFO **only when a counter changed** since last
  emit (avoids 60s idle noise; makes a perpetually-skipping scheduler loud):
  ```python
  snap = (self._consolidations_run, self._consolidation_skips_active,
          self._consolidation_skips_min_interval)
  if snap != self._last_health_emit:
      self._last_health_emit = snap
      log.info("consolidation_health",
               consolidations_run=snap[0], skips_active_requests=snap[1],
               skips_min_interval=snap[2], active_request_count=self._active_request_count,
               seconds_since_last_consolidation=(
                   (now - self.last_consolidation).total_seconds() if self.last_consolidation else None),
               last_request_captured_at=(
                   self._last_request_captured_at.isoformat() if self._last_request_captured_at else None),
               scheduler_uptime_s=(
                   (now - self._started_at).total_seconds() if self._started_at else None),
               trace_id=iteration_trace_id)
  ```

**Behaviour after fix:**
- Cloud (`resource_gating_enabled=False`): `request.captured` → `_should_consolidate` passes
  (min-interval permitting) → consolidates immediately, in-flight count irrelevant, `should_pause=None`
  so it runs to completion. Rate-limited to once / `min_consolidation_interval_seconds` (60s); the
  next post-interval capture drains anything that accrued (`consolidate_recent_captures` reads the
  last 7 days). Robust to a batch endpoint holding `count>0`.
- Local (`resource_gating_enabled=True`): identical to today — active-request + idle/CPU/mem guards
  apply, `should_pause` lambda protects the run.
- Single-flight guard prevents an overlapping run if one consolidation outlasts the next trigger.

ADR-0074-clean: new `log.*` lines thread `trace_id`. No new `bus.publish`/Cypher.

## Tests — `tests/test_brainstem/test_scheduler.py` (TDD: write first, confirm red)
1. **Reproduce the bug → fixed (cloud):** `resource_gating_enabled=False`, `last_consolidation=None`,
   `notify_request_start()` (count=1) → `await _should_consolidate()` → **True** (today it's False;
   this is the core regression test for the gate ordering).
2. **Local still defers on active request:** `resource_gating_enabled=True`, count=1 →
   `_should_consolidate()` → **False** (existing `test_should_not_consolidate_when_request_active`
   stays green — local behaviour preserved).
3. **Min-interval still universal (cloud):** gating off, `last_consolidation=now` →
   `_should_consolidate()` → **False** (rate-limit applies in all modes); increments
   `_consolidation_skips_min_interval`.
4. **Event path end-to-end:** gating off, count=1, patch `_trigger_consolidation`,
   `await on_request_captured(trace, sess)` → `_trigger_consolidation` awaited once.
5. **`should_pause` is resource-gated:** capture the kwarg passed to `consolidate_recent_captures`
   (mock consolidator) — `None` when `resource_gating_enabled=False`; a callable when True.
6. **Single-flight:** with `_consolidation_in_progress=True`, `_trigger_consolidation` returns without
   calling `consolidate_recent_captures` again.
7. **Observability:** after N active-skips (local) `_consolidation_skips_active == N`; min-interval
   skip increments its counter + logs; the health emit produces a `consolidation_health` INFO line
   only when a counter changed, with `consolidations_run` / `skips_active_requests` /
   `scheduler_uptime_s` in the payload.

Test command: `make test-file FILE=tests/test_brainstem/test_scheduler.py`

## Quality gates
`make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
`pre-commit run --all-files`.

## Post-deploy (Linear comment for master — NOT in PR checklist)
- Deploy `seshat-gateway`; AC requires **live** confirmation. After a real turn (non-eval) and an
  eval turn, within ≤ the min-interval: `MATCH (t:Turn) WHERE t.timestamp > <deploy-ts>` → > 0, and
  recent `Entity` nodes appear. Confirms captures now drain to Neo4j (AC-2, eval AND non-eval).
- `consolidation_triggered` / `consolidation_completed` INFO events appear in ES after captures.
- `consolidation_health` line shows `consolidations_run` climbing and `skips_active_requests` no
  longer monotonic-with-zero-runs (AC-3 observability).
- Note: this unblocks FRE-523's KG-half AC-1/AC-3.

## Halt-condition check
Single bug, single PR (fix + its observability + regression tests — all AC items). No historical
rows dropped. No ADR-phase bundling. No expected mypy regression. Root cause confirmed (not assumed)
by the min-interval-vacuous elimination + the publish-inside-request-window code fact + measured
0/32.
