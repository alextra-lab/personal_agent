-- Migration 0037: the settings override store and its audit record (FRE-1722)
--
-- The owner edits a parameter in the PWA. The edit goes in settings_overrides. The config file
-- stays the default. A stored row wins with no rebuild, and removing the row restores the file
-- value. `parameter` is the settings menu row name (`role.primary`) and `value` is JSONB, so a later
-- ticket adds parameters with no schema change.
--
-- settings_edit_audit holds one row for each accepted edit and each Clear, for an override or a
-- saved choice. `changed_by` has no foreign key, so a record outlives its user row.
--
-- Run as the `agent` superuser through AGENT_DATABASE_ADMIN_URL (the restricted app role
-- cannot run DDL -- FRE-808). Run it BEFORE the seshat-gateway rebuild: the gateway reads
-- settings_overrides at startup, and a saved choice write inserts into settings_edit_audit.
-- Idempotent: CREATE ... IF NOT EXISTS, so a re-run is a no-op.
-- No Alembic (project policy): schema lives in init.sql + ordered migrations.

CREATE TABLE IF NOT EXISTS settings_overrides (
    parameter  TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_by UUID NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS settings_edit_audit (
    audit_id   BIGSERIAL PRIMARY KEY,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    changed_by UUID NOT NULL,
    parameter  TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('set', 'clear')),
    old_value  JSONB,
    new_value  JSONB
);

CREATE INDEX IF NOT EXISTS idx_settings_edit_audit_parameter
    ON settings_edit_audit (parameter, changed_at DESC);
