# FRE-547 — Cap-utilization telemetry: `budget_counters` → ES snapshot emitter

> **Date:** 2026-06-12 · **Ticket:** FRE-547 (Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Refs:** ADR-0065 (cost_gate) · FRE-536 (C1 cost & budget dashboard) ·
> `docs/research/2026-06-08-fre-536-cost-budget-dashboard.md` (§ Deferred — cap utilization)

## Problem

The single most useful budget panel — **cap utilization vs configured caps** — can't be built
from ES today. Live counter state (`running_total`, `cap_usd`) lives only in Postgres
`budget_counters`, which Kibana can't read. cost_gate lifecycle events emit reservation *deltas*,
not the standing counter total, and `BudgetDenied` is raised *before* any ES emit in the reserve
path — so there is no ES signal for "how full is each cap right now". FRE-536 shipped every other
cost/budget panel and deliberately deferred this one.

## Approach

A periodic emitter (mirroring the existing `cost_gate` reaper) snapshots each configured cap's
current-window `running_total` to ES as a `budget_counter_snapshot` event, with explicit ES type
mappings, then a **Cap Utilization** panel renders `utilization_ratio` by `role` × `time_window`
with a 1.0 threshold marker.

**Why config-driven (iterate `config.caps`), not table-scan:** "how full is each cap right now"
maps 1:1 to the configured caps. For each cap we compute the current `window_start`
(`_window_start(cap.time_window)`) and read that exact counter row; a missing row means nothing
reserved yet this window → `running_total = 0.0`. This always emits current-window state (never
stale past windows) and reuses the gate's existing window arithmetic.

**ES type discipline (the FRE-536 / "mappings wrong first pass" traps):**
- `running_total`, `cap_usd`, `utilization_ratio` come from Postgres as `Decimal`. `Decimal` is not
  JSON-serializable, so es_handler stringifies it → `keyword` (un-aggregatable). **Emit `float(...)`**,
  exactly as the gate does for `amount_usd`. `float(0)` → `0.0` (JSON float) → ES maps `float`/`double`,
  **never** the `0 → long` trap (that needs a bare integer `0`).
- `window_start` is a `datetime`. `str(datetime)` → `"2026-06-12 00:00:00+00:00"` (space separator)
  which **fails** ES `strict_date_optional_time` date parsing. **Emit `window_start.isoformat()`** →
  `"2026-06-12T00:00:00+00:00"` (T separator) which ES parses.
- `role`, `time_window` are plain strings → resolve to `keyword` via the template's
  `default_string_keyword` dynamic template (the existing cost panels already use `role` as keyword
  successfully). Add explicit `double`/`date` mappings only for the four at-risk numeric/date fields.

**No existing-index conflict:** grep confirms `running_total` / `cap_usd` / `utilization_ratio` /
`window_start` are **never emitted to `agent-logs-*` today** (they exist only as Postgres columns /
Python locals / an unrelated `rate_limiting` struct). Brand-new field names → no `keyword`↔`double`
clash, no destructive reindex.

**Identity threading (ADR-0074):** this is a standing-state *gauge* on a fixed cadence with no
originating request — there is no `trace_id`/`task_id` to thread (same as the reaper's
`cost_gate_reaper_swept`). Documented, not threaded.

## Files

### 0. `src/personal_agent/cost_gate/gate.py` — `_window_start` takes optional `now`
**(codex review #3 — window-boundary consistency)** Add an optional `now` param so a single
captured instant drives every cap's window in one snapshot batch (a run straddling midnight / a week
boundary must not mix old- and new-window rows):
```python
def _window_start(time_window: str, now: datetime | None = None) -> datetime:
    ...
    if now is None:
        now = datetime.now(timezone.utc)
    ...
```
Backward-compatible: existing callers (`reserve`, `commit`, `refund`, `reap_stale`) pass no `now`.

### 1. `src/personal_agent/cost_gate/gate.py` — add `snapshot_counters()` method
New `async def snapshot_counters(self) -> int` on `CostGate` (after `reap_stale`). Captures `now`
once and reads inside a **`repeatable_read`** transaction so all caps share one consistent
point-in-time view (codex review #3 — concurrent reserve/commit between reads):
```python
async def snapshot_counters(self) -> int:
    """Emit one ``budget_counter_snapshot`` event per configured cap.

    Reads each cap's current-window ``running_total`` from ``budget_counters``
    and emits the standing counter state to Elasticsearch so Kibana can render
    cap utilization (``running_total`` vs ``cap_usd``) — state that otherwise
    lives only in Postgres and is invisible to the dashboard.

    A single ``now`` drives every window computation and all reads run inside
    one ``repeatable_read`` transaction, so the batch is a consistent
    point-in-time snapshot even across a window boundary or a concurrent
    reserve/commit. Money/ratio fields are emitted as ``float`` (not
    ``Decimal``) and ``window_start`` as an ISO-8601 string so Elasticsearch
    maps them as ``double``/``date`` rather than ``keyword`` (FRE-536 discipline).

    Returns:
        Number of snapshot events emitted (one per configured cap).
    """
    if self.pool is None:
        raise RuntimeError("CostGate.connect() must be called before snapshot_counters()")

    now = datetime.now(timezone.utc)
    emitted = 0
    async with self.pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read"):
            for cap in self.config.caps:
                window_start = _window_start(cap.time_window, now)
                row = await conn.fetchrow(
                    """
                    SELECT running_total FROM budget_counters
                     WHERE user_id IS NULL
                       AND time_window = $1
                       AND provider IS NULL
                       AND role = $2
                       AND window_start = $3
                    """,
                    cap.time_window,
                    cap.role,
                    window_start,
                )
                running_total = float(row["running_total"]) if row is not None else 0.0
                cap_usd = float(cap.cap_usd)
                utilization_ratio = running_total / cap_usd if cap_usd > 0 else 0.0
                log.info(
                    "budget_counter_snapshot",
                    role=cap.role,
                    time_window=cap.time_window,
                    window_start=window_start.isoformat(),
                    running_total=running_total,
                    cap_usd=cap_usd,
                    utilization_ratio=utilization_ratio,
                )
                emitted += 1
    log.debug("budget_counter_snapshot_emitted", count=emitted)
    return emitted
```
Empty `config.caps` → loop runs zero times → no ES events, just the debug summary (no noisy
empty-batch event; codex review #5).

### 2. `src/personal_agent/cost_gate/snapshotter.py` — new runner (mirrors `reaper.py`)
```python
"""Background task that snapshots budget_counters to ES (FRE-547 / ADR-0065).

Runs every 60 seconds, calling :meth:`CostGate.snapshot_counters`, so the
Cost & Budget dashboard can render cap utilization — counter state that lives
only in Postgres and is otherwise invisible to Kibana.
"""
from __future__ import annotations
import asyncio
from contextlib import suppress
import structlog
from personal_agent.cost_gate.gate import CostGate

log = structlog.get_logger(__name__)
DEFAULT_SNAPSHOT_INTERVAL_SECONDS = 60.0

async def run_counter_snapshotter(
    gate: CostGate, *, interval_seconds: float = DEFAULT_SNAPSHOT_INTERVAL_SECONDS,
) -> None:
    """Snapshot budget counters to ES on a fixed cadence until cancelled.

    Sleeps *before* the first emit (codex review #1): the snapshotter task is
    spawned in the FastAPI lifespan before the Elasticsearch log handler is
    attached, so emitting immediately would route the first batch only to
    file/console — never to ES. The 60s sleep guarantees the handler is wired
    before the first emit.
    """
    log.info("cost_gate_snapshotter_started", interval_seconds=interval_seconds)
    try:
        while True:
            await asyncio.sleep(interval_seconds)  # sleep-first: ES handler attached by now
            try:
                await gate.snapshot_counters()
            except Exception as exc:  # noqa: BLE001 — log + continue
                log.error("cost_gate_snapshot_failed", error=str(exc), exc_info=True)
    except asyncio.CancelledError:
        log.info("cost_gate_snapshotter_stopped")
        with suppress(asyncio.CancelledError):
            raise
```

### 3. `src/personal_agent/cost_gate/__init__.py` — export
Add `from ...snapshotter import DEFAULT_SNAPSHOT_INTERVAL_SECONDS, run_counter_snapshotter`,
add both to `__all__`, mention in module docstring.

### 4. `src/personal_agent/service/app.py` — lifespan wiring
- New global `cost_gate_snapshotter_task: asyncio.Task[None] | None = None` (line ~64).
- Add to the lifespan `global` declaration.
- In startup, right after `cost_gate_reaper_task = asyncio.create_task(run_reaper(cost_gate))`:
  import `run_counter_snapshotter`, `cost_gate_snapshotter_task = asyncio.create_task(run_counter_snapshotter(cost_gate))`.
- In shutdown (after the reaper-task cancel block), mirror the cancel/await/None pattern for
  `cost_gate_snapshotter_task`.

### 5. `docker/elasticsearch/index-template.json` — explicit mappings
Add to `template.mappings.properties` (alongside `amount_usd` etc.):
```json
"running_total": {"type": "double"},
"cap_usd": {"type": "double"},
"utilization_ratio": {"type": "double"},
"window_start": {"type": "date"}
```
(`scripts/setup-elasticsearch.sh` reads this file directly — no script change.)

### 6. `config/kibana/dashboards/cost_budget.ndjson` — Cap Utilization panel
- New visualization object `cb-cap-utilization` (legacy aggs-based `histogram`, **not** Lens — Lens
  fails strict `_import`, FRE-546):
  - query `event_type:budget_counter_snapshot`
  - metric agg: `max` of `utilization_ratio` (label "Max cap utilization")
  - segment agg: `terms` on `role` (size 12)
  - group agg: `terms` on `time_window` (size 5)
  - params: `thresholdLine {show:true, value:1, width:1, style:"full", color:"#E7664C"}`, legend right
  - references the shared `agent-logs-pattern` index-pattern id (no duplicate index patterns).
- Dashboard object `cost-budget-dashboard`: add `panel_6` → `cb-cap-utilization`, gridData
  `{x:0, y:45, w:48, h:15, i:"7"}`, append the reference, update description.
- Already registered in `config/kibana/import_dashboards.sh` (line 37) — no script change.

### 7. `tests/personal_agent/cost_gate/test_snapshotter.py` — NEW (unit + integration)
**Unit tests (no marker, run in `make test`)** — fake asyncpg pool/conn (no live DB), verify the
emit contract that's most likely to be wrong:
- `snapshot_counters()` emits one `budget_counter_snapshot` per cap (return value == len(caps)).
- `running_total`, `cap_usd`, `utilization_ratio` are `float` (not str/Decimal); ratio == running/cap.
- `window_start` is an ISO string containing `"T"` (ES-parseable), not a space-separated `str(datetime)`.
- a cap with **no** counter row → `running_total == 0.0`, `utilization_ratio == 0.0` (no crash).
- `cap_usd == 0` guard → ratio `0.0`, no ZeroDivisionError.
- the same `now` reaches every cap's `_window_start` (patch `_window_start`, assert all calls share
  one `now`) — proves the window-boundary fix.

**Integration test (`@pytest.mark.integration`, needs `make test-infra-up`)** — codex review #4,
mirrors `test_gate_emit_types.py`; reuses the `cost_gate` / `db_pool` / `unique_role` fixtures:
- reserve a known amount against `unique_role`, call `snapshot_counters()`, assert the captured
  `budget_counter_snapshot` for that role carries `running_total` == reserved and
  `utilization_ratio` == reserved / daily_cap — exercises the real SQL + schema path.

## Verification (TDD order)
1. Write `test_snapshotter.py` → `make test-file FILE=tests/personal_agent/cost_gate/test_snapshotter.py` → **fails** (no `snapshot_counters`).
2. Implement gate method + snapshotter + exports + lifespan → test **passes**.
3. `python -c "import json; json.load(open('config/kibana/dashboards/cost_budget.ndjson'.replace(...)))"` per-line JSON validity + template JSON valid.
4. `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` clean.
5. `make test` (cost_gate module, then full) green.

## Post-deploy (Linear comment for master — NOT in PR checklist)
- Register updated `agent-logs-template` + **additive** `PUT agent-logs-<today>/_mapping` for the four
  new fields (template governs only new indices; today's live index needs the additive double/date
  mapping before the emitter's first run, else the fields land as `float`/auto on today's index).
- Import `cost_budget.ndjson` to live Kibana (`_import?overwrite=true`); confirm 9 objects resolve.
- `_field_caps` proof: the four fields resolve to `double`/`date`; panel populates against real
  counter snapshots after one 60s cadence.

## Halt-condition check
Single self-contained feature (emitter + mapping + panel) — **one phase, one PR**. No historical rows
dropped. No ADR-phase bundling. No expected mypy regression.
