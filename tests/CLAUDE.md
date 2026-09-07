# Tests — substrate isolation (FRE-375)

**Policy** (stated in the root `CLAUDE.md`, restated here because this is where it bites): test and eval
scripts must never write to production substrate (Neo4j, Elasticsearch, Postgres, Captain's Log) without
explicit opt-in. Binds `tests/` and `scripts/eval/`.

**How it works:**
- `tests/conftest.py` sets `APP_ENV=test` and redirects substrate URIs to the test stack (Neo4j :7688, ES :9201, Postgres :5433) before any module import.
- `MemoryService.connect()` refuses to attach to prod-fingerprint URIs when `settings.environment == TEST`.
- `AppConfig` raises `ValidationError` at startup if `environment=TEST` and URIs match prod defaults.

**Running the test substrate:**
```bash
make test-infra-up    # start isolated Neo4j/ES/Postgres (test stack)
make test-infra-down  # stop
make test-infra-reset # stop + wipe volumes
```

**Escape hatch** (acceptance tests against prod-equivalent stack only):
```bash
AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE=1 make test
```

**Pre-commit enforcement:** `scripts/check_no_direct_substrate_in_tests.py` blocks new hardcoded prod URIs or bare `MemoryService()` instantiations in `tests/` and `scripts/eval/`. Use `# fre-375-allow: <reason>` on the specific line to exemption when intentional.

**Eval isolation:** `docker-compose.eval.yml` has its own `postgres-eval`, `neo4j-eval`,
`elasticsearch-eval`, `redis-eval` (FRE-1342) services with isolated volumes. Use
`make eval-infra-up` before running evals.

**Cross-arm isolation within one run (FRE-1372):** a shared eval Neo4j persists across an
eval run's arms, so one arm's entity extraction can leak into a later arm's `search_memory`
recall (FRE-1338's incident). Drive eval-gateway turns through
`scripts/eval/eval_isolation.py`'s `IsolatedArmRunner` instead of a bare HTTP POST — it
wipes and, if given a `reseed` coroutine, replays any fixture state before every turn, so
a new eval script inherits isolation without writing wipe/restore code of its own.
