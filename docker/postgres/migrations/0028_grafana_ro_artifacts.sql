-- ===========================================================================
-- Migration: 0028 — grafana_ro grant on artifacts (FRE-1211)
--
-- The turn_session_artifact dashboard rebuild (FRE-1211) queries `artifacts`
-- directly from Postgres via the pg-ledger Grafana datasource, both for a
-- per-session artifact count (Session activity panel) and for an artifact
-- detail table (Artifact envelope detail panel — reframed to what `artifacts`
-- actually tracks; Postgres carries no envelope/probe/gate-decision telemetry,
-- confirmed live via information_schema — that signal, if it exists at all,
-- remains Elasticsearch-only). That datasource connects as grafana_ro
-- (SELECT-only, migration 0025), whose grant is table-grain by deliberate
-- owner ruling (0025's header) — no ALTER DEFAULT PRIVILEGES on `public`, so
-- each table a dashboard needs is granted explicitly, in its own migration.
--
-- Deliberately NOT granting `sessions` here. `sessions.messages` carries raw
-- conversation content — exactly the class of data 0025's header calls out
-- as the reason `public` is table-grain rather than schema-wide. Widening the
-- grant to `sessions` would undo that 2026-08-09 owner ruling to satisfy a
-- dashboard join; the turn_session_artifact rebuild instead keys its
-- per-session rollup off session_id already present on api_costs,
-- route_traces and artifacts (all already or newly grafana_ro-readable),
-- without reading any sessions column.
--
-- ADMIN CREDENTIAL: run as the `agent` SUPERUSER -- AGENT_DATABASE_ADMIN_URL,
-- not the app's AGENT_DATABASE_URL (seshat_app cannot run DDL -- FRE-808).
--
-- Idempotent. Fresh installs receive this via docker/postgres/init.sql (empty
-- volume only); this file brings existing prod/dev/test/eval DBs current.
-- ===========================================================================

BEGIN;

GRANT SELECT ON public.artifacts TO grafana_ro;

COMMIT;
