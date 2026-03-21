# Personal Agent - Cognitive Architecture

> **⚠️ Research & Learning Project**: This is a **learning and research project** exploring cognitive architectures, agentic systems, and biologically-inspired AI design. It is not production-ready software and should be used for educational and experimental purposes.

A personal AI assistant with persistent memory, cognitive architecture, and Apple Silicon optimized local LLM inference. This project serves as a research platform for understanding:

- Cognitive architectures and biologically-inspired system design
- Agentic AI orchestration and tool integration patterns
- Local LLM inference and model routing strategies
- Knowledge graph construction and memory consolidation
- Safety and governance in autonomous systems

See the [`docs/research/`](docs/research/) directory for research notes and analysis.

## Architecture

**Cognitive Architecture (Redesign v2)** — [Full Spec](docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md)

```
+---------------------------------------------------------------------+
|                        INTERFACE LAYER                               |
|  CLI . API /chat (port 9000) . Future: mobile, voice                |
+--------------------------------+------------------------------------+
                                 |
+--------------------------------v------------------------------------+
|                      PRE-LLM GATEWAY (7 stages)                     |
|                                                                     |
|  Security -> Session -> Governance -> Intent Classification         |
|     -> Decomposition Assessment -> Context Assembly -> Budget       |
+--------------------------------+------------------------------------+
                                 |
              +------------------v--------+
              |      PRIMARY AGENT        |
              |   Qwen3.5-35-A3B         |
              |                           |
              |   Conversational core     |
              |   Tool calling (MCP +     |
              |     native)               |
              |   Delegation composer     |
              +--+----------+--------+----+
                 |          |        |
    +------------v--+  +---v-----+  +v---------------------------+
    |   TOOLS       |  |  SESHAT |  |   EXPANSION LAYER          |
    |               |  | MEMORY  |  |                             |
    |  MCP Gateway  |  |         |  |  Internal sub-agents        |
    |  Native tools |  | Protocol|  |  (ephemeral, task-scoped)   |
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
              |  Elasticsearch (traces, logs, insights)          |
              |  Neo4j (knowledge graph, memory)                 |
              +--------------------------------------------------+
```

## Features

### Cognitive Architecture Redesign v2 (Slices 1 & 2 Implemented)

- **Pre-LLM Gateway**: 7-stage deterministic pipeline (security, session, governance, intent classification, decomposition assessment, context assembly, budget management) — all requests processed before the LLM sees them
- **Single-Brain Architecture**: Qwen3.5-35B as sole reasoning center — no role-switching, no router SLM
- **Intent Classification**: Task-type routing (CONVERSATIONAL, MEMORY_RECALL, ANALYSIS, PLANNING, DELEGATION, SELF_IMPROVE, TOOL_USE) replaces model-role routing
- **Decomposition & Expansion**: SINGLE/HYBRID/DECOMPOSE/DELEGATE strategies with sub-agent spawning and concurrent execution
- **Delegation (Stage B)**: Structured handoffs via `DelegationPackage`/`DelegationOutcome` types with telemetry
- **Seshat Memory Protocol**: Abstract `MemoryProtocol` interface with episodic-to-semantic promotion pipeline
- **Expansion Budget**: Brainstem signals expansion safety based on GPU/memory/concurrency state
- **Context Budget**: Token-aware context trimming with priority-based overflow handling
- **Insights Engine**: Delegation pattern analysis across sessions

*Status: Evaluation phase — building real usage traces before Slice 3 (Intelligence)*

### Foundation (Complete)

- **Knowledge Graph**: Neo4j-based persistent memory with entity extraction
- **Service Architecture**: FastAPI-based persistent service (port 9000)
- **Session Management**: PostgreSQL storage with async SQLAlchemy
- **Structured Logging**: Elasticsearch with Kibana dashboards
- **Local LLM Inference**: Multi-model routing via SLM Server (MLX-optimized)
- **MCP Gateway**: Tool discovery and execution
- **Brainstem**: Homeostasis loop, sensors, consolidation scheduler, quality monitoring
- **Captain's Log**: Self-improvement data capture and reflection
- **Persistent Cost Tracking**: PostgreSQL-backed API cost monitoring

## Quick Start

### Prerequisites

- Python 3.12+
- Docker Desktop (for PostgreSQL, Elasticsearch, Neo4j)
- Apple Silicon Mac (for MLX-optimized models)
- [SLM Server](https://github.com/alextra-lab/slm_server) (separate project)

### Installation

1. **Clone and install dependencies**

   ```bash
   git clone https://github.com/alextra-lab/personal_agent
   cd personal_agent
   uv sync
   ```

2. **Start infrastructure services**

   ```bash
   ./scripts/init-services.sh
   ```

   This starts PostgreSQL, Elasticsearch, Kibana, and Neo4j.

3. **Start SLM Server** (separate terminal)

   First, clone and set up the SLM Server:

   ```bash
   git clone https://github.com/alextra-lab/slm_server.git
   cd slm_server
   uv sync
   ./start.sh
   ```

   This starts the local LLM inference backend on port 8000.

4. **Start Personal Agent Service** (separate terminal)

   ```bash
   cd personal_agent
   uv run uvicorn personal_agent.service.app:app --reload --port 9000
   ```

5. **Chat with the agent from terminal**

   ```bash
   # Uses service client, creates/reuses session automatically
   uv run agent "Hello, what should I focus on today?"
   uv run agent "Summarize that into 3 bullets"
   ```

### Verify Installation

```bash
# Check service health
curl http://localhost:9000/health

# Check SLM Server
curl http://localhost:8000/health

# View API documentation
open http://localhost:9000/docs
```

## Service URLs

| Service | URL | Purpose |
|---------|-----|---------|
| Personal Agent API | <http://localhost:9000> | Main service endpoints |
| API Documentation | <http://localhost:9000/docs> | Swagger UI |
| SLM Server | <http://localhost:8000> | LLM inference |
| Kibana | <http://localhost:5601> | Log visualization |
| Neo4j Browser | <http://localhost:7474> | Knowledge graph (Phase 2.2) |

## Usage Examples

### Primary Interface (Conversation CLI)

```bash
uv run agent "Hello"
uv run agent "Follow up on that"
uv run agent chat "Start new conversation" --new
uv run agent session
```

Set target service with `AGENT_SERVICE_URL` if needed:

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

### Check System Health

```bash
curl http://localhost:9000/health
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

This emits `memory_query_quality_metrics` events to Elasticsearch (`agent-logs-*`) for Kibana analysis.

## Configuration

Configuration is managed via environment variables and `.env` file:

```bash
# Copy example configuration
cp .env.example .env

# Edit as needed
vim .env
```

**Key Settings:**

- `AGENT_SERVICE_PORT=9000` - API service port
- `AGENT_DATABASE_URL=postgresql+asyncpg://...` - PostgreSQL connection
- `AGENT_ELASTICSEARCH_URL=http://localhost:9200` - Elasticsearch URL
- `LLM_BASE_URL=http://localhost:8000/v1` - SLM Server endpoint

## Development

### Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/personal_agent

# Run specific test file
uv run pytest tests/test_service/test_session_repository.py
```

### Code Quality

```bash
# Type checking
uv run mypy src/

# Linting
uv run ruff check src/

# Formatting
uv run ruff format src/
```

### Database Migrations

```bash
# Create migration
alembic revision --autogenerate -m "Description"

# Apply migrations
alembic upgrade head

# Rollback
alembic downgrade -1
```

## Project Structure

```
personal_agent/
├── src/personal_agent/
│   ├── service/              # FastAPI service layer
│   │   ├── app.py           # Main application
│   │   ├── models.py        # Data models
│   │   ├── database.py      # DB connection
│   │   └── repositories/    # Data access layer
│   ├── request_gateway/     # Pre-LLM Gateway (7-stage pipeline)
│   │   ├── pipeline.py      # Gateway orchestration
│   │   ├── types.py         # TaskType, IntentResult, GatewayOutput
│   │   ├── intent.py        # Deterministic intent classification
│   │   ├── decomposition.py # SINGLE/HYBRID/DECOMPOSE/DELEGATE
│   │   ├── context.py       # Multi-source context assembly
│   │   ├── budget.py        # Token-aware context trimming
│   │   ├── governance.py    # Mode + expansion gating
│   │   ├── delegation.py    # Delegation instruction composition
│   │   └── delegation_types.py  # DelegationPackage/DelegationOutcome
│   ├── orchestrator/        # Request orchestration + sub-agents + HYBRID expansion
│   ├── memory/              # Seshat memory (protocol, service, promotion)
│   ├── insights/            # Cross-data analysis engine
│   ├── brainstem/           # Homeostasis & monitoring
│   ├── llm_client/          # LLM API client
│   ├── mcp/                 # MCP Gateway integration
│   ├── tools/               # Tool registry
│   ├── telemetry/           # Logging & metrics
│   ├── captains_log/        # Self-improvement
│   ├── config/              # Configuration management
│   └── ui/                  # CLI client
├── docker/                  # Docker configurations
│   ├── postgres/            # PostgreSQL init scripts
│   └── elasticsearch/       # ES templates and policies
├── scripts/                 # Utility scripts
│   ├── init-services.sh    # Initialize all services
│   └── setup-elasticsearch.sh
├── tests/                   # Test suite
├── docs/                    # Documentation
│   ├── architecture/        # Architecture specifications
│   ├── architecture_decisions/ # ADRs and design decisions
│   ├── research/            # Research notes and analysis
│   ├── plans/               # Implementation plans
│   └── SLM_SERVER_INTEGRATION.md
├── docker-compose.yml       # Infrastructure services
└── pyproject.toml          # Python dependencies
```

## Documentation

### Getting Started
- **[Usage Guide](docs/guides/USAGE_GUIDE.md)** - Complete usage guide with examples
- **[Configuration Guide](docs/guides/CONFIGURATION.md)** - Configuration reference
- **[SLM Server Integration](docs/SLM_SERVER_INTEGRATION.md)** - LLM backend setup

### Architecture
- **[Cognitive Architecture Redesign v2](docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md)** - Current architecture specification (supersedes ADR-0017)
- **[Service Implementation Spec](docs/architecture/SERVICE_IMPLEMENTATION_SPEC_v0.1.md)** - Phase 2.1 foundation specification
- **[ADR-0016](docs/architecture_decisions/ADR-0016-service-cognitive-architecture.md)** - Service architecture decision
- **[Research Knowledge Base](docs/research/README.md)** - Research notes and external system analysis

### Development
- **[Slice 1: Foundation](docs/superpowers/plans/2026-03-16-slice-1-foundation.md)** - Gateway + single agent + protocol
- **[Slice 2: Expansion](docs/superpowers/plans/2026-03-18-slice-2-expansion.md)** - Decomposition + sub-agents + memory types + Stage B
- **[Phase 2.1 Complete](docs/plans/completed/PHASE_2.1_COMPLETE.md)** - Service foundation complete
- **[Phase 2.2 Complete](docs/plans/completed/PHASE_2.2_COMPLETE.md)** - Memory implementation complete
- **[Coding Conventions](docs/reference/CODING_CONVENTIONS.md)** - Code style and standards

## Troubleshooting

### Service won't start

```bash
# Check if services are running
docker-compose ps

# Check logs
docker-compose logs postgres
docker-compose logs elasticsearch

# Restart infrastructure
docker-compose down && ./scripts/init-services.sh
```

### Port conflicts

- Personal Agent Service: Port 9000 (changed from 8000 due to SLM Server)
- SLM Server uses port 8000
- Check with: `lsof -i :9000` and `lsof -i :8000`

### Database connection issues

```bash
# Test PostgreSQL connection
docker-compose exec postgres psql -U agent -d personal_agent

# Check if tables exist
docker-compose exec postgres psql -U agent -d personal_agent -c "\dt"
```

### Elasticsearch not connecting

```bash
# Check Elasticsearch health
curl http://localhost:9200/_cluster/health

# Reinitialize Elasticsearch
./scripts/setup-elasticsearch.sh
```

## Contributing

See [CODING_CONVENTIONS.md](docs/reference/CODING_CONVENTIONS.md) and [PR_REVIEW_RUBRIC.md](docs/reference/PR_REVIEW_RUBRIC.md).

## License

MIT License - See [LICENSE](LICENSE) file for details.

## Acknowledgments

- **MLX**: Apple's machine learning framework for Apple Silicon
- **FastAPI**: Modern async web framework
- **PostgreSQL + pgvector**: Vector database capabilities
- **Elasticsearch**: Scalable logging and search
- **Neo4j**: Graph database for knowledge representation
