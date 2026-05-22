# ADR-0072: Test/Eval Substrate Isolation

**Status:** Accepted
**Date:** 2026-05-22
**Deciders:** Project owner
**Issue:** FRE-375
**Supersedes:** —
**Superseded by:** —
**Related:** ADR-0068 (Agent Self-Telemetry Data Plane), ADR-0065 (Cost Check Gate), ADR-0060 (Knowledge Graph Quality Stream)

---

## Context

Production Neo4j accumulated 261/300 (87%) synthetic `:Turn` nodes from test and eval
scripts in a 7-day window. These nodes had `session_id: NULL` — they came from eval
harnesses running fake prompts through the gateway or directly instantiating
`MemoryService`. The pollution affected entity descriptions (the real "Neo4j" entity
had its description overwritten by a synthetic extraction), evaluation baselines, and
Captain's Log reflections.

Root causes:

1. No environment check in `MemoryService.connect()` — test scripts resolved to the
   same URI as production.
2. `memory/service.py:605` applied `e.description = $description` as an unconditional
   overwrite on every entity write, so synthetic descriptions from tests silently
   stomped real ones.
3. `docker-compose.eval.yml` shared prod volumes (`seshat_captures_cloud`,
   `seshat_workspace_cloud`) and depended on the same PostgreSQL, Neo4j, and
   Elasticsearch services as the cloud gateway.
4. Several eval/test scripts hardcoded prod credentials instead of reading from
   `settings`.

---

## Decision

Seven layers of defence, applied at different points in the stack, so that any single
failure cannot reach production substrate.

### Layer 1 — `AppConfig` startup validator

`AppConfig._validate_substrate_isolation` (Pydantic `@model_validator`) raises
`ValidationError` at config load time when `environment=TEST` and any of
`neo4j_uri`, `elasticsearch_url`, or `database_url` matches the prod fingerprint
(localhost on canonical port). Escape hatch: `AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE=1`.

### Layer 2 — `MemoryService.connect()` runtime guard

Before constructing the Neo4j driver, `connect()` checks `settings.environment ==
TEST and is_prod_neo4j_uri(uri) and not settings.allow_test_writes_to_prod_substrate`.
If true, it logs `memory_service_refused_prod_uri_in_test_env` (with `uri_host` and
`uri_port`, no credentials) and returns `False` without connecting.

### Layer 3 — Settings-driven ES + Captain's Log index prefixes

`elasticsearch_index_prefix` was already a settings field but was not passed to
`ElasticsearchHandler` at three call sites. Fixed. `captains_log_index_prefix` (new
field, default `"agent-captains"`) now drives `CAPTURES_INDEX_PREFIX` and
`REFLECTIONS_INDEX_PREFIX` in `capture.py` and `manager.py`. When `APP_ENV=test`,
all log writes land in `agent-logs-test-*` and `agent-captains-test-*` indices.

### Layer 4 — Test substrate (`docker-compose.test.yml`)

New overlay adds `postgres-test` (:5433), `elasticsearch-test` (:9201), `neo4j-test`
(:7688) with isolated named volumes. The default `docker-compose.yml` is unchanged.
`make test-infra-up / test-infra-down / test-infra-reset` manage the test stack.

### Layer 5 — `tests/conftest.py` env bootstrap

Module-level `os.environ.setdefault()` calls at the top of `tests/conftest.py` set
`APP_ENV=test` and point all substrate URIs at the test stack before any module is
imported by pytest. This ensures module-level `settings = get_settings()` calls
(e.g., `memory/service.py:46`) resolve to test configuration.

### Layer 6 — Eval stack rewire (`docker-compose.eval.yml`)

The eval gateway containers now depend on isolated `postgres-eval` / `neo4j-eval` /
`elasticsearch-eval` services with their own volumes rather than sharing the cloud
production stack. `make eval-infra-up` starts the full isolated eval environment.

### Layer 7 — Pre-commit grep guard

`scripts/check_no_direct_substrate_in_tests.py` (modeled on
`scripts/check_no_personal_paths.py`) scans `.py` files under `tests/`,
`scripts/eval/`, and `scripts/research/` for hardcoded prod URIs, raw Neo4j driver
construction, and bare `MemoryService()` instantiation. Files or lines with
`# fre-375-allow: <reason>` are exempted.

### Adjacent fix — `memory/service.py:605` first-write-wins

The entity description SET clause was changed from `e.description = $description`
(unconditional overwrite) to a Cypher `CASE WHEN` that sets the description only when
the node has no existing description. `entity_type` and `properties` received the same
treatment. Telemetry fields (`last_seen`, `mention_count`) remain unconditional.

---

## Consequences

### Positive

- Probe 6 should show `test_turns ≈ 0` for new Turn nodes after this change lands.
- Evaluation baselines are now isolated from synthetic data.
- Entity descriptions are stable across multiple create calls.
- Pre-commit hook prevents regression.

### Tradeoffs

- Running the full integration test suite now requires `make test-infra-up` first.
  Pure unit tests (the majority) continue to run without infra via mock patterns.
- `entity_type` and `properties` are now first-write-wins too. Legitimate corrections
  to entity metadata must go through `promote_entity()` or a new dedicated
  `update_entity_*` method, not through `create_entity()`.

---

## Alternatives Considered

### A. Dry-run flag on extractor/consolidator

More surgical, but doesn't prevent gateway-driven pollution from eval scripts that
POST to `/chat`. *Rejected* — does not address the root cause.

### B. Namespaced Neo4j labels (`:TestEntity` etc.)

Doesn't prevent the description overwrite bug; complicates graph queries.
*Rejected* — addresses labeling but not isolation.

### C. Single-instance with test prefixes only (no sidecar)

Simpler but still allows synthetic Turn nodes to accumulate in the same graph.
*Rejected* — insufficient isolation for eval harnesses that generate hundreds of nodes.

---

## Verification

| AC | What |
|---|---|
| **AC-1** | `AppConfig` with `environment=TEST` and prod Neo4j URI raises `ValidationError`. Unit test in `tests/personal_agent/config/test_app_config.py`. |
| **AC-2** | `MemoryService.connect()` returns `False` and logs `memory_service_refused_prod_uri_in_test_env` when `APP_ENV=test` and URI is prod. Unit test in `tests/personal_agent/memory/test_service.py`. |
| **AC-3** | Two successive `create_entity()` calls with different descriptions leave the first description in place. Unit test asserting first-write-wins. |
| **AC-4** | `scripts/check_no_direct_substrate_in_tests.py` exits non-zero when a test file contains a bare prod URI; exits zero after adding `# fre-375-allow:` exemption. |
| **AC-5** | `make eval-infra-up` starts three isolated containers on eval ports; no shared volumes with cloud stack. Manual verification via `docker volume ls`. |
| **AC-6** | A full `make test` run with `APP_ENV=test` produces zero new `:Turn` nodes in prod Neo4j. Verified via Probe 6 query before/after. |

---

## Related

- **ADR-0068** — Agent Self-Telemetry Data Plane (ES index naming convention extended here)
- **ADR-0065** — Cost Check Gate (same escape-hatch pattern as `AGENT_ALLOW_*` env vars)
- **ADR-0060** — Knowledge Graph Quality Stream (detected the symptom this ADR fixes)
- **FRE-375** — implementation issue
