# FRE-507 — Live cost-meter parity + cadence verification + NoOpBus decision

**Ticket:** FRE-507 (Approved, Tier-2, Observability Foundation)
**Design of record:** ADR-0088 (FRE-513). **No ADR** for this ticket — it is a verification close-out.
**Refs:** `observability/topology/projector.py`, `events/{bus,redis_backend,consumer,models}.py`,
`llm_client/cost_tracker.py`, `transport/agui/transport.py`, `route_traces` / `api_costs`.

## Goal

Prove the ADR-0088 live cost meter on the wire and settle the one open bus-down decision:

- **A** — One end-to-end test driving a decomposed (fan-out) turn through a **real (non-NoOp)
  bus** (`RedisStreamBus` + `ConsumerRunner`, the genuine publish→XADD→XREADGROUP→parse→projector
  →emit path), asserting (i) `turn_cost_usd` strictly climbs across ≥2 emits **during** expansion
  (not one end-of-turn jump) and (ii) the final emit == the authoritative `SUM(api_costs)`.
- **B** — Decide + document NoOpBus/bus-down meter behaviour. **Decision: accept dark-meter as
  documented graceful degradation.** *Why safe:* the durable cost path is fully decoupled from the
  event bus — `cost_tracker.record_api_call` writes the `api_costs` row **before** the best-effort
  publish, authoritative cost == `SUM(api_costs)`, and the seam's route-trace write is
  bus-independent (ADR-0088 D8). So a dark meter loses only the live *cosmetic* cadence, never
  durable data. *Why not the in-band fallback (option ii):* the WS carrier (`emit_turn_status` →
  AG-UI transport: Postgres + asyncio.Queue) is in fact Redis-independent, so a direct emit would
  technically work — but it would re-introduce a **second** in-band `turn_status` writer at the cost
  boundary, exactly the scattered-emit pattern ADR-0088 deliberately removed (FRE-501) to make the
  projector the **sole** emitter (no merge/clobber). Forking the single-emitter contract for a
  degraded-mode cosmetic gain isn't worth it. (Corrected from the first draft's rationale, which
  wrongly claimed Redis-down also kills the WS transport — it does not.)

## Scope decisions / interpretation

- **"Real bus" = the real `RedisStreamBus` class + `ConsumerRunner`, over an in-memory fake Redis
  client.** This is the project's established idiom (`tests/personal_agent/events/test_consumer.py`
  mocks the `redis.asyncio.Redis` client and exercises the real bus/runner). It exercises the
  genuine wire path (event → `model_dump(json)` → XADD → XREADGROUP → `parse_stream_event` →
  `projector.handle` → `emit_turn_status`) with **no live infra**, so it runs in `make test` (the
  default regression gate) — which is what "can't silently regress" requires. A live-LLM/live-Redis
  `integration`-marked test would be *excluded* from `make test`, defeating the purpose.
- **Live capture (AC item 2) is master/owner-gated.** "One captured *real* trace (live
  mid-expansion series + authoritative sum) in a Linear comment" needs a real decomposed turn fired
  through the **live gateway** with the WS `turn_status` series captured. The build session does not
  fire live gateway turns. This goes into the master handoff comment as a post-deploy step, with
  exact commands. The hermetic test below + the already-documented durable three-way reconciliation
  (trace `4af54c58`, $0.477387, in the ticket body) cover the invariant; the live wire capture is
  the post-deploy proof.
- **No ADR / no production-behaviour change.** Deliverable A is a characterization/regression-lock
  test of already-correct behaviour. Deliverable B is a doc decision at the code-docstring level
  (build session owns docstrings/READMEs; it does **not** edit ADR-0088).

## Step 1 — Deliverable A: the wire e2e test

New file: `tests/observability/topology/test_meter_wire_e2e.py`

1. **`_InMemoryStreamRedis`** — a minimal fake implementing only what `RedisStreamBus` +
   `ConsumerRunner` call: `xgroup_create`, `xadd` (append `(id, fields)` to a per-stream list with a
   monotonic id), `xreadgroup` (consumer-group `>` semantics: per-group cursor; return up to
   `count` undelivered entries, advance cursor; `await asyncio.sleep(0)` + return `[]` when none, to
   avoid a tight CPU spin), `xack` (record), `aclose`. ~45 lines. Makes `bus.publish()` genuinely
   serialize and the runner genuinely deserialize what was published — a true round-trip.
   **Scoping comment in the fake:** this models only the happy path — no PEL / pending-entries /
   redelivery / NOACK semantics (the runner ACKs after a successful handler, consumer.py); do **not**
   reuse this fake to validate retry/dead-letter behaviour (codex Q3).
2. Patch `projector_mod.emit_turn_status` with an async capture into `emitted: list[dict]` (same
   idiom as `test_projector.py::_capture`).
3. Build `RedisStreamBus(fake)`; register the subscription with the **real keyword signature**
   `bus.subscribe(stream=STREAM_TURN_OBSERVED, group=CG_TURN_PROJECTOR,
   consumer_name="turn-projector-0", handler=TurnObservationProjector().handle)` (codex Q1 —
   `subscribe(stream, group, consumer_name, handler)`, not handler-first).
4. **Start the runner FIRST, then publish events incrementally while it consumes** (codex Q2 — proves
   a true mid-flight climb, not just per-event ordering of a pre-queued batch). Helper
   `_drain_until(predicate, timeout=2s)` polls `await asyncio.sleep(0.02)` until the predicate over
   `emitted` holds (or fails the test on timeout). Sequence, each published via
   `bus.publish(STREAM_TURN_OBSERVED, ev, maxlen=settings.turn_observed_stream_maxlen)` — exactly
   what the real publishers (`cost_tracker`, `seam`, `executor`, `sub_agent`) do:
   - publish `TopologyEnteredEvent(topology="hybrid_fanout")`; drain until an emit appears.
   - publish `ModelCallCompletedEvent(cost_usd=0.05, model_role="sub_agent")`; drain until latest
     emit `turn_cost_usd == approx(0.05)`. Record it.
   - publish `SubAgentProgressEvent(task_id="a", iteration=1, iteration_max=10)`; drain one tick.
   - publish `ModelCallCompletedEvent(cost_usd=0.07, model_role="sub_agent")`; drain until `0.12`.
   - publish `ModelCallCompletedEvent(cost_usd=0.11, model_role="sub_agent")`; drain until `0.23`.
   - publish `ModelCallCompletedEvent(cost_usd=0.09, model_role="primary")`; drain until `0.32`.
   - cumulative live = 0.05+0.07+0.11+0.09 = **0.32**.
   - publish `TurnCompletedEvent(topology="hybrid_fanout", cost_authoritative_usd=0.32)`; drain until
     an emit lands **after** the trace is evicted (`trace not in projector._by_trace`).
   - `await runner.stop()`.
5. **Assertions:**
   - The recorded mid-expansion values (captured *between* publishes, while the runner was live) are
     `[0.05, 0.12, 0.23, 0.32]`: ≥2 entries and strictly increasing (`v[i] < v[i+1]`) — proves the
     meter climbed **during** expansion, on the wire, not as one end-of-turn jump.
   - At least one mid-expansion emit had `0 < turn_cost_usd < 0.32`.
   - Final emit `turn_cost_usd == pytest.approx(0.32)` == authoritative sum.
   - `topology == "hybrid_fanout"` carried through; trace evicted (`trace not in projector._by_trace`).
6. **Mutation sanity check (manual, during dev — not committed):** temporarily set
   `cost_authoritative_usd=0.99` and confirm the "final == sum" assert fails; temporarily collapse
   the model calls into one and confirm the "≥2 climbing emits" assert fails. Proves the test isn't
   vacuously green. Revert.

Marker: **none** (hermetic ⇒ runs in `make test`).

## Step 2 — Deliverable B: NoOpBus decision + documentation

1. `src/personal_agent/observability/topology/projector.py` — add a **"Bus-down behaviour
   (FRE-507)"** paragraph to the module docstring: under `NoOpBus` (Redis down / flag off) the live
   meter goes **dark** (publishes discarded, projector consumer not wired — `service/app.py`), which
   is **accepted graceful degradation**. Ground the rationale correctly (codex Q4): the durable cost
   path is decoupled from the bus — `api_costs` is written before the best-effort publish,
   authoritative cost == `SUM(api_costs)`, and the seam route-trace write is bus-independent (D8) —
   so a dark meter loses only live cosmetic cadence, never durable data. The in-band fallback is
   declined not because it can't work (the WS carrier is Redis-independent) but because it would
   re-add a second `turn_status` writer at the cost boundary, breaking ADR-0088's sole-emitter
   contract (FRE-501). **Do not state "Redis-down kills the WS transport" — that is false.**
2. `src/personal_agent/service/app.py` (~L797–800 wiring comment) — append a one-line reference to
   the FRE-507 decision (dark-meter accepted) next to the existing "under NoOpBus this block is
   skipped" note.
3. `tests/observability/topology/test_meter_wire_e2e.py` —
   `test_noop_bus_meter_is_dark_and_safe`: publish the same sequence through a `NoOpBus`; assert
   **zero** captured emits (meter dark) and no exception (durable path unaffected). Locks the
   documented decision at the bus boundary.

## Step 3 — Quality gates

```bash
make test-file FILE=tests/observability/topology/test_meter_wire_e2e.py   # new test green
PYTHONITER=1 .venv/bin/python -m pytest tests/observability/topology/ -q   # module green (via make)
make mypy            # src/ clean
make ruff-check && make ruff-format
pre-commit run --all-files
make test            # full suite green (skill: module then full)
```

## Step 4 — PR + master handoff comment, then STOP

- PR via `.github/PULL_REQUEST_TEMPLATE.md`, **pre-merge checklist only**, push branch.
- `save_comment` on FRE-507 for master with: the post-deploy **live-capture runbook** (fire one
  decomposed turn through the live gateway, capture the WS `turn_status` series + final value,
  confirm == `SUM(api_costs)` for the trace, paste into Linear — explicit per-action owner OK
  required, do **not** auto-fire), the NoOpBus decision recorded, and the Linear auto-Done caveat if
  deploy is batched.
- Do not merge/deploy/close/edit MASTER_PLAN.

## Acceptance mapping

| AC | Where satisfied |
|----|-----------------|
| Integration test: real bus, decomposed turn, live mid-expansion climb + final == SUM(api_costs) | Step 1 (`test_meter_wire_e2e.py`) |
| One captured real trace documented in Linear comment | Durable trace `4af54c58` already in ticket body; **live mid-expansion series** = post-deploy step in master handoff (owner-gated) |
| NoOpBus-down behaviour decided + documented (default dark-meter) | Step 2 (docstring + app.py + `test_noop_bus_meter_is_dark_and_safe`) |
| No ADR; ADR-0088 is design of record | Honoured — docstring-level docs only |
