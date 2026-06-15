# FRE-557 — Projector-health counter (`projector_events_received`)

**Ticket:** FRE-557 (Approved, Tier-2:Sonnet) · **Project:** Observability Foundation
**Refs:** ADR-0088 §D3 (cost cadence) / §D6 (two sinks) · FRE-513 (projector + seam) · FRE-517 (filed this)

## Premise correction (verified in code — measure, don't assert)

The ticket (which I filed during FRE-517, against the deprecated FRE-501 rollup) says `not_reconciled` conflates *bus-delivery loss* and *accumulator drift*. **It does not.** Tracing the live paths:

- Durable `cost_live_usd` = `ctx.turn_cost_usd`, a **direct per-loop accumulator** (`executor.py:2690` — `ctx.turn_cost_usd += response["cost_usd"]`), *not* bus-fed. The assembler reads it as `cost_live` (`assembler.py:283`), and `cost_reconciled = |cost_live − authoritative| ≤ tol`. So **`not_reconciled` is purely an accumulator-drift signal.**
- The **projector's** `live_cost_usd` is the *separate* path: it sums bus `ModelCallCompletedEvent`s (`projector.py:136`). If the best-effort `stream:turn.observed` publish drops (`_publish` / `_publish_model_call_completed`), the **live UI meter is silently wrong and nothing records it** — the real, currently-invisible gap.
- The authoritative per-trace model-call count already exists durably: `COUNT(api_costs WHERE trace_id)` — the natural "published" denominator, so the seam needs **no new publish-counter**.

**FRE-557's real value:** give the bus/live-projector path its **own** health signal, **orthogonal** to `not_reconciled`. (Owner-confirmed: keep orthogonal + document; do not couple to `cost_reconciled`.)

## Design (owner-confirmed)

1. **Per-trace health doc** at `TurnCompletedEvent`, keyed by `trace_id`, joinable to `api_costs` / `route_traces`.
2. **Global rolling counter** so systemic/total delivery loss (a trace the projector saw *zero* times → no completed event → no per-trace doc) is still visible.

## Files

### 1. `src/personal_agent/observability/topology/projector.py`
- `TurnObservation`: add `events_received: int = 0`, `model_calls_received: int = 0`.
- `TurnObservationProjector.__init__`: add global counters `self._events_received_total: int = 0` and `self._events_by_type: dict[str, int] = {}`.
- `handle()`:
  - **Top (all events, before dispatch):** `self._events_received_total += 1`; `self._events_by_type[type(event).__name__] = ... + 1`; call `self._maybe_emit_rolling()`.
  - **Each known branch:** `obs.events_received += 1`; in the `ModelCallCompletedEvent` branch also `obs.model_calls_received += 1`.
  - **`TurnCompletedEvent` branch:** capture `complete = event.trace_id in self._by_trace` (codex #1 — was the full lifecycle observed, or is the obs about to be freshly created?) **before** `_observation`; then capture `bus_live_cost = obs.live_cost_usd` **before** the `obs.live_cost_usd = event.cost_authoritative_usd` reconcile-overwrite; then `self._emit_turn_health(obs, bus_live_cost, event.cost_authoritative_usd, observation_complete=complete)` before the existing `_emit` + pop.
- `_observation()` eviction (codex #1): when evicting a trace whose obs had activity (`events_received > 0`), raise the existing `debug` to `log.warning("projector_evicted_active_trace", trace_id=..., events_received=...)` so a mid-turn eviction (which would otherwise produce a misleading zero-count health doc) is loud.
- New `_maybe_emit_rolling()` (codex #3 — process-local + time-based so low-volume instances still emit): emit `projector_events_rolling` when `events_total % _ROLLING_EMIT_EVERY == 0` **OR** `monotonic() − self._last_rolling_emit ≥ _ROLLING_EMIT_SECONDS` (= 300). `log.info("projector_events_rolling", events_total=..., by_type=dict(...), tracked_traces=len(self._by_trace))` → `agent-logs` (operational, no join). Update `self._last_rolling_emit` on emit. **Documented:** counters are process-local and reset on restart — an operational rolling gauge, not a durable counter.
- New `_emit_turn_health(obs, projector_live_cost_usd, cost_authoritative_usd, *, observation_complete)`: build the explicit-typed doc + `schedule_es_index(index, doc, doc_id=obs.trace_id)` (non-blocking, best-effort, **whole body wrapped in try/except** — mirrors FRE-548 `project_route_trace_to_es`; `schedule_es_index` only guards the scheduled write). Index `agent-monitors-projector-health-YYYY-MM-DD`.

Health doc (explicit schema, `@timestamp` emitted manually — `index_document` adds no envelope):

| field | type | source |
|---|---|---|
| `@timestamp` | date | `datetime.now(UTC).isoformat()` |
| `trace_id` | keyword | `obs.trace_id` |
| `session_id` | keyword | `obs.session_id` |
| `topology` | keyword | `obs.topology` |
| `events_received` | long | `obs.events_received` |
| `model_calls_received` | long | `obs.model_calls_received` |
| `projector_live_cost_usd` | double | `float(projector_live_cost_usd)` (bus-accumulated, pre-reconcile) |
| `cost_authoritative_usd` | double | `float(cost_authoritative_usd)` |
| `cost_delta_usd` | double | `round(projector_live − authoritative, 6)` |
| `observation_complete` | boolean | `False` when the obs was freshly created at completion (evicted mid-turn, or never-seen-until-completion) → its counters are NOT trustworthy |

**Analysis contract (documented, not coded):**
- `observation_complete = true` AND `model_calls_received < COUNT(api_costs WHERE trace_id)` ⇒ **partial bus/projector delivery loss** (the UI meter undercounted); `cost_delta_usd ≠ 0` corroborates.
- `cost_reconciled = FALSE` on the same trace (route_traces) ⇒ **accumulator drift** — orthogonal axis, unrelated to the bus path.
- `observation_complete = false` ⇒ counters unreliable (eviction/late-attach); ignore the delivery comparison for that trace.

**Coverage gap — stated honestly (codex #2):** a trace the projector saw **zero** times emits **no** completion event ⇒ **no health doc at all**. The global rolling counter shows total volume, **not** which traces are missing — it does **not** close this gap. The companion check is an external **reconciliation query**: traces with `COUNT(api_costs WHERE trace_id) > 0` that have **no** `agent-monitors-projector-health` doc ⇒ total delivery loss for that trace. (Documented as the coverage check; building that query/monitor is out of scope here — note as a possible follow-up.)

### 2. `docker/elasticsearch/monitors-projector-health-index-template.json` — NEW dedicated template
`index_patterns: ["agent-monitors-projector-health-*"]`, `priority: 110`, standard settings, `mappings.dynamic: false` + the 10 explicit `properties` above (incl. `observation_complete: boolean`). `_meta.description` + `managed_by`. (Dedicated + `dynamic:false` = the FRE-548 / monitors-family discipline; isolates the schema, no shared-template drift, no dynamic-mapping trap.)

### 3. `scripts/setup-elasticsearch.sh` — register the template
One `put_resource "Index template: agent-monitors-projector-health-template" "/_index_template/agent-monitors-projector-health-template" "$PROJECT_ROOT/docker/elasticsearch/monitors-projector-health-index-template.json"`.

### 4. Docs — `docs/skills/seshat-observations.md`
Short subsection: `agent-monitors-projector-health-*` = bus/live-projector delivery health (per turn), **orthogonal** to `not_reconciled` (accumulator drift). Note the join (`model_calls_received` vs `api_costs` count).

## Tests (TDD — `tests/observability/topology/test_projector.py`, write first / confirm red)

- `test_handle_counts_events_per_trace`: feed TopologyEntered + 2×ModelCallCompleted → `obs.events_received == 3`, `obs.model_calls_received == 2` (inspect via a completed-event health emit or a test seam on `_by_trace` before completion).
- `test_turn_completed_emits_health_doc`: monkeypatch `schedule_es_index`; a TopologyEntered + 2×ModelCallCompleted(cost 0.01 each) + TurnCompleted(authoritative 0.05) → one `schedule_es_index` call to `agent-monitors-projector-health-<date>`, `doc_id == trace_id`, doc has `model_calls_received == 2`, `projector_live_cost_usd == 0.02` (pre-reconcile, **not** 0.05), `cost_authoritative_usd == 0.05`, `cost_delta_usd == -0.03`.
- `test_health_emit_never_raises`: `schedule_es_index` raises → `handle()` still completes (turn_status still emitted, trace popped).
- `test_rolling_counter_emits_every_interval`: patch `_ROLLING_EMIT_EVERY` low (e.g. 3); feed N events → `projector_events_rolling` logged at the interval (capture via `structlog.testing.capture_logs`).
- `test_rolling_counter_time_heartbeat`: with the count threshold not reached, force `_last_rolling_emit` into the past (or patch `_ROLLING_EMIT_SECONDS=0`) → the next event emits `projector_events_rolling` (low-volume heartbeat path — codex #3).
- `test_global_counter_counts_unknown_events`: an unknown event type increments `_events_received_total` but creates no per-trace obs.
- `test_completion_without_prior_events_flags_incomplete` (codex #1): a bare `TurnCompletedEvent` for an untracked trace → health doc has `observation_complete == False`, `model_calls_received == 0` (the reader must not read that as real zero-delivery).
- `test_eviction_of_active_trace_warns` (codex #1): fill `_by_trace` past `_MAX_TRACKED_TRACES` with an active obs → eviction logs `projector_evicted_active_trace` at warning.
- Template JSON validity + 10 keys/types (incl. `observation_complete: boolean`).

## Quality gates
`make test-file FILE=tests/observability/topology/test_projector.py` → `make test` → `make mypy` → `make ruff-check`/`format` → `pre-commit run --all-files`.

## Post-deploy (Linear comment for master — NOT in PR checklist)
- Register the template (`scripts/setup-elasticsearch.sh`) before the projector's first health emit.
- `_field_caps` proof: `model_calls_received`/`events_received` → `long`; `*_usd` → `double`; join keys `keyword`.

## Halt-condition check
Single coherent observability feature (projector counters + dedicated health index + rolling log). No historical rows dropped. No ADR-phase bundling. No expected mypy regression. One phase = one PR.
