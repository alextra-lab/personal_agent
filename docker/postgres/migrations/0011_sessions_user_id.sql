-- ===========================================================================
-- Migration: 0011 — sessions.user_id schema divergence (FRE-591)
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0011_sessions_user_id.sql
--
-- Fresh installs receive this column via docker/postgres/init.sql, which only
-- runs on an empty Postgres volume.
--
-- Background: SessionModel.user_id is declared NOT NULL + FK and inserted by
-- SessionRepository.create, but no user_id column was ever added to init.sql or
-- a migration. Base.metadata.create_all only creates *missing tables* — never a
-- column on an existing table — so a fresh volume produced a sessions table
-- without user_id and the first session INSERT failed. Live prod already has
-- the column (created historically by create_all: hence the ix_ index name and
-- the sessions_user_id_fkey constraint), all 1197 rows populated, so this
-- migration is a documented no-op on prod.
--
-- Backfill: prod has 11 users, not 1 — there is no safe way to auto-attribute
-- orphan (column-less) sessions to a user. This migration enforces NOT NULL
-- only when zero orphan rows exist; otherwise it leaves the column nullable and
-- raises a NOTICE so an operator can backfill deliberately. No real DB has
-- orphan rows (prod is fully populated; test-infra builds fresh from init.sql).
-- ===========================================================================

BEGIN;

-- 1. Add the column nullable first so the backfill gate below can run.
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS user_id UUID;

-- 2. Index — ix_ name matches the prod/SQLAlchemy index so this is a no-op there.
CREATE INDEX IF NOT EXISTS ix_sessions_user_id ON sessions(user_id);

-- 3. FK constraint. pg_constraint has no IF NOT EXISTS; the DO block swallows
--    duplicate_object so a re-apply (or prod, which already has it) is a no-op.
--    Relation-scoped: the constraint name lookup is per-table, so a same-named
--    constraint in another schema cannot collide.
DO $$
BEGIN
    ALTER TABLE sessions
        ADD CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(user_id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END$$;

-- 4. Enforce NOT NULL only when no orphan rows remain. Multi-user installs
--    cannot auto-attribute orphan sessions, so leave the column nullable and
--    warn instead of guessing. SET NOT NULL is a no-op when already enforced.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM sessions WHERE user_id IS NULL) THEN
        ALTER TABLE sessions ALTER COLUMN user_id SET NOT NULL;
    ELSE
        RAISE NOTICE 'sessions.user_id left NULLABLE: % orphan row(s) need manual backfill before SET NOT NULL',
            (SELECT count(*) FROM sessions WHERE user_id IS NULL);
    END IF;
END$$;

COMMIT;
