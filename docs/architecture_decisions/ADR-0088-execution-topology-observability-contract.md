# ADR-0088 — Execution Topology Observability Contract (Trace-Scoped Spine for Status, Cost, and Loud Degradation)

**Status:** Proposed — 2026-06-06
**Related:** ADR-0076 (`turn_status` / STATE_DELTA live surface — the sink this contract feeds), ADR-0074 (identity / joinability — the `trace_id`/`session_id`/`task_id` discipline every spine event inherits), ADR-0086 (HYBRID/DECOMPOSE topology — the new topology that ran below the observability floor), ADR-0036 (expansion controller — a topology that must emit through the spine), ADR-0053 (Agent-Driven Gate Health Monitoring — adjacent gateway-stage observability, kept separate), ADR-0082 (tier-aware model selection — model role is a spine attribute). **Supersedes (tactically):** FRE-501's per-loop cost accumulation. **Shares its seam + event model with:** FRE-452 (route-trace ledger — written by the seam's *direct durable write*, not via the bus stream; designed together).
**Implements:** FRE-504 → **Observability Foundation** project (L0). Keystone of L0.
**Spec:** `docs/specs/SESHAT_PROGRAM_ARCHITECTURE.md` (the parent program architecture; this ADR is its L0 keystone)
**Evidence:** trace `87cbd720` (decomposition first-run, `docs/research/2026-06-06-decomposition-first-run-findings.md`); code audit of the live cost/status substrate (cited inline)

---

## Context

### The measured problem

The 2026-06-06 decomposition first-run (trace `87cbd720`, ADR-0086) shipped a new execution topology that ran **nearly invisibly**: the live meter showed **$0.57 / tools 2/25** (primary-only) while the backend ledger booked **$0.9028** across 9 calls; no `turn_status` flowed during the ~13-minute discovery+build; and the planner failed schema-validation → degraded to tool-less sub-agents while the turn reported **success**. Sub-agents produced 64% of output tokens and ~$0.33 of cost — none of it on the live surface.

The root cause is structural, not a missing log line: **the live surface is bolted to a single execution topology.** It works for the primary loop and goes dark the moment work moves into any other topology.

### What already exists (the real boundary picture)

A code audit shows the two signals are in *opposite* states:

- **Cost already has a single, identity-enforced boundary.** `cost_tracker.record_api_call` writes every model call to the `api_costs` Postgres table keyed by `trace_id`/`session_id`/`purpose`, and **raises `MissingIdentityError` without identity** (`llm_client/cost_tracker.py:95`, per ADR-0074). No model call escapes it — which is why the backend tally was exact. The bug is purely that the **live meter reads `ctx.turn_cost_usd`** (`orchestrator/executor.py:210`), a per-`ExecutionContext` accumulator local to the primary loop.
  - **FRE-501 (merged) is a tactical bridge, not the fix.** It rolls expansion + sub-agent + planner cost *into that per-loop accumulator* (`executor.py:1773`, `:2768`). This restores the two-level number but **re-implements accumulation at every integration point** and does not generalize to deeper topologies (planner-executor, nested sub-agents).
- **Status/degradation has only a shared *sink*, not a boundary.** `transport.emit_turn_status` (`transport/agui/transport.py:182`) persists + enqueues a STATE_DELTA (ADR-0076), but it is *called* from scattered per-loop sites built from `ctx` (`executor.py:_emit_turn_status`, and post-FRE-501 the expansion/sub-agent paths). Sub-agents have no such `ctx`; degradation (e.g. planner fallback) is emitted from nowhere.

Both substrates a real boundary would need **already exist**:
- `TraceContext` already threads into sub-agents (`orchestrator/sub_agent.py:153`).
- A full `EventBus` exists — `publish(stream, event)`, Redis backend + `NoOpBus` fallback (`events/bus.py`), with a consumer framework and identity-threaded events.

### The execution topologies in play

The contract must hold across every way a turn can run:

| Topology | Where | Status today |
|----------|-------|--------------|
| Primary loop | `orchestrator/executor.py` | emits status + cost (per-loop) |
| Expansion / sub-agent fan-out (HYBRID) | `orchestrator/expansion_controller.py` (ADR-0036), `sub_agent.py` | cost rolled up by FRE-501; no native status |
| Decomposition (DECOMPOSE/DELEGATE) | ADR-0086 | dark on the live surface |
| Planner-executor (future) | FRE-401 | n/a |

### Scope boundary

This ADR owns the **contract and the spine** — where and how status/cost/degradation are emitted so any topology is observable. It does **not** define the route-trace ledger's schema (FRE-452, which shares the same event model + seam — designed together) nor the result-type taxonomy (FRE-451). It does not change *what* gets routed where (that is L1/Inference).

---

## Decision

### D1 — Name execution topology as a first-class concept

A turn runs under exactly one **execution topology** drawn from `{ primary, hybrid_fanout, decompose, delegate, planner_executor (future) }`. Observability is a property of the **topology abstraction**, not of any one implementation. Listing `planner_executor` as *future* states a **design constraint** the seam (D2) must satisfy — a new topology is observable only by being constructed to run inside the seam — not a claim that it already converges today. New topologies inherit the contract by construction; they do not re-implement emission.

### D2 — The boundary is a mandatory emission *seam*, not a stream topologies are trusted to use

The common boundary is realized as a **mandatory emission seam** every topology passes through — a code seam, so emission is not optional. Concretely the seam is three call sites a topology cannot run real work without:

1. **`observe_topology(...)`** — a context manager wrapping each topology's execution; on enter/exit it produces `topology_entered` / `turn_completed`.
2. **the existing `cost_tracker` hook** — `model_call_completed` (cost/tokens/latency/role); already **hard-enforced** because `record_api_call` raises without identity (`llm_client/cost_tracker.py:95`).
3. **`report_degradation(...)`** — the single sanctioned way to signal "did less" (see D5).

Each seam call does **two things**: (i) writes a **durable record directly** (a route-trace ledger row; cost already lands in `api_costs`), and (ii) publishes a **best-effort bus event** to the shared `stream:turn.observed` (ADR-0074 identity on every event: `trace_id`, `session_id`, `task_id`, `topology`, `model_role`) for the live projector. Topologies **report through the seam**; the projector **emits** the live surface. This replaces the scattered per-loop `emit_turn_status` calls.

**Enforcement honesty (per Codex review):** cost is *mechanically* enforced (the identity guard — a call cannot be billed without being recorded). Status and degradation are enforced by the *structural seam plus CI tests* (running topology work outside `observe_topology`, or taking a fallback without `report_degradation`, is a reviewable/test-catchable defect) — **weaker than cost's hard guard, and stated as such**, not claimed as a mechanical invariant.

### D3 — Cost: one cadence (carry-on-event live, ledger-sum authoritative)

There is exactly one cost model, to remove the SUM-vs-accumulate ambiguity:

- **Authoritative / durable:** `api_costs`, summed by `trace_id`. Identity-enforced, bus-independent, the source of truth.
- **Live:** the cost carried on each `model_call_completed` event drives the meter for low-latency display, and is **reconciled to `SUM(api_costs WHERE trace_id)` at `turn_completed`** (authoritative wins).

This is *not* per-loop accumulation: no topology adds sub-results into `ctx.turn_cost_usd`. **FRE-501's per-loop accumulation is deprecated** by this ADR and removed once the seam + projector are live (interim bridge only).

### D4 — Status is a single projection of a trace-scoped observation

The projector maintains a per-trace **TurnObservation** (topology, phase, tool iteration + max, live cost, degradations, token estimate) and emits `turn_status` from that one place (reusing the ADR-0076 sink and FRE-407 `trace_id` stamping). Because `turn_status` is a **STATE_DELTA — full-state replacement keyed by session**, the live path is naturally **idempotent**: duplicate or replayed events simply re-set the same state, and a missed event self-corrects on the next one. Topologies never call `emit_turn_status` directly.

### D5 — Degradation is a first-class, loud signal — through the seam

Every topology that does less than intended — planner schema-fail → tool-less fallback (FRE-502), artifact strip-and-deliver (FRE-496), budget-trimmed memory (10→2), a discarded sub-agent result — **must call `report_degradation(...)`** (`reason`, `where`/topology, `severity`, `expected_vs_actual`). The call writes a durable ledger entry **and** publishes the event; the projector raises a visible "degraded" state with reason onto `turn_status`. Existing degradation signals (e.g. `ExpansionResult.degraded`, `expansion_controller.py:180`) are migrated to flow through this one call. **A silent fallback that reports success is a contract violation** — enforced by the seam + a CI fixture (D7), acknowledged as convention-plus-test rather than a hard guard.

### D6 — One event model, one seam, two sinks (durable ≠ live)

The seam has **two sinks**, deliberately separated (this is the fix to the D6/D8 contradiction Codex flagged):

1. **Durable sink — direct write at the seam.** The route-trace ledger row (FRE-452) is written **directly** (Postgres), and cost lands in `api_costs`. **This does not go through the bus**, so it survives a bus outage. The reconciliation loop and FRE-452 read this durable store.
2. **Live sink — best-effort bus event.** The same event is published to `stream:turn.observed`; the projector consumes it to drive `turn_status`. If the bus is down, only the live meter is lost.

So "one spine" means **one event model + one seam**, not one process: the FRE-452 ledger writer is the seam's *direct write*, the projector is a *separate, live-only* consumer. ADR-0088 and FRE-452 are still designed together (shared event model + seam), but the ledger's durability is independent of the bus.

### D7 — The observable-first done bar (with CI teeth)

A new orchestration capability is **not shippable-to-default** until: (a) its model calls go through `cost_tracker` (hard-enforced by identity); (b) it runs inside `observe_topology(...)` (so `topology_entered`/`turn_completed` + the durable ledger row are produced by construction); and (c) it routes every fallback through `report_degradation(...)`. The done-bar is **checkable in CI**: a fixture that forces a fallback must produce a durable degradation record and a `turn_status` "degraded" state; a topology that runs model work outside the seam fails a test. "Backend joinability" alone (the ADR-0086 bar) is insufficient.

### D8 — Resilience: durability never depends on the bus

Both durable writes — `api_costs` and the route-trace ledger row — are **direct, synchronous, bus-independent** (D6 sink 1). Under `NoOpBus` (Redis down / flag off): the live meter is absent, but cost and the actual-traversal record are fully intact (direct writes), so post-hoc reconciliation and the FRE-452 ledger are unaffected. **Only the live projection is best-effort.** This is now consistent with D6 — the bus carries the live signal, never the durability.

---

## Consequences

### Positive
- **Every topology is observable by construction** — primary, fan-out, decomposition, future planner-executor — without per-loop emission code.
- **One cost model**: live via carry-on-event, authoritative via `SUM(api_costs)` reconciliation — the ledger that was already correct, now surfaced without per-loop accumulation.
- **Degradation becomes loud** — the planner-fallback / strip-and-deliver / memory-trim cases that hid on `87cbd720` are first-class signals.
- **L0 unified**: live surface (bus), FRE-452 ledger (direct durable write), and the reconciliation loop share **one event model + one seam** — not one transport.
- **Generalizes**: depth- and locality-independent (remote sub-agents publish to the same stream).

### Negative / tradeoffs
- **A new seam to thread**: every topology must run inside `observe_topology(...)` and route fallbacks through `report_degradation(...)`. This is the cost of making the boundary real rather than trusted; enforced by CI (D7).
- **Weaker enforcement for status/degradation than for cost**: cost has a hard identity guard; status/degradation rely on the seam + tests. Acknowledged, not hidden — closing that gap fully would require a deeper guard and is out of scope.
- **Bus dependency for the *live* surface only**: under `NoOpBus` the meter is dark, though `api_costs` and the route-trace ledger (direct writes) survive intact.
- **Latency**: async event → projector adds small delay vs. inline emit. Acceptable for a status surface; idempotent STATE_DELTA tolerates at-least-once delivery (D4).
- **Migration**: FRE-501's per-loop accumulation must be removed cleanly once the seam + projector are live; the durable ledger (direct write) is authoritative, so no double-source.

---

## Verification

- A decomposition build shows the live meter climbing **during** expansion and ending ≈ `SUM(api_costs WHERE trace_id)` (cross-check: meter == ledger sum, not the primary-only figure).
- `turn_status` carries the active `topology` and updates from sub-agent activity, not just the primary loop.
- A forced planner schema-fail produces a visible `degraded` state with reason on `turn_status` **and** a ledger entry — no silent "success."
- With the bus disabled (`NoOpBus`), the turn still writes full cost to `api_costs` **and a durable route-trace ledger row** (both direct writes); only the live meter is absent.
- A CI fixture that forces a fallback produces a durable degradation record **and** a `turn_status` "degraded" state; a topology that runs model work outside `observe_topology(...)` fails a test (D7 teeth).
- No remaining direct `emit_turn_status` calls from topology loops; the projector is the sole live emitter.

## Open decisions (data-gated / to settle in implementation tickets)
- **`observe_topology` shape**: context manager vs. decorator vs. both; how it threads through the async call chain alongside `TraceContext`.
- **Degradation severity taxonomy**: align `severity`/`reason` with the FRE-451 result-type taxonomy so degradation is expressible in the same vocabulary (settle jointly with FRE-451).
- **Route-trace ledger storage**: Postgres table vs. ES index for the direct durable write (settle with FRE-452, which owns the schema).

*Settled in this ADR (previously open):* stream is the single shared `stream:turn.observed`, `maxlen`-bounded, events carry `trace_id` (D2); cost cadence is carry-on-event live + `SUM(api_costs)` authoritative at `turn_completed` (D3); the durable ledger writer is the seam's direct write, the projector is a separate live-only consumer (D6).

## References
- Spec: `docs/specs/SESHAT_PROGRAM_ARCHITECTURE.md` (§4 L0, §5 reconciliation loop, §7 sequencing)
- Research: `docs/research/2026-06-06-decomposition-first-run-findings.md` (trace `87cbd720`)
- Code: `llm_client/cost_tracker.py:95`, `orchestrator/executor.py:190,210,1773,2768`, `transport/agui/transport.py:182`, `events/bus.py`, `orchestrator/sub_agent.py:153`
- Linear: FRE-504 (origin), FRE-452 (shared spine), FRE-501 (superseded bridge), FRE-505 (sub-agent auditability — a spine consumer), FRE-506 (gate-decision telemetry — `degraded`/gate events on the spine), FRE-401 (planner-executor — future topology)
- ADRs: ADR-0076, ADR-0074, ADR-0086, ADR-0036, ADR-0053, ADR-0082
