# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Infrastructure — local (PostgreSQL, Elasticsearch, Kibana, Neo4j, SearXNG, Redis)
make up              # start all services (docker compose + init-services.sh)
make down            # remove containers
make stop            # stop containers (preserve volumes)
make restart         # restart all services
make ps              # show container status
make logs            # tail all logs
make services        # list all available service names
make health          # ping /health (port 9000 local, 9001 cloud; use ENV=cloud make health)

# Target a single service with SERVICE=<name>  (see: make services)
make up SERVICE=neo4j
make logs SERVICE=seshat-gateway
make restart SERVICE=searxng
make rebuild SERVICE=seshat-gateway  # local build + restart
make shell SERVICE=neo4j             # exec into container

# Run the agent service (requires infra up)
make dev             # uvicorn --reload on port 9000
make sandbox-build   # build seshat-sandbox-python:0.1 Docker image (required for run_python)

# VPS / cloud (run from Mac)
make deploy          # SSH → pull + restart (no rebuild)
make build           # SSH → pull + rebuild seshat-gateway
make build-full      # SSH → pull + rebuild all images
make tunnel-up       # start cloudflared tunnel
make tunnel-status   # show tunnel container status
ENV=cloud make ps    # check VPS container status

# Chat CLI
uv run agent "Your message here"
uv run agent chat "Start fresh" --new
uv run agent session
```

### Testing

```bash
make test                  # fast unit tests only (no LLM, safe for agents)
make test-file FILE=tests/test_tools/test_web.py   # single file
make test-k K=test_intent  # filter by name pattern
make test-cov              # with coverage report
make test-verbose          # verbose output

# Integration tests require a live LLM server — do NOT run in an agent session
PERSONAL_AGENT_INTEGRATION=1 make test-integration
```

**One pytest at a time.** A pre-tool-use hook (`.claude/hooks/check-pytest-lock.sh`) blocks concurrent pytest runs — the full suite takes 7+ minutes and parallel runs saturate CPU/memory.

### Test substrate isolation (FRE-375)

**Policy:** Test and eval scripts must never write to production substrate (Neo4j, Elasticsearch, Postgres, Captain's Log) without explicit opt-in.

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

**Eval isolation:** `docker-compose.eval.yml` now has its own `postgres-eval`, `neo4j-eval`, `elasticsearch-eval` services with isolated volumes. Use `make eval-infra-up` before running evals.

### Code quality

```bash
make mypy          # uv run mypy src/
make ruff-check    # uv run ruff check src/
make ruff-format   # uv run ruff format src/
```

### Pre-commit

```bash
pre-commit install       # install once after cloning
pre-commit run --all-files
```

The only hook runs `scripts/check_no_personal_paths.py` — blocks committing machine-specific absolute paths.

---

## Architecture

### Request flow (Redesign v2)

```
CLI / API /chat
    ↓
Pre-LLM Gateway (request_gateway/)   ← deterministic 7-stage pipeline
    Stage 1: security stub
    Stage 2: session hydration
    Stage 3: governance (mode + expansion gating)
    Stage 4: intent classification → TaskType enum
    Stage 5: decomposition assessment → SINGLE/HYBRID/DECOMPOSE/DELEGATE
    Stage 6: context assembly (memory + session history)
    Stage 7: token-aware budget trimming
    ↓ GatewayOutput
Orchestrator (orchestrator/executor.py)
    ↓ calls LLM
LocalLLMClient → SLM Server on :8000 (separate repo, MLX-optimized)
    ↓ tool calls
Tools: native Python tools (tools/) + MCP gateway (mcp/)
    ↓
Response → Captain's Log + Elasticsearch telemetry
```

Expansion paths:
- **HYBRID** — sub-agents spawned concurrently (`orchestrator/sub_agent.py`)
- **DELEGATE** — structured `DelegationPackage` handed to external agent
- **DECOMPOSE** — task split into sequential sub-tasks

### Module map

| Module | Role |
|--------|------|
| `request_gateway/` | 7-stage pre-LLM pipeline; `pipeline.py` is the entry point |
| `orchestrator/` | State machine executor, session manager, context window, sub-agents |
| `memory/` | `MemoryProtocol` interface + Neo4j-backed `MemoryService`; episodic→semantic promotion |
| `llm_client/` | `LocalLLMClient` + `LiteLLMClient` with concurrency control and cost tracking |
| `brainstem/` | Homeostasis: mode manager, sensors, expansion budget signals, consolidation scheduler |
| `tools/` | Native Python tool executors + `ToolRegistry`; each tool: `ToolDefinition` + executor + governance entry |
| `mcp/` | MCP gateway adapter; tool discovery runs once at startup (~10-15s), calls are fast |
| `events/` | Redis Streams event bus (`EventBus` protocol); `NoOpBus` fallback when Redis unavailable |
| `service/` | FastAPI app on :9000; PostgreSQL-backed session/message persistence via SQLAlchemy |
| `config/` | `AppConfig(BaseSettings)` with `AGENT_` env prefix; access via `from personal_agent.config import settings` |
| `governance/` | Mode-aware policy evaluation; tools declared in `config/governance/tools.yaml` |
| `telemetry/` | structlog + Elasticsearch handler; all logs include `trace_id` |
| `captains_log/` | Self-improvement data capture; reflection via DSPy `ChainOfThought` |
| `insights/` | Cross-session delegation pattern analysis |
| `second_brain/` | Entity extraction, quality monitoring, consolidation (called by brainstem) |
| `transport/` | AG-UI protocol endpoint for streaming events to UI |
| `delegation/` | Protocol adapters for structured delegation handoffs |
| `gateway/` | Versioned REST API for external clients — knowledge, sessions, observations, chat (FRE-206) |
| `cost_gate/` | Atomic Postgres-backed cost reservation and budget enforcement (ADR-0065) |
| `observability/` | Joinability probe and infrastructure monitors (ADR-0074) |
| `storage/` | Object store wrappers for artifact byte persistence (ADR-0069) |
| `ui/` | `service_cli.py` — the `uv run agent` entrypoint; connects to :9000 |
| `gateway/` | Seshat API Gateway — standalone FastAPI app over storage only (Neo4j, Postgres, ES); mountable as a router in local mode or run standalone on :9001 |
| `storage/` | R2-backed artifact store (ADR-0069); async S3-protocol wrapper for Cloudflare R2; owns key layout and artifact lifecycle |
| `cost_gate/` | Atomic Postgres budget reservation gate (ADR-0065); transactional reserve/commit/refund lifecycle replacing advisory checks in `LiteLLMClient` |

### Tool integration tiers (ADR-0028)

New tools follow this decision order — MCP is **not** the default:

1. **Tier 1 — Native Python** (`tools/<name>.py`): default for REST APIs. Pattern: `ToolDefinition` + async executor + registration in `tools/__init__.py` + `config/governance/tools.yaml` entry + unit tests with mocked httpx.
2. **Tier 2 — CLI + SKILL.md** (`docs/skills/<name>.md`): when a mature CLI already exists (gh, docker, git). No Python needed.
3. **Tier 3 — MCP** (reserved): only for browser automation, bidirectional streaming, or stateful protocol requirements. Requires explicit ADR justification.

### Configuration

All config through `from personal_agent.config import settings` (never `os.getenv()`). Environment variables use `AGENT_` prefix. Copy `.env.example` → `.env`.

Key settings:
- `AGENT_SERVICE_PORT=9000`
- `AGENT_DATABASE_URL` — PostgreSQL (asyncpg)
- `AGENT_ELASTICSEARCH_URL=http://localhost:9200`
- `LLM_BASE_URL=http://localhost:8000/v1` — SLM Server

### Infrastructure services (docker-compose.yml)

| Service | Port | Purpose |
|---------|------|---------|
| PostgreSQL 17 + pgvector | 5432 | Sessions, messages, cost tracking |
| Elasticsearch 8.19 | 9200 | Logs, traces, insights |
| Kibana 8.19 | 5601 | Log visualization |
| Neo4j 5.26 LTS | 7474/7687 | Knowledge graph (cloud: graph.frenchforet.com via Caddy WebSocket split) |
| SearXNG | 8888 | Self-hosted web search |
| Redis | 6379 | Event bus (Redis Streams) |

### Memory types (`memory/protocol.py`)

`MemoryType` enum: `WORKING` · `EPISODIC` · `SEMANTIC` · `PROCEDURAL` · `PROFILE` · `DERIVED`

Promotion pipeline: episodic interactions → entity extraction (qwen3-8b) → semantic facts in Neo4j.

### Key conventions

Full coding standards in `.claude/CLAUDE.md` § Coding Standards. Quick summary:

- **Never** `os.getenv()` / `print()` / bare `except:` — see `.claude/CLAUDE.md` for details
- Async for all I/O; pass `TraceContext` through call chains
- Test markers: `integration` (requires live LLM), `requires_llm_server`, `evaluation` (100+ calls) — unit tests carry no marker

### Current status (as of 2026-05-24)

Waves A–C, E (partial: FRE-227 artifact substrate shipped), G (partial), J complete. FRE-350 post-deploy eval gate earliest 2026-05-24; FRE-328 naming-stability decision 2026-05-26. See `docs/plans/MASTER_PLAN.md` for the current wave sequence.
