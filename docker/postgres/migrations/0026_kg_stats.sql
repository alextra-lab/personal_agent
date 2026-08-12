-- ===========================================================================
-- Migration: 0026 — kg_stats (FRE-1210 T6.1)
--
-- Daily Neo4j -> Postgres projection of KG freshness/health metrics
-- (ADR-0042 / FRE-161). Written by a dedicated daily job
-- (brainstem/jobs/kg_stats_projection.py), alongside -- not replacing -- the
-- existing weekly freshness_review JSONL snapshot (ADR-0054 D4:
-- durable-first, bus-second; the JSONL write is untouched by this ticket).
--
-- One row per (observed_at, metric_name, dimension) triple. `dimension` is
-- NULL for scalar ratios/counts (cold_mass_ratio, unmeasured_ratio,
-- embedding_missing, duplicate_group_count, type_disagreement_count,
-- turns_without_entities_ratio), a bucket/type label for histograms
-- (access_count_bucket, recency_bucket, entity_count, relationship_count),
-- a pipe-joined bucket pair for the two heatmap cross-tabs
-- (recency_frequency_cell, type_recency_cell), or an entity name for the
-- top_heat_entity ranking.
--
-- `dimension VARCHAR(255)`, wider than the 64 chars a first draft used --
-- entity names (top_heat_entity) and pipe-joined bucket pairs both exceed a
-- short bucket-label width.
--
-- `UNIQUE NULLS NOT DISTINCT`: Postgres's default UNIQUE treats every NULL
-- as distinct, so two same-instant rows sharing (observed_at, metric_name)
-- with dimension=NULL (every scalar ratio/count metric) would NOT conflict
-- and the writer's `ON CONFLICT DO NOTHING` guard would silently do nothing
-- for exactly those metrics. NULLS NOT DISTINCT (Postgres 15+; this stack
-- runs 17) closes that gap.
--
-- ADMIN CREDENTIAL: run as the `agent` SUPERUSER -- AGENT_DATABASE_ADMIN_URL,
-- not the app's AGENT_DATABASE_URL (seshat_app cannot run DDL -- FRE-808).
--
-- grafana_ro grant: table-grain, explicit, per migration 0025's convention
-- (no ALTER DEFAULT PRIVILEGES on `public` -- a new table is granted
-- deliberately, in its own migration).
--
-- Idempotent. Fresh installs receive this via docker/postgres/init.sql
-- (empty volume only); this file brings existing prod/dev/test/eval DBs
-- current.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS kg_stats (
    id              BIGSERIAL PRIMARY KEY,
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name     VARCHAR(64) NOT NULL,
    dimension       VARCHAR(255),
    metric_value    DOUBLE PRECISION NOT NULL,
    UNIQUE NULLS NOT DISTINCT (observed_at, metric_name, dimension)
);
CREATE INDEX IF NOT EXISTS idx_kg_stats_metric_time ON kg_stats(metric_name, observed_at DESC);

GRANT SELECT ON public.kg_stats TO grafana_ro;

COMMIT;
