-- Migration 0035: route_traces Seshat effort level and primary mode (ADR-0157 D8, FRE-1622)
--
-- Every turn records its level and the catalog mode the answering deployment requested, so the
-- turns of each level can be compared (ADR-0157 AC-1, AC-4, AC-8). Both stay NULL on a turn
-- outside a chat entry path, and on rows written before this change.
--
-- Run as the `agent` superuser through AGENT_DATABASE_ADMIN_URL (the restricted app role
-- cannot run DDL -- FRE-808). Run it BEFORE the seshat-gateway rebuild: the ledger's INSERT
-- names both columns. Idempotent: ADD COLUMN IF NOT EXISTS, so a re-run is a no-op.
-- No Alembic (project policy): schema lives in init.sql + ordered migrations.

ALTER TABLE route_traces
    ADD COLUMN IF NOT EXISTS seshat_effort VARCHAR(16),
    ADD COLUMN IF NOT EXISTS primary_mode VARCHAR(40);

-- RouteTraceRow's schema_version default moves 3 -> 4 with this change (same alignment as
-- migrations 0031 and 0032: the DB default follows the DTO default).
ALTER TABLE route_traces ALTER COLUMN schema_version SET DEFAULT 4;
