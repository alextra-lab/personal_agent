# FRE-591 — Fix `sessions.user_id` schema divergence (fresh-env provisioning)

- **Ticket:** FRE-591 (Approved, High, Tier-2:Sonnet) — project *VPS/Cloud Architecture Stabilization*
- **Source review:** `docs/superpowers/plans/the-following-information-comes-logical-pie.md` §2 (DB-1a)
- **Convention:** No Alembic — schema changes go in `docker/postgres/init.sql` + `docker/postgres/migrations/`

## Live state (verified read-only, 2026-06-26)

`docker exec cloud-sim-postgres psql -U agent -d personal_agent -c '\d sessions'` shows prod **already has**
`user_id uuid NOT NULL`, index `ix_sessions_user_id`, and FK `sessions_user_id_fkey → users(user_id)`.
Counts: **11 users**, **1197 sessions, all with user_id populated**.

Diagnosis: this is the **drift** branch from the ticket. Prod's `sessions` table was created historically by
SQLAlchemy `create_all` (hence the `ix_` index name, not init.sql's `idx_` convention). A fresh volume runs
`init.sql` (which lacks `user_id`), `create_all` then skips the existing table → first session INSERT fails.
Fix = make init.sql match reality + ship an idempotent migration that is a **no-op on prod**.

## Constraints that shape the fix

1. **Table ordering** — `sessions` is created at init.sql:8, `users` at init.sql:319. The FK cannot be inline in
   the `sessions` block (referenced table doesn't exist yet). It must be a post-`users` `ALTER TABLE ADD CONSTRAINT`.
2. **Index name** — use `ix_sessions_user_id` (the name SQLAlchemy already created on prod) so `CREATE INDEX IF
   NOT EXISTS` is a true no-op on prod, not a duplicate index.
3. **No safe single-user backfill** — 11 users on prod; the ticket's "single known user" assumption is wrong.
   No real DB has orphan rows (prod is fully populated; test-infra builds fresh from init.sql). The migration
   enforces `NOT NULL` only when zero orphan rows exist; otherwise it leaves the column nullable and raises a
   `NOTICE` for deliberate operator backfill — never auto-attributes.

## Steps

### 1 — Failing test first (TDD)
File: `tests/migrations/test_0011_sessions_user_id_migration.py` (mirrors `test_0004_identity_migration.py` —
runs in an ephemeral schema on the test-stack Postgres, `pytest.skip` if it's down).

Three cases:
- `test_migration_adds_user_id_notnull_fk_on_empty_sessions`: seed pre-0011 `users` + `sessions` (no `user_id`,
  no rows) → apply migration → assert `user_id` present & `is_nullable='NO'`, `ix_sessions_user_id` exists,
  `sessions_user_id_fkey` exists; INSERT without user_id → `NotNullViolationError`; INSERT with unknown user_id
  → `ForeignKeyViolationError`.
- `test_migration_is_idempotent`: apply twice → no error, column still NOT NULL.
- `test_migration_leaves_nullable_when_orphan_rows`: seed a pre-0011 `sessions` row → apply → `user_id` added
  but `is_nullable='YES'` (no crash, NOTICE path).

Confirm it fails (migration file doesn't exist yet): `make test-infra-up` then
`uv run pytest tests/migrations/test_0011_sessions_user_id_migration.py -v`.

### 2 — Write migration `docker/postgres/migrations/0011_sessions_user_id.sql`
Idempotent, ordered after 0010. ADD COLUMN IF NOT EXISTS (nullable first) · CREATE INDEX IF NOT EXISTS
`ix_sessions_user_id` · ADD CONSTRAINT in a `DO` block guarded by `EXCEPTION WHEN duplicate_object` (schema-scoped,
no false-positive against `public` from the ephemeral test schema) · conditional SET NOT NULL (only when no
orphan rows) with a NOTICE fallback. Header documents manual application + prod no-op.

### 3 — Mirror in `docker/postgres/init.sql` (fresh-volume path)
- In the `sessions` CREATE TABLE (line 8): add `user_id UUID NOT NULL,` with a comment.
- After the `sessions` block: add `CREATE INDEX IF NOT EXISTS ix_sessions_user_id ON sessions(user_id);`.
- After the `users` CREATE TABLE (line 324): add `ALTER TABLE sessions ADD CONSTRAINT sessions_user_id_fkey
  FOREIGN KEY (user_id) REFERENCES users(user_id);` (FK declared here because of table ordering).

### 4 — Make the test pass
`make test-infra-up && uv run pytest tests/migrations/test_0011_sessions_user_id_migration.py -v` → green.

### 5 — Quality gates
`make test-file FILE=tests/migrations/test_0011_sessions_user_id_migration.py` · `make mypy` · `make ruff-check`
· `make ruff-format` · `pre-commit run --all-files`.

## Acceptance mapping
- (a) fresh `make test-infra-up` → `\d sessions` shows `user_id` — satisfied by init.sql edit (test-infra mounts
  init.sql at entrypoint; no migration runner).
- (b) fresh stack creates a session end-to-end — satisfied by init.sql edit; verified live by master post-merge.
- (c) init.sql ↔ migration mirror the new column — both declare `user_id UUID NOT NULL` + `ix_sessions_user_id`
  + `sessions_user_id_fkey`; guarded by the parity test.
- (d) regression note in test substrate — `tests/migrations/test_0011_*` is the regression guard.

## Out of scope
No `src/` change (model + repository already declare/insert `user_id` correctly). No prod data migration needed
(prod fully populated). Master applies 0011 to prod post-merge as a documented no-op.
