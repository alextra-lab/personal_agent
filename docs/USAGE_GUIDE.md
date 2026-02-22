# Usage Guide

Complete guide to using Personal Agent for common tasks.

## Table of Contents

- [Quick Start](#quick-start)
- [Basic Usage](#basic-usage)
- [API Endpoints](#api-endpoints)
- [Configuration](#configuration)
- [Common Tasks](#common-tasks)
- [Troubleshooting](#troubleshooting)

## Quick Start

### 1. Start Infrastructure

```bash
./scripts/init-services.sh
```

This starts PostgreSQL, Elasticsearch, Kibana, and Neo4j.

### 2. Start SLM Server

```bash
git clone https://github.com/alextra-lab/slm_server.git
cd slm_server
uv sync
./start.sh
```

### 3. Start Personal Agent Service

```bash
cd personal_agent
uv run uvicorn personal_agent.service.app:app --reload --port 9000
```

### 4. Verify Services

```bash
# Check Personal Agent
curl http://localhost:9000/health

# Check SLM Server
curl http://localhost:8000/health

# View API docs
open http://localhost:9000/docs
```

## Basic Usage

### Command-line chat

To ask a question or send a request from the terminal, run the CLI with **`uv run`** so the project environment (and dependencies like `typer`) is used:

```bash
# From the project root, after: uv sync
uv run python -m personal_agent.ui.cli "Your question or request here"
```

You can omit the `chat` subcommand; a single argument is treated as a chat message. For explicit `chat` and options:

```bash
uv run python -m personal_agent.ui.cli chat "Your question or request here"
uv run python -m personal_agent.ui.cli chat "Follow-up" --session-id my-session
```

**Note:** Do not use `python -m ...` alone (system Python may miss dependencies) or `uv python -m ...` (`uv python` is for managing Python versions). Use **`uv run python -m personal_agent.ui.cli`**.

### Create a Session

```bash
curl -X POST http://localhost:9000/sessions \
  -H "Content-Type: application/json" \
  -d '{"channel": "CLI", "mode": "NORMAL"}'
```

Response:
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "channel": "CLI",
  "mode": "NORMAL",
  "created_at": "2026-01-26T12:00:00Z"
}
```

### Send a Chat Message

```bash
curl -X POST "http://localhost:9000/chat?message=Hello&session_id=YOUR_SESSION_ID"
```

Response:
```json
{
  "content": "Hello! How can I help you today?",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "trace_id": "abc123",
  "usage": {
    "prompt_tokens": 50,
    "completion_tokens": 20,
    "total_tokens": 70
  }
}
```

### List Sessions

```bash
curl http://localhost:9000/sessions
```

## API Endpoints

### Health Check

```bash
GET /health
```

Returns service health status.

### Create Session

```bash
POST /sessions
Content-Type: application/json

{
  "channel": "CLI" | "API" | "WEB",
  "mode": "NORMAL" | "ALERT" | "DEGRADED" | "LOCKDOWN" | "RECOVERY"
}
```

### Chat

```bash
POST /chat?message=<message>&session_id=<session_id>
```

Sends a message to the agent and receives a response.

### List Sessions

```bash
GET /sessions?limit=10&offset=0
```

Returns recent sessions.

### Get Session

```bash
GET /sessions/{session_id}
```

Returns session details and history.

## Configuration

### Environment Variables

Key configuration options (see `.env.example` for full list):

```bash
# LLM Configuration
LLM_BASE_URL=http://localhost:8000/v1
LLM_TIMEOUT_SECONDS=120
LLM_MAX_RETRIES=3

# Service Configuration
AGENT_SERVICE_PORT=9000
AGENT_SERVICE_HOST=0.0.0.0

# Database
AGENT_DATABASE_URL=postgresql+asyncpg://agent:password@localhost:5432/personal_agent

# Elasticsearch
AGENT_ELASTICSEARCH_URL=http://localhost:9200

# MCP Gateway (optional)
AGENT_MCP_GATEWAY_ENABLED=false
AGENT_MCP_GATEWAY_COMMAND='["docker", "mcp", "gateway", "run"]'
```

### Governance Configuration

Edit `config/governance/` files to customize:

- **modes.yaml**: Operational mode thresholds
- **tools.yaml**: Tool permissions and risk levels
- **safety.yaml**: Safety policies and content filtering
- **models.yaml**: Model-specific governance rules

## Common Tasks

### Query System Health

The agent can check system health using built-in tools:

```bash
curl -X POST "http://localhost:9000/chat?message=What is my system CPU usage?&session_id=YOUR_SESSION_ID"
```

### File Operations

```bash
curl -X POST "http://localhost:9000/chat?message=List files in /Users/me/Documents&session_id=YOUR_SESSION_ID"
```

### Memory Queries

With Phase 2.2 memory enabled:

```bash
curl -X POST "http://localhost:9000/chat?message=What topics have we discussed about Python?&session_id=YOUR_SESSION_ID"
```

### MCP Tools

With MCP Gateway enabled, the agent can use containerized MCP tools:

```bash
# Enable in .env
AGENT_MCP_GATEWAY_ENABLED=true

# Restart service, then use MCP tools
curl -X POST "http://localhost:9000/chat?message=Search GitHub for python async libraries&session_id=YOUR_SESSION_ID"
```

## Troubleshooting

### Service Won't Start

```bash
# Check if ports are in use
lsof -i :9000
lsof -i :8000

# Check Docker services
docker-compose ps

# View logs
docker-compose logs postgres
docker-compose logs elasticsearch
```

### Database Connection Issues

```bash
# Test PostgreSQL connection
docker-compose exec postgres psql -U agent -d personal_agent

# Check tables
docker-compose exec postgres psql -U agent -d personal_agent -c "\dt"
```

### SLM Server Not Responding

```bash
# Check SLM Server health
curl http://localhost:8000/health

# Check backend models
curl http://localhost:8000/v1/backends/health

# View SLM Server logs
cd slm_server
tail -f logs/slm_server.log
```

### MCP Gateway Issues

**Issue**: MCP tools not available

**Solutions**:
1. Verify Docker is running: `docker ps`
2. Check MCP Gateway enabled: `grep MCP_GATEWAY_ENABLED .env`
3. Check logs for initialization errors
4. Verify Docker MCP Gateway feature is enabled in Docker Desktop

**Known Limitations**:
- MCP integration requires async execution (handled automatically)
- Some async cleanup warnings may appear in logs (harmless)
- Docker must be running for MCP Gateway to work

### Memory Service Issues

**Issue**: Memory queries return empty results

**Solutions**:
1. Verify Neo4j is running: `docker-compose ps neo4j`
2. Check Neo4j connection: `curl http://localhost:7474`
3. Verify memory is enabled: `grep AGENT_ENABLE_MEMORY_GRAPH .env`
4. Check memory service logs

## Advanced Usage

### Custom Tools

Add custom tools by implementing the `ToolExecutor` interface:

```python
from personal_agent.tools.types import ToolExecutor, ToolResult

async def my_tool_executor(arg1: str, arg2: int) -> ToolResult:
    # Your tool logic
    return ToolResult(success=True, output="Result")
```

Register in `config/governance/tools.yaml`.

### Custom Models

Configure models in `config/models.yaml`:

```yaml
models:
  router:
    id: "qwen/qwen3-1.7b"
    backend: "mlx"
    port: 8500
    # ... other settings
```

### Observability

- **Logs**: Structured JSON logs in `telemetry/logs/` or Elasticsearch
- **Metrics**: PostgreSQL `metrics` table or Elasticsearch
- **Traces**: Elasticsearch with trace_id correlation
- **Kibana**: http://localhost:5601 for log visualization

## Next Steps

- See [Configuration Guide](CONFIGURATION.md) for detailed configuration
- See [SLM Server Integration](SLM_SERVER_INTEGRATION.md) for LLM backend setup
- See [Architecture Documentation](architecture/) for system design
- See [Contributing](../../README.md#contributing) for development setup
