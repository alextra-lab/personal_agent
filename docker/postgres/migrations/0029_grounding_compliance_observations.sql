-- ===========================================================================
-- Migration: 0029 — grounding_compliance_observations (FRE-1284)
--
-- ADR-0138 D5's per-model citation-compliance metric. One row per UNCONFOUNDED
-- turn: verification ran, the turn contained at least one non-exempt span, and
-- retrieval was NOT forced before generation.
--
-- WHY A TABLE RATHER THAN THE STRUCTURED LOG. FRE-1286's offline entailment arm
-- emits to structlog because nothing reads it inline. This signal is different:
-- FRE-1285 reads it once per turn, before generation, to choose light or heavy
-- enforcement. That is a control-plane read, and an Elasticsearch query in the
-- turn path is neither cheap nor deterministic. One indexed LIMIT read on
-- (model_key, observed_at DESC) is.
--
-- WHY PRE-FORCED TURNS ARE ABSENT RATHER THAN FILTERED. Heavy enforcement
-- supplies sources before generation, so first-generation compliance measured
-- under forcing is largely a measurement of the enforcement. Scoring those turns
-- lets a model that only complies when spoon-fed earn promotion, fail under
-- light, be demoted, recover under heavy, and oscillate forever -- a blocking
-- finding in ADR review round 2. A row that is never written cannot later be
-- counted by mistake; a filter can be forgotten.
--
-- `observed_at` is the VERIFICATION instant, written explicitly by the app. The
-- DEFAULT NOW() exists only so a create_all-before-migration boot still gets a
-- non-null column; relying on it would record insertion lag (the write is
-- backgrounded) as turn age and weaken the staleness rule that reads this column.
--
-- `trace_id UNIQUE`: one observation per turn. `_record_grounding` has a single
-- call site, sub-agents never reach it, and only attempt 1 is ever eligible, so a
-- second row for a trace is a defect. The writer's ON CONFLICT DO NOTHING makes a
-- replay idempotent rather than letting it inflate the numerator.
--
-- No retention job. One row per eligible turn is low volume, and the metric's own
-- max-window-age setting already bounds what can influence a reading. Recorded as
-- an accepted cost rather than designed around.
--
-- ADMIN CREDENTIAL: run as the `agent` SUPERUSER -- AGENT_DATABASE_ADMIN_URL,
-- not the app's AGENT_DATABASE_URL (seshat_app cannot run DDL -- FRE-808).
-- seshat_app needs no explicit grant: init.sql's ALTER DEFAULT PRIVILEGES FOR
-- ROLE agent covers every table a later migration creates.
--
-- grafana_ro grant: table-grain, explicit, per migration 0025's convention.
--
-- Idempotent. Fresh installs receive this via docker/postgres/init.sql (empty
-- volume only); this file brings existing prod/dev/test/eval DBs current.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS grounding_compliance_observations (
    id           BIGSERIAL PRIMARY KEY,
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_key    VARCHAR(255) NOT NULL,
    compliant    BOOLEAN NOT NULL,
    trace_id     VARCHAR(64) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_grounding_compliance_model_time
    ON grounding_compliance_observations(model_key, observed_at DESC);

GRANT SELECT ON public.grounding_compliance_observations TO grafana_ro;

COMMIT;
