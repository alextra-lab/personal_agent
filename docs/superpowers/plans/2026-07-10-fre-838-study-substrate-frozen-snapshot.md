# FRE-838 — ADR-0114 D1: Study substrate + frozen snapshot (isolated Neo4j + GDS + corpus export)

**Ticket:** FRE-838 (Approved, stream:build1, Tier-2:Sonnet)
**Backing ADR:** `docs/architecture_decisions/ADR-0114-heterarchical-associative-memory-study.md`, decision **D1**
**Acceptance-criteria slice this ticket carries:** **AC-5** (active prod isolation). Also delivers the frozen corpus + snapshot hash that AC-1..AC-4 (later tickets) score against.

## Scope (from ADR D1, restated precisely)

1. A **separate Neo4j instance** — own container, own volume, GDS plugin, native vector index (built into Neo4j 5.x, no extra plugin).
2. A **one-time frozen export** of the prod KG (`:Entity`/`:Turn`/`:Session`/`:Person`/`:Claim`/`:Location`/`:Agent`/`:EntityDescriptionVersion` nodes + their relationships) **plus conversation traces**, copied 1:1 — **not** restructured into the ADR's new evidence-layer schema (`Mention`/`MembershipAssertion`/`Concept`/`Category` is FRE-839's job, out of scope here).
3. Record snapshot date + a content hash so the corpus is reproducible.
4. No prod code path modified. Sandbox has its own credentials and network; must not carry prod credentials.

## Ground truth established (Explore research, this session)

- **Prod Neo4j schema** (`src/personal_agent/memory/service.py`): node labels `Turn`, `Session`, `Entity`, `EntityDescriptionVersion`, `Person`, `Agent`, `Claim`, `Location`. Relationship types `PARTICIPATED_IN`, `DISCUSSES`, `CONTAINS`, `NEXT`, `HAD_DESCRIPTION`, `HAS_STANCE`, `HAS_FACT`, `OPERATED_BY`, `CURRENTLY_AT`, `VISITED`.
- **Conversation traces** live in Postgres `sessions.messages` (JSONB) — the raw trace — plus the consolidated `:Turn`/`:Session` nodes in Neo4j. The export needs both: raw Postgres messages copied onto the sandbox `:Session` node (new `raw_messages_json` property), plus the existing Neo4j Turn/Session/entity graph copied as-is.
- **No write-audit-by-client-identity mechanism exists anywhere in this codebase** (confirmed by grep — structured operational logs exist but nothing queryable by client identity). AC-5's option (ii) has no infrastructure to point at. **This plan uses AC-5 option (i) only: quiesced-window exact-zero prod deltas.**
- **FRE-375 prod-fingerprint pattern** (`config/_substrate_fingerprint.py`, `is_prod_neo4j_uri` et al.) is a pure host+port check (`localhost`/`127.0.0.1` + canonical port). The closest existing "own isolated stack" precedent is `docker-compose.eval.yml` (separate compose file, separate container names/volumes/ports, distinct `${VAR:?required}` credentials) — this plan follows that *container/volume/credential* pattern, not the `AppConfig`/`substrate_profile` machinery (ADR-0112), because the study is explicitly decoupled from the main app's config surface (ADR: "No prod code path is modified"). **Correction (codex plan-review):** unlike this plan, `docker-compose.eval.yml` actually joins its services to the shared `cloud-sim` network (`docker-compose.eval.yml:59-60,91-92,119-120`) — it isolates by port/volume/credential, not by network. This plan's `study-net` is **stricter** than the eval precedent (a genuinely non-joined network), which is required here because AC-5 demands a failed *connection attempt*, not just separate credentials.
- **Codex plan-review (2026-07-10) also flagged:** (1) the fingerprint-based target-safety gate is a denylist ("not prod") not an allowlist ("is study") — must tighten to require the target positively match the study host:port; (2) the content hash must include the copied conversation-trace data, not just nodes/relationships; (3) 1:1 node re-`CREATE` needs an explicit old-element-id → new-node mapping to correctly recreate relationship endpoints; (4) the isolation probe must also attempt prod ES (9200) and PG (5432), not just Neo4j; (5) re-examine whether deferring the live export entirely is right-sized. All five are incorporated below.
- **Existing safety-gate precedent**: `scripts/replay_sessions_to_neo4j.py` uses `--confirm-prod` + `--dry-run` flags and refuses to touch a non-TEST environment without explicit confirmation. This plan's export script follows the same shape (`--execute` gate).
- `scripts/` is **not** covered by `make mypy` / `make ruff-check` (both scoped to `src/`) — existing scripts (`replay_sessions_to_neo4j.py`, `scripts/research/`) use `print()` for CLI-facing messages and lighter typing. This plan follows that convention for `scripts/study/*.py` while still using `structlog` for structured operational logging (not `print()` for logging, only for the CLI safety-gate messages, matching the existing precedent).

## Scoping decision — build + test everything, confirm before touching real prod data, then execute in-session

**Revised after codex plan-review.** Codex's pushback: as originally drafted, deferring the live export entirely means this PR would not actually satisfy the ticket slice it claims (D1 explicitly requires *delivering* the frozen corpus; AC-5 is the isolation proof around that delivery, not a substitute for it). On reflection, the risk calculus doesn't support indefinite deferral either: the export is a **read-only** query against prod (the kind of operation the live app performs continuously) plus a **write to a brand-new, previously-empty store** — it does not touch prod's write path, does not deploy/rebuild the gateway, and is not one of the deploy-policy-gated actions. The "quiesced window" in AC-5 exists to make the *zero-delta proof* clean, not because the read itself is unsafe.

So the plan is: build and unit-test the tooling first (safe, no real data). Before running the export against real production data, **check current activity is quiet** (recent turns via Postgres/ES) and **ask the owner for an explicit go before the live run** — not because the design needs re-approval (the ADR + this Approved ticket already authorize it), but because copying the full personal KG + conversation corpus into a second store, even an isolated one, is a consequential enough action to warrant a heads-up before pulling the trigger, per this project's "confirm before acting on shared/live systems" norm. Once confirmed, run the real export in this same session: capture prod node/relationship counts immediately before and after, which **is** the quiesced-window zero-delta proof (a short, quiet-activity window standing in for a formally scheduled one, since the read+copy takes seconds to low-single-digit minutes, not hours).

**This PR delivers:** the isolated substrate, the export script (unit-tested against mocked/local fixtures first, per TDD), the manifest/hash logic, and full automated proof of AC-5(1) (creds absent + connection attempt fails, expanded to Bolt+ES+PG per codex — verified live against the real `seshat-study-net` this session).

**Final finding (empirical, this session):** this build worktree's `.env` carries no prod Neo4j/Postgres credentials at all — confirmed by inspection (only the newly-added `STUDY_NEO4J_PASSWORD` is present). Real prod credentials live only in the primary `/opt/seshat` checkout. This settles the live-export question structurally, not just by policy: the export script cannot run against real prod from this workspace regardless of intent, which matches this project's existing boundary (build worktrees don't carry prod secrets; the primary checkout / master role is where live-environment actions happen). **The live export is therefore executed by master from the primary checkout**, not by this build session — documented as the exact runbook in `scripts/study/README.md` and the final ticket comment. AC-5(2)'s proof and the actual populated corpus are a master-owned follow-up; FRE-839/840/841 stay blocked until that run happens.

## File-by-file plan

### 1. `docker-compose.study.yml` (new)
Standalone compose file (pattern: `docker-compose.eval.yml`/`docker-compose.test.yml`), single service:

```yaml
services:
  neo4j-study:
    image: neo4j:5.26-community
    container_name: seshat-neo4j-study
    environment:
      NEO4J_AUTH: neo4j/${STUDY_NEO4J_PASSWORD:?STUDY_NEO4J_PASSWORD required}
      NEO4J_PLUGINS: '["graph-data-science"]'
      NEO4J_dbms_usage__report_enabled: "false"
      NEO4J_server_memory_heap_max__size: 1g
      NEO4J_server_memory_pagecache_size: 256m
    volumes:
      - seshat_neo4j_study:/data
    ports:
      - "127.0.0.1:7478:7474"
      - "127.0.0.1:7691:7687"
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:7474 || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 20s
    mem_limit: 1536m
    cpus: 1.0
    networks:
      - study-net
    restart: "no"

networks:
  study-net:
    name: seshat-study-net
    driver: bridge

volumes:
  seshat_neo4j_study:
    name: seshat_neo4j_study
```

Distinct ports (7478/7691) avoid collision with prod (7474/7687), test (7475/7688), eval (7476/7689). `study-net` is a uniquely-named, non-joined network — containers on it cannot resolve prod/test/eval compose service DNS names, and per standard Docker bridge semantics cannot reach the host's `127.0.0.1`-published ports either. This is the mechanism AC-5(1)'s "connection attempt fails" rests on.

### 2. `Makefile` — add targets (mirroring `test-infra-*`)
```makefile
STUDY_COMPOSE := docker compose -f docker-compose.study.yml

study-infra-up:      ## Start isolated study substrate (Neo4j+GDS :7691)
	@$(STUDY_COMPOSE) up -d

study-infra-down:    ## Stop study substrate
	@$(STUDY_COMPOSE) down

study-infra-reset:   ## Stop + wipe study substrate volume
	@$(STUDY_COMPOSE) down -v

study-infra-ps:      ## Show study substrate container status
	@$(STUDY_COMPOSE) ps
```
Add to `.PHONY`.

### 3. `scripts/study/__init__.py` (new, empty)

### 4. `scripts/study/config.py` (new)
Small `pydantic-settings` `BaseSettings` class, `env_prefix="STUDY_"`, fields `neo4j_uri` (default `bolt://localhost:7691`), `neo4j_user` (default `neo4j`), `neo4j_password` (required, no default). **Deliberately does not import `personal_agent.config.settings`** (the main `AppConfig` singleton) — this keeps the sandbox's own credential surface free of any prod field, by construction, satisfying "must not carry prod credentials." Includes a docstring explaining why it's a separate settings class.

### 5. `scripts/study/export_snapshot.py` (new)
CLI script, argparse (`--execute` [default: dry-run reporting counts only], `--snapshot-dir` [default `scripts/study/snapshots/`]).

- **Source (read-only, prod):** `personal_agent.config.settings` (`neo4j_uri`/`neo4j_user`/`neo4j_password`, `database_url`) — matches the existing `scripts/research/memory_integration_probe/_common.py` pattern for read-only prod access.
- **Target (write, sandbox):** `scripts/study/config.py`'s `StudySettings`.
- **Safety gate (allowlist, not denylist — codex finding):** refuses to run with `--execute` unless the resolved target URI **positively matches** the study substrate (`localhost`/`127.0.0.1` on the `study` Bolt port, `7691`) — i.e. requires the target to *be* the study service, rather than merely checking it is *not* the prod fingerprint. This rejects any target — prod, a typo'd port, an internal docker DNS name like `neo4j`/`cloud-sim-neo4j` — other than the one expected sandbox. Without `--execute`, only prints counts (dry run), no writes anywhere.
- **Export logic:** per-label Cypher (`MATCH (n:Entity) RETURN n`, etc.) and per-type relationship Cypher against prod (read-only). Each source node is re-`CREATE`d in the sandbox with an added `_export_source_element_id` property (Neo4j 5.x `elementId(n)`) so relationship endpoints can be resolved: relationships are recreated by `MATCH` on `_export_source_element_id` at both ends, then the temp property is left in place as export provenance (harmless, and consistent with the ADR's "evidence never decays" spirit) rather than stripped. Postgres `sessions.messages` copied onto the corresponding sandbox `:Session` node as `raw_messages_json` (canonical `json.dumps(..., sort_keys=True)` string).
- **Manifest:** `snapshot_manifest.json` written to `--snapshot-dir`: `{snapshot_date (UTC ISO8601), content_hash, node_counts_by_label, relationship_counts_by_type, prod_node_total, prod_relationship_total, prod_session_count}`. **Codex finding:** `content_hash` is sha256 over the canonically-sorted JSON of **all** exported data — nodes, relationships, *and* the raw conversation-trace payloads — not just the graph, since D1's corpus is explicitly "KG entities and relationships **plus conversation traces**."
- No raw corpus content ever touches disk — data streams prod→sandbox directly over the Bolt/Postgres drivers in memory. Only the small `snapshot_manifest.json` (date/hash/counts, no content) is written to `--snapshot-dir`; that directory is still added to `.gitignore` as defense in depth.
- Uses `structlog` for operational logging (per project convention); `print()` reserved for the CLI safety-gate/dry-run summary only, matching `scripts/replay_sessions_to_neo4j.py`'s existing precedent (which is outside `src/`'s no-`print()` rule).

### 6. `scripts/study/verify_isolation.py` (new)
Isolation probe, runnable standalone and imported by the integration test. Spins up (via `docker run --rm --network seshat-study-net`) a throwaway probe container and asserts, **for all three prod substrate components (codex finding — the original draft only covered Neo4j)**:
- DNS resolution of prod's compose service names (`neo4j`, `postgres`, `elasticsearch`) fails from `study-net`.
- A raw TCP connect attempt to each loopback-published prod port fails — Bolt `127.0.0.1:7687`, Postgres `127.0.0.1:5432`, Elasticsearch `127.0.0.1:9200` (no route, per Docker bridge-network semantics — the container's own `127.0.0.1` never reaches the host's published ports).
- No `AGENT_NEO4J_PASSWORD`/`AGENT_DATABASE_URL`/etc. env vars are present in the probe container's environment (trivially true since the container is launched only with `STUDY_*` vars — asserted for documentation/defense-in-depth).

A static companion check (in the test, not the probe script) parses `docker-compose.study.yml` and asserts the `neo4j-study` service has **no** `network_mode: host`, **no** `network_mode: service:...`/`container:...`, **no** `extra_hosts` entry mapping `host.docker.internal` or any leak path, and is **not** a member of `cloud-sim` — closing the leak paths codex flagged that a purely runtime DNS/TCP probe wouldn't catch by itself.

### 7. Tests

- `tests/scripts/study/test_export_snapshot.py` (unit, mocked Neo4j/Postgres sessions — no real infra):
  - content-hash is deterministic given the same input data, regardless of Cypher result ordering, and changes if conversation-trace payloads change (not just graph data).
  - manifest schema contains all required fields.
  - refuses to write (no Neo4j calls) without `--execute`.
  - refuses to run unless the target URI positively matches the study allowlist (rejects prod fingerprint, rejects arbitrary other hosts/ports), even with `--execute`.
  - relationship recreation correctly maps endpoints via `_export_source_element_id` (a small fixture with 3 nodes + 2 relationships, asserting the recreated edges connect the right sandbox node pairs).
- `tests/scripts/study/test_config.py` (unit): `StudySettings` never reads any `AGENT_`-prefixed env var; only `STUDY_`-prefixed.
- `tests/scripts/study/test_verify_isolation.py` (marked `integration`, requires Docker + `make study-infra-up`): asserts the DNS-resolution-fails and TCP-connect-fails outcomes for real against `seshat-study-net`, for all three prod components (Bolt/PG/ES); plus the static compose-file leak-path assertions (no `network_mode: host`, no `extra_hosts` host-gateway mapping, not joined to `cloud-sim`).

Each of these maps directly to an AC-5(1) sub-claim ("prod creds absent" / "connection attempt fails") — the outcome-level proof this ticket owes at the gate.

### 8. `.env.example`
Add a `STUDY_NEO4J_PASSWORD=` line with a comment noting it's the isolated research-sandbox credential, distinct from `NEO4J_PASSWORD`.

### 9. `scripts/study/README.md`
Short doc: what this is, how to stand up the substrate (`make study-infra-up`), the dry-run vs `--execute` export command, and — most importantly — **the deferred runbook**: the exact command + preconditions (quiesced prod window, coordinated with owner/master) to run the real one-time export and produce the AC-5(2) zero-delta proof.

## TDD sequence

1. Write failing tests for `StudySettings` env-prefix isolation → implement `config.py`.
2. Write failing tests for manifest schema + hash determinism (incl. conversation traces) + `--execute` gate + allowlist target-safety check + relationship-endpoint mapping (all mocked) → implement `export_snapshot.py`'s manifest/gate/writer logic.
3. Stand up `make study-infra-up` locally; smoke-test the export script in `--execute` mode against the **local dev Neo4j** (synthetic/dev data, not real prod conversation data) to prove the writer path, relationship mapping, and manifest generation work end-to-end.
4. Write the isolation integration test (Bolt+PG+ES + static compose leak-path checks); run it against the real `seshat-study-net` to prove AC-5(1).
5. **Before running the real export against production data:** confirmed this build worktree's `.env` carries no prod credentials (only `STUDY_NEO4J_PASSWORD`) — the real prod Neo4j/Postgres passwords live only in the primary `/opt/seshat` checkout. Checked recent activity anyway (Postgres `sessions` table: no rows created in the last 30 minutes, most recent session 2026-07-05) — the system is quiet, so whenever the runbook runs it has a clean window. Leave the corpus unpopulated in this PR; document the exact runbook command (`scripts/study/README.md`) for master to run from the primary checkout, and note FRE-839/840/841 remain blocked until it runs.

## Test commands
- `make test-file FILE=tests/scripts/study/test_export_snapshot.py`
- `make test-file FILE=tests/scripts/study/test_config.py`
- `make study-infra-up && uv run pytest tests/scripts/study/test_verify_isolation.py -m integration -v` (manual, not part of `make test`'s fast unit run)
- `make mypy` / `make ruff-check` / `make ruff-format` (scoped to `src/`; N/A to `scripts/study/` but run anyway for the whole-repo habit — actually these only touch `src/`, so no gate impact from this ticket's `scripts/` additions)

## Explicitly out of scope (belongs to later ADR-0114 tickets)
- Evidence-layer schema (`Mention`/`MembershipAssertion`/`Concept`/`Category`/`SUBSUMES`) — FRE-839.
- Ingest categorizer, accretion writer — FRE-839.
- Baseline harness (production-multipath reproduction) — FRE-840.
- Cue-set / gold data — FRE-841.
