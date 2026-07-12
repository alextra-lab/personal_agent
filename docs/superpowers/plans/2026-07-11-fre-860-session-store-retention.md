# FRE-860 — Session-store retention (ADR-0098 D4/D6)

**Status:** owner-approved retention window (180 days) + prune mechanism (soft-prune) via AskUserQuestion, 2026-07-11.

## Scope

The Postgres `sessions` table (1225 rows) has no retention, TTL, or expiry — nothing ages
out session history. This is distinct from ADR-0098 D4/D6's Neo4j `:Turn` transcript-offload
concern (which is a separate, larger piece of work); this ticket is the analogous "session
history shouldn't accumulate forever" gap in the service-layer Postgres `sessions` table.

**Owner-confirmed decisions (no ADR-given number existed for this table):**
- Retention window: **180 days** (matches the existing `captains_log_reflections` cold_duration
  anchor in `telemetry/lifecycle.py`).
- Mechanism: **soft-prune** — add `sessions.purged_at TIMESTAMPTZ NULL`; the job clears
  `messages` to `[]` and stamps `purged_at` in place. A hard `DELETE` was rejected because
  `artifacts.session_id` and `session_events.session_id` both FK to `sessions(session_id)`
  with `NO ACTION` on delete — a hard delete would throw whenever a session has an artifact
  or WS event attached, or would require adding `ON DELETE CASCADE` (a bigger behavior change
  than this ticket's scope).

"Pruned" = `purged_at` is not null and `messages = '[]'`. "Retained" = `purged_at IS NULL` and
`messages` intact.

**Reactivation semantics (added after codex plan-review):** codex correctly flagged that the
plan as first drafted didn't say what happens when a pruned session is resumed. Both
`SessionRepository.append_message` and `SessionRepository.update` (the two places that write
`messages`) will now also set `purged_at = NULL` whenever they write `messages` — so appending
to (or explicitly updating) a pruned session clears the tombstone and the row is "active"
again (old content is genuinely gone — that's the point of pruning — but the row is no longer
flagged as a purged husk). A test proves this: append a message to a purged session → asserts
`purged_at` clears and the new message is present.

**Known accepted race (documented, not solved):** there is a narrow theoretical window where
a chat request reads a session's full `messages` into memory *just before* the daily prune
sweep clears that same row, then writes back the stale in-memory list (now resurrecting the
"pruned" content) after the sweep committed. Closing this fully would require optimistic
concurrency control (a version column / conditional UPDATE) on `append_message` — the busiest
write path in the service layer — which no other scheduled sweep in this codebase does either
(the WS event-buffer cleanup, the upload-expiry sweep, and the cost-gate reservation reaper all
have the same class of race with no CAS guard). Given the window requires a session to be both
180+ days dormant *and* actively resumed in the same instant the daily sweep scans that exact
row, this is accepted as a known, narrow, pre-existing-pattern-consistent risk rather than
scope-creeping CAS locking onto the hot chat path for this ticket. Called out explicitly in the
PR/ticket handoff so master reads it as a documented tradeoff, not a missed gap.

## Files

1. **`docker/postgres/init.sql`** — add `purged_at TIMESTAMPTZ NULL` to the `sessions` table
   block; add `CREATE INDEX IF NOT EXISTS idx_sessions_retention_scan ON sessions(last_active_at) WHERE purged_at IS NULL;`
   so the sweep only scans not-yet-purged rows.

2. **`docker/postgres/migrations/0019_sessions_purged_at.sql`** — mirror of the above for
   existing DBs (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`).
   Applied via `AGENT_DATABASE_ADMIN_URL` per FRE-808.

3. **`src/personal_agent/config/settings.py`** — two new fields near `data_lifecycle_enabled`:
   - `session_retention_days: int = Field(default=180, ge=1, ...)`
   - `session_retention_sweep_interval_seconds: int = Field(default=86400, ge=60, ...)` (daily)

4. **`src/personal_agent/service/models.py`** — add `purged_at = Column(DateTime(timezone=True), nullable=True)`
   to `SessionModel`.

5. **`src/personal_agent/service/repositories/session_repository.py`** — add
   `async def prune_expired(self, retention_days: int) -> int`: bulk `UPDATE sessions SET
   messages = '[]'::jsonb, purged_at = NOW() WHERE purged_at IS NULL AND last_active_at <
   NOW() - make_interval(days => :days)`, returns rowcount. Mirrors the existing
   `clear_pending_confirmation` raw-SQL style already in this file. Additionally: update
   `append_message`'s and `update`'s UPDATE statements to also set `purged_at = NULL`
   whenever they write `messages` (the reactivation-semantics fix above).

6. **`src/personal_agent/service/session_retention.py`** (new) — two functions, mirroring
   `cost_gate/reaper.py`'s `run_reaper()` shape (the closest existing precedent for a
   standalone, importable, testable sweep-loop — as opposed to the three ad-hoc closures
   inline in `app.py`'s lifespan, none of which are independently tested):
   - `async def prune_expired_sessions(db_factory: Any, retention_days: int | None = None) -> int`
     — opens `async with db_factory() as db:`, delegates to `SessionRepository.prune_expired`,
     logs `session_retention.pruned` (count) at INFO when count > 0.
   - `async def run_session_retention_loop(db_factory: Any, *, interval_seconds: float, retention_days: int | None = None) -> None`
     — `while True: sleep(interval_seconds); try: await prune_expired_sessions(...); except
     Exception: log.exception(...)`. Matches `run_reaper`'s cancellation contract (suppresses
     `CancelledError`, re-raises so the lifespan sees clean cancellation).

7. **`src/personal_agent/service/app.py`** — replace the closure-loop pattern with:
   `session_retention_task = asyncio.create_task(run_session_retention_loop(AsyncSessionLocal,
   interval_seconds=settings.session_retention_sweep_interval_seconds))`, alongside the
   existing `ws_cleanup_task` / `dedup_cleanup_task` / `upload_expiry_task` creation. Cancelled
   at shutdown alongside `upload_expiry_task`.

## Tests

1. **`tests/migrations/test_0019_sessions_purged_at_migration.py`** — ephemeral-schema
   integration test mirroring `tests/migrations/test_0011_sessions_user_id_migration.py`:
   seed pre-0019 `sessions`, apply migration, assert `purged_at` column exists (nullable),
   the partial index exists, and the migration is idempotent on re-apply. Skips cleanly if
   the test-stack Postgres (`make test-infra-up`) is unreachable.

2. **`tests/personal_agent/service/repositories/test_session_repository_retention.py`** —
   real-DB test against the test-stack Postgres (`AsyncSessionLocal`, same style as
   `tests/integration/test_notes_search_db.py`; skips cleanly if port 5433 unreachable):
   - insert session A: `last_active_at = now() - 200 days`, non-empty `messages`
   - insert session B: `last_active_at = now() - 10 days`, non-empty `messages`
   - call `SessionRepository(db).prune_expired(retention_days=180)`
   - assert A: `purged_at is not None` and `messages == []` (**pruned**)
   - assert B: `purged_at is None` and `messages` unchanged (**retained**)
   - assert the returned count is exactly 1
   - re-run `prune_expired` and assert it returns 0 (idempotent — already-purged rows excluded)
   - **reactivation test** (closes the codex-flagged gap): prune session A, then call
     `append_message` on session A with a new message; assert `purged_at` is cleared to
     `NULL` and `messages == [<the new message>]` — proves resuming a pruned session is
     well-defined, not a silent inconsistency between `purged_at` and `messages`.

3. **`tests/personal_agent/service/test_session_retention.py`** — pure-unit test of
   `prune_expired_sessions(db_factory, retention_days)` wiring using a stub session/db_factory
   (same style as `tests/personal_agent/service/test_uploads_router.py`'s `_StubSession`) —
   no real Postgres, no marker, always runs under `make test`. `run_session_retention_loop`
   itself (the `while True: sleep; call; except: log` wrapper) is intentionally left without
   a dedicated test, matching this codebase's existing convention: `cost_gate/reaper.py`'s
   `run_reaper()` has no test either — only the underlying sweep (`reap_stale`, tested in
   `tests/personal_agent/cost_gate/test_reaper.py`) gets real-DB coverage. The loop wrapper
   has no interesting logic of its own.

## Acceptance criteria (from the ticket)

| AC | Proof |
|----|-------|
| A retention window is enforced by a scheduled job | `app.py` `_session_retention_loop` + `settings.session_retention_days`/`session_retention_sweep_interval_seconds` |
| A session older than the window is pruned | `test_session_repository_retention.py` session-A assertion |
| A session inside the window is retained | `test_session_repository_retention.py` session-B assertion |
| Tests prove both | Both assertions live in the same test, same run |

## Non-goals

- Not touching Neo4j `:Turn` retention / R2 transcript offload (ADR-0098 D6's literal
  conversation-transcript concern) — that is separate, larger work already implied by the
  Memory Recall Quality project, not this ticket.
- Not adding `sessions` to the file-based `telemetry/lifecycle.py` `RETENTION_POLICIES` /
  `DataLifecycleManager` — that abstraction models hot/warm/cold *file* archival with
  gzip-and-move semantics; a Postgres soft-prune doesn't fit it, and forcing the fit would
  be needless complexity for a single UPDATE query. The closest fit and closest precedent is
  the existing `expire_pending_uploads`-style standalone asyncio loop in `app.py`'s lifespan.
