# ADR-0065: Cost Check Gate — Atomic Reservation, Layered Budgets, Retry Telemetry

**Status**: Accepted
**Date**: 2026-04-30
**Accepted**: 2026-05-01
**Deciders**: Project owner
**Related**: ADR-0064 (inbound user identity — provides `user_id` for future per-user scoping), FRE-244 (error pattern monitoring — consumes the new retry telemetry), FRE-300 (extend pattern monitor to scan warnings — complementary signal), FRE-301 (Captain's Log iteration-limit reflection — same pattern of "agent was constrained, surface it"), FRE-311 (auto-tuning monitor that proposes cap adjustments via Linear tickets — consumes this gate's telemetry)
**Implementation Plan**: `docs/superpowers/plans/2026-05-01-fre-302-cost-check-gate.md`
**Linear**: FRE-302 (parent) · FRE-303 / FRE-304 / FRE-305 / FRE-306 / FRE-307 (sub-tasks)

---

## Context

On 2026-04-30, the agent gateway stopped responding to user messages. Investigation traced the failure to the weekly cloud API budget enforcement in `src/personal_agent/llm_client/litellm_client.py` (lines 169–186):

```python
weekly_cost = await cost_tracker.get_weekly_cost(provider=None)
if weekly_cost >= _settings.cloud_weekly_budget_usd:
    raise ValueError(...)
```

The configured cap was `AGENT_CLOUD_WEEKLY_BUDGET_USD=10.0`. Actual weekly spend at the time of failure: **$15.44** — 54% over the cap. Every subsequent inference call raised `ValueError` immediately. The PWA silently rendered the resulting error as an empty assistant turn, so users saw "agent never responded" while the service log filled with budget violations.

Two distinct failure modes drove the overshoot:

**Failure mode 1 — concurrent overshoot (the race).** The check is read-then-execute: pre-call check reads the running total, returns go/no-go, then the call proceeds. There is no atomic primitive between read and the eventual cost write to `api_costs`. When the consolidation pipeline fans out entity-extraction workers across multiple sessions concurrently (visible in ES at 06:35:28 — six trace_ids failing simultaneously, again at 06:41:47 — same six retried), every worker reads the same pre-call total, every worker passes the check, every worker spends, total exceeds cap by Σ(in-flight cost).

**Failure mode 2 — single-call overshoot.** Even with one caller, a single call from $9.50 to $15.50 passes the `$9.50 < $10.00` check and is only caught by the *next* call. The current gate cannot prevent any single call from breaching the cap by its own full cost.

Neither failure mode is solved by raising the limit. The structural problem is that the gate is advisory, not transactional.

A separate but reinforcing observation came from the same investigation: when entity-extraction workers fail (budget or otherwise), the consolidator emits `consolidation_extraction_fallback_skip` (with `trace_id`) and the underlying executor emits `entity_extraction_failed` (with **no** `trace_id`). Neither carries an attempt number, a denial reason enum, or any aggregable signal. Retry health is reconstructable only via bespoke ES queries grouping warnings by `trace_id` and counting hits. This is the same shape of observability gap that ADR-0056 (error pattern monitoring) and FRE-300/301 already track: actionable signals exist in the runtime but are not first-class telemetry.

The user explicitly framed the desired behavior: "A cost check gate" — a single primitive that any caller asks before spending money, that survives restart, that maintains fine-grained per-call visibility, and that produces the retry telemetry the current system lacks.

---

## Decision

Introduce a **Cost Check Gate** — a Postgres-backed atomic reservation primitive in front of every paid LLM call, with policy keyed by `(time_window, role)` for v1, schema-ready for `(user_id, provider)` in v2. Failure semantics are role-aware: background workers NACK and retry on the next window; user-facing inference raises a structured error the PWA renders explicitly. Retry telemetry is folded into the same change so cost pressure becomes observable end-to-end.

### D1 — Atomic reservation, not advisory check

The gate replaces the read-then-execute pattern with a transactional reserve-and-commit pattern. Every cloud LLM call follows the same lifecycle:

1. **Reserve.** Caller asks the gate for a reservation sized to the *estimated* cost (see D3). Inside a single Postgres transaction, the gate:
   a. `SELECT … FOR UPDATE` the active counter row for `(time_window, role)`.
   b. Compares `running_total + estimated_cost` against the policy cap.
   c. On approval: increments `running_total`, writes a `budget_reservations` row with a UUID token and `expires_at = now + 90s`, returns `(token, approved=True)`.
   d. On denial: returns `(token=None, approved=False, reason=…)` without modifying state.
2. **Spend.** Caller makes the LLM call.
3. **Commit.** On success, caller commits actual cost: writes a row to `api_costs` (existing ledger, unchanged) and writes a `budget_commits` row referencing the reservation token. The difference between estimated and actual is refunded to the counter (`running_total -= estimate - actual`).
4. **Refund.** On failure, caller refunds the full reservation (`running_total -= estimated`).
5. **Reaper.** A periodic job sweeps `budget_reservations` where `expires_at < now AND status = 'active'` and refunds them — this catches cases where the caller crashed between reserve and commit.

The `SELECT … FOR UPDATE` is the atomicity primitive. Postgres serializes concurrent transactions on the same row; concurrent extraction workers no longer share a stale snapshot. The 90-second reservation TTL is longer than the worst-case LLM call (`default_timeout: 60` for Sonnet) and is reaped on a 30-second cadence.

### D2 — Layered budgets keyed by (time_window, role); schema-ready for (user_id, provider)

The policy table `budget_policies` is keyed as `(user_id NULL, time_window, provider NULL, role)` with a `cap_usd` value:

| user_id | time_window | provider | role | cap_usd |
|---|---|---|---|---|
| NULL | `daily` | NULL | `entity_extraction` | 1.50 |
| NULL | `daily` | NULL | `captains_log` | 0.50 |
| NULL | `daily` | NULL | `main_inference` | 5.00 |
| NULL | `weekly` | NULL | `entity_extraction` | 7.00 |
| NULL | `weekly` | NULL | `main_inference` | 18.00 |
| NULL | `weekly` | NULL | `_total` | 25.00 |

A reservation for role `R` checks every policy row matching `(NULL or matching user_id) AND time_window AND (NULL or matching provider) AND (R or '_total')`. All matching caps must approve; a single denial denies the reservation. The most restrictive cap wins.

For v1, only `(time_window, role)` rows are populated. `user_id` and `provider` columns are `NULL` for all policy rows but exist in the schema. v2 (when a second user arrives or per-provider policy is needed) is a config change, not a migration. This honors the "schema-ready, policy-deferred" decision from brainstorming.

The `_total` role is a synthetic role representing the global cap — it is checked on every reservation regardless of caller role, which prevents one role from starving the others.

The default v1 caps above are placeholders intended to be edited in `config/governance/budget.yaml` before first deploy; the values shown reflect the user's stated tolerance ($25/week) and the observed extraction-vs-inference fan-out ratio. Final values are operator-tunable without code changes.

### D3 — Reservation amount: estimated output × safety factor

Reservation amount per call:

```
input_cost  = exact_input_tokens   × model.input_price_per_token
output_cost = min(max_tokens, default_output_tokens) × model.output_price_per_token × safety_factor
estimated   = input_cost + output_cost
```

Where `default_output_tokens` and `safety_factor` are per-role values declared in `config/governance/budget.yaml` alongside the caps (e.g. `main_inference: {default_output_tokens: 1024, safety_factor: 1.2}`, `entity_extraction: {default_output_tokens: 256, safety_factor: 1.2}`). The model registry in `config/models.yaml` already exposes `max_tokens` and per-token prices; the helper to compute estimates lives in `src/personal_agent/llm_client/cost_estimator.py` (new file) and is unit-tested against actual completion costs.

The post-call commit step refunds the gap between estimated and actual, so the gate over-reserves transiently but settles to actual within the call duration. This rejects 95%+ of overshoots without rejecting normal-shaped calls (the fixed-deposit alternative caught only the concurrency race; the reserve-max alternative starved concurrent calls when `max_tokens=8192` for Sonnet).

### D4 — Implementation substrate: Postgres only

All gate state lives in Postgres. Three new tables:

- `budget_policies` — caps keyed as in D2. Operator-editable via SQL or `config/governance/budget.yaml` loader.
- `budget_counters` — running totals per `(user_id, time_window, provider, role, window_start)`. Indexed on the composite key. The row is what `SELECT … FOR UPDATE` locks during reservation. `window_start` is normalized to UTC midnight (daily) or UTC Monday midnight (weekly) so windows roll automatically without a cron job — a reservation against a "new" window writes a new row with zero running total.
- `budget_reservations` — active and settled reservations. Status enum: `active | committed | refunded | expired`. UUID token referenced from `api_costs` rows for audit.

The existing `api_costs` table is unchanged. It remains the durable per-call ledger. `budget_counters.running_total` is a denormalized cache rebuilt at startup from `api_costs` to defend against cache drift (the rebuild is a single `SELECT SUM(cost_usd) GROUP BY` and runs in milliseconds against a week of data).

Redis is intentionally not used. Cost gating fires once per LLM call, not once per token; 5–15ms of Postgres overhead is negligible against a 2–60s LLM call. The Redis-Lua hot-path option was evaluated and rejected: it adds a coordination problem (sync Redis to Postgres ledger), a state-recovery problem (replay `api_costs` into Redis on startup), and an ops surprise (someone forgets `appendonly yes`). Postgres-only earns simplicity; the latency Redis offers is wasted on this call site.

### D5 — Failure semantics: role-aware policy

When a reservation is denied, the gate returns a `BudgetDenied` exception carrying:

- `denied_role` — which role tripped the cap
- `denied_window` — `daily` / `weekly` / `_total`
- `current_spend_usd`, `cap_usd`
- `window_resets_at` — UTC timestamp of the next window boundary

Each calling role declares its denial behavior in `config/governance/budget.yaml`:

| Role | On denial |
|---|---|
| `main_inference` | Raise; FastAPI handler converts to a structured `503 BudgetDenied` JSON response; PWA renders an explicit error card with the spend/cap/reset time and a "raise cap" link. **No silent empty turns.** |
| `entity_extraction` | Caller catches and **NACKs** the consumer-group event. Redis Streams redelivers after `AGENT_EVENT_BUS_ACK_TIMEOUT_SECONDS=300`. The next consolidation tick or the next window roll succeeds naturally. |
| `captains_log`, `insights`, `promotion` | Same as `entity_extraction` — NACK, redeliver. After `event_bus_max_retries=3` failures, dead-letter to `stream:dead_letter` (existing path). |

The NACK behavior for event-driven consumers is the implementation that makes "queue up and retry naturally" actually work. Without it, denied background events ACK silently and the work is lost — the same shape of bug that produced the original "agent never responded" failure at a different layer.

The structured `503 BudgetDenied` payload is deliberately user-facing. The original incident was made worse because the PWA had no way to distinguish "agent is thinking" from "agent silently failed." The gate forces an unambiguous failure surface for user-facing calls.

### D6 — Retry telemetry (folded in from FRE-244 observability gap)

The same change instruments the consolidation pipeline so retry health becomes first-class telemetry. Three concrete fixes:

1. **`entity_extraction_failed` gains `trace_id`** (entity_extraction.py:328). Every error log on the extraction path carries the originating capture's trace_id. Correlation with consolidation events is then a single ES join.
2. **A new `consolidation_attempts` table** records `(trace_id, attempt_number, started_at, completed_at, outcome, denial_reason)` per attempt. Outcome enum: `success | budget_denied | model_error | extraction_returned_fallback`. `attempt_number` increments per `trace_id`. Scope: this table covers entity-extraction retries only, because consolidation runs are *scheduled* (the next idle-tick re-picks an unconsolidated trace, which Redis Streams cannot represent). Event-driven retries (`cg:captain-log`, `cg:insights`, `cg:promotion`, `cg:freshness`) already have first-class redelivery telemetry via Redis Streams pending-entry counts (`XPENDING` exposes `delivery_count` per stuck event); the dashboard joins both sources.
3. **Existing log events extended** — `consolidation_extraction_fallback_skip` and `entity_extraction_failed` both gain `attempt_number`, `previous_failure_count`, `time_since_first_attempt_seconds`, and a structured `denial_reason` enum (replacing the freeform `reason` string). All four fields are Kibana-aggregable.

This data flows into a new Kibana dashboard panel ("Extraction Retry Health") showing median attempts to success, dead-letter rate per role, retry-storm density during budget-exhaustion windows, and a per-trace timeline. The same data feeds Captain's Log proposals for Linear when retry rates spike — the existing pattern from FRE-244.

The telemetry change is small (one new table, one log-field addition) and is folded into this ADR rather than a separate ticket because it directly addresses the observability gap that hid this incident. Splitting it would create a dependency between two PRs that share a single commit's worth of consolidator changes.

---

## Consequences

**Positive:**

- Concurrent overshoots are eliminated. `SELECT … FOR UPDATE` makes check+reserve atomic; concurrent workers serialize on the counter row.
- Single-call overshoots are bounded by the safety factor. A call that exceeds its 1.2× estimate is the only path to overshoot, and the next reservation catches it.
- Background work no longer disappears silently on budget denial. NACK + Redis Streams redelivery replays the work after the window rolls or the cap is raised.
- User-facing inference produces an explicit, renderable failure when over budget. The "empty assistant turn" failure mode that prompted this ADR cannot reproduce.
- Retry health is observable. Operators can answer "are we burning budget on retries?" without bespoke ES queries.
- Per-user and per-provider policy is a v2 config change, not a v2 migration. Schema is forward-ready.

**Negative:**

- Every paid LLM call now does an extra Postgres round-trip (reserve) plus a second one on commit. ~5–15ms each, negligible against LLM call latency, but it does add load to Postgres.
- The reaper job is a new periodic process that must run reliably. Failure to reap stale reservations leaks budget headroom (calls get rejected when actual spend is fine). Mitigation: the reaper is a single SQL `UPDATE … WHERE expires_at < now()` running every 30s; it has no other state.
- Estimated-cost reservation transiently over-reserves. A request that ultimately costs $0.10 may temporarily hold $0.30 in reservation, blocking concurrent calls during the LLM round-trip. Mitigation: estimates are accurate to within 1.2× by construction; the post-call refund settles within seconds.
- Operators must set sensible per-role caps in `budget.yaml`. Wrong caps produce the same user-visible failure as the current bug, just with better telemetry. Mitigation: the structured `BudgetDenied` payload includes `cap_usd` so the user sees what to change.

**Neutral / explicit non-goals:**

- This ADR does not introduce per-session cost limits. The brainstorming explicitly chose not to (cost_limit_per_session in profiles/cloud.yaml exists today as a soft hint and is unrelated).
- This ADR does not introduce automatic provider failover (Sonnet→Haiku→local). Brainstorming option C was rejected; silent fallback defeats the gate's observability purpose.
- Local-model calls are not gated. `LocalLLMClient` has no cost; its omission is correct.
- The `_total` synthetic role is not user-configurable; it always equals the sum of all per-role caps for the same window.

---

## Implementation Notes

The work decomposes into five sub-tasks suitable for sequential PRs:

1. **Schema + migration.** Add `budget_policies`, `budget_counters`, `budget_reservations`, `consolidation_attempts`. Backfill `budget_counters` from `api_costs` on first start.
2. **Gate primitive.** New module `src/personal_agent/cost_gate/` with `reserve()`, `commit()`, `refund()`, `reaper()`. Unit tests cover concurrent reservation contention via `psycopg`'s `transaction(isolation_level=...)` simulation.
3. **Caller integration.** `LiteLLMClient.respond()` switches from advisory check to gate primitive. Each role passes its identifier; estimator computes reservation amount.
4. **Failure semantics.** Event-driven consumers (`cg:freshness`, `cg:captain-log`, `cg:insights`, `cg:promotion`) catch `BudgetDenied` and NACK. FastAPI handler maps `BudgetDenied` to a structured 503. PWA renders the structured payload.
5. **Telemetry.** Consolidation_attempts table, log-field additions, Kibana dashboard panel.

Each sub-task is independently testable. Sub-tasks 1–3 are the critical path for closing the original bug; 4–5 are the polish layer.

---

## Status Tracking

| Sub-task | Linear | Commit | Status |
|---|---|---|---|
| Schema + migration | FRE-303 | `550e77a` | ✅ Done |
| Schema NULL hotfix (NULLS NOT DISTINCT) | inline (FRE-303 follow-up) | `73ab4cf` | ✅ Done |
| Gate primitive | FRE-304 | `2a9241f` | ✅ Done |
| Caller integration + cost_estimator | FRE-305 | `8834a9a` | ✅ Done |
| Failure semantics + PWA error card | FRE-306 | `9e3d94b` | ✅ Done |
| Retry telemetry + Kibana panel | FRE-307 | `1aa2151` | ✅ Done |

### Implementation deltas from the ADR text

- **Alembic skipped** — the ADR's "schema + migration" implementation note assumed Alembic was a working migration tool; it isn't and never has been in this repo. Migrations live as standalone SQL files under `docker/postgres/migrations/` (matching every other schema change in the project) plus the `init.sql` append for fresh installs. Bootstrapping Alembic was scoped to a separate decision.
- **Schema NULL bug surfaced + fixed mid-flight** — `UNIQUE (user_id, time_window, provider, role, …)` doesn't enforce uniqueness when those NULL columns are NULL (Postgres default). 50-racer concurrent test caught it: 8 wins on a 5-cap. Fixed via `NULLS NOT DISTINCT` (PG 15+; we run PG 17). Migration `0002_cost_gate_null_uniqueness.sql` cleans up duplicate rows on already-migrated DBs.
- **Background consumer NACK semantics** — the ADR text describes "Redis Streams redelivers after AGENT_EVENT_BUS_ACK_TIMEOUT_SECONDS=300" but the codebase doesn't yet implement XCLAIM-based pending-entry reclamation. Pragmatic implementation: catch BudgetDenied in the consumer runner, ACK + log a structured `consumer_budget_denied`, no dead-letter. Recovery happens via the next scheduled consolidation tick re-picking the trace. XCLAIM-based reclamation is a separate follow-up.
- **Auto-tuning monitor (FRE-311)** filed as a follow-up — the user surfaced the need for a Captain's-Log-style monitor that reads the gate's telemetry and proposes Linear tickets to raise/lower caps. Out of FRE-302 scope; depends on this ADR and FRE-307 landing first.
- **Stale-test sweep (FRE-312)** filed — the FRE-302 work uncovered 74 pre-existing test failures + 1 collection error from earlier refactors (FRE-261 / FRE-262 / FRE-282) that didn't update their tests. Tracked separately so `make test` can be made meaningful again.
