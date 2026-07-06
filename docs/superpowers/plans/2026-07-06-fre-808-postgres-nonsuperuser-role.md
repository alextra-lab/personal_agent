# FRE-808 — Migrate the app's live Postgres connection off the `agent` superuser role

- **Ticket:** FRE-808 (Approved, High, Tier-1:Opus, stream:build1) — parent FRE-708; related FRE-714.
- **Backing:** ADR-0105 T1 (FRE-714) built the isolated `sysgraph` schema + `sysgraph_role`/`recall_role`
  and proved AC-2's permission-denied requirement against `recall_role` (a *stand-in* for the
  recall/user-facing connection). This ticket makes that proof real against the app's **actual**
  deployed connection, which today is the `agent` bootstrap **superuser** and bypasses every grant.
- **One PR.** Cross-cutting (config + every compose env + prod DB grants + tests), but a single
  cohesive change.

## Acceptance criteria (from the ticket) → proof

| # | Criterion | Proof |
|---|-----------|-------|
| AC-1 | App's `AGENT_DATABASE_URL` (main + test) connects as a **non-superuser** role. | New `seshat_app` role created with `NOSUPERUSER` (implicit — `CREATE ROLE … LOGIN` is not superuser). Settings default + all compose app URLs + test conftest use `seshat_app`. Test asserts `rolsuper = false`. |
| AC-2 | That role has explicit grants **only** on the tables/schemas the app uses — no superuser bypass of `sysgraph`. | Grants scoped to `public` (DML on all tables + sequence usage). New integration test: `seshat_app` gets `InsufficientPrivilegeError` on `SELECT FROM sysgraph.proposal` (the real app connection now denied — the core proof). Positive test: `seshat_app` CAN read/write a `public` table. |
| AC-3 | Migrations needing `CREATE ROLE`/`CREATE SCHEMA`/superuser DDL run via a separate admin credential (or the app role never needs them). | App role never runs DDL: `create_all` is inert (checkfirst, models ⊆ init.sql tables) — proven by an integration test running `init_db()` as `seshat_app`. Migrations continue to be applied by the `agent` superuser (the separate admin credential, already present as `POSTGRES_USER`); documented in the 0015 header + `.env.example` + root `CLAUDE.md`. |
| AC-4 | `make test` + existing integration suites still pass with the restricted role. | Full suite run in-session; sysgraph integration suite (`test_isolation.py`, `test_repository.py`) run against the test stack with `seshat_app` as the app URL. |

## Key facts established (Step-2 recon)

- `agent` = Postgres bootstrap superuser (`POSTGRES_USER=agent`) in **all** stacks
  (`docker-compose{,.test,.eval,.cloud}.yml`). App connects as it via `AGENT_DATABASE_URL` → bypasses grants.
- App runtime uses only **`public`** schema objects:
  - SQLAlchemy models (`service/models.py`): `users, sessions, metrics, budget_policies,
    budget_counters, budget_reservations, artifacts, session_events, user_constraint_preferences,
    consolidation_attempts` — a strict subset of init.sql tables.
  - Raw asyncpg pools on `settings.database_url`: `route_trace/ledger.py` (route_traces),
    `llm_client/cost_tracker.py` (api_costs), `cost_gate` (budget_*), `second_brain/attempts.py`
    (consolidation_attempts), `observability/joinability/scheduler_runner.py`. All `public`, DML only.
- **`create_all` at startup** (`service/database.py:25`, called from `service/app.py:540` and
  `gateway/app.py:125`): with SQLAlchemy `checkfirst=True` and models ⊆ existing tables, it emits
  **zero DDL** → safe under a role with no CREATE. (No other runtime DDL exists in `src/`.)
- No code runs migrations — they are applied manually by the superuser (`psql` in file headers).
- `is_prod_postgres_url()` keys on **host+port only** (user-agnostic) → changing the URL's user is
  safe for the FRE-375 prod-substrate guard.
- All compose stacks mount `docker/postgres/init.sql` (runs only on an empty volume). Existing
  volumes (prod + the running test/eval volumes) need migration `0015` applied by the superuser.
- Precedent: `sysgraph_role`/`recall_role` use **hardcoded dev passwords** in init.sql; only the
  superuser `agent` carries the real `${POSTGRES_PASSWORD}` secret in prod. `seshat_app` follows the
  internal-role precedent (see the one open decision below).

## Design

Keep `agent` as the bootstrap/admin/migration superuser. Add a dedicated **non-superuser** login
role `seshat_app` that the app connects as, granted only `public`-schema DML + sequence usage +
`ALTER DEFAULT PRIVILEGES` so future `public` objects auto-grant. `sysgraph` stays inaccessible: no
`USAGE` grant on schema `sysgraph`, and `seshat_app` is not a superuser → `permission denied`.

**Two named DSNs (post-codex-review):** the "separate admin credential" the AC asks for becomes a
real, consumed setting — `AGENT_DATABASE_ADMIN_URL` (the `agent` superuser). The app + restricted
tests use `AGENT_DATABASE_URL` (`seshat_app`); every admin/DDL path (migration tests, migration
runbook) uses `AGENT_DATABASE_ADMIN_URL`.

## Codex plan-review deltas (2026-07-06 — incorporated)

- **[BLOCKING] Admin-DDL tests break under the restricted app DSN.** `tests/migrations/test_0004_*`,
  `test_0011_*`, `test_init_sql_model_parity.py` (runs full `init.sql`: CREATE ROLE/EXTENSION/SCHEMA +
  SET ROLE), and any other test doing `CREATE/DROP SCHEMA`/DDL via `settings.database_url`
  (candidates: `tests/integration/test_joinability_walk.py`, `tests/scripts/test_backfill_*`) must
  repoint to `settings.database_admin_url`. DML-only tests (cost_gate, second_brain/attempts,
  sysgraph/test_repository) stay on the app DSN. Verified empirically by full-suite green (AC-4).
- **[BLOCKING] Stale migration-runbook headers.** All 9 migration headers that say
  `psql $AGENT_DATABASE_URL` (0001,0003,0004,0005,0006,0007,0011,0013,0014) → `$AGENT_DATABASE_ADMIN_URL`,
  plus a central note in root `CLAUDE.md`. DDL migrations run as the superuser (AC-3).
- **[BLOCKING] Prod app-role password.** Codex flags a repo-known password on a role with DML over
  all user data (users/sessions/artifacts/cost) as a real exposure. → default recommendation flips to
  a dedicated **`SESHAT_APP_PASSWORD`** secret; init.sql ships the dev password, prod sets it via
  `ALTER ROLE seshat_app PASSWORD …` at deploy. Owner decision below.
- **[nice-to-have] pgvector proof.** Extend the positive test to exercise an `artifacts` INSERT with
  `CAST(:embedding AS vector)` + a vector-distance read (`notes_search`/`artifact_tools` path), proving
  `seshat_app` can use the `vector` type/operators.
- **[nice-to-have] Eval harnesses** (`scripts/eval/**`) hardcode `agent:agent_dev_password` — **out of
  scope**: admin/eval tooling legitimately keeps the superuser; they still connect (role unchanged) and
  do not exercise the restricted path. Noted, not changed.
- **Confirmed non-issues:** no app runtime op needs superuser/ownership (cost_gate `FOR UPDATE` is
  plain DML; no TRUNCATE/LISTEN/advisory-lock/COPY); `ALTER DEFAULT PRIVILEGES FOR ROLE agent` ordering
  is correct (block appended after `RESET ROLE`); `create_all` is inert *iff* models ⊆ existing tables
  (the restricted-role `init_db()` test is precisely the drift tripwire).

## Steps (TDD)

1. **Test first — isolation proof (fails until schema+config land).**
   - `tests/personal_agent/sysgraph/conftest.py`:
     - Repoint `agent_pool` to an explicit superuser DSN via `_role_dsn("agent", "agent_dev_password")`
       (settings.database_url is now `seshat_app`, so the fixture can no longer rely on it for the
       superuser handle).
     - Add `app_role_pool` fixture (asyncpg pool on `settings.database_url` = `seshat_app`).
   - `tests/personal_agent/sysgraph/test_isolation.py`: add
     - `test_app_role_denied_on_sysgraph` — `app_role_pool` → `InsufficientPrivilegeError` on
       `SELECT * FROM sysgraph.proposal` (AC-2, the core proof).
     - `test_app_role_is_not_superuser` — `SELECT rolsuper FROM pg_roles WHERE rolname='seshat_app'`
       is `false` (AC-1).
     - `test_app_role_can_use_public` — `app_role_pool` can `SELECT`/`INSERT`…`ROLLBACK` on a `public`
       table (AC-2 positive).
   - New `tests/personal_agent/service/test_init_db_restricted_role.py`:
     - `test_init_db_succeeds_as_app_role` — `await init_db()` (create_all as `seshat_app`) raises
       nothing (AC-3). `@pytest.mark.integration`.
   - Confirm they FAIL (role absent / app still connects as agent).

2. **Schema — `docker/postgres/init.sql`.** Append after the sysgraph block (`RESET ROLE`), as the
   bootstrap superuser:
   ```sql
   -- App role (non-superuser) — the live AGENT_DATABASE_URL connection (FRE-808).
   DO $$ BEGIN
     IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'seshat_app') THEN
       CREATE ROLE seshat_app LOGIN PASSWORD 'seshat_app_dev_password';
     END IF;
   END $$;
   GRANT CONNECT ON DATABASE personal_agent TO seshat_app;
   GRANT USAGE ON SCHEMA public TO seshat_app;
   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO seshat_app;
   GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO seshat_app;
   ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public
     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO seshat_app;
   ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public
     GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO seshat_app;
   -- No grant on schema sysgraph → seshat_app is denied there (AC-2).
   ```

3. **Migration — `docker/postgres/migrations/0015_app_role_grants.sql`.** Idempotent BEGIN/COMMIT
   mirror of Step 2 for existing DBs. Header: **run as the `agent` superuser (admin credential), not
   the app `AGENT_DATABASE_URL`** — states the migration-credential split (AC-3).

4. **Config — `src/personal_agent/config/settings.py`.** Change `database_url` default user
   `agent`→`seshat_app`, keep `@localhost:5432/personal_agent`; update its description. **Add**
   `database_admin_url` (`AGENT_DATABASE_ADMIN_URL`, default
   `postgresql+asyncpg://agent:agent_dev_password@localhost:5432/personal_agent`) — the superuser
   admin/migration credential (real consumers: migration tests + runbook).

5. **Compose app URLs (bootstrap `POSTGRES_USER` stays `agent`).**
   - `docker-compose.cloud.yml:305`, `docker-compose.eval.yml:155,187`: app `AGENT_DATABASE_URL`
     `agent:${POSTGRES_PASSWORD}` → `seshat_app:<app-password>` per the password decision below.
   - `docker-compose.yml` / `.test.yml`: no `AGENT_DATABASE_URL` set there (local reads `.env`; tests
     read conftest) → no change beyond a clarifying comment.

6. **`tests/conftest.py`.** `AGENT_DATABASE_URL` default → `seshat_app` (:5433); **add**
   `AGENT_DATABASE_ADMIN_URL` default → `agent` superuser (:5433). Repoint every admin/DDL test to
   `settings.database_admin_url` (migration parity/identity tests; audit `test_joinability_walk`,
   `test_backfill_*`). DML-only tests unchanged.

7. **Migration headers.** All 9 `psql $AGENT_DATABASE_URL` headers → `$AGENT_DATABASE_ADMIN_URL`.

8. **`.env.example`.** Update commented `AGENT_DATABASE_URL` → `seshat_app`; add
   `AGENT_DATABASE_ADMIN_URL` (agent superuser) with the migration-credential note (AC-3).

9. **Docs.** Root `CLAUDE.md` migration line + `0014` header one-liner: app-role vs admin-role split;
   DDL migrations run as the superuser.

## In-session provisioning (so `make test` passes)

The running test volume predates `seshat_app`. Before the suite: apply `0015` to the test DB as the
superuser (`psql postgresql://agent:agent_dev_password@localhost:5433/personal_agent -f
docker/postgres/migrations/0015_app_role_grants.sql`) **or** `make test-infra-reset` to rebuild the
volume from init.sql. (Same for the eval volume when evals next run — runbook item for master.)

## The one genuine decision → owner — RESOLVED 2026-07-06: dedicated secret (a)

Owner chose the dedicated `SESHAT_APP_PASSWORD` secret. init.sql ships `seshat_app_dev_password`
(dev); `docker-compose.cloud.yml` requires the secret (`${SESHAT_APP_PASSWORD:?…}`, mirroring
`POSTGRES_PASSWORD`); `docker-compose.eval.yml` falls back to the dev password
(`${SESHAT_APP_PASSWORD:-seshat_app_dev_password}`, internal stack). Runbook: master adds the secret
to prod `.env`, runs `ALTER ROLE seshat_app PASSWORD …` after migration 0015, then deploys.


**Prod `seshat_app` password.** (a) **New `SESHAT_APP_PASSWORD` secret** — init.sql ships the dev
password; master adds the secret to prod `.env`, runs `ALTER ROLE seshat_app PASSWORD …`, and points
prod `AGENT_DATABASE_URL` at it before deploy. **Recommended** (Codex security finding: a repo-known
password on a role with DML over all user data is a real exposure if the DB port is ever reachable).
(b) **Hardcoded internal** (`seshat_app_dev_password`, matching the `sysgraph_role`/`recall_role`
precedent — no new secret). Net security improves either way (app path drops superuser), but (a)
closes the residual app-role exposure. Recommend (a).

## Out of scope

- Wiring prod `AGENT_SYSGRAPH_DATABASE_URL` (still unset in cloud; FRE-715 territory).
- Retiring `recall_role` (harmless; its FRE-714 test still passes).
- Splitting per-subsystem app roles (single app role is sufficient).
