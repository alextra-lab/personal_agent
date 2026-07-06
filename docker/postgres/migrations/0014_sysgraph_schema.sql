-- ===========================================================================
-- Migration: 0014 — sysgraph schema (ADR-0105 D2/D3 / FRE-714)
--
-- Isolated System-graph store: physically separate schema + role/grant
-- isolation, proven at the DB permission layer (AC-2). No pgvector column
-- yet — D10 (semantic dedup) is gated on the FRE-720 separation probe, not
-- yet resolved; add the column in a follow-up migration once it reports.
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0014_sysgraph_schema.sql
--
-- Fresh installs receive these tables via docker/postgres/init.sql, which
-- only runs on an empty Postgres volume. This file mirrors that DDL so
-- existing prod/dev DBs can be brought current with a single psql invocation.
-- ===========================================================================

BEGIN;

-- Dedicated roles (idempotent — CREATE ROLE has no IF NOT EXISTS).
-- sysgraph_role: owns the sysgraph schema and everything in it exclusively.
-- recall_role: stands in for "the recall/user-facing connection" per
-- ADR-0105 AC-2(a) — a non-superuser role that is never granted anything on
-- sysgraph, proving the permission-denied requirement at the DB layer.
-- NOTE: when this migration shipped, the app's actual live connection
-- (AGENT_DATABASE_URL) still ran as the `agent` bootstrap superuser and
-- bypassed all grants; migration 0015 (FRE-808) moves it to the restricted
-- non-superuser `seshat_app` role, so the isolation now holds against the
-- deployed connection too, not just the recall_role stand-in.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sysgraph_role') THEN
        CREATE ROLE sysgraph_role LOGIN PASSWORD 'sysgraph_dev_password';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'recall_role') THEN
        CREATE ROLE recall_role LOGIN PASSWORD 'recall_dev_password';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE personal_agent TO sysgraph_role, recall_role;

CREATE SCHEMA IF NOT EXISTS sysgraph AUTHORIZATION sysgraph_role;

-- Explicit isolation (defensive — a newly created schema isn't PUBLIC-usable
-- by default, but state the intent so it can never be assumed away by a
-- future grant elsewhere):
REVOKE ALL ON SCHEMA sysgraph FROM PUBLIC;
REVOKE ALL ON SCHEMA sysgraph FROM recall_role;
-- Symmetric: sysgraph_role gets nothing on the user-facing (public) schema.
REVOKE ALL ON SCHEMA public FROM sysgraph_role;

-- Create all sysgraph objects AS sysgraph_role so it owns them outright
-- (the migration-running role, `agent`, is a superuser and can SET ROLE to
-- any role without membership).
SET ROLE sysgraph_role;

-- Node tables ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS sysgraph.proposal (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source      TEXT NOT NULL CHECK (source IN ('statistical_detector', 'reflection')),
    category    TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    what        TEXT NOT NULL,
    why         TEXT,
    how         TEXT,
    seen_count  INTEGER NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sysgraph_proposal_fingerprint ON sysgraph.proposal(fingerprint);
CREATE INDEX IF NOT EXISTS idx_sysgraph_proposal_source_category ON sysgraph.proposal(source, category);

CREATE TABLE IF NOT EXISTS sysgraph.stat (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    value       DOUBLE PRECISION,
    metadata    JSONB NOT NULL DEFAULT '{}',
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sysgraph_stat_name ON sysgraph.stat(name, observed_at DESC);

CREATE TABLE IF NOT EXISTS sysgraph.ticket (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    linear_issue_id TEXT NOT NULL UNIQUE,
    title           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sysgraph.outcome (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    result      TEXT NOT NULL CHECK (result IN ('shipped', 'owner-rejected', 'canceled-as-noise', 'deferred')),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Edge tables ------------------------------------------------------------
-- DERIVES_FROM: Proposal -> Stat
CREATE TABLE IF NOT EXISTS sysgraph.derives_from (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id UUID NOT NULL REFERENCES sysgraph.proposal(id) ON DELETE CASCADE,
    stat_id     UUID NOT NULL REFERENCES sysgraph.stat(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (proposal_id, stat_id)
);

-- PROMOTED_TO: Proposal -> Ticket
CREATE TABLE IF NOT EXISTS sysgraph.promoted_to (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id UUID NOT NULL REFERENCES sysgraph.proposal(id) ON DELETE CASCADE,
    ticket_id   UUID NOT NULL REFERENCES sysgraph.ticket(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (proposal_id, ticket_id)
);
CREATE INDEX IF NOT EXISTS idx_sysgraph_promoted_to_ticket ON sysgraph.promoted_to(ticket_id);

-- PRODUCED: Ticket -> Outcome
CREATE TABLE IF NOT EXISTS sysgraph.produced (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id  UUID NOT NULL REFERENCES sysgraph.ticket(id) ON DELETE CASCADE,
    outcome_id UUID NOT NULL REFERENCES sysgraph.outcome(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ticket_id, outcome_id)
);

-- CORRELATES_WITH / INFLUENCE: polymorphic Proposal<->Proposal or Proposal<->Stat
-- edges. No DB-level FK across the two possible node tables (heterogeneous
-- target type) — validated at the sysgraph repository layer, not the schema.
CREATE TABLE IF NOT EXISTS sysgraph.correlates_with (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_node_type TEXT NOT NULL CHECK (from_node_type IN ('proposal', 'stat')),
    from_node_id   UUID NOT NULL,
    to_node_type   TEXT NOT NULL CHECK (to_node_type IN ('proposal', 'stat')),
    to_node_id     UUID NOT NULL,
    weight         DOUBLE PRECISION,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sysgraph_correlates_from ON sysgraph.correlates_with(from_node_type, from_node_id);

CREATE TABLE IF NOT EXISTS sysgraph.influence (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_node_type TEXT NOT NULL CHECK (from_node_type IN ('proposal', 'stat')),
    from_node_id   UUID NOT NULL,
    to_node_type   TEXT NOT NULL CHECK (to_node_type IN ('proposal', 'stat')),
    to_node_id     UUID NOT NULL,
    weight         DOUBLE PRECISION,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sysgraph_influence_from ON sysgraph.influence(from_node_type, from_node_id);

RESET ROLE;

-- Convention for future sysgraph migrations (T3/T4 and beyond): wrap new
-- sysgraph.* DDL in the same SET ROLE sysgraph_role / RESET ROLE pair so new
-- objects stay owned by sysgraph_role, not the migration-running superuser.

COMMIT;
