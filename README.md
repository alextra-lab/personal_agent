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

**Service-Based Design (Phase 2.1+)**

```
┌─────────────────────────────────────────────────────────────┐
│                 Personal Agent Service (Port 9000)          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │ Orchestrator │  │  Brainstem   │  │  Telemetry   │    │
│  │              │  │  (Homeostasis)│  │              │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
│         │                  │                  │             │
│         │                  │                  │             │
│  ┌──────▼───────┐  ┌──────▼───────┐  ┌──────▼───────┐    │
│  │     MCP      │  │    Tools     │  │  Captain's   │    │
│  │   Gateway    │  │   Registry   │  │     Log      │    │
│  └──────────────┘  └──────────────┘  └──────────────┘    │
└─────────┬──────────────────────────────────────┬──────────┘
          │                                       │
          │ LLM API (OpenAI-compatible)          │ Persistence
          ▼                                       ▼
┌──────────────────────┐        ┌─────────────────────────────┐
│   SLM Server (8000)  │        │   Storage Infrastructure    │
│  ┌────────────────┐  │        │  ┌──────────────────────┐  │
│  │ Router (8500)  │  │        │  │ PostgreSQL (5432)    │  │
│  │ Standard (8501)│  │        │  │ - Sessions           │  │
│  │ Reasoning(8502)│  │        │  │ - Metrics            │  │
│  │ Coding (8503)  │  │        │  │ - API Costs          │  │
│  └────────────────┘  │        │  └──────────────────────┘  │
│   MLX-optimized      │        │  ┌──────────────────────┐  │
└──────────────────────┘        │  │ Elasticsearch (9200) │  │
                                │  │ - Logs & Events      │  │
                                │  │ - Traces             │  │
                                │  └──────────────────────┘  │
                                │  ┌──────────────────────┐  │
                                │  │ Neo4j (7474/7687)    │  │
                                │  │ - Knowledge Graph    │  │
                                │  │ - Memory (Phase 2.2) │  │
                                │  └──────────────────────┘  │
                                └─────────────────────────────┘
```

## Features

### Phase 2.2 (Current) ✅

- **Knowledge Graph**: Neo4j-based persistent memory (OPERATIONAL)
- **Entity Extraction**: qwen3-8b via SLM server (100% tested)
- **Second Brain**: Background consolidation and learning (100% tested)
- **Task Capture**: Fast structured logging for later processing
- **Brainstem Scheduler**: Smart consolidation triggering (100% tested)
- **Persistent Cost Tracking**: PostgreSQL-backed API cost monitoring
- **Relevance Scoring**: Multi-factor memory query ranking

### Phase 2.1 (Complete) ✅

- **Service Architecture**: FastAPI-based persistent service
- **Session Management**: PostgreSQL storage with async SQLAlchemy
- **Structured Logging**: Elasticsearch with Kibana visualization
- **Metrics Storage**: Time-series metrics in PostgreSQL
- **Local LLM Inference**: Multi-model routing via SLM Server (MLX-optimized)
- **MCP Gateway**: Tool discovery and execution
- **Health Monitoring**: Brainstem sensors for homeostasis

### Phase 2.3 (Planned)

- **Homeostasis Loop**: Adaptive threshold adjustment
- **Feedback Learning**: Quality-based consolidation improvement
- **Memory-Based Context**: Proactive conversation suggestions
- **Topic Clustering**: Automatic conversation threading

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

### Create a Session

```bash
curl -X POST http://localhost:9000/sessions \
  -H "Content-Type: application/json" \
  -d '{"channel": "CLI", "mode": "NORMAL"}'
```

### Send a Chat Message

```bash
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
│   ├── orchestrator/        # Request orchestration
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
- **[Usage Guide](docs/USAGE_GUIDE.md)** - Complete usage guide with examples
- **[Configuration Guide](docs/CONFIGURATION.md)** - Configuration reference
- **[SLM Server Integration](docs/SLM_SERVER_INTEGRATION.md)** - LLM backend setup

### Architecture
- **[Service Implementation Spec](docs/architecture/SERVICE_IMPLEMENTATION_SPEC_v0.1.md)** - Detailed Phase 2.1 specification
- **[ADR-0016](docs/architecture_decisions/ADR-0016-service-cognitive-architecture.md)** - Service architecture decision
- **[Implementation Roadmap](docs/plans/IMPLEMENTATION_ROADMAP.md)** - Full project roadmap
- **[Research Knowledge Base](docs/research/README.md)** - Research notes and external system analysis

### Development
- **[Phase 2.1 Complete](docs/plans/completed/PHASE_2.1_COMPLETE.md)** - Service foundation complete
- **[Phase 2.2 Complete](docs/plans/completed/PHASE_2.2_COMPLETE.md)** - Memory implementation complete
- **[Coding Conventions](docs/CODING_CONVENTIONS.md)** - Code style and standards

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

See [CODING_CONVENTIONS.md](docs/CODING_CONVENTIONS.md) and [PR_REVIEW_RUBRIC.md](docs/PR_REVIEW_RUBRIC.md).

## License

MIT License - See [LICENSE](LICENSE) file for details.

## Acknowledgments

- **MLX**: Apple's machine learning framework for Apple Silicon
- **FastAPI**: Modern async web framework
- **PostgreSQL + pgvector**: Vector database capabilities
- **Elasticsearch**: Scalable logging and search
- **Neo4j**: Graph database for knowledge representation
