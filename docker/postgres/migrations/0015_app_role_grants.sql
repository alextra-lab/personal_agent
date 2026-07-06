-- ===========================================================================
-- Migration: 0015 — app role (non-superuser) for the live connection
--            (ADR-0105 T1 / FRE-808)
--
-- FRE-714 (0014) built the isolated sysgraph schema and proved AC-2's
-- permission-denied requirement against `recall_role`, a stand-in for the
-- recall/user-facing connection. But the app's ACTUAL live connection
-- (AGENT_DATABASE_URL) runs as the `agent` bootstrap SUPERUSER, which bypasses
-- every grant — so the isolation is not proven against the deployed path.
--
-- This migration adds `seshat_app`: a non-superuser login role scoped to exactly
-- the public-schema DML the app needs, granted NOTHING on schema sysgraph. After
-- this migration the app connects as seshat_app and a stray `SELECT … FROM
-- sysgraph.*` from the app path raises `permission denied` (the real AC-2 proof).
--
-- ADMIN CREDENTIAL: run this migration (and every DDL migration) as the `agent`
-- SUPERUSER — i.e. AGENT_DATABASE_ADMIN_URL, NOT the app's AGENT_DATABASE_URL,
-- which after FRE-808 is the restricted seshat_app role that cannot run DDL:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0015_app_role_grants.sql
--
-- PRODUCTION PASSWORD: this ships the dev password. In prod, after applying,
-- set the real secret and point AGENT_DATABASE_URL at it:
--   ALTER ROLE seshat_app PASSWORD '<SESHAT_APP_PASSWORD>';
--
-- Idempotent. Fresh installs receive this via docker/postgres/init.sql (empty
-- volume only); this file brings existing prod/dev/test/eval DBs current.
-- ===========================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'seshat_app') THEN
        CREATE ROLE seshat_app LOGIN PASSWORD 'seshat_app_dev_password';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE personal_agent TO seshat_app;
GRANT USAGE ON SCHEMA public TO seshat_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO seshat_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO seshat_app;

-- Future public objects created by the `agent` superuser (later migrations)
-- auto-grant to the app role.
ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO seshat_app;
ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO seshat_app;

-- Intentionally NO grant on schema sysgraph — the app connection stays denied.

COMMIT;
