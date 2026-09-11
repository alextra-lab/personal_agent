-- ---------------------------------------------------------------------------
-- Migration: 0032 — seshat_diag_ro, a full-read, zero-write diagnostic role
--
-- Owner-directed 2026-09-11 ("Create a read only full access account").
--
-- WHY THIS EXISTS
-- A session diagnosing live behaviour reads across the whole database: sessions,
-- users, route_traces, captains_log, api_costs, sysgraph. Every one of those reads
-- runs today as `agent`, a superuser, which means the only thing standing between a
-- diagnostic SELECT and a DROP is the command that was typed. The permission layer
-- cannot close that gap: its patterns match command prefixes, and no prefix can
-- express "no write verb anywhere in the statement".
--
-- Moving the guarantee into a role closes it at the server. A role with no write
-- privilege cannot write, whatever statement reaches it.
--
-- WHY NOT REUSE grafana_ro
-- grafana_ro (migration 0025) is table-grain by deliberate convention — each new
-- table gets an explicit GRANT in its own migration, so the dashboard datasource's
-- surface stays reviewed. Correct for a dashboard. Wrong for diagnosis: it cannot
-- read 8 tables today (sessions, users, session_model_selections, metrics,
-- session_events, user_constraint_preferences, captains_log_captures,
-- captains_log_reflections), and a table-grain convention hides every NEW table from
-- diagnosis at exactly the moment that table is the thing being diagnosed.
--
-- Widening grafana_ro would enlarge what a leaked Grafana credential reaches, so
-- this is a separate role instead.
--
-- THE DEPARTURE, STATED
-- This role takes schema-wide grants plus ALTER DEFAULT PRIVILEGES, so future
-- tables are readable without a migration. That is the opposite of 0025's
-- convention and it is intentional. The safety it trades away is "a human reviews
-- each table this role can read"; what it buys is that the role can never write.
-- For a read-only role those are not equivalent risks.
--
-- SCOPE
-- SELECT on all tables and sequences in public and sysgraph, present and future.
-- No INSERT, UPDATE, DELETE, TRUNCATE, or DDL against persistent objects. Not a
-- superuser, no role membership, no BYPASSRLS.
--
-- ONE THING IT CAN DO, stated because a seeded negative caught it and an earlier
-- draft of this comment claimed otherwise: it can CREATE TEMP TABLE. PostgreSQL
-- grants TEMPORARY on every database to PUBLIC by default, and that grant is not
-- revocable for one role — only from PUBLIC, which would also strip `agent` and
-- `seshat_app`. Left in place deliberately. A temp table is session-local, vanishes
-- on disconnect, and cannot alter persistent data; revoking it database-wide to
-- close a non-risk would enlarge this migration's blast radius to every role.
--
-- The password is set by the operator applying this migration and is stored in
-- `pass` at seshat/AGENT_DIAG_RO_PASSWORD. Local socket connections inside the
-- container authenticate by `trust` (pg_hba.conf), so no credential appears in a
-- diagnostic command line; the password covers the scram-sha-256 network path only.
--
-- Apply as the `agent` superuser via AGENT_DATABASE_ADMIN_URL, never as seshat_app,
-- which cannot run DDL (FRE-808).
-- ---------------------------------------------------------------------------

BEGIN;

-- Idempotent: re-applying must not fail, and must not silently reset a password.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'seshat_diag_ro') THEN
        CREATE ROLE seshat_diag_ro LOGIN;
    END IF;
END
$$;

-- Explicitly deny every attribute that could become a write path later.
ALTER ROLE seshat_diag_ro NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

GRANT CONNECT ON DATABASE personal_agent TO seshat_diag_ro;

-- USAGE only. No CREATE, so the role cannot make a temp or permanent object.
GRANT USAGE ON SCHEMA public   TO seshat_diag_ro;
GRANT USAGE ON SCHEMA sysgraph TO seshat_diag_ro;

GRANT SELECT ON ALL TABLES    IN SCHEMA public   TO seshat_diag_ro;
GRANT SELECT ON ALL TABLES    IN SCHEMA sysgraph TO seshat_diag_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public   TO seshat_diag_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA sysgraph TO seshat_diag_ro;

-- Future tables, so a new table is readable without a migration. Scoped to the
-- objects each writing role creates — `agent` runs migrations, `seshat_app` is the
-- application. A table created by any other role stays unreadable until granted,
-- which is the residual gap and is recorded here rather than papered over.
ALTER DEFAULT PRIVILEGES FOR ROLE agent      IN SCHEMA public   GRANT SELECT ON TABLES    TO seshat_diag_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE agent      IN SCHEMA public   GRANT SELECT ON SEQUENCES TO seshat_diag_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE agent      IN SCHEMA sysgraph GRANT SELECT ON TABLES    TO seshat_diag_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE agent      IN SCHEMA sysgraph GRANT SELECT ON SEQUENCES TO seshat_diag_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE seshat_app IN SCHEMA public   GRANT SELECT ON TABLES    TO seshat_diag_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE seshat_app IN SCHEMA sysgraph GRANT SELECT ON TABLES    TO seshat_diag_ro;

COMMIT;

-- ---------------------------------------------------------------------------
-- Verification, run after applying (expected results in brackets):
--
--   -- reads every table grafana_ro cannot:
--   SELECT count(*) FROM information_schema.tables t
--    WHERE t.table_schema IN ('public','sysgraph') AND t.table_type='BASE TABLE'
--      AND NOT has_table_privilege('seshat_diag_ro',
--            quote_ident(t.table_schema)||'.'||quote_ident(t.table_name), 'SELECT');
--   [0]
--
--   -- seeded negatives, each must ERROR when run as seshat_diag_ro.
--   -- Observed 2026-09-11 on the live database, verbatim:
--   CREATE TABLE __probe(i int);          [ERROR: permission denied for schema public]
--   INSERT INTO sessions DEFAULT VALUES;  [ERROR: permission denied for table sessions]
--   DELETE FROM sessions;                 [ERROR: permission denied for table sessions]
--   UPDATE users SET email=email;         [ERROR: permission denied for table users]
--   DROP TABLE sessions;                  [ERROR: must be owner of table sessions]
--   TRUNCATE sessions;                    [ERROR: permission denied for table sessions]
--
--   -- and the one that SUCCEEDS, by design — see the note in SCOPE above:
--   CREATE TEMP TABLE __probe(i int);     [CREATE TABLE]
-- ---------------------------------------------------------------------------
