# FRE-214 Track 2b — Compose Unification + Laptop Mirror + Tunnel Mode

> **Status**: Draft — written 2026-05-08, **execution deferred** until owner signals (post-backlog reduction).
> **Parent**: [FRE-214 audit](../../architecture/2026-05-08-fre-214-vps-topology-audit.md), [ADR-0045 amendment](../../architecture_decisions/ADR-0045-infrastructure-cloud-knowledge-layer.md).
> **Blocked by**: Track 2a (endpoint abstraction). **Must land first** — without `endpoints[]` resolution the laptop's containerized gateway will route to `slm.frenchforet.com` for its own MLX models.
> **Tier**: 2 (Sonnet — implementation from plan).
> **Branch when executed**: `fre-214-2b-compose-unification` off `main` (after 2a is merged).

---

## Context

The FRE-214 audit ratified full-harness-on-VPS as the canonical topology and asked the laptop to mirror that shape rather than diverge from it (audit §7.1, ADR-0045 amendment "What this amendment ratifies"). Two compose files (`docker-compose.yml` and `docker-compose.cloud.yml`) currently encode the divergence: 6 services on laptop vs 10 on VPS, different bring-up procedures, drift potential.

This track collapses them. One `docker-compose.yml` drives both deployments; an overlay file (`docker-compose.cloud.yml` — same path, repurposed) carries cloud-only services and resource caps; compose profiles mark VPS-only services so they don't spin up on laptop. The laptop additionally gets a **containerized gateway** with hot-reload via Compose's `develop.watch` so the dev loop stays under 5 s.

Two structural constraints (from the audit):

1. **Apple Silicon GPU is not accessible to Docker**. Inference (`primary`, `sub_agent`, `embedding`, `reranker`) stays as native MLX `slm_server` on the host. Containerized services on laptop are limited to gateway + PWA + Caddy + datastores + SearXNG. The gateway container reaches MLX via `host.docker.internal`.
2. **Each location runs its own self-contained datastores by default** (peer-deployment model). Schema migrations + ES index templates + Kibana saved objects must be kept in sync between deployments — handled in §4 (sync procedure) and mitigated by the opt-in tunnel mode in §3.

---

## Design decisions (made; do not defer during execution)

1. **Two compose files, layered**: `docker-compose.yml` is the laptop default (mirrors the VPS shape but minus VPS-only services). `docker-compose.cloud.yml` becomes a strict overlay layered with `-f docker-compose.yml -f docker-compose.cloud.yml`, adding cloudflared + resource caps + cloud-specific env. This matches the existing `docker-compose.eval.yml` overlay pattern in the project.
2. **Compose profiles** gate VPS-only services within the unified base. Specifically: nothing in this round — all "cloud-only" things (cloudflared, resource caps, public-facing Caddy host blocks) live in the overlay, not under profiles. Profiles are reserved for future modes (e.g. `[eval]`, `[debug]`).
3. **Embedding/reranker llama.cpp containers stay only in the cloud overlay** — never in the base. Laptop never starts them (Apple Silicon constraint). The VPS adds them via overlay.
4. **`make dev` keeps native uvicorn as the default** (fastest reload, debugger-friendly). `make dev-mirror` is a new target that runs the gateway containerized with `develop.watch` for the rare case of validating the laptop mirror shape end-to-end. They coexist.
5. **`develop.watch` strategy**: `sync` action on `src/`, `config/`, `docs/skills/` (≤500 ms reload). `rebuild` on `pyproject.toml`, `uv.lock`. No watch on `tests/` — tests run from the host.
6. **Tunnel-mode env variable: `AGENT_TUNNEL_MODE=1`** (explicit, not heuristic). The opt-in `.env.local-to-vps` file sets this. Safety guard reads it, checks for `FORCE_LIVE_DATA=1`, and refuses to start without confirmation when write-capable roles are configured.
7. **Tunnel transport**: `ssh -L` via `autossh -M 0` for resilience (auto-reconnect on drop). Dependency is `autossh` on the developer's Mac; documented prerequisite, install via Homebrew.
8. **Migration sync is manual + scripted, not automatic**: `make sync-from-vps` and `make sync-to-vps` targets ship migration SQL + Kibana ndjson; Postgres replays via `psql -f`, Kibana via `import_dashboards.sh`. ES index templates are gateway-bootstrapped on startup (already today); no separate sync needed.
9. **Caddy's host blocks** for `*.frenchforet.com` and the WARP IP move into the cloud overlay. Laptop's Caddy serves `localhost` only (HTTPS via `local_certs`). The PWA's runtime config (Track 4 D-3) lets it adapt to either host.
10. **PWA build is environment-agnostic via runtime config**. This depends on Track 4 D-3 (runtime config endpoint) landing first, OR being delivered in this track. Decision: deliver D-3 here (small) since the PWA otherwise needs a per-deployment build, defeating the unification goal.

---

## Phase 1 — Compose merge

**Goal**: produce a single `docker-compose.yml` that drives the laptop deployment and a strict overlay `docker-compose.cloud.yml` that adds VPS-only pieces.

### 1.1 Promote shared services to the base file

The new `docker-compose.yml` contains the following (resource caps removed — those move to the overlay):

* `postgres` (pgvector/pg17) — port `5432:5432` (laptop direct); cloud overlay rewrites to `127.0.0.1:5432:5432`.
* `neo4j` (5.26-community) — ports `7474`, `7687`; cloud overlay rewrites to localhost-only.
* `elasticsearch` (8.19) — port `9200`; cloud overlay rewrites to localhost-only.
* `kibana` (8.19) — port `5601`; cloud overlay rewrites to localhost-only.
* `redis` (7-alpine) — port `6379`; cloud overlay rewrites to localhost-only and adds `--appendonly yes`.
* `searxng` — port `8888`; cloud overlay rewrites to localhost-only.
* `seshat-gateway` (built from `Dockerfile.gateway`) — bind-mounted source for `develop.watch`; ports `9000:9001` on laptop (preserves CLAUDE.md's documented port 9000 for `make dev`); cloud overlay changes the host port to `127.0.0.1:9001:9001`.
* `seshat-pwa` (built from `Dockerfile.pwa`) — same on both; build args sourced from runtime config (see §3 for the runtime-config endpoint). Port `3000:3000` on laptop; cloud overlay rewrites to `127.0.0.1:3000:3000` and adds the static IP `172.25.0.11`.
* `caddy` — base file uses `Caddyfile.local` (localhost only with `local_certs`); cloud overlay swaps to `Caddyfile.cloud` (current production routing including the four CF Tunnel hosts).

### 1.2 Cloud-only services, in the overlay

`docker-compose.cloud.yml` (rewritten) carries:

* `embeddings` (llama.cpp container) — unchanged from today
* `reranker` (llama.cpp container) — unchanged from today
* `cloudflared` (tunnel daemon) — unchanged from today
* All resource caps (`mem_limit`, `cpus`) for every service
* `Caddyfile.cloud` mount override
* Cloud-specific env additions (CF Access tokens, gateway auth enabled, etc.)

### 1.3 Caddyfile split

The current single Caddyfile has both `localhost` blocks and `*.frenchforet.com` blocks. Split:

* `config/cloud-sim/Caddyfile.local` — only the `localhost { import routing }` block plus the `(routing)` snippet. Used by laptop.
* `config/cloud-sim/Caddyfile.cloud` — the full set including `agent.frenchforet.com`, `api.frenchforet.com`, `graph.frenchforet.com`, the WARP IP, and the `(routing)` snippet. Used by VPS overlay.

### 1.4 Gateway env file split

`docker-compose.cloud.yml` today does `env_file: [/opt/seshat/.env]` for the gateway. Laptop should not point at a VPS path. Use the project root `.env` for laptop (which `make dev` already does today via uvicorn + pydantic-settings) and override per-host paths in the overlay.

### 1.5 Verify no functional regression

```bash
# Laptop bring-up
docker compose up -d --remove-orphans
docker compose ps  # all 6 base services + gateway + PWA + caddy healthy
make health        # → 200 on http://localhost:9000/health

# VPS bring-up (executed during deployment, not during planning)
docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d
ENV=cloud make ps  # same shape + embeddings + reranker + cloudflared
ENV=cloud make health
```

---

## Phase 2 — Gateway containerization with `develop.watch`

### 2.1 Add `develop.watch` to the gateway service

In `docker-compose.yml`:

```yaml
seshat-gateway:
  build:
    context: .
    dockerfile: Dockerfile.gateway
  environment:
    AGENT_DATABASE_URL: postgresql+asyncpg://agent:${POSTGRES_PASSWORD:?required}@postgres:5432/personal_agent
    AGENT_NEO4J_URI: bolt://neo4j:7687
    AGENT_NEO4J_PASSWORD: ${NEO4J_PASSWORD:?required}
    AGENT_ELASTICSEARCH_URL: http://elasticsearch:9200
    AGENT_EVENT_BUS_REDIS_URL: redis://redis:6379/0
    AGENT_SEARXNG_BASE_URL: http://searxng:8080
    AGENT_LLM_BASE_URL: http://host.docker.internal:8000/v1   # laptop default — Track 2a's resolver overrides per-model
  ports:
    - "9000:9001"   # laptop preserves :9000 (CLAUDE.md convention)
  develop:
    watch:
      - action: sync
        path: ./src
        target: /app/src
      - action: sync
        path: ./config
        target: /app/config
      - action: sync
        path: ./docs/skills
        target: /app/docs/skills
      - action: rebuild
        path: ./pyproject.toml
      - action: rebuild
        path: ./uv.lock
  command:
    - sh
    - -c
    - exec uv run uvicorn personal_agent.service.app:app --host 0.0.0.0 --port 9001 --reload
```

### 2.2 New Make target: `make dev-mirror`

Add to `Makefile`:

```makefile
# Run the gateway in containerized mirror mode with hot reload.
# `make dev` remains the fast native-uvicorn path; this target validates the
# laptop mirror shape end-to-end (containerized gateway + datastores + PWA + Caddy).
dev-mirror:
	docker compose up --watch
```

`make dev` is unchanged — it still runs native uvicorn against the docker-compose datastores.

### 2.3 Verify reload time

After Phase 2 lands, edit `src/personal_agent/service/app.py` and time the reload cycle:

```bash
make dev-mirror &
# In another shell, edit a file in src/, then:
time curl http://localhost:9000/health
# Expected: < 5s from edit→200 response (sync + uvicorn --reload)
```

If the reload is slower than 5 s, check `develop.watch` is firing (`docker compose logs seshat-gateway` should show sync events) before adjusting strategy.

---

## Phase 3 — Opt-in tunnel mode

### 3.1 Override env file template

**File** (new): `.env.local-to-vps.example`

```dotenv
# Laptop → VPS datastore tunnel mode (FRE-214 §8.5 mitigation).
#
# To use:
#   1. cp .env.local-to-vps.example .env.local-to-vps
#   2. Fill in passwords / hostnames.
#   3. make tunnel-data       # opens ssh -L for postgres / neo4j / es / redis
#   4. AGENT_ENV_FILE=.env.local-to-vps make dev
#
# Safety: the gateway refuses to start with AGENT_TUNNEL_MODE=1 unless
# FORCE_LIVE_DATA=1 is also set. This prevents accidental writes to prod
# (e.g. replaying a docker/postgres/migrations/*.sql file against live data).

AGENT_TUNNEL_MODE=1

# All datastore URLs point at localhost — make tunnel-data forwards 5432/7474/
# 7687/9200/6379 to the VPS via SSH.
AGENT_DATABASE_URL=postgresql+asyncpg://agent:CHANGEME@localhost:5432/personal_agent
AGENT_NEO4J_URI=bolt://localhost:7687
AGENT_NEO4J_PASSWORD=CHANGEME
AGENT_ELASTICSEARCH_URL=http://localhost:9200
AGENT_EVENT_BUS_REDIS_URL=redis://localhost:6379/0

# Confirm intent before each tunnel-mode session (set to 1 once you've read
# the warning and understand you are operating on live data).
# FORCE_LIVE_DATA=1
```

`.env.local-to-vps` is added to `.gitignore`. `.env.local-to-vps.example` is committed.

### 3.2 Tunnel target

Add to `Makefile`:

```makefile
# Open SSH tunnels from laptop to VPS datastores. Requires:
#   - VPS_SSH_HOST set (existing convention)
#   - autossh installed: brew install autossh
# Stop tunnels: make tunnel-data-down (or kill the background autossh PID).
tunnel-data: _tunnel-guard
	@which autossh >/dev/null 2>&1 || { echo "autossh required: brew install autossh"; exit 1; }
	@echo "Opening SSH tunnels: 5432→postgres, 7474/7687→neo4j, 9200→es, 6379→redis"
	@autossh -M 0 -f -N \
		-L 5432:127.0.0.1:5432 \
		-L 7474:127.0.0.1:7474 \
		-L 7687:127.0.0.1:7687 \
		-L 9200:127.0.0.1:9200 \
		-L 6379:127.0.0.1:6379 \
		$(SSH_HOST)
	@echo "Tunnels up. AGENT_ENV_FILE=.env.local-to-vps make dev"

tunnel-data-down:
	@pkill -f "autossh.*$(SSH_HOST)" 2>/dev/null || echo "no tunnels running"
```

### 3.3 Safety guard at gateway startup

**File**: `src/personal_agent/service/app.py` (or wherever `lifespan`/startup runs)

Add an early-startup check:

```python
def _check_tunnel_mode_safety() -> None:
    """Refuse to start in tunnel mode without explicit FORCE_LIVE_DATA=1.

    Tunnel mode (AGENT_TUNNEL_MODE=1) routes the gateway's datastore
    connections to localhost ports forwarded to the VPS. Running write-capable
    code paths (migrations, ingestion, consolidation) in this mode without a
    deliberate confirmation step risks corrupting prod data.
    """
    import os
    import sys

    if os.environ.get("AGENT_TUNNEL_MODE") != "1":
        return  # not in tunnel mode

    if os.environ.get("FORCE_LIVE_DATA") == "1":
        log.warning(
            "tunnel_mode_active",
            warning="Operating against LIVE VPS data — writes go to prod.",
        )
        return

    sys.stderr.write(
        "\n"
        "ERROR: AGENT_TUNNEL_MODE=1 detected without FORCE_LIVE_DATA=1.\n"
        "\n"
        "You are about to run the gateway against VPS datastores via SSH tunnel.\n"
        "Any writes — including schema migrations (docker/postgres/migrations/),\n"
        "embeddings, and consolidation — will hit production data.\n"
        "\n"
        "If this is intentional, set FORCE_LIVE_DATA=1 and re-run.\n"
        "If this was accidental, unset AGENT_TUNNEL_MODE and use your local stack.\n"
        "\n"
    )
    sys.exit(2)


# Wire into the FastAPI lifespan / startup event:
@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_tunnel_mode_safety()
    # … existing startup logic …
```

Tests:

* `tests/test_service/test_tunnel_mode_safety.py` — three cases: no tunnel mode → no exit; tunnel mode without FORCE → SystemExit(2) + stderr message; tunnel mode + FORCE → warning logged but no exit.

### 3.4 Verify tunnel-mode round-trip

```bash
cp .env.local-to-vps.example .env.local-to-vps
# fill in real passwords from VPS
make tunnel-data

# Without FORCE_LIVE_DATA — should refuse:
AGENT_ENV_FILE=.env.local-to-vps make dev
# Expected: stderr message, exit code 2

# With FORCE_LIVE_DATA — should start, log warning, serve VPS data:
FORCE_LIVE_DATA=1 AGENT_ENV_FILE=.env.local-to-vps make dev
# Expected: "tunnel_mode_active" warning; /health returns 200; a memory query
# returns whatever's in the VPS Neo4j (not laptop's empty Neo4j).
```

---

## Phase 4 — Sync procedure (peer-deployment hygiene)

The peer-deployment model means each location's datastores drift unless synchronized. Three concerns:

### 4.1 Postgres schema migrations

The project already uses ordered SQL files in `docker/postgres/migrations/000N_*.sql` (see FRE-302's plan). Sync = replay the missing files.

Add to `Makefile`:

```makefile
# Replay any Postgres migrations that exist locally but haven't been applied
# to the target deployment. Idempotent via "CREATE TABLE IF NOT EXISTS".
sync-postgres-migrations:
	@for f in docker/postgres/migrations/*.sql; do \
		echo "==> $$f"; \
		$(COMPOSE) exec -T postgres psql -U agent -d personal_agent < $$f || true; \
	done
```

`make sync-postgres-migrations` runs against whichever ENV is active (`ENV=local` default, `ENV=cloud` for VPS via SSH).

### 4.2 ES index templates

Already auto-bootstrapped by the gateway service on startup (see `src/personal_agent/telemetry/`). No separate sync needed — when the offline location comes back online and the gateway starts, templates are created if missing. Document this; no new code.

### 4.3 Kibana saved objects

`config/kibana/dashboards/*.ndjson` is the source of truth. `config/kibana/import_dashboards.sh` already handles import.

Add to `Makefile`:

```makefile
# Import Kibana dashboards into the active Kibana instance.
sync-kibana-dashboards:
	$(COMPOSE) exec kibana sh -c 'KIBANA_URL=http://localhost:5601 /app/config/kibana/import_dashboards.sh'
```

(Or run from host: `KIBANA_URL=http://localhost:5601 ./config/kibana/import_dashboards.sh` — already works today.)

After editing dashboards in Kibana on either side, re-export per the existing `config/kibana/dashboards/README.md` and commit. The other side picks them up on next `make sync-kibana-dashboards`.

### 4.4 Document the runbook

**File** (new): `docs/guides/peer-deployment-sync.md` — short runbook covering:

* Why two stacks exist (link to ADR-0045 amendment)
* When to sync (after schema/dashboard changes; before extended laptop usage)
* How to sync each (`make sync-postgres-migrations`, ES auto, `make sync-kibana-dashboards`)
* Drift-detection (how to spot if laptop is behind: `git log` shows new migrations not yet replayed; gateway logs show "creating index template" on first VPS write to a new index)

---

## Phase 5 — Verification & rollout

### 5.1 Laptop, default mode (`make dev` — native uvicorn against docker-compose datastores)

```bash
make up                       # base compose: 6 datastores + gateway + PWA + caddy
make dev                      # native uvicorn on :9000 (unchanged behavior)
make health                   # 200
uv run agent "smoke test"     # round-trip works

# Verify endpoint resolution went to localhost (Track 2a)
make logs SERVICE=seshat-gateway 2>&1 | grep endpoint_resolved
# Expected: endpoint=http://localhost:8000/v1 (or :8503/:8504 for embed/rerank)
```

### 5.2 Laptop, mirror mode (`make dev-mirror` — containerized with hot reload)

```bash
make dev-mirror               # gateway in container with develop.watch
# Edit src/personal_agent/service/app.py — add a log line
time curl http://localhost:9000/health
# Expected: < 5s reload; new log line visible

make logs SERVICE=seshat-gateway 2>&1 | grep endpoint_resolved
# Expected: endpoint=http://host.docker.internal:8000/v1 (containerized → host MLX)
```

### 5.3 Tunnel mode

```bash
cp .env.local-to-vps.example .env.local-to-vps
# fill in passwords from `op` / 1Password / wherever VPS creds live
make tunnel-data

AGENT_ENV_FILE=.env.local-to-vps make dev
# Expected: stderr "ERROR: AGENT_TUNNEL_MODE=1 ... FORCE_LIVE_DATA=1", exit 2

FORCE_LIVE_DATA=1 AGENT_ENV_FILE=.env.local-to-vps make dev
# Expected: "tunnel_mode_active" warning logged; /health 200; memory queries
# return VPS data
make tunnel-data-down
```

### 5.4 VPS

```bash
ENV=cloud make deploy        # uses docker-compose.yml + docker-compose.cloud.yml overlay
ENV=cloud make ps
ENV=cloud make health        # 200 on https://agent.frenchforet.com/health (via CF Tunnel)
```

### 5.5 Test suite

```bash
make test                    # unit
# Expected: same baseline pass rate; new test_tunnel_mode_safety passes

make test-eval               # eval harness scripts
# Expected: green

make ruff-check && make mypy
# Expected: clean
```

### 5.6 Sync flow exercise

```bash
# Add a no-op migration locally (e.g. CREATE TABLE IF NOT EXISTS scratch_test);
# commit it to docker/postgres/migrations/000N_scratch_test.sql
ENV=cloud make sync-postgres-migrations
ENV=cloud make shell SERVICE=postgres
psql> \dt scratch_test    # should exist on VPS
```

---

## Rollback

Compose changes are reversible by checkout:

```bash
git revert <track-2b-commit-sha>
# Restores docker-compose.yml + docker-compose.cloud.yml to pre-merge state.
# Re-run `make up` / `ENV=cloud make deploy`.
```

Tunnel-mode and develop.watch additions are additive; reverting just removes targets/options. No data is touched by the revert.

If only the gateway containerization breaks (e.g. develop.watch fails on a specific host): drop back to `make dev` (native uvicorn) which is unaffected.

---

## Out of scope (do not pull in)

* Endpoint abstraction (Track 2a — prerequisite, lands first).
* Test parity / `requires_llm_server` rename / embedding-runtime parity test (Track 3 / FRE-336).
* MCP env-driven server list (Track 4 D-1, separate Linear ticket).
* Hardcoded transfer-models.sh paths (Track 4 D-5, separate Linear ticket).
* Removing the dead `execution-service` gateway token (Track 4 D-6).
* Stationary home server provisioning (future, separate ADR).

The PWA runtime config (Track 4 D-3) is in scope only as a small dependency: the PWA needs to fetch backend URL at runtime instead of bake-time so one image works on both laptop and VPS. Implementation is small (a new `/runtime-config.json` endpoint on the gateway + a fetch in the PWA bootstrap). Can be split out as a sub-ticket if execution scoping demands it.

---

## Done means

1. One `docker-compose.yml` works for laptop default; `docker-compose.cloud.yml` is a strict overlay used only on VPS.
2. `Caddyfile.local` and `Caddyfile.cloud` are split; laptop never serves `*.frenchforet.com`.
3. `make dev` continues to work unchanged (native uvicorn, fast).
4. `make dev-mirror` runs containerized gateway with `develop.watch`; edit-to-reload < 5 s.
5. `.env.local-to-vps.example` ships; `make tunnel-data` opens the four SSH forwards via autossh.
6. Tunnel-mode safety guard refuses to start without `FORCE_LIVE_DATA=1`.
7. `make sync-postgres-migrations` and `make sync-kibana-dashboards` work against either ENV.
8. `docs/guides/peer-deployment-sync.md` documents the sync runbook.
9. `make test` + `make ruff-check` + `make mypy` clean.
10. PWA serves correctly on both `http://localhost` (laptop) and `https://agent.frenchforet.com` (VPS) without rebuilding the image.

---

*End of plan. Execution gated on owner trigger; do not start until backlog reduction is complete and Track 2a has merged (per audit §8.6 / §8.7).*
