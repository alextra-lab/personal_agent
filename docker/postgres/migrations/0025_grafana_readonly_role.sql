-- ===========================================================================
-- Migration: 0025 — grafana_ro read-only role for the Grafana Postgres
--            datasource (FRE-1203 part 2)
--
-- Grafana Viewer (anonymous, OSS) can issue arbitrary queries against every
-- datasource in the org — per-datasource permissions are a Grafana Enterprise
-- feature, not available here (ADR-0129). The instant a Postgres datasource
-- exists, an anonymous caller can run arbitrary SQL as whatever role backs
-- it. `grafana_ro` is the containment boundary: a login role with SELECT
-- only, no INSERT/UPDATE/DELETE anywhere, so the worst an anonymous Explore
-- query can do is read.
--
-- sysgraph: owner ruled 2026-08-08 that a read-only observer may see
-- sysgraph.* — ADR-0105 AC-2's isolation is against the application
-- write/recall path, not a read-only Viewer behind Cloudflare Access. This
-- grant does not touch seshat_app or recall_role, which stay denied; their
-- existing permission-denied proof (0014/0015) is untouched.
--
-- ADMIN CREDENTIAL: run this migration (and every DDL migration) as the
-- `agent` SUPERUSER — AGENT_DATABASE_ADMIN_URL, not the app's
-- AGENT_DATABASE_URL (seshat_app, which cannot run DDL — FRE-808). psql
-- can't parse the `postgresql+asyncpg://` prefix:
--   psql "$(echo $AGENT_DATABASE_ADMIN_URL | sed 's|postgresql+asyncpg|postgresql|')" \
--     -f docker/postgres/migrations/0025_grafana_readonly_role.sql
--
-- PRODUCTION PASSWORD: this ships the dev password. In prod, after applying,
-- set the real secret and point the Grafana datasource's GRAFANA_RO_PASSWORD
-- env var at it:
--   ALTER ROLE grafana_ro PASSWORD '<GRAFANA_RO_PASSWORD>';
--
-- Idempotent. Fresh installs receive this via docker/postgres/init.sql (empty
-- volume only); this file brings existing prod/dev/test/eval DBs current.
-- ===========================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
        CREATE ROLE grafana_ro LOGIN PASSWORD 'grafana_ro_dev_password';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE personal_agent TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public
    GRANT SELECT ON TABLES TO grafana_ro;

-- sysgraph: read-only observation, owner ruling 2026-08-08 (see migration header). ADR-0105
-- AC-2's isolation is against the application write/recall path, NOT against a read-only
-- observer -- seshat_app and recall_role remain denied and their permission-denied proof is
-- untouched by this grant. Default-privileges target is sysgraph_role (the schema owner,
-- migration 0014), not agent -- sysgraph objects are created under SET ROLE sysgraph_role, so
-- a grant FOR ROLE agent would silently miss every future table there.
GRANT USAGE ON SCHEMA sysgraph TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA sysgraph TO grafana_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE sysgraph_role IN SCHEMA sysgraph
    GRANT SELECT ON TABLES TO grafana_ro;

COMMIT;
