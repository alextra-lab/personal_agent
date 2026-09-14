# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Infrastructure, single-service (SERVICE=<name>) and health targets — see `make help`

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

**One pytest at a time (by convention, not enforced).** The full suite takes 7+ minutes and parallel
runs saturate CPU/memory, so avoid starting a second one. The `check-pytest-lock` PreToolUse hook that
used to enforce this was **removed 2026-07-18** (owner-directed): it matched the substring `pytest`
anywhere in a command, so it blocked read-only diagnostics (`pgrep -f pytest`, `grep pytest <log>`)
precisely when a suite was running and you needed them most.

### Test substrate isolation (FRE-375)

**Policy:** Test and eval scripts must never write to production substrate (Neo4j, Elasticsearch, Postgres,
Captain's Log) without explicit opt-in. This binds `tests/` **and** `scripts/eval/`.

Mechanics, test-stack commands, the escape hatch and the pre-commit guard: `tests/CLAUDE.md`.

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

### Tool integration tiers (ADR-0028)

MCP is **not** the default. Tier 1 (native Python in `tools/`) → Tier 2 (existing CLI + SKILL.md) →
Tier 3 (MCP), and **Tier 3 requires explicit ADR justification**. Per-tier mechanics:
`docs/reference/TOOL_INTEGRATION_GUIDE.md`.

### Configuration

All config through `from personal_agent.config import settings` (never `os.getenv()`). Environment variables use `AGENT_` prefix. Copy `.env.example` → `.env`.

Key settings:
- `AGENT_SERVICE_PORT=9000`
- `AGENT_DATABASE_URL` — PostgreSQL (asyncpg)
- `AGENT_ELASTICSEARCH_URL=http://localhost:9200`
- `LLM_BASE_URL=http://localhost:8000/v1` — SLM Server

### Memory types (`memory/protocol.py`)

`MemoryType` enum: `WORKING` · `EPISODIC` · `SEMANTIC` · `PROCEDURAL` · `PROFILE` · `DERIVED`

Promotion pipeline: episodic interactions → entity extraction (qwen3-8b) → semantic facts in Neo4j.

### Current status — see the authoritative sources

This section deliberately holds **no status narrative**. A second copy of "what's active" rots within
weeks, costs context on every session, and disagrees with the real source. Instead:

| Question | Authoritative source |
|----------|----------------------|
| What are we doing next, in order? | The dispatch resolver (`python -m scripts.dispatch.next_resolver --stream <s> --eligible --json`), overlaid by `docs/plans/OWNER_CONSOLE.md` (the owner's standing directives) |
| What may a session do without asking? | The trust ladder in `docs/plans/OWNER_CONSOLE.md` — a grant exists only if it is recorded there (ADR-0131 D3) |
| What did the last session decide? | `docs/plans/LAST_SESSION.md` |
| What shipped, and when? | `git log` |
| Why was this decided? | The Linear ticket's comments |
| What is ADR-XXXX's status? | That ADR's own `Status:` header (`docs/architecture_decisions/`) |
| Per-ticket state | [Linear](https://linear.app/frenchforest) — FrenchForest team |

Structural context that does *not* change week to week: the portfolio is organised as **L0–L3
substrate-pillars-vs-consumers** (`docs/specs/SESHAT_PROGRAM_ARCHITECTURE.md`, FRE-504), and
`docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` remains the primary architecture reference.
