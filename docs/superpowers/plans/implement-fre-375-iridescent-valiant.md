# FRE-375 — Isolate Test/Eval Scripts from Production Memory + Log Substrate

**Linear**: [FRE-375](https://linear.app/frenchforest/issue/FRE-375/isolate-testeval-scripts-from-production-memory-log-substrate) · Approved · Tier-1:Opus
**Blocks**: [FRE-374](https://linear.app/frenchforest/issue/FRE-374) (memory integration ADR)
**Branch**: `starry-plaza-1s/fre-375-isolate-testeval-scripts-from-production-memory-log`

---

## Context

Investigation of FRE-374 (memory integration) revealed that test/eval scripts share the same Neo4j/Elasticsearch/Captain's Log backend with production. Probe 6 over the last 7 days on the VPS shows **261 of 300 (87%)** `:Turn` nodes have `session_id: NULL` — synthetic queries from eval harnesses, not real Postgres sessions. Sample polluting prompts: *"Tell me about RareLanguage"*, *"Recent message about RecencyLang_…"*, *"Python question 0"*. The pollution poisons the entity graph (real entities like "Neo4j" get test-mode descriptions stamped onto them), evaluation baselines, Captain's Log reflections, and ES traces.

Two architectural facts make this worse than the surface count suggests:

1. **`memory/service.py:605` is an unconditional overwrite**: `e.description = $description` — *not* first-write-wins as the issue text guessed. Every test entity named "Python"/"Django"/"FastAPI" stomps the production entity's description on every run. (Probe 5's `KNOWN_DRIFTED_ENTITIES` finding is the visible symptom of this bug.)
2. **`docker-compose.eval.yml` shares prod volumes**: the "control + treatment" eval stack mounts `seshat_captures_cloud` / `seshat_workspace_cloud` and `depends_on` the same `postgres` / `neo4j` / `elasticsearch` services as the cloud gateway. Eval runs *cannot* be isolated by env-var alone today.

This issue blocks FRE-374's replay-from-Postgres value — any cleanup is immediately re-polluted by the next eval run.

---

## Acceptance Criteria

### Pre-merge (must pass before PR merges)

| # | Criterion | Verification |
|---|-----------|--------------|
| A1 | `MemoryService.connect()` refuses to attach to the prod Neo4j URI when `settings.environment == TEST` unless `settings.allow_test_writes_to_prod_substrate=True` | New unit test: `tests/personal_agent/memory/test_connect_environment_guard.py` |
| A2 | `AppConfig` `@model_validator` fails fast at startup when `environment == TEST` and substrate URIs match prod fingerprints (ports 7687/9200/5432 on localhost) | New unit test: `tests/personal_agent/config/test_environment_substrate_validator.py` |
| A3 | `e.description` write at `memory/service.py:605` is changed so existing entity descriptions are not silently overwritten (FRE-375 bundles this fix) | New unit test asserts a second `create_entity` with different description leaves the first description intact |
| A4 | ES `index_prefix` setting is honored at all 3 production write sites (`gateway/app.py:130`, `service/app.py:421`, `config/bootstrap.py:131`) and at the 2 Captain's Log constant sites (`capture.py:22`, `manager.py:26`) | New unit test asserts ES writes from a `TEST`-configured singleton land on `agent-logs-test-*` and `agent-captains-*-test` indices |
| A5 | `docker-compose.test.yml` overlay adds isolated `postgres-test` (5433), `elasticsearch-test` (9201), `neo4j-test` (7688) with their own volumes; the existing default stack is unchanged | `make test-infra-up` brings up the sidecar stack; `docker compose ps` shows 3 new containers |
| A6 | `docker-compose.eval.yml` is rewired to its own dedicated `neo4j-eval` / `elasticsearch-eval` / `postgres-eval` services with their own volumes, no longer sharing prod | `make eval-infra-up` brings up an isolated stack; running an eval and then `cypher-shell` against prod Neo4j shows zero new test-shaped Turn nodes |
| A7 | Root `tests/conftest.py` adds an autouse fixture that flips `settings.environment` to `TEST` and points URIs at the test stack for any test that touches a substrate | `make test` and `make test-integration` pass against the test stack with no writes to prod Neo4j |
| A8 | `tests/test_memory/test_graph_structure.py` and other named-entity-seeding tests are migrated to UUID-namespaced entity names on the test Neo4j | Tests pass against test stack; grep finds no remaining hard-coded "Python"/"Django"/"FastAPI" entity seeds in `tests/` |
| A9 | Hardcoded creds removed from `scripts/eval_03_memory_promotion.py:40-42` and `tests/manual/cleanup_graph_noise.py:25-27` — both read from settings | grep for `neo4j_dev_password` in `scripts/` and `tests/` returns zero hits |
| A10 | Backfill/migration scripts (`scripts/migrate_*.py`, `scripts/backfill_*.py`) require explicit `--confirm-prod` flag when `settings.environment != TEST` | Running without flag exits with `EXIT 2` and a clear error |
| A11 | `scripts/check_no_direct_substrate_in_tests.py` pre-commit hook blocks new `MemoryService()` / raw `AsyncGraphDatabase.driver` / direct `:9200` `:5432` literals in `tests/` and `scripts/eval/` (with allowlist for read-only research probes) | `pre-commit run --all-files` passes; an intentional violation test confirms the hook fires |
| A12 | `make test` (unit), `make test-integration` (integration), `make mypy`, `make ruff-check` all pass | Run all four locally; attach output to PR |
| A13 | CLAUDE.md updated with the "tests must never write to production substrate" policy, naming the env-var / settings field that gates this | grep for `AGENT_ENVIRONMENT` in CLAUDE.md returns a non-empty match in the new section |

### Post-deploy (run in the same session as deploy)

| # | Criterion | Verification |
|---|-----------|--------------|
| P1 | Re-run Probe 6 against prod Neo4j; record `test_turns` count over the prior 7-day window | `uv run python scripts/research/memory_integration_probe/probe_6_recent_production_only.py` → output md file |
| P2 | After running the test suite **against the test stack with the guard active**, re-run Probe 6 again; the rolling-7-day `test_turns` count must not increase | Numeric diff in output md |
| P3 | After running `tests/evaluation/run_primitive_tools_eval.py` against the *rewired* eval stack, no new turns appear in prod Neo4j with `session_id IS NULL` | `cypher-shell` count query before/after |
| P4 | MASTER_PLAN.md updated with FRE-375 verdict + link to PR | git diff shows the update |

### Future gates (tracked, not blocking)

- **FRE-374 unblocking**: replay-from-Postgres path can now run against a clean Neo4j.
- **Adjacent**: tighten `TurnNode.session_id` to required (currently `str | None`) — needs schema audit and out of scope here.
- **Adjacent**: split eval-stack-cleanup logic out of `scripts/cleanup_eval_data.py` once it no longer has anything to clean.

---

## Approach

The intervention is a stack of seven layers, each enforcing the previous one:

```
Layer 1: AppConfig.@model_validator                 ← fails fast at startup
Layer 2: MemoryService.connect() environment guard  ← refuses bad writes at the chokepoint
Layer 3: Settings-driven ES + Captain's Log prefixes ← test indices are isolated by name
Layer 4: docker-compose.test.yml overlay            ← physical sidecar substrate
Layer 5: Autouse conftest fixture                   ← every test runs against the sidecar
Layer 6: docker-compose.eval.yml rewire             ← eval stack gets its own substrate
Layer 7: pre-commit grep guard                      ← catches new violations at commit time
```

Plus two adjacent fixes that ride along:

```
+ memory/service.py:605 unconditional-overwrite fix
+ Hardcoded creds removed from eval scripts; backfill scripts gated by --confirm-prod
```

### Layer 1 — `AppConfig` model validator

`src/personal_agent/config/settings.py`

Add `allow_test_writes_to_prod_substrate: bool = Field(default=False)` and a `@model_validator(mode="after")` that raises `ValueError` when:

```
environment == Environment.TEST
AND any URI in (neo4j_uri, elasticsearch_url, database_url) matches the prod fingerprint
AND allow_test_writes_to_prod_substrate is False
```

Prod fingerprint = the default URIs (`bolt://localhost:7687`, `http://localhost:9200`, `postgresql+asyncpg://agent:.../personal_agent` on `localhost:5432`). The fail-fast message tells the caller exactly what env-vars to set to fix it.

The validator is also a no-op for `Environment.PROD` and `Environment.DEV` — it only fires when `TEST` is misconfigured. This makes prod startup risk-free.

### Layer 2 — `MemoryService.connect()` environment guard

`src/personal_agent/memory/service.py:97-121`

After resolving the URI, before calling `Neo4jAsyncGraphDatabase.driver(...)`:

```python
if settings.environment == Environment.TEST and _is_prod_neo4j_uri(uri):
    if not settings.allow_test_writes_to_prod_substrate:
        log.error(
            "memory_service_refused_prod_uri_in_test_env",
            uri=uri,
            remediation="Set AGENT_NEO4J_URI=bolt://localhost:7688 (test stack) "
                       "or AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE=1 (escape hatch).",
        )
        self.connected = False
        return False
```

`_is_prod_neo4j_uri()` lives in a new `src/personal_agent/memory/_substrate_fingerprint.py` module so the same fingerprint logic can be reused by the config validator and by future ES/Postgres guards.

### Layer 3 — Settings-driven ES + Captain's Log prefixes

The audit found three sites that ignore `settings.elasticsearch_index_prefix` and pass nothing (so the constructor default `"agent-logs"` always wins):

- `src/personal_agent/gateway/app.py:130` — `ElasticsearchHandler(settings.elasticsearch_url)`
- `src/personal_agent/service/app.py:421` — same
- `src/personal_agent/config/bootstrap.py:131` — `ElasticsearchLogger(es_url=...)`

Fix: pass `index_prefix=settings.elasticsearch_index_prefix` at all three. With this fixed, setting `AGENT_ELASTICSEARCH_INDEX_PREFIX=agent-logs-test` on the test stack lands all logs in test indices.

Captain's Log has two hardcoded constants:

- `src/personal_agent/captains_log/capture.py:22` — `"agent-captains-captures"`
- `src/personal_agent/captains_log/manager.py:26` — `"agent-captains-reflections"`

Fix: introduce `settings.captains_log_index_prefix` (default `"agent-captains"`) and derive both names from it. Test stack uses `agent-captains-test`.

### Layer 4 — `docker-compose.test.yml` overlay

New file, overlays the default stack. Adds three services:

- `postgres-test` on host port `5433` → container `5432`, volume `postgres_test_data`, fresh init.sql
- `elasticsearch-test` on host port `9201` → container `9200`, volume `es_test_data`, single-node
- `neo4j-test` on host port `7688` → container `7687`, volume `neo4j_test_data`, same APOC plugin

Plus `Makefile` targets `test-infra-up`, `test-infra-down`, `test-infra-reset`. The default `make up` is unchanged.

### Layer 5 — Autouse conftest fixture

`tests/conftest.py` gains an autouse session-scoped fixture that, when any test imports or invokes a substrate path:

1. Sets `os.environ["APP_ENV"] = "test"` before `settings` is loaded.
2. Sets `os.environ["AGENT_NEO4J_URI"] = "bolt://localhost:7688"`, ES → `:9201`, Postgres → `:5433`.
3. Sets `AGENT_ELASTICSEARCH_INDEX_PREFIX=agent-logs-test` and `AGENT_CAPTAINS_LOG_INDEX_PREFIX=agent-captains-test`.
4. Asserts at session start that the test Neo4j is reachable; otherwise skips substrate-touching tests with a clear reason.

Tests that are pure-mock are unaffected. Tests that touch substrate now hit the sidecar.

### Layer 6 — `docker-compose.eval.yml` rewire

Replace the `depends_on: postgres/neo4j/elasticsearch` of the existing services (`docker-compose.eval.yml:27-41`) with their `*-eval` counterparts. Replace the shared `seshat_captures_cloud` / `seshat_workspace_cloud` volumes (lines 43-45) with `seshat_captures_eval` / `seshat_workspace_eval`. Add the three new services to the same file (or a sibling `docker-compose.eval-infra.yml`).

Update `tests/evaluation/run_primitive_tools_eval.py` and other eval-stack consumers to point at the new ports. The eval stack remains independently runnable.

### Layer 7 — Pre-commit grep guard

`scripts/check_no_direct_substrate_in_tests.py`, modeled exactly on `scripts/check_no_personal_paths.py`:

- Scans `git ls-files` for `.py` files under `tests/`, `scripts/eval/`, `scripts/research/` (excluding `scripts/research/memory_integration_probe/_common.py` which is read-only).
- Patterns to flag:
  - `MemoryService\s*\(\s*\)` followed within 10 lines by `\.connect\(`
  - `AsyncGraphDatabase\.driver\(` or `GraphDatabase\.driver\(`
  - `r?["\']bolt://localhost:7687`
  - `r?["\']http://localhost:9200`
  - `r?["\']postgresql.*localhost:5432`
  - `neo4j_dev_password`
- Allowlist via a marker comment: `# fre-375-allow: <reason>`.
- New `.pre-commit-config.yaml` hook entry mirroring the existing `check-no-personal-paths` hook.

### Adjacent fix A — `memory/service.py:605` unconditional overwrite

Change the SET clause in `create_entity()`:

```cypher
# Before:
e.description = $description,
e.entity_type = $entity_type,
e.properties = $properties,

# After:
e.description = CASE
    WHEN $description IS NULL OR $description = "" THEN e.description
    WHEN e.description IS NULL OR e.description = "" THEN $description
    ELSE e.description  // first-write-wins on description (FRE-375)
END,
e.entity_type = COALESCE(e.entity_type, $entity_type),
e.properties = CASE
    WHEN e.properties IS NULL OR e.properties = "{}" THEN $properties
    ELSE e.properties
END,
```

`mention_count`, `last_seen`, `last_accessed_at` still update on every call (those are *meant* to mutate). The change is *only* about description/type/properties: identity fields, not telemetry fields.

Add unit test: write entity "Python" with description "real description"; write again with description "test description"; assert MATCH returns "real description".

### Adjacent fix B — Hardcoded creds + backfill `--confirm-prod`

- `scripts/eval_03_memory_promotion.py:40-42` — replace hardcoded `NEO4J_URI` / `NEO4J_PASSWORD` with `from personal_agent.config import settings; settings.neo4j_uri`.
- `tests/manual/cleanup_graph_noise.py:25-27` — same.
- `scripts/migrate_fre229_visibility_backfill.py`, `scripts/backfill_participated_in.py`, `scripts/migrate_fre268_add_user_identity.py` — add `--confirm-prod` CLI flag; refuse to run when `settings.environment != "prod"` unless the flag is passed.

---

## Files to Modify

### New files

| Path | Purpose |
|------|---------|
| `src/personal_agent/memory/_substrate_fingerprint.py` | Shared prod-URI fingerprint detection |
| `docker-compose.test.yml` | Sidecar test substrate (Neo4j/ES/Postgres) |
| `tests/personal_agent/memory/test_connect_environment_guard.py` | Layer-2 unit test |
| `tests/personal_agent/config/test_environment_substrate_validator.py` | Layer-1 unit test |
| `tests/personal_agent/memory/test_entity_description_first_write_wins.py` | 605-fix unit test |
| `scripts/check_no_direct_substrate_in_tests.py` | Pre-commit grep guard |
| `docs/architecture_decisions/ADR-XXXX-test-prod-substrate-isolation.md` | ADR documenting the design |

### Modified files

| Path | Change |
|------|--------|
| `src/personal_agent/config/settings.py:30-46` | Add `allow_test_writes_to_prod_substrate` + `captains_log_index_prefix`; add `@model_validator` |
| `src/personal_agent/memory/service.py:97-121` | Add environment guard inside `connect()` |
| `src/personal_agent/memory/service.py:602-607` | Apply 605-fix CASE clauses |
| `src/personal_agent/gateway/app.py:130` | Pass `index_prefix` to `ElasticsearchHandler` |
| `src/personal_agent/service/app.py:421` | Same |
| `src/personal_agent/config/bootstrap.py:131` | Same |
| `src/personal_agent/captains_log/capture.py:22` | Read prefix from settings |
| `src/personal_agent/captains_log/manager.py:26` | Same |
| `tests/conftest.py` | Add autouse fixture for test-stack URIs |
| `docker-compose.eval.yml` | Rewire to dedicated `*-eval` services + volumes |
| `tests/test_memory/test_graph_structure.py` | Migrate named entities to UUID-namespaced |
| `tests/test_memory/test_memory_service.py` fixtures | Migrate to test stack |
| `tests/test_memory/test_relevance_scoring.py` | Migrate to test stack |
| `tests/manual/test_memory_graph_integration.py` | Migrate to test stack |
| `tests/manual/cleanup_graph_noise.py:25-27` | Read from settings |
| `scripts/eval_03_memory_promotion.py:40-42` | Read from settings |
| `scripts/migrate_*.py` (3 files) | Add `--confirm-prod` flag |
| `scripts/backfill_participated_in.py` | Add `--confirm-prod` flag |
| `Makefile` | Add `test-infra-up/-down/-reset`, `eval-infra-up/-down/-reset` targets |
| `.pre-commit-config.yaml` | Add `check-no-direct-substrate-in-tests` hook |
| `CLAUDE.md` (root) | Add policy section: "Tests never write to production substrate" |
| `.claude/CLAUDE.md` | Cross-reference the new policy |
| `docs/plans/MASTER_PLAN.md` | Update with FRE-375 verdict + PR link (post-deploy) |

### Reused — do not modify

- `scripts/check_no_personal_paths.py` — template for the new hook (read-only reference).
- `src/personal_agent/config/env_loader.py:22-52` — already provides `Environment.TEST`; we reuse it.
- `scripts/research/memory_integration_probe/probe_6_recent_production_only.py` — already provides the acceptance gate; we don't need to modify it.

---

## Verification

### Local

```bash
# 1. Pre-merge gates
make test                                  # unit tests pass with autouse fixture
make test-infra-up                         # bring up sidecar stack
make test-integration                      # integration tests pass against sidecar
make mypy && make ruff-check && make ruff-format
pre-commit run --all-files                 # new hook is green; intentional violation in scratch file fails it

# 2. Verify guard fires when env is misconfigured
APP_ENV=test uv run python -c "from personal_agent.memory import MemoryService; import asyncio; \
  m = MemoryService(); print(asyncio.run(m.connect()))"
# Expected: log line "memory_service_refused_prod_uri_in_test_env", return False

# 3. Re-run Probe 6 baseline (before any test runs)
uv run python scripts/research/memory_integration_probe/probe_6_recent_production_only.py
# Record test_turns count in output md

# 4. Run a previously-offending test against test stack
make test-file FILE=tests/test_memory/test_graph_structure.py
# Expected: passes; Neo4j on :7687 (prod) unchanged; Neo4j on :7688 (test) has new nodes

# 5. Re-run Probe 6 — count must not increase
uv run python scripts/research/memory_integration_probe/probe_6_recent_production_only.py
```

### VPS (post-deploy)

```bash
# 1. SSH to VPS
ssh seshat                                 # uses ~/.ssh/config alias
cd /opt/seshat

# 2. Pull + rebuild
git pull && make build

# 3. Bring up sidecar eval infra
make eval-infra-up

# 4. Run a control-stack eval
docker compose -f docker-compose.cloud.yml -f docker-compose.eval.yml up -d \
    seshat-gateway-control seshat-gateway-treatment
uv run python tests/evaluation/run_primitive_tools_eval.py --gateway http://localhost:9002

# 5. Confirm prod Neo4j is untouched
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
  "MATCH (t:Turn) WHERE t.timestamp >= datetime() - duration('PT5M') AND t.session_id IS NULL \
   RETURN count(t)"
# Expected: 0

# 6. Update MASTER_PLAN.md, commit + push
```

### Acceptance gate (Probe 6 numeric)

Pass criterion: after the rewired eval stack runs an eval, the `test_turns` count in Probe 6's output for the last 7 days does *not* increase between baseline and post-run measurements. (The historical count won't drop to zero until FRE-374's replay-from-Postgres lands — that's explicitly out of scope here.)

---

## Out of Scope

- **Historical cleanup of existing pollution** — handled by FRE-374's replay-from-Postgres path, unblocked by this issue.
- **Tightening `TurnNode.session_id` to required** — schema audit needed; separate effort.
- **Extractor quality improvements** — separate concern downstream of model upgrade.
- **PWA/UI changes** — none required.

---

## Risk + Rollback

- **Risk**: Layer-4/6 docker-compose changes touch the local dev stack. Mitigation: the existing `docker-compose.yml` is unchanged; the test/eval stacks are additive overlays.
- **Risk**: Layer-5 conftest fixture could surprise pure-mock tests that don't expect env vars to be set. Mitigation: only set env vars; do not patch `settings` directly. Pure-mock tests already patch what they need.
- **Risk**: 605-fix could prevent legitimate entity description updates (e.g. a new fact about an entity). Mitigation: this is *intended*. Consolidation/promotion of new facts goes through `EntityFactor` / `promote_entity()`, not via raw `create_entity` re-call. Add explicit `update_entity_description()` method if a real use case surfaces.
- **Rollback**: each layer is independent. The hardest to roll back is Layer-6 (compose file restructure). Tag the pre-change compose file; document `git revert` path in the PR.
