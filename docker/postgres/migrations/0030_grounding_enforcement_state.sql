-- ===========================================================================
-- Migration: 0030 — grounding_enforcement_state (FRE-1285)
--
-- ADR-0138 D5's enforcement selection. One row per model catalog key, holding
-- the two things the compliance rate cannot express by itself.
--
-- WHY ANY STATE AT ALL, when D5 says selection is keyed on the computed rate.
-- It still is: `level` is always recomputed from the rate, and this table never
-- overrides it. What the rate alone cannot say is (a) which level was in force
-- when a reading lands INSIDE the hysteresis band -- the band's whole purpose is
-- that an in-band reading holds whatever was already applied -- and (b) WHEN the
-- model was demoted, which is the only fact no later turn can reconstruct from
-- the observations. Everything else is derived per turn.
--
-- WHY `demoted_at` IS NULLABLE AND WHAT NULL MEANS. NULL is "never demoted",
-- which is different from "demoted long ago". D5 gives the cooldown to a
-- *demoted* model; a model that has never been light has never been demoted and
-- serves none, so the bootstrap does not punish a new model for a demotion that
-- never happened. Every light-to-heavy transition stamps it, INCLUDING the one
-- caused by the window going stale -- a model that stopped producing recognized
-- spans would otherwise re-promote the instant it rebuilt a window, having
-- served no cooldown, which is promotion without earning it.
--
-- WHY A TABLE RATHER THAN AN IN-PROCESS CACHE. The cooldown outlives a process
-- restart, and a gateway rebuild during a cooldown must not hand a demoted model
-- an immediate promotion. Same argument migration 0029 made for the
-- observations themselves: this is a control-plane read in the turn path, so it
-- wants one indexed primary-key lookup, not a log query.
--
-- CONCURRENCY. Turns run concurrently and each selects independently, so two
-- turns can hold the same stale standing. The writer's ON CONFLICT DO UPDATE is
-- guarded on `updated_at` so a slower turn carrying an older reading cannot
-- clobber a newer transition -- last-write-wins on a demotion would silently
-- reset a cooldown, and nothing downstream could tell that it had.
--
-- WRITE FREQUENCY. Written only when the level or the stamp actually CHANGES,
-- which is on the order of once per hundreds of turns. That is what makes it
-- affordable to await the write rather than background it, and awaiting is what
-- makes the cooldown durable: a lost demotion write is the one loss no later
-- turn repairs, because the next turn re-demotes with a LATER stamp.
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

CREATE TABLE IF NOT EXISTS grounding_enforcement_state (
    model_key    VARCHAR(255) PRIMARY KEY,
    level        VARCHAR(16) NOT NULL,
    demoted_at   TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

GRANT SELECT ON public.grounding_enforcement_state TO grafana_ro;

COMMIT;
