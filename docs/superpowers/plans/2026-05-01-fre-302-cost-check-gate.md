# FRE-302 — ADR-0065 Cost Check Gate Implementation Plan

**Worktree:** `/opt/seshat/.worktrees/fre-302`
**Branch:** `fre-302-adr-0065-cost-check-gate-atomic-reservation-layered-budgets-retry`
**ADR:** `docs/architecture_decisions/ADR-0065-cost-check-gate.md`
**Linear parent:** FRE-302 (Approved). Children FRE-303 / 304 / 305 / 306 / 307 — **all Approved** (user confirmed in this session).

---

## Context

On 2026-04-30 the agent gateway stopped responding to user messages. The pre-call advisory budget check in `src/personal_agent/llm_client/litellm_client.py` (lines 169–186) is read-then-execute: it reads `SUM(cost_usd) FROM api_costs`, returns go/no-go, and proceeds. The actual weekly spend reached **$15.44 against a $10.00 cap** because (a) concurrent extraction/insights/promotion/freshness consumers all read the same pre-call total and all proceeded, and (b) a single call could individually breach the cap by its own full cost. The PWA silently rendered the resulting `ValueError` as **empty assistant turns** for every chat request — denials were never surfaced to the user.

ADR-0065 replaces the advisory check with a Postgres-backed atomic reservation (`reserve` → call → `commit`/`refund`), introduces layered per-role / per-window caps loaded from `config/governance/budget.yaml`, defines role-aware denial semantics (raise for user-facing chat, NACK for background consumers so Redis Streams redelivers), and folds in the retry-telemetry observability gap that hid the original incident (per-attempt `consolidation_attempts` table, `trace_id` on `entity_extraction_failed`, structured `denial_reason` enum, new "Extraction Retry Health" Kibana panel).

This plan delivers all five sub-tasks (FRE-303 → FRE-307) in dependency order. The intended outcome: a single call to `cost_gate.reserve(role, amount)` either approves and atomically increments a Postgres counter, or raises `BudgetDenied` carrying enough information for the PWA to render an explicit error card. No more empty turns, no more concurrent overshoots, and retry health is observable in Kibana.

---

## Decision deltas from the ADR

The ADR was written assuming Alembic was a working migration tool in the project. **It isn't.** Git history confirms zero Alembic usage (no `alembic.ini`, no `alembic/` directory, zero migration files, zero `migration` commits) — the package is in `pyproject.toml` but the docs (`CLAUDE.md` migrations section) were aspirational. Every table the project has ever shipped lives in `docker/postgres/init.sql`.

**Decision:** Skip Alembic bootstrap. For each new table:
1. Append `CREATE TABLE IF NOT EXISTS …` to `docker/postgres/init.sql` so fresh installs work.
2. Add a standalone **one-shot SQL file** at `docker/postgres/migrations/0001_cost_gate_schema.sql` so existing DBs (including production) can be brought up with a single `psql -f`. Idempotent (`IF NOT EXISTS`).
3. Add SQLAlchemy ORM models in `src/personal_agent/service/models.py` for ORM consumers.

This matches every prior schema change in the repo. If you'd rather bootstrap Alembic properly, that's a separate prerequisite ticket — flag it before approval and I'll restructure FRE-303.

Everything else in the ADR stands as written.

---

## Phase 1 — FRE-303: Schema + ORM models + backfill

**Files to modify / create:**

- `docker/postgres/init.sql` (append) — fresh-install path
- `docker/postgres/migrations/0001_cost_gate_schema.sql` (new) — existing-DB path
- `src/personal_agent/service/models.py` (append) — ORM models
- `src/personal_agent/service/cost_gate_models.py` (new, optional split) — keep gate models out of the bigger models.py if it's already sprawling
- `tests/personal_agent/cost_gate/test_schema.py` (new) — sanity test that verifies the four tables + backfill against a real Postgres test DB

**SQL — four new tables.** Money columns use the project's existing `DECIMAL(10, 6)` convention (cost_tracker.py:92, init.sql:68). UUIDs use `gen_random_uuid()`. Datetimes use `TIMESTAMPTZ`.

```sql
-- 0001_cost_gate_schema.sql

-- Layered policies (D2). user_id and provider columns present from day 1
-- (nullable in v1) so v2 per-user / per-provider caps drop in without migration.
CREATE TABLE IF NOT EXISTS budget_policies (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID,                       -- v1: NULL; v2: per-user policy
    time_window VARCHAR(16) NOT NULL,   -- 'daily' | 'weekly'
    provider VARCHAR(32),               -- v1: NULL; v2: per-provider policy
    role VARCHAR(64) NOT NULL,          -- 'main_inference' | 'entity_extraction' | ... | '_total'
    cap_usd DECIMAL(10, 6) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, time_window, provider, role)
);
CREATE INDEX IF NOT EXISTS idx_budget_policies_lookup
    ON budget_policies(time_window, role) WHERE user_id IS NULL AND provider IS NULL;

-- Running totals — the row that SELECT … FOR UPDATE locks during reservation.
-- window_start normalised to UTC midnight (daily) / UTC Monday midnight (weekly)
-- so windows roll automatically without a cron job.
CREATE TABLE IF NOT EXISTS budget_counters (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID,                       -- v1: NULL
    time_window VARCHAR(16) NOT NULL,   -- 'daily' | 'weekly'
    provider VARCHAR(32),               -- v1: NULL
    role VARCHAR(64) NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,  -- normalised window boundary
    running_total DECIMAL(10, 6) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, time_window, provider, role, window_start)
);
CREATE INDEX IF NOT EXISTS idx_budget_counters_lookup
    ON budget_counters(time_window, role, window_start);

-- Active and settled reservations (D1). UUID token referenced for audit.
CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    counter_id BIGINT NOT NULL REFERENCES budget_counters(id),
    role VARCHAR(64) NOT NULL,
    amount_usd DECIMAL(10, 6) NOT NULL,
    actual_cost_usd DECIMAL(10, 6),     -- populated on commit
    status VARCHAR(16) NOT NULL,        -- 'active' | 'committed' | 'refunded' | 'expired'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,    -- created_at + 90s
    settled_at TIMESTAMPTZ,
    trace_id UUID
);
-- Reaper hot-path: only scan active reservations past their TTL.
CREATE INDEX IF NOT EXISTS idx_budget_reservations_reaper
    ON budget_reservations(expires_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_budget_reservations_trace
    ON budget_reservations(trace_id);

-- Per-attempt telemetry (D6). Covers entity-extraction retries; event-driven
-- redelivery is observable separately via Redis Streams XPENDING.
CREATE TABLE IF NOT EXISTS consolidation_attempts (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    role VARCHAR(64) NOT NULL,          -- which consumer attempted: entity_extraction | promotion | …
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    outcome VARCHAR(32) NOT NULL,       -- 'success' | 'budget_denied' | 'model_error' | 'extraction_returned_fallback' | 'transient_failure' | 'dead_letter'
    denial_reason VARCHAR(64),          -- enum: 'cap_exceeded' | 'policy_violation' | 'reservation_failed' | 'provider_error' | NULL
    UNIQUE (trace_id, attempt_number, role)
);
CREATE INDEX IF NOT EXISTS idx_consolidation_attempts_trace
    ON consolidation_attempts(trace_id);
CREATE INDEX IF NOT EXISTS idx_consolidation_attempts_outcome
    ON consolidation_attempts(outcome, started_at DESC);
```

**Backfill — `budget_counters` from `api_costs`.** Apply at the bottom of the migration file; idempotent because of `ON CONFLICT … DO NOTHING`. v1 only populates the unscoped (`user_id NULL`, `provider NULL`) `_total` rows for the current daily and weekly windows so the gate sees existing spend immediately on first start. (Per-role backfill isn't possible — `api_costs.purpose` is freeform and doesn't map cleanly to ADR roles; the gate will start tracking per-role spend going forward.)

```sql
-- Weekly _total backfill — current ISO week (UTC Monday midnight)
INSERT INTO budget_counters (user_id, time_window, provider, role, window_start, running_total)
SELECT
    NULL,
    'weekly',
    NULL,
    '_total',
    date_trunc('week', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',
    COALESCE(SUM(cost_usd), 0)
FROM api_costs
WHERE timestamp >= date_trunc('week', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
ON CONFLICT (user_id, time_window, provider, role, window_start) DO NOTHING;

-- Daily _total backfill
INSERT INTO budget_counters (user_id, time_window, provider, role, window_start, running_total)
SELECT
    NULL, 'daily', NULL, '_total',
    date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',
    COALESCE(SUM(cost_usd), 0)
FROM api_costs
WHERE timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
ON CONFLICT (user_id, time_window, provider, role, window_start) DO NOTHING;
```

**ORM models** mirror existing patterns in `src/personal_agent/service/models.py` (declarative_base, `PG_UUID(as_uuid=True)`, `DateTime(timezone=True)`, `Numeric(10, 6)` for money). Used by FRE-307's consolidation_attempts writes; FRE-304's gate primitive uses raw asyncpg (matches `cost_tracker.py` hot-path style).

**Verification:**
```bash
# Apply migration to a fresh test DB
docker compose up -d postgres
docker compose exec postgres psql -U agent -d personal_agent -f /docker-entrypoint-initdb.d/migrations/0001_cost_gate_schema.sql
docker compose exec postgres psql -U agent -d personal_agent -c "\d budget_policies budget_counters budget_reservations consolidation_attempts"
# Backfill check
docker compose exec postgres psql -U agent -d personal_agent -c \
  "SELECT role, time_window, running_total FROM budget_counters WHERE role='_total';"
# Run unit test
make test-file FILE=tests/personal_agent/cost_gate/test_schema.py
```

Acceptance: `\d` shows all four tables with the documented columns, indexes, constraints. Backfill row exists for current weekly + daily `_total` matching the api_costs sum.

---

## Phase 2 — FRE-304: Gate primitive (`reserve` / `commit` / `refund` / `reaper`)

**New module:** `src/personal_agent/cost_gate/`

```
cost_gate/
├── __init__.py        # public API: reserve, commit, refund, BudgetDenied
├── gate.py            # core implementation
├── policy.py          # loads budget.yaml + queries budget_policies
├── reaper.py          # background task sweeping stale reservations
└── types.py           # ReservationId (NewType[UUID]), DenialReason enum
```

**Public API (matches FRE-304 ticket spec):**

```python
async def reserve(
    role: str,
    amount: Decimal,
    *,
    ctx: TraceContext,
    user_id: UUID | None = None,
    provider: str | None = None,
) -> ReservationId: ...

async def commit(
    reservation_id: ReservationId,
    actual_cost: Decimal,
    *,
    ctx: TraceContext,
) -> None: ...

async def refund(
    reservation_id: ReservationId,
    *,
    ctx: TraceContext,
) -> None: ...
```

**`reserve()` transaction (D1)** — runs in a single asyncpg transaction:

1. Resolve every matching policy row from cache: `(NULL or matching user_id) AND time_window AND (NULL or matching provider) AND (role or '_total')`. All matching caps must approve.
2. For each matching policy, compute the `(time_window, role, window_start)` key (window_start normalised by `date_trunc` on UTC). Upsert the `budget_counters` row if missing (zero total).
3. `SELECT … FOR UPDATE` every matching counter row in a deterministic order (by `id`) to avoid deadlocks.
4. For each: check `running_total + amount <= cap_usd`. If any fails → raise `BudgetDenied` carrying the most-restrictive denied row's `(role, time_window, current_spend, cap, window_resets_at, denial_reason='cap_exceeded')`.
5. On approval: increment every locked counter by `amount`, insert one `budget_reservations` row with `status='active'`, `expires_at = NOW() + interval '90 seconds'`, return its UUID.

The `_total` synthetic role: every reservation also locks the `_total` row for the relevant window — prevents one role from starving others.

**`commit()`** — single transaction: read the reservation, verify `status='active'`, compute `delta = actual_cost - amount_usd` (negative when actual < estimate), apply `delta` to every counter row that was incremented at reserve time, mark reservation `committed` with `actual_cost_usd`, `settled_at = NOW()`. Also writes a row to `api_costs` (existing ledger, unchanged) with `trace_id` and a new `purpose=role` mapping so audits work.

**`refund()`** — single transaction: read the reservation, verify `status='active'`, decrement every counter row by the original `amount_usd`, mark reservation `refunded` with `settled_at = NOW()`. Idempotent: a `refund` on an already-`refunded` row is a no-op (logged at debug).

**`reaper()`** — async background task launched at app startup. Every 30s: `UPDATE budget_reservations SET status='expired', settled_at=NOW() WHERE status='active' AND expires_at < NOW() RETURNING reservation_id, counter_id, amount_usd`. Use the returned rows to decrement each counter (single transaction per sweep). Logs `cost_gate_reaper_swept` with count.

**Policy cache:** load `config/governance/budget.yaml` once at startup (mirrors `governance_loader.py` pattern), keep an in-memory dict keyed by `(user_id, time_window, provider, role)`. Fall back to a SELECT from `budget_policies` if the YAML is missing — but YAML is canonical, table is the audit/v2 substrate.

**`config/governance/budget.yaml`** (new):
```yaml
version: 1
roles:
  main_inference:
    default_output_tokens: 1024
    safety_factor: 1.2
    on_denial: raise
  entity_extraction:
    default_output_tokens: 256
    safety_factor: 1.2
    on_denial: nack
  captains_log:
    default_output_tokens: 512
    safety_factor: 1.2
    on_denial: nack
  insights:
    default_output_tokens: 512
    safety_factor: 1.2
    on_denial: nack
  promotion:
    default_output_tokens: 256
    safety_factor: 1.2
    on_denial: nack
  freshness:
    default_output_tokens: 128
    safety_factor: 1.2
    on_denial: nack
caps:
  - {time_window: daily,  role: entity_extraction, cap_usd: 2.50}   # user-confirmed 2026-05-01
  - {time_window: daily,  role: captains_log,      cap_usd: 2.50}   # user-confirmed 2026-05-01
  - {time_window: daily,  role: main_inference,    cap_usd: 5.00}
  - {time_window: weekly, role: entity_extraction, cap_usd: 7.00}
  - {time_window: weekly, role: main_inference,    cap_usd: 18.00}
  - {time_window: weekly, role: _total,            cap_usd: 25.00}
```

✅ **Caps user-confirmed 2026-05-01.** `entity_extraction` and `captains_log` dailies bumped from the ADR placeholders ($1.50 / $0.50) to $2.50 each — *"for the days I have a lot of interaction"*. Memory: `feedback_budget_cap_values.md`. Deviation requires re-confirmation per memory `feedback_ask_before_budget_changes`.

ℹ️ **Auto-tuning follow-up filed: FRE-311** — a periodic monitor reads gate telemetry and proposes Linear tickets to raise/lower caps (Captain's-Log self-observability pattern). Out of scope for FRE-302; depends on FRE-302 + FRE-307 landing first.

**Tests** (`tests/personal_agent/cost_gate/`):

- `test_gate.py` — happy path: reserve → commit settles to actual cost; reserve → refund returns counter to zero
- `test_concurrent.py` — N=50 async tasks reserve $0.20 each against a `_total` cap of $1.00; assert exactly 5 succeed and 45 raise `BudgetDenied`, final counter = $1.00 (per FRE-304 acceptance)
- `test_reaper.py` — reserve, sleep past 90s, run one reaper sweep, assert reservation now `expired` and counter decremented (per FRE-304 acceptance)
- `test_overshoot.py` — commit with `actual_cost > amount_usd × safety_factor` is allowed but logged at warning (overshoot path bounded by safety factor)
- `test_idempotent.py` — `refund` on already-refunded reservation is a no-op

Concurrent contention test uses real Postgres (testcontainers or a `conftest.py` fixture against the dev DB) — `SELECT … FOR UPDATE` is the whole point and can't be mocked.

**Verification:**
```bash
make test-file FILE=tests/personal_agent/cost_gate/test_concurrent.py
make test-file FILE=tests/personal_agent/cost_gate/test_reaper.py
make mypy           # cost_gate/ must type-check
make ruff-check
```

Acceptance: all FRE-304 acceptance bullets pass; reaper sweeps stale reservation within one 30s cycle.

---

## Phase 3 — FRE-305: LiteLLMClient integration + cost_estimator

**New module:** `src/personal_agent/llm_client/cost_estimator.py`

```python
def estimate_reservation(
    role: str,
    input_tokens: int,
    max_tokens: int | None,
    input_price_per_token: Decimal,
    output_price_per_token: Decimal,
    *,
    config: BudgetConfig,
) -> Decimal:
    """reservation = exact_input_cost + (min(max_tokens, default_output_tokens)
                       × output_price × safety_factor)"""
```

- `default_output_tokens` and `safety_factor` come from `BudgetConfig.roles[role]` loaded from `budget.yaml` (Phase 2)
- `input_tokens` source: pre-call counter — `litellm.token_counter(model=…, messages=…)`. Verified to exist; falls back to a tiktoken-based count if the litellm helper is unavailable for the model. Result is approximate; the post-call commit refunds the gap.
- Per-token pricing: `litellm.model_cost[model]` exposes `input_cost_per_token` / `output_cost_per_token` for every supported model; the existing post-call path already uses `litellm.completion_cost()` from the same registry.

**`LiteLLMClient.respond()` change** (`src/personal_agent/llm_client/litellm_client.py`):

Replace lines 169–186 with:

```python
# Atomic reservation (replaces advisory _check_budget — see ADR-0065 D1)
reservation_amount = estimate_reservation(
    role=role.value,
    input_tokens=litellm.token_counter(model=self._litellm_model, messages=api_messages),
    max_tokens=effective_max_tokens,
    input_price_per_token=Decimal(str(litellm.model_cost[self._litellm_model]["input_cost_per_token"])),
    output_price_per_token=Decimal(str(litellm.model_cost[self._litellm_model]["output_cost_per_token"])),
    config=load_budget_config(),
)
reservation_id = await cost_gate.reserve(
    role=role.value,
    amount=reservation_amount,
    ctx=trace_ctx,
)
```

Then wrap the existing `litellm.acompletion(...)` call so:
- success path → `await cost_gate.commit(reservation_id, Decimal(str(actual_cost)))` (after `litellm.completion_cost()` resolves around line 309)
- failure path → `await cost_gate.refund(reservation_id)` then re-raise

The existing `cost_tracker.record_api_call` call (lines 313–324) is **kept** — `api_costs` remains the durable per-call ledger. `cost_gate.commit()` is the counter update; `record_api_call` is the audit row. No double-write race because both run in the success path after the LLM round-trip succeeds.

**Role string mapping.** The current `ModelRole` enum (`PRIMARY | SUB_AGENT | COMPRESSOR`) is the *executor* role, not the *budget* role. Budget roles are richer: `main_inference, entity_extraction, captains_log, insights, promotion, freshness`. The existing `role_name: str` parameter that callers already pass through `get_llm_client(role_name=…)` (visible in `factory.py` and `models.yaml`) is the right channel — `LiteLLMClient` will accept a `budget_role: str` kwarg distinct from `role: ModelRole`. Default for back-compat: budget_role mirrors the role_name string.

**Tests** (`tests/personal_agent/llm_client/`):

- `test_cost_estimator.py` — estimator math, edge cases (max_tokens=None, max_tokens < default_output_tokens)
- `test_litellm_gate_integration.py` — mock `cost_gate.reserve/commit/refund`, drive `respond()` with success and exception paths, assert sequences:
  - success: `reserve → completion → commit(actual)`
  - failure: `reserve → completion(raises) → refund → raise`
  - denied: `reserve(raises BudgetDenied) → completion never called`

**Verification:**
```bash
make test-file FILE=tests/personal_agent/llm_client/test_cost_estimator.py
make test-file FILE=tests/personal_agent/llm_client/test_litellm_gate_integration.py
make test-k K=test_litellm  # full litellm suite still green
make mypy
```

Acceptance: every successful call leaves a `committed` reservation row matching actual cost; every failed call leaves a `refunded` row; `BudgetDenied` propagates without being swallowed; the advisory `_check_budget` block is gone.

---

## Phase 4 — FRE-306: BudgetDenied semantics — 503 mapping, NACK, PWA error card

**Exception** (`src/personal_agent/exceptions.py`, append):

```python
@dataclass
class BudgetDenied(Exception):
    role: str
    time_window: str           # 'daily' | 'weekly'
    current_spend: Decimal
    cap: Decimal
    window_resets_at: datetime # UTC
    denial_reason: str         # 'cap_exceeded' | 'policy_violation' | …

    def __str__(self) -> str:
        return (f"Budget denied for role={self.role} ({self.time_window}): "
                f"${self.current_spend:.4f} >= ${self.cap:.4f}")
```

**FastAPI mapping** (`src/personal_agent/gateway/chat_api.py`, around lines 199–211):

Add a `try: … except BudgetDenied as e:` arm that returns 503 with body:
```json
{
  "error": "budget_denied",
  "role": "main_inference",
  "time_window": "weekly",
  "cap": 18.00,
  "spend": 18.04,
  "reset_time": "2026-05-05T00:00:00Z",
  "denial_reason": "cap_exceeded"
}
```
Mirror the existing `gateway_error()` shape from `src/personal_agent/gateway/errors.py` so the wire format stays consistent. `BudgetDenied` for `role != 'main_inference'` should never bubble up to the chat router (background paths catch it) — defensive 500 with `error="unexpected_budget_denial_in_chat"` if it does.

**Background consumer NACK** — Redis Streams redelivery is implicit (no explicit NACK; you simply don't ACK and let `AGENT_EVENT_BUS_ACK_TIMEOUT_SECONDS=300` timeout the message back into the consumer group). Update each consumer handler:

- `src/personal_agent/events/pipeline_handlers.py` — `cg:insights`, `cg:captain-log`, `cg:promotion` builders
- `src/personal_agent/events/consumers/freshness_consumer.py` — `cg:freshness`

For each handler, wrap the LLM-using call in `try: … except BudgetDenied as e:`. Log structured `consumer_budget_denied` with `trace_id`, `role`, `denial_reason`. **Re-raise** so the consumer runner's existing exception path sees it and the message is left un-ACKed for redelivery. The runner's max-retries → dead-letter behaviour (consumer.py:158–199) takes over after `event_bus_max_retries=3`.

**PWA error card** (`seshat-pwa/src/components/`):

The PWA frontend lives in the same monorepo. Two changes:

1. `seshat-pwa/src/hooks/useSSEStream.ts` (or the equivalent fetch wrapper): detect 503 with `error === "budget_denied"` body, expose as a typed error state instead of opening the SSE stream.
2. `seshat-pwa/src/components/StreamingChat.tsx` (around line 79 where messages render): conditionally render a new `<BudgetDeniedCard />` component showing `cap`, `spend`, `time_window`, `reset_time`, and a "raise cap" link to wherever the backend admin lives. The empty-turn rendering path the regression came from must no longer be reachable for this error class.

If working on the PWA in this same session is infeasible (different toolchain, different test runner), the backend changes are still independently shippable: a hand-rolled `curl` test against `/chat` confirms the 503 body, and we file the PWA change as a follow-up.

**Tests:**

- `tests/personal_agent/gateway/test_chat_api_budget_denied.py` — drive the chat handler with a mocked `cost_gate.reserve` raising `BudgetDenied`; assert 503 + body shape (per FRE-306 acceptance)
- `tests/personal_agent/events/test_consumer_nack_on_denial.py` — drive each of the four consumers with a handler that raises `BudgetDenied`; assert ACK is **not** called (per FRE-306 acceptance)
- Manual: pre-fill the weekly counter to over-cap, send chat request, observe PWA error card render (per FRE-306 acceptance)

**Verification:**
```bash
make test-file FILE=tests/personal_agent/gateway/test_chat_api_budget_denied.py
make test-file FILE=tests/personal_agent/events/test_consumer_nack_on_denial.py
# Manual: PWA over-cap rendering
docker compose exec postgres psql -U agent -d personal_agent -c \
  "UPDATE budget_counters SET running_total = 99 WHERE role='_total' AND time_window='weekly';"
curl -X POST http://localhost:9000/chat -d '{"message": "hi"}' -H "Content-Type: application/json" | jq
# expect 503 + budget_denied body, not 200 + empty turn
```

Acceptance: PWA renders the error card (not an empty turn) when over-cap; background consumers NACK and the message redelivers after the budget window rolls.

---

## Phase 5 — FRE-307: Retry telemetry + Extraction Retry Health Kibana panel

**Logging fixes:**

- `src/personal_agent/second_brain/entity_extraction.py:328` — `log.error("entity_extraction_failed", error=str(e), exc_info=True)` → add `trace_id=ctx.trace_id` and `attempt_number`, `denial_reason` (None when not budget-related)
- `src/personal_agent/second_brain/consolidator.py:383–386` — `consolidation_extraction_fallback_skip` already has `trace_id`; add `attempt_number`, `previous_failure_count`, `time_since_first_attempt_seconds`, structured `denial_reason` enum (replacing the freeform `reason` string)

**`consolidation_attempts` row writes:** Per attempt across the four event-driven consumers and the consolidator's local retry path. Write happens at the end of each attempt regardless of outcome:

```python
async with AsyncSessionLocal() as session:
    session.add(ConsolidationAttempt(
        trace_id=ctx.trace_id,
        attempt_number=attempt,
        role=role,                       # 'entity_extraction' | 'promotion' | …
        started_at=started,
        completed_at=datetime.now(timezone.utc),
        outcome=outcome,                 # 'success' | 'budget_denied' | 'transient_failure' | 'extraction_returned_fallback' | 'dead_letter'
        denial_reason=denial_reason,
    ))
    await session.commit()
```

**`denial_reason` enum** — codify in `src/personal_agent/cost_gate/types.py`:

```python
class DenialReason(str, Enum):
    CAP_EXCEEDED = "cap_exceeded"
    POLICY_VIOLATION = "policy_violation"
    RESERVATION_FAILED = "reservation_failed"
    PROVIDER_ERROR = "provider_error"
```

Used by both gate logs and the existing budget/governance logs (replacing freeform `reason` strings).

**Kibana panel** — new file `config/kibana/dashboards/extraction_retry_health.ndjson`. Three visualizations on a single dashboard, joined to the existing Slice 2 dashboard:

- Median attempts to success per role (line chart over time)
- Dead-letter rate per role (bar chart, last 24h)
- Top `denial_reason` breakdown (donut chart)

Document the dashboard in `docs/guides/KIBANA_DASHBOARDS.md` per the existing format (mirror the entries already in that file). Import command (already documented):
```bash
curl -X POST "http://localhost:5601/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" --form file=@config/kibana/dashboards/extraction_retry_health.ndjson
```

**Tests:**

- `tests/personal_agent/second_brain/test_extraction_logging.py` — assert `entity_extraction_failed` log payload contains `trace_id`, `attempt_number`, `denial_reason`
- `tests/personal_agent/second_brain/test_consolidation_attempts.py` — drive the four consumers' attempt paths, assert one row per attempt with the right outcome enum
- Manual: import the Kibana dashboard, confirm panels render against current data

**Verification:**
```bash
make test-file FILE=tests/personal_agent/second_brain/test_extraction_logging.py
make test-file FILE=tests/personal_agent/second_brain/test_consolidation_attempts.py
# Manual Kibana check
curl -X POST "http://localhost:5601/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" --form file=@config/kibana/dashboards/extraction_retry_health.ndjson
open http://localhost:5601/app/dashboards
```

Acceptance: `entity_extraction_failed` joinable to chat requests via `trace_id`; `consolidation_attempts` rows present for every consumer attempt; Kibana panel renders.

---

## Final ADR + ticket bookkeeping

When all five phases land:

- `docs/architecture_decisions/ADR-0065-cost-check-gate.md` status `Proposed` → `Accepted`; populate the **Status Tracking** table at the bottom with the actual Linear IDs (FRE-303–307)
- Mark each Linear sub-issue completed via `save_issue` as its phase merges
- Final commit on the worktree branch is the ADR status flip + status table fill

---

## Out of scope (explicit non-goals from the ADR)

- Per-session cost limits — `cost_limit_per_session` in `profiles/cloud.yaml` remains a soft hint, not a gate
- Automatic provider failover (Sonnet → Haiku → local) — silent fallback defeats the gate's observability purpose
- Local-model gating — `LocalLLMClient` has no cost; not gated
- The `_total` synthetic role is not user-configurable; always equals the sum of per-role caps for the same window

---

## Critical files (touched per phase)

| Phase | Files |
|-------|-------|
| 1 (FRE-303) | `docker/postgres/init.sql`, `docker/postgres/migrations/0001_cost_gate_schema.sql` (new), `src/personal_agent/service/models.py` |
| 2 (FRE-304) | `src/personal_agent/cost_gate/{__init__,gate,policy,reaper,types}.py` (new), `config/governance/budget.yaml` (new), `src/personal_agent/config/settings.py` (add path resolution) |
| 3 (FRE-305) | `src/personal_agent/llm_client/cost_estimator.py` (new), `src/personal_agent/llm_client/litellm_client.py` (replace lines 169–186 + wrap acompletion) |
| 4 (FRE-306) | `src/personal_agent/exceptions.py`, `src/personal_agent/gateway/chat_api.py`, `src/personal_agent/events/pipeline_handlers.py`, `src/personal_agent/events/consumers/freshness_consumer.py`, `seshat-pwa/src/hooks/useSSEStream.ts`, `seshat-pwa/src/components/StreamingChat.tsx`, `seshat-pwa/src/components/BudgetDeniedCard.tsx` (new) |
| 5 (FRE-307) | `src/personal_agent/second_brain/entity_extraction.py`, `src/personal_agent/second_brain/consolidator.py`, `src/personal_agent/cost_gate/types.py` (DenialReason enum), `config/kibana/dashboards/extraction_retry_health.ndjson` (new), `docs/guides/KIBANA_DASHBOARDS.md` |

## Key reused utilities

- `cost_tracker.py` raw-asyncpg pool pattern — reused for the gate's hot-path
- `governance_loader.py` YAML pattern — reused for `budget.yaml`
- `events/consumer.py` ACK/retry/dead-letter loop — reused as-is; we only change handler-level exception handling
- `gateway/errors.py` 503 envelope shape — reused for `BudgetDenied` responses
- `litellm.token_counter()` and `litellm.model_cost[…]` — reused for pre-call estimation
- `service/models.py` declarative_base + DateTime(tz=True) + PG_UUID conventions — reused for ORM models

## End-to-end verification (after all phases)

```bash
# 1. Concurrent overshoot impossible (FRE-302 acceptance)
make test-file FILE=tests/personal_agent/cost_gate/test_concurrent.py

# 2. Single-call overshoot bounded by safety_factor (FRE-302 acceptance)
make test-file FILE=tests/personal_agent/cost_gate/test_overshoot.py

# 3. User-facing 503 — never empty turn (FRE-302 acceptance)
docker compose exec postgres psql -U agent -d personal_agent -c \
  "UPDATE budget_counters SET running_total = 99 WHERE role='_total' AND time_window='weekly';"
curl -X POST http://localhost:9000/chat -d '{"message": "hi"}' -H "Content-Type: application/json" -i
# expect: HTTP/1.1 503; body has error="budget_denied"

# 4. Background NACK + redeliver (FRE-302 acceptance)
# pre-fill counter, publish a captain-log event, observe XPENDING shows it un-ACKed
docker compose exec redis redis-cli XPENDING stream:promotion.issue_created cg:promotion

# 5. Reaper sweeps stale reservations (FRE-302 acceptance)
make test-file FILE=tests/personal_agent/cost_gate/test_reaper.py

# 6. Kibana panel (FRE-302 acceptance)
open http://localhost:5601/app/dashboards/extraction-retry-health

# 7. Full suite green
make test
make mypy
make ruff-check
```

When all seven pass, mark each Linear issue Completed and flip ADR-0065 to Accepted.
