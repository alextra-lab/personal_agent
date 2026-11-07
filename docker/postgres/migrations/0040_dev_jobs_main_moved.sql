-- ===========================================================================
-- Migration: 0040 — whether a dev-agent job moved the repo's main (ADR-0159 D8.9 / FRE-1685)
--
-- dev_jobs.main_moved   After a successful publish, the gateway fast-forwards the repo's main to
--                       the result commit, but only when the result descends from main. A fix
--                       can start from an older version while main holds a later fix. Then main
--                       stays, and the job card says so.
--                         true  = main is at the result commit
--                         false = main was not an ancestor of the result, so it did not move
--                         NULL  = not tried yet, or the outcome is unknown (the push and the
--                                 re-read both failed)
--
-- ADMIN CREDENTIAL: run this migration as the `agent` SUPERUSER, through AGENT_DATABASE_ADMIN_URL,
-- not the app's AGENT_DATABASE_URL (seshat_app cannot run DDL -- FRE-808):
--   psql "$(echo $AGENT_DATABASE_ADMIN_URL | sed 's|postgresql+asyncpg|postgresql|')" \
--     -f docker/postgres/migrations/0040_dev_jobs_main_moved.sql
--
-- Idempotent. Additive: one nullable column, no rewrite of existing rows. Fresh installs receive
-- this via docker/postgres/init.sql (empty volume only).
-- ===========================================================================

BEGIN;

ALTER TABLE dev_jobs ADD COLUMN IF NOT EXISTS main_moved BOOLEAN;

COMMIT;
