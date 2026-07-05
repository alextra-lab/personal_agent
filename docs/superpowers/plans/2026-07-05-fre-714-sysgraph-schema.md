# FRE-714 — ADR-0105 T1: Isolated System graph store (sysgraph Postgres schema)

**Ticket:** FRE-714 (Approved) · **Backing:** ADR-0105 D2/D3, AC-2
**Branch:** `fre-714-sysgraph-schema`

## Scope

Foundation ticket. Builds the isolated `sysgraph` Postgres schema (nodes: proposal, stat,
ticket, outcome; edges: derives_from, promoted_to, produced, correlates_with, influence),
a dedicated role that owns it exclusively, a proof of DB-permission-layer isolation (AC-2),
and the `sysgraph` repository module — the only code path allowed to open this schema.

**Explicitly out of scope for T1** (owned by later tickets, per Linear relations):
- Producer wiring / source discriminator (T2, FRE-715)
- Bidirectional proposal↔ticket linkage writes (T3, FRE-716)
- Outcome ingestion + realized-value signal (T4, FRE-717)
- pgvector embedding column on `proposal` — the ticket's own text says "include it only if
  wanted; otherwise omit rather than leave it unindexed." D10 (semantic dedup) is still an
  open measurement gate — FRE-720 (T0 separation probe) is **In Progress** in build2, not yet
  resolved. Omitting the column now avoids building on an undecided premise; a follow-up
  migration adds it once FRE-720 reports.
- Migrating the *live app's* actual Postgres connection to a restricted, non-superuser role.
  Today the whole app connects as `agent`, which is the Postgres bootstrap **superuser**
  (`POSTGRES_USER=agent` in docker-compose — superusers bypass all grants). Actually switching
  the app's live credential is a cross-cutting, higher-blast-radius change (deploy secrets,
  every existing query path). T1 proves isolation with a purpose-built `recall_role` standing
  in for "the recall/user-facing connection" per the ADR's AC-2(a) test — filing a follow-up
  ticket to evaluate moving the app's real connection off the superuser role.

  **Caveat flagged by codex plan-review (2026-07-05), must be surfaced to master at the PR
  gate:** AC-2(a) is proven against the *schema/role design* — `recall_role` genuinely gets a
  real `InsufficientPrivilegeError` from Postgres. It is **not** proven against the app's
  *actual deployed* connection, which remains the `agent` superuser and would in fact bypass
  every grant in this migration if it ever queried `sysgraph.*` directly. Closing that gap is
  the filed follow-up ticket, not this one — the isolation-by-construction argument (D2:
  "no recall/tutor code path constructs or opens it," proven by AC-2c's import grep) is what
  actually protects prod today, not the grant alone, until that follow-up lands.

## Files

### 1. `docker/postgres/migrations/0014_sysgraph_schema.sql` (new)

Idempotent, `BEGIN...COMMIT`, mirrors the `0003_artifacts_schema.sql` header convention
(catch-up script for already-provisioned DBs). Contents:

```sql
-- Migration: 0014 — sysgraph schema (ADR-0105 D2/D3 / FRE-714)
-- Isolated System-graph store: physically separate schema + role/grant isolation,
-- proven at the DB permission layer (AC-2). No pgvector column yet — D10 (semantic
-- dedup) is gated on the FRE-720 separation probe, not yet resolved.
--
-- Idempotent. Apply against existing databases via:
--   psql $AGENT_DATABASE_URL -f docker/postgres/migrations/0014_sysgraph_schema.sql
--
-- Fresh installs receive this via docker/postgres/init.sql (mirrors this DDL).

BEGIN;

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

-- Explicit isolation (defensive — new schemas aren't PUBLIC-usable by default,
-- but state the intent so it can never be assumed away by a future grant):
REVOKE ALL ON SCHEMA sysgraph FROM PUBLIC;
REVOKE ALL ON SCHEMA sysgraph FROM recall_role;
-- Symmetric: sysgraph_role gets nothing on the user-facing (public) schema.
REVOKE ALL ON SCHEMA public FROM sysgraph_role;

-- Create all sysgraph objects AS sysgraph_role so it owns them outright
-- (agent is a superuser and can SET ROLE to any role).
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
CREATE TABLE IF NOT EXISTS sysgraph.derives_from (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id UUID NOT NULL REFERENCES sysgraph.proposal(id) ON DELETE CASCADE,
    stat_id     UUID NOT NULL REFERENCES sysgraph.stat(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (proposal_id, stat_id)
);

CREATE TABLE IF NOT EXISTS sysgraph.promoted_to (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id UUID NOT NULL REFERENCES sysgraph.proposal(id) ON DELETE CASCADE,
    ticket_id   UUID NOT NULL REFERENCES sysgraph.ticket(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (proposal_id, ticket_id)
);
CREATE INDEX IF NOT EXISTS idx_sysgraph_promoted_to_ticket ON sysgraph.promoted_to(ticket_id);

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
```

### 2. `docker/postgres/init.sql` (modify)

Append the identical DDL block (mirrors how `artifacts` (0003) is duplicated into
`init.sql` for fresh installs — see existing comment at line 350). Add a header comment
pointing at 0014 for existing DBs, matching the artifacts precedent exactly.

### 3. `src/personal_agent/config/settings.py` (modify)

- Add a new field next to `database_url` (~line 1076):
  ```python
  sysgraph_database_url: str = Field(
      default="postgresql+asyncpg://sysgraph_role:sysgraph_dev_password@localhost:5432/personal_agent",
      description="PostgreSQL URL for the isolated sysgraph schema (ADR-0105 D2). "
      "Connects as the dedicated sysgraph_role, never the app's main role.",
  )
  ```
- Extend `_validate_substrate_isolation` (~line 2162) with a fourth offender check:
  ```python
  if is_prod_postgres_url(self.sysgraph_database_url):
      offenders.append(f"sysgraph_database_url={self.sysgraph_database_url!r}")
  ```
  and add `AGENT_SYSGRAPH_DATABASE_URL=<test-db-url>` to the remediation message.
- Update the `_validate_substrate_isolation` docstring's "all three conditions hold" /
  "three substrate URIs" wording to four, now that `sysgraph_database_url` is checked too
  (codex plan-review nit, 2026-07-05).

### 4. `.env.example` (modify)

Add a commented line under the existing `AGENT_DATABASE_URL` entry (~line 472):
```
# AGENT_SYSGRAPH_DATABASE_URL=postgresql+asyncpg://sysgraph_role:sysgraph_dev_password@localhost:5432/personal_agent
```

### 5. `tests/conftest.py` (modify)

Add a test-stack override alongside the existing `AGENT_DATABASE_URL` setdefault (~line 21),
**required** — without it every test process fails at settings-construction time because the
default `sysgraph_database_url` fingerprint-matches prod (port 5432) under `APP_ENV=test`:
```python
os.environ.setdefault(
    "AGENT_SYSGRAPH_DATABASE_URL",
    "postgresql+asyncpg://sysgraph_role:sysgraph_dev_password@localhost:5433/personal_agent",
)
```

### 6. `src/personal_agent/sysgraph/__init__.py` (new)

Exports `SysgraphRepository`.

### 7. `src/personal_agent/sysgraph/repository.py` (new)

Mirrors the `cost_gate` connection pattern (raw asyncpg, not SQLAlchemy — same rationale:
a narrow-domain repository, not general app ORM traffic). Reuses
`personal_agent.llm_client.cost_tracker._normalize_asyncpg_dsn` (already imported across
module boundaries by `cost_gate/gate.py` despite the leading underscore).

```python
class SysgraphRepository:
    """The only code path permitted to open a connection to the sysgraph schema
    (ADR-0105 D2/D3). No memory/recall/tutor code path may construct or use this
    class — enforced by test_isolation.py's import-boundary check (AC-2c).

    Fail-closed on connect: if the DSN does not resolve to sysgraph_role, connect()
    raises rather than silently running as a different (possibly over-privileged)
    role. D9's producer-side fail-open behavior (a later ticket) governs read
    *availability* only — it must never be read as license to weaken this check.
    """

    def __init__(self, dsn: str) -> None: ...

    async def connect(self) -> None:
        """Open the asyncpg pool, then assert the connection is sysgraph_role.

        Raises:
            RuntimeError: if `SELECT current_user` != 'sysgraph_role' — a
                misconfigured DSN must never silently run as a broader-privileged
                role (Codex review finding, 2026-07-05: the substrate-isolation
                guard checks host/port only, not role, so this is the only place
                that would otherwise catch it).
        """

    async def disconnect(self) -> None: ...

    async def proposal_lineage(self, proposal_id: UUID) -> list[dict[str, Any]]:
        """Recursive CTE: proposal -> promoted_to -> ticket -> produced -> outcome,
        depth capped at 3 (ADR-0105 D2 — shallow-path traversal)."""

    async def one_hop_correlations(
        self, node_type: Literal["proposal", "stat"], node_id: UUID
    ) -> list[dict[str, Any]]:
        """Recursive CTE, depth capped at 1: CORRELATES_WITH neighbors of a node."""
```

Both traversal methods use `WITH RECURSIVE` per the ADR's explicit mechanism choice (D2:
"recursive common-table-expressions for the shallow proposal-to-ticket-to-outcome and
one-hop correlation paths"), even though depth is bounded — matching the architectural
decision, not just achieving the same result via a plain JOIN.

### 8. Tests (new)

- `tests/personal_agent/sysgraph/__init__.py` (empty)
- `tests/personal_agent/sysgraph/conftest.py` — session-scoped fixture applying the 0014
  migration SQL against the test Postgres (idempotent, safe to re-run).
- `tests/personal_agent/sysgraph/test_isolation.py` (AC-2, `@pytest.mark.integration`):
  - `test_recall_role_denied_on_sysgraph` — connect as `recall_role`, assert
    `SELECT * FROM sysgraph.proposal` raises `asyncpg.exceptions.InsufficientPrivilegeError`.
  - `test_sysgraph_role_has_no_public_table_privilege` — connect as `agent` (the
    migration-running superuser) and assert `has_table_privilege('sysgraph_role',
    'sessions', 'SELECT')` (and INSERT/UPDATE/DELETE) is `false`. Deterministic
    against the actual grant state, not incidental on `sessions` happening to
    have no other grants (Codex review finding, 2026-07-05: a plain `SELECT ...
    AS sysgraph_role` would pass even without any REVOKE, since no role has ever
    been granted access to `sessions` — it wouldn't prove anything). **Manually
    verified 2026-07-05 against the shared test Postgres before writing the
    automated test:** `has_schema_privilege('sysgraph_role', 'public', 'USAGE')`
    is `true` — this is Postgres's ambient default PUBLIC-role grant on the
    `public` schema (ubiquitous, not something this migration controls or should
    revoke — doing so is a separate, much larger blast-radius change) — so the
    schema-USAGE check is dropped from the test; only the table-level privilege
    (which *is* what this migration controls) is asserted.
  - `test_no_recall_path_imports_sysgraph` (plain unit test, no DB) — grep
    `src/personal_agent/memory/`, `orchestrator/`, `tools/` for any import of
    `personal_agent.sysgraph`; assert none found (AC-2c).
- `tests/personal_agent/sysgraph/test_repository.py` (`@pytest.mark.integration`):
  seed a proposal→ticket→outcome chain and a correlates_with edge via `sysgraph_role`,
  confirm `proposal_lineage` and `one_hop_correlations` return the expected nodes.

### 9. Docs

- Root `CLAUDE.md` module-map table: add a `sysgraph/` row (mirrors `cost_gate/`, `storage/`
  entries already there).

## Verify

```bash
make test-infra-reset && make test-infra-up   # pick up new roles/schema on a fresh volume
make test-file FILE=tests/personal_agent/sysgraph/test_isolation.py
make test-file FILE=tests/personal_agent/sysgraph/test_repository.py
make test          # full unit suite unaffected
make mypy
make ruff-check && make ruff-format
pre-commit run --all-files
```

## AC-2 proof mapping

| AC-2 sub-check | How T1 proves it |
|---|---|
| (a) permission-denied test | `test_recall_role_denied_on_sysgraph` — real `InsufficientPrivilegeError` from Postgres, not mocked |
| (b) different engine | Structural — sysgraph is Postgres, user KG is Neo4j; no Cypher path can reference a Postgres table. Documented, not independently testable beyond (c) |
| (c) repository grep | `test_no_recall_path_imports_sysgraph` |

## Follow-ups to file (Step 5)

- Needs Approval: evaluate moving the app's live Postgres connection off the `agent`
  superuser role to a properly scoped non-superuser role (today `recall_role` only exists
  as an isolation-proof stand-in, not the app's real runtime credential).
