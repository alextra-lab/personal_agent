# FRE-568 — ADR-0092 impl 1/5: projector session aggregate + hydration

**Ticket:** FRE-568 (Approved, Tier-2:Sonnet, project Observability Foundation)
**ADR:** ADR-0092 §D2/§D3/§D4 · upholds ADR-0088 D3 (cost roll-up) + D4 (sole `turn_status` emitter)
**Scope:** session-scoped cost + context-occupancy on the `TurnObservationProjector`. **Out of scope** (FRE-570, impl 2/5): the A/B/D compaction marker events and the four compaction `turn_status` fields. **Out of scope** (FRE-573): PWA render.

## What ships

1. A `SessionAggregate` (cost-by-trace + carried context tokens), held in a new `_by_session` map alongside the unchanged per-trace `_by_trace`.
2. Session cost rolled up **idempotently by trace_id** (set, never `+=`) at `turn.completed`; surfaced value = `sum(...)`.
3. Session context occupancy carried across turns (latest `context_tokens`, no reset-to-0).
4. Hydrate-on-first-touch from `api_costs` grouped by `trace_id` (restart-safe, no double-count) via an injected source.
5. Two new `turn_status` STATE_DELTA fields: `session_cost_usd`, `session_context_tokens`.
6. A static teeth test asserting the projector is the **only** `emit_turn_status` invoker (ADR-0088 D4).

## Files

- `src/personal_agent/observability/topology/projector.py` — `SessionAggregate`, `_by_session`, `_ensure_session`, `_hydrate`, branch updates, `_emit` fields, `SessionCostHydrator` type, `_MAX_TRACKED_SESSIONS`.
- `src/personal_agent/observability/route_trace/ledger.py` — `fetch_session_costs_by_trace(session_id: str) -> dict[str, float]`.
- `src/personal_agent/service/app.py` — wire the ledger-backed hydration source into the projector constructor.
- `tests/observability/topology/test_projector.py` — session-cost accumulation, carry, hydration restart-safety, no-double-count, best-effort hydration.
- `tests/observability/route_trace/test_ledger.py` — `fetch_session_costs_by_trace` grouping + no-pool path.
- `tests/observability/topology/test_ci_teeth.py` — sole-`emit_turn_status` static guard.

## Design

### `SessionAggregate`
```python
@dataclass
class SessionAggregate:
    session_id: str
    costs: dict[str, float] = field(default_factory=dict)  # trace_id -> authoritative cost
    context_tokens: int = 0                                 # latest occupancy, carried across turns
    hydrated: bool = False
```

### Hydration source (injected, keeps projector DB-decoupled + unit-testable)
```python
from collections.abc import Awaitable, Callable
SessionCostHydrator = Callable[[str], Awaitable[dict[str, float]]]  # session_id -> {trace_id: cost}
```
`__init__(self, hydration_source: SessionCostHydrator | None = None)`. Default `None` ⇒ carry-only (no read), which is also the ADR's slow-substrate fallback.

### Lifecycle
- `_ensure_session(session_id)` (async): get-or-create with LRU eviction (`_MAX_TRACKED_SESSIONS = 2000`); on first touch set `hydrated = True` then `await self._hydrate(sess)`.
- `_hydrate(sess)`: if source `None` return; else `try costs = await source(...)` (best-effort, `except` → debug log + return); `for tid, c in costs.items(): sess.costs.setdefault(tid, c)` — `setdefault` so a value already set live this process is never clobbered (idempotent convergence, ADR §D4).
- `handle`: in every handled branch call `sess = await self._ensure_session(event.session_id)`.
  - `TurnProgressEvent`: `sess.context_tokens = event.context_tokens` (D3 carry-latest).
  - `TurnCompletedEvent`: `sess.costs[event.trace_id] = event.cost_authoritative_usd` (D2 set/overwrite — live always wins over a stale hydrated partial). Done before the existing `_emit` + per-trace pop.
- `_emit(obs)`: look up `sess = self._by_session.get(obs.session_id)`; add
  `"session_cost_usd": round(sum(sess.costs.values()), 6) if sess else 0.0` and
  `"session_context_tokens": sess.context_tokens if sess else 0`.

### Idempotency / restart-safety argument
Hydration runs once, on the session's first event in-process (before any live completion for that session in this process). `setdefault` on hydrate + plain `=` on live completion ⇒ a trace present in both is stored once and live wins. After a restart, a fresh `_by_session` re-hydrates from `SUM(api_costs) GROUP BY trace_id`, which already includes any completed trace's final cost ⇒ correct totals, no double-count (fresh map).

**In-flight partial surfacing (ADR-accepted, codex flag #3).** A restart *mid-turn* hydrates the current trace's already-written partial `api_costs` rows, so session cost briefly shows partial-then-final until that trace's `turn.completed` overwrites the same key. ADR-0092 §D4 explicitly accepts this ("reconciled by the same `trace_id`-keyed overwrite … never counts a trace twice nor stalls on one") — bounded, self-correcting, no change.

**Eviction note (codex flag #4).** Cost survives eviction (re-hydrated from `api_costs` on the next touch — a fresh aggregate has `hydrated=False`). `context_tokens` is **carry-only, not hydrated**: evicting an idle session and resuming it resets session context to `0` until the next `TurnProgressEvent` re-establishes it (within the same turn). Accepted for a live cosmetic gauge given `_MAX_TRACKED_SESSIONS = 2000` makes active-session eviction unlikely; durable context is not a D2/D3 requirement. Documented in the projector docstring.

### Ledger read
```python
async def fetch_session_costs_by_trace(self, session_id: str) -> dict[str, float]:
    if not self.pool:
        return {}
    try:
        sid = UUID(session_id)
    except ValueError:
        return {}
    rows = await self.pool.fetch(
        "SELECT trace_id, COALESCE(SUM(cost_usd), 0) AS cost "
        "FROM api_costs WHERE session_id = $1 GROUP BY trace_id",
        sid,
    )
    return {str(r["trace_id"]): float(r["cost"]) for r in rows}
```
(`api_costs.session_id`/`trace_id` are `UUID`, indexed — `idx_api_costs_session_id`.)

### Wiring (app.py, at the existing projector construction)
```python
from personal_agent.observability.route_trace import get_route_trace_ledger
_ledger = get_route_trace_ledger()
async def _hydrate_session_costs(session_id: str) -> dict[str, float]:
    return await _ledger.fetch_session_costs_by_trace(session_id)
_turn_projector = TurnObservationProjector(hydration_source=_hydrate_session_costs)
```
Ledger pool is opened earlier in the same lifespan (≈line 455) than the projector registration (≈line 802), so it is connected before the first event.

## Steps (TDD)

1. **RED** — add session tests to `test_projector.py`:
   - `test_session_cost_accumulates_across_turns`: two completed traces (0.5, 0.3) same session ⇒ `session_cost_usd == 0.8`.
   - `test_session_cost_set_not_added_on_replay`: same `turn.completed` (t-1, 0.5) twice ⇒ `0.5`.
   - `test_session_context_carries_across_turns`: progress(ctx=8000) then a new model-call event with no progress ⇒ `session_context_tokens == 8000` still surfaced.
   - `test_hydration_restores_session_cost`: projector with stub source `{t-1:0.5, t-2:0.3}`; first event for session ⇒ `session_cost_usd == 0.8`; then complete t-3 (0.2) ⇒ `1.0`.
   - `test_hydration_no_double_count_with_live`: stub `{t-1:0.5}`; live complete t-1 final 0.7 ⇒ `0.7` (live wins).
   - `test_hydration_best_effort`: stub source raises ⇒ no exception, `session_cost_usd == 0.0`, carry-only continues.
   Run `make test-file FILE=tests/observability/topology/test_projector.py` → confirm new tests fail.
2. **GREEN** — implement projector changes; rerun the file → pass.
3. Ledger test in `test_ledger.py`: no-pool returns `{}`; (grouping happy-path mirrors existing ledger test style — mock pool.fetch). Implement `fetch_session_costs_by_trace`.
4. Teeth test in `test_ci_teeth.py`: static scan of `src/personal_agent/**.py` for `emit_turn_status(` invocations (regex excluding the `async def emit_turn_status` definition); assert the only file is `observability/topology/projector.py`.
5. Wire app.py.
6. Full gates.

## Test commands / expected

- `make test-file FILE=tests/observability/topology/test_projector.py` → all green (existing + new).
- `make test-file FILE=tests/observability/route_trace/test_ledger.py` → green.
- `make test-file FILE=tests/observability/topology/test_ci_teeth.py` → green.
- `make test` (full) → green.
- `make mypy` → clean (≤ pre-existing).
- `make ruff-check` + `make ruff-format` → clean.
- `pre-commit run --all-files` → pass.

## Invariants honoured
- Sole `turn_status` emitter (D4) — no new emit site; new teeth test enforces it.
- Cost is a roll-up reconciled to `SUM(api_costs)` (D3) — never a per-loop re-emit; not `cost_gate`.
- Per-trace map + eviction unchanged. FRE-553 engagement `tool_iteration` untouched.
- Compaction fields deferred to FRE-570 (one phase = one PR).
