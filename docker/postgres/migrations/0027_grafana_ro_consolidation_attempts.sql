-- ===========================================================================
-- Migration: 0027 — grafana_ro grant on consolidation_attempts (FRE-1211)
--
-- The extraction_retry_health dashboard rebuild (FRE-1211) queries
-- consolidation_attempts (created in 0001_cost_gate_schema.sql) directly from
-- Postgres via the pg-ledger Grafana datasource. That datasource connects as
-- grafana_ro (SELECT-only, migration 0025), whose grant is table-grain by
-- deliberate owner ruling (0025's header) -- no ALTER DEFAULT PRIVILEGES on
-- `public`, so each table a dashboard needs is granted explicitly, in its own
-- migration. consolidation_attempts holds no PII (id, trace_id,
-- attempt_number, role, started_at, completed_at, outcome, denial_reason).
--
-- ADMIN CREDENTIAL: run as the `agent` SUPERUSER -- AGENT_DATABASE_ADMIN_URL,
-- not the app's AGENT_DATABASE_URL (seshat_app cannot run DDL -- FRE-808).
--
-- Idempotent. Fresh installs receive this via docker/postgres/init.sql (empty
-- volume only); this file brings existing prod/dev/test/eval DBs current.
-- ===========================================================================

BEGIN;

GRANT SELECT ON public.consolidation_attempts TO grafana_ro;

COMMIT;
