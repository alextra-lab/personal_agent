# ADR-0092 — Context-Compaction Observability and Surfacing (session meter + per-mechanism signals)

**Status:** Implemented — 2026-06-23 (all five impl tickets shipped + deployed: FRE-568 ✅ projector session aggregate · FRE-570 ✅ A/B/D markers + the 4 `turn_status` fields · FRE-571 ✅ explicit ES mappings for the session fields (PR #241, `_field_caps` verified) · FRE-572 ✅ backend monitors · FRE-573 ✅ PWA two-lane render (+ FRE-584 ✅ regression test). **Carved out (not part of this ADR's done-bar):** mechanism-C tool-result digest **FRE-569** (Held). Originally Proposed 2026-06-15 → Accepted 2026-06-22 → Implemented 2026-06-23.
**Related:** ADR-0088 (turn-observation projector — D3 cost cadence, D4 sole `turn_status` emitter; **extended here**), ADR-0090 (telemetry surface contract — the `turn_status` surface), ADR-0076 (`turn_status` STATE_DELTA + the hard-compaction user pause), ADR-0061 (within-session compression — **B**), ADR-0047 (gateway budget compaction — **A**), ADR-0081 (cache-aware frozen reset — **D**), ADR-0085 / FRE-486 (tool-result digest — **C**, parked; carved out), ADR-0065 (`cost_gate` / `api_costs`), ADR-0069 (artifact store), FRE-554 (this ADR), FRE-553 (live engagement count), FRE-501 (cost+status meter)
**Project:** Observability Foundation (L0/L1)

---

## Context

### The ask (FRE-554) and what it grew into

The PWA `TurnStatusBar` is **per-turn for everything** — `context_tokens`, `turn_cost_usd`, and `tool_iteration` all reset on each new user input. FRE-554 asked for two *scopes*: cost and context occupancy should be **per-session** (cumulative / persistent), with a new **compaction counter**, while the tool/turn count stays **per-engagement** (the current harness run; FRE-553).

Working the design through with the owner (2026-06-15) surfaced that "compaction count" is not one thing. The agent has **four** distinct context-compaction mechanisms with very different quality and cost profiles, and they warrant **different treatment** — one alerts, one counts, one informs, one is a dormant separate concern. So FRE-554's "mostly a surfacing change" became a small **compaction-observability program**. This ADR is the single decision record for it; the parked mechanism (C) is carved out to its own ticket (D10).

### The four mechanisms (verified against the code)

| # | Mechanism | Where it fires | Quality effect | Signal |
|---|---|---|---|---|
| **A** | Gateway budget compaction (ADR-0047, `request_gateway/budget.py:140-260`) | Pre-LLM **Stage 7**, once per turn, when assembled context > budget | **Highest risk** — blunt deletion in order: drop oldest history → **drop *all* recalled memory** → drop tool defs (`strategy=drop_oldest`, not summarise). The "I have no record of that" failure shape; dropped entity IDs are tracked for recall-quality feedback (`telemetry/compaction.py:90-93`) | structlog `context.compaction` (per tier; **not** a bus event) |
| **B** | Within-session compression (ADR-0061, `within_session_compression.py`) | Orchestrator. **soft** = async between turns (`executor.py:3546`); **hard** = synchronous mid-turn over-budget (`executor.py:2058`) | **Moderate** — head-middle-tail: head (system + first user) and tail (last K) verbatim, middle LLM-summarised. Hard already **pauses and asks the user** (ADR-0076) | bus `context.within_session_compressed` (+ `tokens_saved`) |
| **C** | Tool-result digest (ADR-0085 / FRE-475) | Orchestrator, per oversized tool result, at birth | **Lowest risk — recoverable** (full bytes in artifact store, `expand_tool_result`). **PARKED / OFF** (FRE-486, `tool_result_compression_enabled` default false) due to a file-read truncation problem | bus `context.tool_result_digested` (silent while parked) |
| **D** | Cache-aware **frozen reset** (ADR-0081 §D3, `cache_reset_scheduler.py` → `build_frozen_reset`, `executor.py:948-982,1833`) | Orchestrator, **scheduled** at the cost/quality optimum run length `L*` | **No content loss of its own** — a cost/cache optimiser timing *when* a recompaction/reset is paid (sawtooth), backend-aware | structlog `frozen_reset_built` / `frozen_reset_fired` |

**Verified distinctness, and an ADR-0081 divergence the counters must respect.** In the **shipped** code, D's scheduled reset is a *separate* function — `build_frozen_reset` (`within_session_compression.py:237`; unconditional since the `cache_frozen_layout_enabled` flag was retired in FRE-941) — that emits only `frozen_reset_fired` (structlog) and does **not** call `record_compression`, so it never fires B's `context.within_session_compressed`. The bus event's `trigger` Literal is still only `["soft","hard"]` (`events/models.py:860`). So B and D are distinct events today. **But** ADR-0081 §D3 Decision 4 *planned* the opposite — to route the scheduled reset through `compress_in_place` with `trigger="scheduled_reset"` on the same `WithinSessionCompressionRecord`. That plan did **not** ship (a parallel `frozen_reset` path did instead). The counters below are therefore pinned to be robust **either way** (D6/D7), and the divergence is recorded as an open item.

### Two premises in the ticket corrected

1. **Cost source.** `cost_gate.running_total` is **per budget-cap window** (daily/weekly caps in `budget_counters`), **not** per-session. The session-cost source is `SUM(api_costs.cost_usd WHERE session_id)`.
2. **"No compaction event exists."** B's per-pass bus event already exists; the counter is a *consumption*, not a new emit. (A and D emit structlog, not bus — see D8.)

### The projector constraint this must respect

The `TurnObservationProjector` (ADR-0088 D4) is the **sole** `turn_status` emitter and is deliberately **per-trace and live-only**: it reads no substrate and evicts every trace at `turn.completed` (`projector.py:152`). Session scope requires new state that *survives across turns* — a real lifecycle addition (D4 below), bounded by the ADR-0088 D3/D4 invariants.

---

## Decision

### D1 — Two surfaced scopes: a **session lane** and an **engagement lane**

`turn_status` (and the PWA bar) surface two distinct lanes, both visible:
- **Session lane** (persists across turns): cumulative **cost**, **context occupancy**, and the three per-mechanism compaction signals (⚠ A-alert, ⟳ B-count, ↻ D-reset).
- **Engagement lane** (per harness run, unchanged): live `tool_iteration` X/Y (FRE-553) — climbs live, resets at the next user engagement. **It is not folded into the session lane.**

### D2 — Session-cumulative cost

A session-cumulative cost field, **rolled up idempotently by `trace_id`** (not blind-incremented). The session aggregate holds a `dict[trace_id, authoritative_cost_usd]`; the surfaced session cost is its sum. On a turn's `turn.completed` the projector **sets** `costs[trace_id] = cost_authoritative_usd` (overwrite, never `+=`), so re-processing the same trace — after a replay or a mid-session restart that re-hydrates — is a **no-op, not a double-add**. Source of truth stays `SUM(api_costs)` (ADR-0088 D3); this is a **roll-up, never a re-introduced per-loop emit**. Not `cost_gate`.

### D3 — Session context occupancy

The session lane carries the **latest** `context_tokens` forward across turns (no reset-to-0 on new input). Because `context_tokens` already reflects current working-window occupancy, it *grows* as history accumulates and *drops* when a compaction trims the window — the compaction signals (D5–D7) explain the drop. No new high-water arithmetic is required; "persist the last value across turns" is the whole change.

### D4 — Projector session-state lifecycle + substrate hydration

The projector gains a second map, `_by_session: dict[session_id, SessionAggregate]`, **alongside** the per-trace map. The per-trace map is unchanged (still evicted at `turn.completed`); the session map **survives across turns**.

- **Hydrate-on-first-touch (owner decision, 2026-06-15), into identity-keyed sets — never blind counters.** The first time the projector sees a session not in `_by_session`, it hydrates from durable substrate **once** (not per turn): the `dict[trace_id, cost]` (D2) from `api_costs` **grouped by `trace_id`** (not a bare `SUM`), and the B/D facts as **sets of event identities** (the `context.within_session_compressed` / `frozen_reset_fired` rows' own ids) rather than integer counts. The surfaced cost and counts are derived (`sum(...)` / `len(set)`). Because every fact is keyed by its own identity, hydration and the live marker path (D8) **converge idempotently**: a fact present in both the hydration read and a live marker is stored once, so a mid-turn restart that re-hydrates a partially-written turn cannot double-count. Cost: ~2 cheap reads **per session** (not per turn) — a deliberate, bounded departure from the projector's current no-read purity.
- **In-flight boundary.** Cost for the *current, not-yet-completed* trace is added only at its own `turn.completed` (D2); hydration that happens to include in-flight `api_costs` rows for that trace is reconciled by the same `trace_id`-keyed overwrite, so the live total never counts a trace twice nor stalls on one.
- **Bound.** The session map is LRU-bounded exactly like `_MAX_TRACKED_TRACES` (evict oldest beyond the cap), so it stays memory-stable without a TTL sweeper.
- **Invariants preserved.** The projector stays the **sole** `turn_status` emitter (ADR-0088 D4); session cost is a **roll-up reconciled to authoritative** (D3), never a per-loop re-emit.

### D5 — Mechanism **A** → quality **alert** (backend incident + user warning)

A gateway budget compaction is a degradation event, not a routine counter:
- **User-facing:** a **⚠ quality alert** on `turn_status` — "recalled memory/history was dropped to fit the context budget; answers this turn may be degraded."
- **Backend:** an incident signal (building on the existing context-quality incident tracker, `telemetry/context_quality.py`, which already governs budget tightening).
- **Severity split:** phase-2 **memory-context drop is high severity** (recalled memory wholesale lost — the worst quality case); phase-1 history-trim and phase-3 tool-def-drop are lower. The alert carries which phase(s) fired.
- **Lifetime (two distinct fields).** The `quality_alert` is **transient** — it reflects *this turn's* state and clears when a subsequent turn completes without A firing (the copy says "this turn"). Separately, a **persistent per-session A-incident count** records "memory was dropped N times this session" (it lives in the session aggregate like the B/D counts, hydrated and deduped the same way, D4). The transient warning and the cumulative count are not conflated.

### D6 — Mechanism **B** → **⟳ N** compaction count

The user-facing ⟳ N counts **within-session compression passes** — `context.within_session_compressed` filtered to **`trigger IN (soft, hard)`** (the filter is explicit so that if ADR-0081's planned `trigger="scheduled_reset"` ever lands on this event, scheduled resets are *not* absorbed into B and double-counted against D). One increment per pass, per session. This is the conversation-was-summarised signal. It **builds on, and does not duplicate**, the existing ADR-0076 hard-compaction user pause.

### D7 — Mechanism **D** → **↻ reset** signal + backend cadence monitor

- **User-facing:** a **↻ "cache reset ran"** signal on `turn_status` (and a per-session reset count) so the user is informed when the optimiser paid a reset (which recompacts history). Count source: **`frozen_reset_fired`** (the shipped scheduled-reset event; unconditional since the `cache_frozen_layout_enabled` flag was retired in FRE-941) — distinct from B's source (D6). If a future change reroutes the reset through `compress_in_place` with `trigger="scheduled_reset"`, the D-count source moves to that trigger and B's filter (D6) keeps them disjoint.
- **Backend:** a cadence monitor — how often the frozen reset fires vs the `L*` optimum the scheduler computed — to validate the ADR-0081 optimiser in production.

### D8 — Event wiring that preserves the sole-emitter invariant (ADR-0088 D4)

A, B, and D currently surface as a structlog event (A, D) or a bus event on a non-projector stream (B). To carry their counts/flags onto `turn_status` **without** adding a second `turn_status` emitter, each site emits a **`turn.observed`-family marker event** onto `stream:turn.observed`, which the projector folds into the session aggregate. The projector remains the sole emitter; the counts are **roll-ups** (ADR-0088 D3). (Alternative considered — subscribing the projector to additional streams — is rejected to keep the projector single-stream.)

**Marker schema.** Each marker carries the **full ADR-0088 D2 / ADR-0074 identity envelope** that every `stream:turn.observed` event requires (`trace_id`, `session_id`, `task_id`, `topology`, `model_role`) — *not* `session_id` alone — plus (a) the minimal compaction fact (which mechanism; for A, the phase/severity) and (b) a **stable fact identity** (the underlying event's id, or `(trace_id, mechanism, monotonic_index)`) so the projector can dedup it against the hydration set (D4). These markers join the existing event family (`TopologyEnteredEvent`, `ModelCallCompletedEvent`, …) the projector already dispatches on.

### D9 — `turn_status` contract extension + PWA two-lane render

- **`turn_status` STATE_DELTA** (ADR-0090 surface) gains session-lane fields: `session_cost_usd`, `session_context_tokens`, `compaction_count` (B), `cache_reset_count` (D), `quality_alert_count` (A — the **persistent per-session A-incident count** from D5), and a `quality_alert` structure (A — the **transient** this-turn warning: present/absent + severity + phases). The engagement fields are unchanged. (`quality_alert_count` and `quality_alert` are the two distinct A fields from D5's lifetime split; both are named here so the producer, PWA type contract, and ADR-0090 mapping all reference the same names.)
- **PWA** (`seshat-pwa/src/components/TurnStatusBar.tsx`, `lib/types.ts`): render the **session lane** (cumulative $, context occupancy, ⟳ B-count, ↻ D-reset, ⚠ A-alert) distinctly from the **engagement lane** (tools X/Y). Keep the existing colour thresholds. The A-alert is visually weightier than the ⟳/↻ counters (it is a degradation warning, not a tally).

### D10 — Scope boundary: **C** is carved out (separate ticket)

Mechanism C (tool-result digest) is **parked/OFF** (FRE-486) and is **not user-facing**. It is a distinct backend **data-quality + artifact-lifecycle** concern, filed as its own Needs-Approval ticket, **not** part of this ADR. Its un-park preconditions: (1) an artifact lifecycle/cleanup policy — today there is **no reaper** for digest objects in R2, only a manual `delete()` and an in-turn pin TTL; (2) creation + size + expansion-rate monitoring (`context.tool_result_digested` is the count source); (3) the file-read truncation redesign that caused the park. This ADR neither blocks nor depends on it.

### Invariants (must not regress)

- **ADR-0088 D3** — cost stays on `turn.model_call_completed`; session cost is a roll-up, not a per-loop re-emit.
- **ADR-0088 D4** — the projector stays the sole `turn_status` emitter (D8 upholds this).
- **FRE-553** — engagement `tool_iteration` keeps climbing live and resetting per engagement (D1 keeps it in its own lane).
- **ADR-0076** — the hard-compaction user pause is preserved; B's ⟳ count builds on it.

---

## Open decisions (refine in implementation / baseline)

1. **A alert severity copy + thresholds** — exact user-facing wording per phase, and whether repeated A-incidents in a session escalate the alert.
2. **D cadence-monitor thresholds** — what reset-frequency-vs-`L*` deviation is worth a backend signal.
3. **D reset detail** — whether the ↻ signal also surfaces tokens reclaimed by the reset, or just the count.
4. **PWA iconography** — the exact ⟳ / ↻ / ⚠ glyphs and placement within the session lane (footer, co-located with input per the persistent-status convention).
5. **Hydration cost** — confirm the per-session (not per-turn) hydration reads are acceptable on the gateway; if ES count reads are too slow, fall back to carry-only for the B/D counters (cost still hydrates from `api_costs`).
6. **ADR-0081 `scheduled_reset` divergence (doc-drift to reconcile).** ~~ADR-0081 §D3 Decision 4 specified the scheduled reset would emit `WithinSessionCompressionRecord` with `trigger="scheduled_reset"`; the shipped path is a separate `build_frozen_reset` / `frozen_reset_fired`. This ADR's counters are pinned to the shipped reality (D6 filters `trigger IN (soft,hard)`; D7 counts `frozen_reset_fired`) and stay correct under either model. The underlying ADR-0081 ↔ code drift should be reconciled separately (master/doc-drift), not in this ADR.~~ **Resolved 2026-07-23 (FRE-942).** This was left open in error: the reconciliation had *already* landed in the ADR-0081 direction on 2026-06-16, when ADR-0081 §D3 Decision 4 gained its "As-shipped correction" note documenting that the shipped path is `build_frozen_reset` / `frozen_reset_fired` and that `WithinSessionCompressionRecord.trigger` stays `Literal["soft","hard"]` — no `"scheduled_reset"` value exists. Code and ADR-0081 now describe one path; this ADR's counters (D6 `trigger IN (soft,hard)`, D7 `frozen_reset_fired`) already match it. This closes a **documentation** divergence only; it does **not** close the item-#7 behavioural gap (the reset *action* is still unreachable on gateway turns). Line refs at the time of resolution: `within_session_compression.py:240` (`build_frozen_reset`), `executor.py:1391` (`frozen_reset_fired`).
7. **Mechanism D has never fired in production — its counters are pinned to an uninvoked event (FRE-944, 2026-07-22).** D6/D7/D8 above all source the reset from `frozen_reset_fired`, and the ↻ "cache reset ran" signal counts it. That event is at **zero** in `agent-logs-*` — not because no reset condition was met, but because the D path is **structurally uninvoked**: `step_init`'s gateway-driven branch ends in an unconditional `return`, so `_maybe_frozen_reset`, which sits below it, is unreachable on every gateway-driven turn — and 157/157 observed turns over 30 days took that branch. FRE-944 restores the *per-turn decision emit* (`cache_reset_decision`, now carrying `accumulated_tokens` + `accum_max_tokens` so headroom is readable off one document) on the gateway path as **evaluate-and-log only**, deliberately without invoking the reset — visibility only, per that ticket's scope. So after FRE-944 the D **decision** is observable while the D **action** remains uninvoked: any consumer counting `frozen_reset_fired` should expect a legitimate zero until the separate compaction review decides whether the reset itself should run on this path. The sibling `conversation_context_loaded` emit was unreachable for the identical reason and is now restored on the gateway path the same way (FRE-945, 2026-07-22) — evaluate-and-log only, called at the top of the same branch so every gateway sub-path (including the enforced-expansion early return) emits. Its `messages_truncated` field reports `0` on the gateway path as a structural fact, not a placeholder: `step_init`'s own `apply_context_window` call is what that field measures, and that call remains unreachable on this path by design (see next). `apply_context_window` itself is still deliberately out of scope and remains unreachable (not an unbounded-context bug — gateway Stage 7 `apply_budget` trims first); restoring it would add a second truncation layer with an unverified interaction and stays a finding for the larger compaction review. **Consumers of `accumulated_tokens`: the two emit call sites do not measure the same thing.** The gateway-path emit reads *untrimmed* history; the legacy-path one runs after `apply_context_window` has truncated. Gateway-sourced and legacy-sourced values are therefore not like-for-like and must not be pooled in one series without a path dimension — though in practice the legacy path carries no production traffic. Untrimmed is the correct reading for the gateway path: the §D3 accumulation ceiling (`cache_frozen_accum_max_ratio`, 0.50) exists to schedule a reset *before* the 0.85 hard-truncation backstop engages, so measuring post-truncation would mask the very pressure it watches for.

---

## Consequences

### Positive

- The bar answers two different questions at once: *"is this turn/engagement expensive?"* (engagement lane) and *"where is this whole conversation at — cost, context pressure, how compacted, any quality risk?"* (session lane).
- The **A alert** turns the most dangerous, previously-invisible compaction (wholesale memory drop) into a user-visible and backend-tracked degradation signal.
- The **D monitor** gives the first production read on whether the ADR-0081 cache optimiser fires at its computed optimum.
- Restart-safe session totals (D4 hydration) — the meter reads true session values across redeploys.
- The four mechanisms are treated according to their actual quality/cost profiles instead of being flattened into one number.

### Negative / tradeoffs

- The projector gains substrate reads (hydration) it did not have — a bounded, per-session departure from its no-read purity (mitigated: per session, not per turn; falls back to carry-only if slow).
- More `turn.observed`-family marker events and more `turn_status` fields — a wider surface to keep consistent (ADR-0090 territory; mind the ES mapping for the new float/count fields).
- The session map is another bounded in-memory structure to reason about under eviction.

---

## Verification

Satisfied when the implementation (sequenced tickets, Observability Foundation) achieves:

1. **Cost** reads a cumulative **session** total that accumulates across turns and persists (no per-input reset); reconciles to `SUM(api_costs)`; survives a mid-session projector restart (hydration).
2. **Context** reads **session occupancy** (carried across turns; drops on compaction; no reset-to-0).
3. **⟳ B-count** increments per within-session compression pass, per session; resets on a new session; restart-safe.
4. **⚠ A-alert** fires on a gateway budget compaction, with severity reflecting memory-drop vs history/tool-def trim, both backend (incident) and user-facing.
5. **↻ D-reset** signal increments per frozen reset, with a backend cadence monitor vs `L*`.
6. **Engagement** tool count unchanged — per-engagement, climbs live (FRE-553), resets next engagement.
7. **ADR-0088 D3/D4 preserved** — sole emitter; no per-loop cost rollup re-introduced (a test asserts the projector remains the only `emit_turn_status` caller).
8. **New `turn_status` fields pass the ADR-0090 surface-contract review (D2/D6).** The added session-lane fields go through the telemetry-surface reconciliation: explicit ES mappings are added for the **float** (`session_cost_usd`), the **integer counts** (`compaction_count`, `cache_reset_count`, `quality_alert_count`), and the **structured `quality_alert`** object — none left to dynamic mapping (the first-value-0.0→`long` trap for the cost float, and the `keyword ignore_above` trap for any alert text/digest fields, are explicitly guarded).
9. PWA renders the two lanes distinctly with the existing colour thresholds; Vitest/Playwright cover the session-lane values; a projector/transport test covers the carried session fields + the three compaction signals and asserts the projector remains the only `emit_turn_status` caller (ADR-0088 D4).
10. **C** is filed as a separate ticket and is absent from this work.

---

## References

- **ADR-0088** — execution-topology observability contract (`projector.py`; D3 cost cadence, D4 sole emitter)
- **ADR-0090** — telemetry surface contract (the `turn_status` surface)
- **ADR-0076** — `turn_status` STATE_DELTA + hard-compaction pause
- **ADR-0061 / ADR-0047 / ADR-0081 / ADR-0085** — mechanisms B / A / D / C
- **Code** — `observability/topology/projector.py`, `events/models.py`, `request_gateway/budget.py`, `orchestrator/within_session_compression.py`, `orchestrator/cache_reset_scheduler.py`, `orchestrator/tool_result_digest.py`, `telemetry/compaction.py`, `seshat-pwa/src/components/TurnStatusBar.tsx`, `seshat-pwa/src/lib/types.ts`
- **Tickets** — FRE-554 (this ADR), FRE-553, FRE-501, FRE-486 (C park)
