# FRE-693 — Joinability: thread trace/session/task onto image byte-fetch, cost, and resolution events

**Backing:** ADR-0101 §8c (shared control spine, image case), ADR-0074 (end-to-end traceability).
**Blocked-by (both merged):** FRE-666 (`attachment_resolution.py` resolver), FRE-691 (cloud image cost / ADR-0065 gate use in the vision path).
**AC owned:** AC-12 — "An image turn's cost row and resolution events carry `trace_id` + `session_id` +
`task_id` and join back to the turn ... zero orphans" (verified by the ADR-0074 joinability probe).

## Scope decision (read before implementing)

`task_id` already has an established meaning in this codebase: **the sub-agent's own task identifier**
(ADR-0088 route-trace spine — `route_traces.task_id`, `NULL` for turn-level rows, set only for a
sub-agent segment row). `TraceContext` and `ExecutionContext` do **not** carry a `task_id` field, and
`resolve_attachments()` is only ever called at turn-assembly (`step_init`), never from inside a
sub-agent. So for every call site this ticket touches, **`task_id` is threaded as an explicit parameter
that is `None` at the turn level** — this is not a gap, it is the same convention `route_traces` already
uses (`task_id IS NULL` = turn-level row, by design). Do not invent a synthetic non-null task_id and do
not add a `task_id` field to `TraceContext`/`ExecutionContext` — that would be scope creep past what the
ticket or ADR asks for. If a future ticket resolves attachments inside a sub-agent, that call site can
pass a real `task_id` — the parameter is already there to accept it.

The existing generic `_walk_es_agent_logs` joinability check already flags *any* ES log document under
a session that lacks `trace_id` — so any new `log.*` call added below that carries `trace_id`/`session_id`
is automatically covered by the probe with no new ES-side walk code. Only the Postgres
`budget_reservations` walk needs a real code change, because that table currently has no `session_id`/
`task_id` columns and its walk never flags an orphan at all (it only counts rows).

## Files touched

1. `docker/postgres/migrations/0013_budget_reservations_identity.sql` (new) — adds the columns **and**
   backfills `session_id` on pre-existing rows from `api_costs` (see Step 1a — codex review flagged that
   without a backfill, historical reservations would read as false-positive orphans under Step 7's new
   check)
2. `docker/postgres/init.sql` — same columns on the fresh-install `CREATE TABLE budget_reservations`
3. `src/personal_agent/service/models.py` — `BudgetReservationModel` gains `session_id`/`task_id`
   columns. **Required**, not optional: `tests/migrations/test_init_sql_model_parity.py` asserts every
   model column has an `init.sql` counterpart — missed by the original plan draft, caught in codex review.
4. `src/personal_agent/cost_gate/gate.py` — `reserve()` gains `session_id`/`task_id`; `commit()` gains
   `session_id` (log-only, no new column — the row already carries session_id from `reserve()`)
5. `src/personal_agent/llm_client/litellm_client.py` — thread `trace_ctx.session_id` into the
   `gate.reserve()` / `gate.commit()` calls
6. `src/personal_agent/storage/artifact_store.py` — `get()` gains `session_id`/`task_id` kwargs, threaded
   into the existing failure log
7. `src/personal_agent/orchestrator/attachment_resolution.py` — `resolve_attachments()` gains
   `session_id`/`task_id` params; new resolution-telemetry log calls (none exist today) before **all
   three** `AttachmentUnsupportedError` raises (unsupported content type, store unconfigured, oversized
   after downscale — codex review caught that the original draft only counted two)
8. `src/personal_agent/orchestrator/executor.py` — thread `session_id`/`task_id=None` at the
   `resolve_attachments()` call site; new `vision_routing_decision` log at the authoritative routing
   call site (`step_llm_call`, line ~2730), gated on a **raster image** attachment being present (not
   just any attachment — codex review caught that gating on `ctx.attachments` alone would fire a
   misleading "vision routing" log on document-only/PDF-only turns)
9. `src/personal_agent/observability/joinability/walk.py` — `_walk_budget_reservations` gains real
   orphan detection: signature grows `session_id`/`orphans` params (wired from `run()`), and flags a row
   as orphaned when `session_id IS NULL` **or** `session_id != sampled_session_id` (codex review: a
   same-trace-different-session mismatch would slip past a NULL-only check)

Not touched (confirmed out of scope): `tool_result_expand.py`, `notes_tools.py`, `artifact_tools.py`,
`artifacts_router.py` — these call `store.get()` for unrelated paths (tool-result expansion, notes,
artifact download), not turn-assembly attachment resolution. `CostGate.refund()` — not named in the
ticket text and not on the AC-12 happy path (a refund means the call errored before any cost was
committed). `scripts/identity_threading_allowlist.yaml` — confirmed no existing entries in any file this
plan touches, so no line-pin bump needed (grepped before writing this plan).

## Steps

### 1. Migration — `budget_reservations` gains `session_id`, `task_id` (+ backfill)

`docker/postgres/migrations/0013_budget_reservations_identity.sql` (idempotent, follows the
`0011_sessions_user_id.sql` pattern):

```sql
BEGIN;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS session_id UUID;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS task_id UUID;
CREATE INDEX IF NOT EXISTS idx_budget_reservations_session ON budget_reservations(session_id);

-- Backfill pre-migration rows from api_costs (ADR-0074 I4: api_costs.session_id is NOT NULL
-- and already keyed by the same trace_id). Every trace_id maps to exactly one session_id, so
-- DISTINCT ON is a determinism safeguard, not a correctness requirement, in case a trace
-- produced more than one api_costs row (tool-loop iterations). Without this backfill, Step 7's
-- new orphan check would red-flag every pre-cutoff reservation the probe happens to sample —
-- a false positive, not a real identity gap (caught in codex plan review).
UPDATE budget_reservations br
   SET session_id = ac.session_id
  FROM (SELECT DISTINCT ON (trace_id) trace_id, session_id FROM api_costs) ac
 WHERE br.trace_id = ac.trace_id
   AND br.session_id IS NULL;
COMMIT;
```

Mirror the same two columns + index onto the `CREATE TABLE budget_reservations` block in
`docker/postgres/init.sql` (fresh installs skip the migrations dir entirely — no backfill needed there,
the table starts empty).

**Verify:** `psql $AGENT_DATABASE_URL -f docker/postgres/migrations/0013_budget_reservations_identity.sql`
against local test infra (`make test-infra-up`) exits 0, re-running it is a no-op.

### 1a. `BudgetReservationModel` — mirror the columns (required by the parity guard)

`src/personal_agent/service/models.py`, `BudgetReservationModel` (~line 310): add
`session_id = Column(PG_UUID(as_uuid=True), nullable=True)` and
`task_id = Column(PG_UUID(as_uuid=True), nullable=True)`. `tests/migrations/test_init_sql_model_parity.py`
asserts every model column has an `init.sql` counterpart — skipping this step fails that test the moment
`make test-infra-up` is running (missed in the original draft, caught in codex plan review).

### 2. `CostGate.reserve()` / `commit()` — accept and store identity

In `src/personal_agent/cost_gate/gate.py`:

- `reserve(self, role, amount, *, trace_id=None, user_id=None, provider=None, session_id: UUID | None = None, task_id: UUID | None = None)` —
  add both columns to the `INSERT INTO budget_reservations (...)` (both the capped-path INSERT at
  ~line 205 and `_insert_uncapped_reservation`'s INSERT), and to the `cost_gate_reserved` log kwargs.
- `commit(self, reservation_id, actual_cost, *, trace_id=None, session_id: UUID | str | None = None)` —
  no new column (the row already has `session_id` from `reserve()`); just add `session_id` to the
  `cost_gate_committed` log kwargs, matching the existing `trace_id` "observability hook" docstring
  language.

### 3. `litellm_client.py` — thread `trace_ctx.session_id` through

At the `gate.reserve(...)` call (~line 466): add
`session_id=UUID(trace_ctx.session_id) if trace_ctx.session_id else None, task_id=None` with a short
comment: turn-level call, no sub-agent task_id reaches this layer (matches `route_traces` convention).

At the `gate.commit(...)` call (~line 591): add `session_id=trace_ctx.session_id`.

### 4. `artifact_store.py` — `get()` accepts `session_id`/`task_id`

`async def get(self, r2_key: str, *, trace_id: str | None = None, session_id: str | None = None, task_id: str | None = None) -> bytes`
— thread both into the existing `artifact_store_get_failed` log call. Update the docstring's Args block.

### 5. `attachment_resolution.py` — resolution telemetry (new)

- Add `import structlog` + `log = structlog.get_logger(__name__)`.
- `resolve_attachments(attachments, *, trace_id=None, session_id: str | None = None, task_id: str | None = None)`.
- Thread `session_id=session_id, task_id=task_id` into the `store.get(...)` call.
- Before **all three** `AttachmentUnsupportedError` raises — unsupported content type (line ~128), store
  not configured (line ~149), oversized after downscale (line ~165; codex review caught the original
  draft only counted two of the three) — add
  `log.warning("attachment_resolution_failed", trace_id=trace_id, session_id=session_id, task_id=task_id, artifact_id=<attachment.artifact_id if in an attachment loop, else None for the store-unconfigured raise which fires before the loop>, content_type=<attachment.content_type where available>, reason=<short literal>)`.
- Before each of the two `return ResolvedAttachments(...)` statements (the early no-raster return and the
  final return), add `log.info("attachment_resolution_completed", trace_id=trace_id, session_id=session_id, task_id=task_id, attachment_count=len(attachments), resolved_count=len(blocks_or_empty), disclosure_count=len(disclosures))`.
- Update the function docstring's Args to describe `session_id`/`task_id`.

### 6. `executor.py` — thread the call site + add routing telemetry

- Line ~1973: `resolve_attachments(ctx.attachments, trace_id=ctx.trace_id, session_id=ctx.session_id, task_id=None)`.
- Line ~2730 (`step_llm_call`, right after `effective_model_key = _resolve_vision_routing_key(...)`
  succeeds): when `ctx.attachments` contains a raster image (`content_type in RASTER_CONTENT_TYPES`, the
  same predicate `_resolve_vision_routing_key` itself uses to decide whether to no-op — import from
  `attachment_resolution`), emit
  `log.info("vision_routing_decision", trace_id=ctx.trace_id, session_id=ctx.session_id, task_id=None, model_role=model_role.value, role_key=role_key, effective_model_key=effective_model_key, escalated=effective_model_key != role_key)`.
  Gating on raster content specifically (not `ctx.attachments` truthy) avoids a misleading "vision
  routing" log on document-only/PDF-only turns where no vision routing decision actually happened
  (codex review).
- Do **not** add new logging for the `_resolve_vision_routing_key` failure path — it's already caught by
  the existing `except Exception as e:` at line 3268 which logs `MODEL_CALL_ERROR` with
  `trace_id`+`session_id`+`span_id`+`error`+`error_type`. Confirmed by reading the handler before writing
  this plan; do not duplicate it.
- Do **not** touch the `ORCHESTRATOR_FATAL_ERROR` handler (line ~1890, missing `session_id`) — pre-existing
  gap, not attachment-specific, out of scope for this ticket. Flag as a follow-up ticket in Step 5 of the
  build skill if still true after implementation.

### 7. Joinability probe — real orphan detection for `budget_reservations`

In `src/personal_agent/observability/joinability/walk.py`:

- `_walk_budget_reservations` signature grows two params: `session_id: str` (the walk's sampled anchor
  session — already available in `run()`, just not currently threaded to this method) and
  `orphans: list[Orphan]` (every other Postgres/ES walk already takes this; `_walk_budget_reservations`
  and `_walk_artifacts` are currently the only two that don't — codex review caught that the original
  draft said "append an Orphan" without updating the signature to accept the list). Update the `run()`
  call at line ~160 to `await self._walk_budget_reservations(session_id, trace_ids, checks, orphans)`.
- Change the `SELECT` to `SELECT reservation_id, trace_id, session_id, task_id FROM budget_reservations WHERE trace_id = ANY($1::uuid[])`
  (the original draft selected `session_id, task_id` but dropped `trace_id`, which the orphan `detail`
  needs to cite — codex review).
- For each row where `session_id IS NULL` **or** `session_id != _to_uuid(session_id)` (a row whose
  trace_id belongs to this session but whose own `session_id` column disagrees — a real mismatch, not
  just a missing value; codex review: a NULL-only check would miss this case), append
  `Orphan(substrate="postgres.budget_reservations", kind="missing_identity", detail={"reservation_id": str(row["reservation_id"]), "trace_id": str(row["trace_id"])}, severity="red")`
  and mark the check `status="red"`.
- Do **not** flag `task_id IS NULL` as an orphan — that is the expected, correct state for every
  turn-level reservation (mirrors `route_traces`' own convention). Only check `session_id`.
- `checks.append(...)` keeps `expected="conditional"` (unchanged) but `status` now reflects the orphan
  scan instead of being hardcoded `"green"`.

### 8. Tests (TDD — write failing first)

- `tests/personal_agent/cost_gate/test_gate.py`: `test_reserve_stores_session_and_task_id` — call
  `gate.reserve(..., session_id=..., task_id=...)`, fetch the row directly via `db_pool`, assert both
  columns match. `test_commit_logs_session_id` (or extend `test_gate_emit_types.py`) — assert the
  `cost_gate_committed` log event carries `session_id` when passed.
- `tests/personal_agent/orchestrator/test_attachment_resolution.py`: extend the existing
  fake-store-based tests to assert `store.get` receives `session_id=`/`task_id=` when
  `resolve_attachments` is called with them; assert `attachment_resolution_completed` fires (use
  `structlog.testing.capture_logs()` — check the existing test file for the current log-capture idiom
  before adding a new one); assert `attachment_resolution_failed` fires immediately before **each of the
  three** `AttachmentUnsupportedError` raises, with the right identity kwargs.
- `tests/migrations/test_init_sql_model_parity.py`: no new test needed — the existing
  `test_every_model_column_exists_in_init_sql` is the guard; just confirm it passes after Steps 1/1a/2
  land (`make test-infra-up` first).
- `tests/observability/test_joinability_walk_unit.py`: extend `FakePgConn`'s `budget_reservations`
  fixture data to include `session_id`/`task_id` columns; add
  `test_budget_reservations_orphan_when_session_id_null` (asserts an `Orphan` is produced, status red),
  `test_budget_reservations_orphan_when_session_id_mismatch` (row's `session_id` is a different UUID than
  the sampled session — also red), and `test_budget_reservations_no_orphan_when_session_id_matches`
  (green, no orphan). Confirm `task_id` NULL alone does not produce an orphan.
- `tests/integration/test_joinability_walk.py` (real Postgres via `make test-infra-up`): extend the
  existing round-trip to insert a `budget_reservations` row with `session_id` set and assert the walk
  reports zero orphans for that substrate.
- Migration backfill: a quick manual/scripted check (not necessarily a formal pytest) that
  `0013_budget_reservations_identity.sql`'s backfill UPDATE correctly populates `session_id` for a
  pre-migration row from a matching `api_costs` row on test infra — run the migration against
  `make test-infra-up`, seed one `api_costs` row + one pre-existing NULL-`session_id` `budget_reservations`
  row sharing a `trace_id`, re-run the migration, assert `session_id` is now populated.

### 9. Quality gates

`make test-file FILE=tests/personal_agent/cost_gate/test_gate.py` ·
`make test-file FILE=tests/personal_agent/orchestrator/test_attachment_resolution.py` ·
`make test-file FILE=tests/observability/test_joinability_walk_unit.py` · `make test` (full) ·
`AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE=0 make test-infra-up && uv run pytest tests/integration/test_joinability_walk.py` ·
`make mypy` · `make ruff-check` · `make ruff-format` · `uv run python scripts/check_identity_threaded.py src/personal_agent/` ·
`pre-commit run --all-files`.

## Risk classification

**Standard/Complex** — touches `src/` logic across 6 modules plus a Postgres schema migration
(`budget_reservations`). Codex plan-review required before implementation per the build skill's risk
tiering.
