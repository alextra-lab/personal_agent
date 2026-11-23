-- ===========================================================================
-- Migration: 0042 — share links for anyone (ADR-0153 Amendment 1 L4 / FRE-1782)
--
-- artifact_share_links  One row per share link. A link opens exactly one artifact. The gateway
--                       stores only the SHA-256 of the 32 random token bytes (L3), never the
--                       token. A link always has an expiry of at most 30 days, set in SQL from
--                       now(). A revoke sets revoked_at. No path deletes a row, so the table is
--                       the record of every link.
--
-- No column holds an IP address, a user agent, a referrer or a location (L11).
--
-- Grants: seshat_app gets SELECT, INSERT, UPDATE, DELETE from the ALTER DEFAULT PRIVILEGES FOR
-- ROLE agent of init.sql / 0015; DELETE is then revoked, because no path deletes a link.
-- grafana_ro and seshat_dev_broker get no grant (L4).
--
-- ADMIN CREDENTIAL: run this migration as the `agent` SUPERUSER, through AGENT_DATABASE_ADMIN_URL,
-- not the app's AGENT_DATABASE_URL (seshat_app cannot run DDL -- FRE-808):
--   psql "$(echo $AGENT_DATABASE_ADMIN_URL | sed 's|postgresql+asyncpg|postgresql|')" \
--     -f docker/postgres/migrations/0042_artifact_share_links.sql
--
-- Idempotent. Additive: one new table and its indexes. Fresh installs receive this via
-- docker/postgres/init.sql (empty volume only).
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS artifact_share_links (
    id              UUID PRIMARY KEY,
    artifact_id     UUID NOT NULL REFERENCES artifacts(id) ON DELETE RESTRICT,
    created_by      UUID NOT NULL REFERENCES users(user_id),
    token_hash      BYTEA NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    snapshot_r2_key TEXT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ NULL,
    view_count      BIGINT NOT NULL DEFAULT 0 CHECK (view_count >= 0),
    last_viewed_at  TIMESTAMPTZ NULL,
    CONSTRAINT ck_artifact_share_links_expiry
        CHECK (expires_at > created_at AND expires_at <= created_at + INTERVAL '30 days')
);

CREATE INDEX IF NOT EXISTS idx_artifact_share_links_creator_created
    ON artifact_share_links (created_by, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifact_share_links_artifact
    ON artifact_share_links (artifact_id);

REVOKE DELETE ON artifact_share_links FROM seshat_app;

COMMIT;
