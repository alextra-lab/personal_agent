# Personal Agent - Cognitive Architecture

> **⚠️ Research & Learning Project**: This is a **learning and research project** that explores cognitive architectures, agentic systems, and biologically-inspired AI design. It is not production-ready software. Use it for educational and experimental purposes.

A personal AI assistant with persistent memory and a cognitive architecture. The project is a research platform for these topics:

- Cognitive architectures and biologically-inspired system design
- Agentic AI orchestration and tool integration patterns
- Model routing across local and cloud models
- Knowledge graph construction and memory consolidation
- Safety and governance in autonomous systems

See the [`docs/research/`](docs/research/) directory for research notes and analysis.

## How it runs today

- **Gateway**: the FastAPI service runs in Docker on a VPS. The gateway listens on port 9001. The cloud stack is defined in [`docker-compose.cloud.yml`](docker-compose.cloud.yml).
- **Clients**: the PWA (`seshat-pwa/`) is a web client. The CLI is a second client. The CLI form is `uv run agent chat "<message>"`.
- **Models**: the file [`config/model_roles.yaml`](config/model_roles.yaml) binds each role to a model deployment. It is the source for the role bindings. The deployments are listed in [`config/models.yaml`](config/models.yaml). Most roles use cloud models. The primary role can use a local model, and the owner can choose another primary model per session.
- **Local model server**: local models run on llama.cpp on the owner's Mac. The service reaches that server through `AGENT_SLM_BASE_URL`. This setting has no default. No local model server runs on the VPS.
- **Data stores**: PostgreSQL stores sessions, metrics, and cost. Elasticsearch stores traces and logs. Neo4j stores the knowledge graph.
- **Schema changes**: the schema is in `docker/postgres/init.sql`. Schema changes go into `docker/postgres/init.sql` and `docker/postgres/migrations/`. The project does not use Alembic.

### Grounding

The design rule (ADR-0138) is that a model may generate but may not assert. Code for the verification of model claims exists in `src/personal_agent/grounding/`. The setting `grounding_verification_mode` is `off` in production today (`src/personal_agent/config/settings.py:2453`). With this value, no verification runs. The other values are `observe` and `enforce`. See [ADR-0138](docs/architecture_decisions/ADR-0138-the-model-may-generate-but-may-not-assert.md).

## Architecture

**Cognitive Architecture (Redesign v2)** — [Full Spec](docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md)

```
+---------------------------------------------------------------------+
|                        INTERFACE LAYER                               |
|  PWA . CLI . API /chat (VPS gateway :9001, `make dev` :9000)        |
+--------------------------------+------------------------------------+
                                 |
+--------------------------------v------------------------------------+
|                      PRE-LLM GATEWAY (7 stages)                     |
|                                                                     |
|  Security -> Session -> Intent Classification -> Governance         |
|     -> Decomposition Assessment -> Context Assembly -> Budget       |
+--------------------------------+------------------------------------+
                                 |
              +------------------v--------+
              |      PRIMARY AGENT        |
              |   (role: primary, see     |
              |   config/model_roles.yaml)|
              |                           |
              |   Conversational core     |
              |   Tool calling (native    |
              |     tools + MCP)          |
              |   Delegation composer     |
              +--+----------+--------+----+
                 |          |        |
    +------------v--+  +---v-----+  +v---------------------------+
    |   TOOLS       |  |  SESHAT |  |   EXPANSION LAYER          |
    |               |  | MEMORY  |  |                             |
    |  Native tools |  |         |  |  Internal sub-agents        |
    |  MCP Gateway  |  | Protocol|  |  (ephemeral, task-scoped)   |
    |  (extensible) |  | Neo4j   |  |  External delegation        |
    |               |  |         |  |  (Claude Code, Codex, etc.) |
    +---------------+  +----+----+  +-------------+---------------+
                            |                     |
              +-------------v---------------------v--------------+
              |              BRAINSTEM                            |
              |  Homeostasis . Sensors . Mode manager             |
              |  Consolidation . Expand/contract signals          |
              +------------------------+-------------------------+
                                       |
              +------------------------v-------------------------+
              |           SELF-IMPROVEMENT LOOP                  |
              |  Captain's Log . Insights engine                 |
              |  Promotion pipeline                              |
              +------------------------+-------------------------+
                                       |
              +------------------------v-------------------------+
              |              INFRASTRUCTURE                      |
              |  PostgreSQL (sessions, metrics, cost)            |
              |  Elasticsearch (traces, logs)                    |
              |  Neo4j (knowledge graph, memory)                 |
              +--------------------------------------------------+
```

## Features

### Cognitive Architecture Redesign v2 (Slices 1 to 3 implemented)

- **Pre-LLM Gateway**: a 7-stage deterministic pipeline (security, session, intent classification, governance, decomposition assessment, context assembly, budget management). The pipeline runs before the LLM sees a request.
- **Role-bound models**: each role (primary, entity extraction, embedding, reranker, and others) is bound to a model deployment in `config/model_roles.yaml`.
- **Intent Classification**: task-type routing (CONVERSATIONAL, MEMORY_RECALL, ANALYSIS, PLANNING, DELEGATION, SELF_IMPROVE, TOOL_USE).
- **Decomposition & Expansion**: SINGLE/HYBRID/DECOMPOSE/DELEGATE strategies with sub-agent spawning and concurrent execution.
- **Delegation**: structured handoffs through the `DelegationPackage` and `DelegationOutcome` types, with telemetry.
- **Seshat Memory Protocol**: the abstract `MemoryProtocol` interface, with an episodic-to-semantic promotion pipeline.
- **Expansion Budget**: the brainstem signals expansion safety from CPU, memory, and active inference count.
- **Context Budget**: token-aware context trimming with priority-based overflow handling.
- **Insights Engine**: delegation pattern analysis across sessions.

Slice 3 was completed on 2026-03-29. Follow-on work is tracked in Linear.

### Foundation

- **Knowledge Graph**: Neo4j-based persistent memory with entity extraction
- **Service Architecture**: FastAPI-based persistent service
- **Session Management**: PostgreSQL storage with async SQLAlchemy
- **Structured Logging**: Elasticsearch, with Grafana dashboards
- **Tools**: native Python tools, with an MCP gateway for tool discovery and execution
- **Brainstem**: homeostasis loop, sensors, consolidation scheduler, quality monitoring
- **Captain's Log**: self-improvement data capture and reflection
- **Persistent Cost Tracking**: PostgreSQL-backed API cost monitoring

## Quick Start

The steps below start a local development stack. The production gateway runs from `docker-compose.cloud.yml`.

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Docker (for PostgreSQL, Elasticsearch, Neo4j)
- A llama.cpp model server, if you want local models. Set `AGENT_SLM_BASE_URL` to its origin, without `/v1`. Cloud-bound roles need the provider keys that `config/model_roles.yaml` lists under `required_secrets`.

### Installation

1. **Clone and install dependencies**

   ```bash
   git clone https://github.com/alextra-lab/personal_agent
   cd personal_agent
   uv sync --extra dev
   ```

2. **Create the configuration files**

   ```bash
   cp .env.example .env
   cp config/governance/budget.yaml.example config/governance/budget.yaml
   ```

3. **Start infrastructure services**

   ```bash
   make up
   ```

   This starts the services in `docker-compose.yml`. Run `make help` for the other targets.

4. **Start the Personal Agent service**

   ```bash
   make dev
   ```

   This starts the service with hot reload on port 9000.

5. **Chat with the agent from the terminal**

   ```bash
   # The CLI creates or reuses a session automatically
   uv run agent chat "Hello, what should I focus on today?"
   uv run agent chat "Summarize that into 3 bullets"
   ```

### Verify Installation

```bash
# Check service health
curl http://localhost:9000/health

# View API documentation
open http://localhost:9000/docs
```

## Usage Examples

### Conversation CLI

```bash
uv run agent chat "Hello"
uv run agent chat "Follow up on that"
uv run agent chat "Start new conversation" --new
uv run agent session
```

Set the target service with `AGENT_SERVICE_URL`:

```bash
export AGENT_SERVICE_URL=https://agent.example.com
```

### Raw API (Optional)

```bash
curl -X POST http://localhost:9000/sessions \
  -H "Content-Type: application/json" \
  -d '{"channel": "CLI", "mode": "NORMAL"}'

curl -X POST "http://localhost:9000/chat?message=Hello&session_id=YOUR_SESSION_ID"
```

### List Recent Sessions

```bash
curl http://localhost:9000/sessions
```

### Query Memory with Feedback Telemetry

```bash
curl -X POST http://localhost:9000/memory/query \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "YOUR_SESSION_ID",
    "query_text": "python frameworks",
    "entity_names": ["Python", "FastAPI"],
    "limit": 5
  }'
```

This emits `memory_query_quality_metrics` events to Elasticsearch (`agent-logs-*`) for Grafana analysis.

## Configuration

Configuration comes from environment variables and the `.env` file. All code reads it through `personal_agent.config.settings`.

**Key Settings:**

- `AGENT_SERVICE_PORT=9000` - API service port for `make dev`
- `AGENT_DATABASE_URL=postgresql+asyncpg://...` - PostgreSQL connection
- `AGENT_ELASTICSEARCH_URL=http://localhost:9200` - Elasticsearch URL
- `AGENT_SLM_BASE_URL` - origin of the local llama.cpp model server (no default, no `/v1`)

The file `config/governance/budget.yaml` holds the per-role LLM spend caps. The real file is gitignored. A fresh clone must create it from `config/governance/budget.yaml.example`. The tests and the cost gate both need it.

See the [Configuration Guide](docs/guides/CONFIGURATION.md) for the full reference.

## Development

### Running Tests

```bash
# Fast unit tests (no LLM needed)
make test

# One test file
make test-file FILE=tests/test_tools/test_web_search.py

# With coverage
make test-cov
```

### Code Quality

```bash
make mypy
make ruff-check
make ruff-format
```

### Database Schema Changes

The project does not use Alembic. Put schema changes in these places:

- `docker/postgres/init.sql` for a fresh database
- `docker/postgres/migrations/` for an existing database

Run migrations as the `agent` superuser (`AGENT_DATABASE_ADMIN_URL`). The restricted application role cannot run DDL.

## Project Structure

```
personal_agent/
├── src/personal_agent/
│   ├── service/              # FastAPI service layer
│   ├── gateway/              # HTTP access control for the service
│   ├── request_gateway/      # Pre-LLM Gateway (7-stage pipeline)
│   ├── orchestrator/         # Request orchestration, sub-agents, HYBRID expansion
│   ├── grounding/            # Claim verification code (mode: see Grounding)
│   ├── memory/               # Seshat memory (protocol, service, promotion)
│   ├── insights/             # Cross-data analysis engine
│   ├── brainstem/            # Homeostasis & monitoring
│   ├── llm_client/           # LLM client (LiteLLM, role-bound)
│   ├── mcp/                  # MCP Gateway integration
│   ├── tools/                # Native tools
│   ├── telemetry/            # Logging & metrics
│   ├── captains_log/         # Self-improvement
│   ├── config/               # Configuration management
│   └── ui/                   # CLI client
├── seshat-pwa/               # PWA web client
├── config/                   # model_roles.yaml, models.yaml, governance
├── docker/                   # Docker configurations
│   ├── postgres/             # init.sql and migrations/
│   └── elasticsearch/        # ES templates and policies
├── scripts/                  # Utility scripts
├── tests/                    # Test suite
├── docs/                     # Documentation
│   ├── architecture_decisions/ # ADRs
│   ├── guides/               # Usage and configuration guides
│   ├── research/             # Research notes and analysis
│   ├── specs/                # Technical specifications
│   └── plans/                # Plans and owner directives
├── docker-compose.yml        # Local infrastructure services
├── docker-compose.cloud.yml  # Production stack on the VPS
└── pyproject.toml            # Python dependencies
```

## Documentation

### Getting Started
- **[Usage Guide](docs/guides/USAGE_GUIDE.md)** - Complete usage guide with examples
- **[Configuration Guide](docs/guides/CONFIGURATION.md)** - Configuration reference

### Architecture
- **[Cognitive Architecture Redesign v2](docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md)** - Current architecture specification (supersedes ADR-0017)
- **[ADR-0016](docs/architecture_decisions/ADR-0016-service-cognitive-architecture.md)** - Service architecture decision
- **[ADR-0138](docs/architecture_decisions/ADR-0138-the-model-may-generate-but-may-not-assert.md)** - The model may generate but may not assert
- **[Research Knowledge Base](docs/research/README.md)** - Research notes and external system analysis

### Development
- **[Phase 2.1 Complete](docs/plans/completed/PHASE_2.1_COMPLETE.md)** - Service foundation complete
- **[Phase 2.2 Complete](docs/plans/completed/PHASE_2.2_COMPLETE.md)** - Memory implementation complete
- **[Coding Standards](docs/reference/CODING_STANDARDS.md)** - Code style and standards

## Troubleshooting

### Service will not start

```bash
# Check if services are running
make ps

# Check logs
make logs SERVICE=postgres
make logs SERVICE=elasticsearch

# Restart infrastructure
make down && make up
```

### Port conflicts

- `make dev` uses port 9000.
- The VPS gateway uses port 9001.
- Check a port with `lsof -i :9000`.

### Database connection issues

```bash
# Test PostgreSQL connection
docker compose exec postgres psql -U agent -d personal_agent

# Check if tables exist
docker compose exec postgres psql -U agent -d personal_agent -c "\dt"
```

### Elasticsearch not connecting

```bash
# Check Elasticsearch health
curl http://localhost:9200/_cluster/health

# Reinitialize Elasticsearch
./scripts/setup-elasticsearch.sh
```

## Contributing

See [CODING_STANDARDS.md](docs/reference/CODING_STANDARDS.md) and [PR_REVIEW_RUBRIC.md](docs/reference/PR_REVIEW_RUBRIC.md).

Path hygiene: do not commit machine-specific absolute paths or home layouts that fingerprint one machine. See [PATH_PRIVACY.md](docs/reference/PATH_PRIVACY.md). Run `uv run python scripts/check_no_personal_paths.py`. The check also runs from [`.pre-commit-config.yaml`](.pre-commit-config.yaml) after `pre-commit install`.

## License

MIT License - See [LICENSE](LICENSE) file for details.

## Acknowledgments

- **llama.cpp**: local model inference
- **FastAPI**: modern async web framework
- **PostgreSQL + pgvector**: vector database capabilities
- **Elasticsearch**: scalable logging and search
- **Neo4j**: graph database for knowledge representation
