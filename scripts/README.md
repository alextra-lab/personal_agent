# Service Management Scripts

Scripts for managing Phase 2.1 service infrastructure.

## Quick Start

```bash
# Install dependencies
uv sync

# Initialize all services (PostgreSQL, Elasticsearch, Neo4j)
./scripts/init-services.sh

# Start SLM Server (LLM inference backend)
# First clone: git clone https://github.com/alextra-lab/slm_server.git
cd slm_server && ./start.sh

# Start Personal Agent Service (in new terminal)
cd ~/Dev/personal_agent
uv run uvicorn personal_agent.service.app:app --reload --port 9000
```

**Note**: Port 9000 is used for the Personal Agent Service because SLM Server uses port 8000.

## Scripts

### `init-services.sh`
**Full service initialization**

Starts all docker-compose services and initializes:
- PostgreSQL (creates tables from init.sql)
- Elasticsearch (index templates and ILM policies)
- Neo4j (ready for Phase 2.2)

```bash
./scripts/init-services.sh
```

### `setup-elasticsearch.sh`
**Elasticsearch-only setup**

Creates index templates and ILM policies. Run this if you need to reset Elasticsearch configuration.

```bash
./scripts/setup-elasticsearch.sh
```

Environment variable: `ES_URL` (default: http://localhost:9200)

## Service URLs

| Service | URL | Credentials |
|---------|-----|-------------|
| PostgreSQL | `localhost:5432` | user: `agent`, db: `personal_agent` |
| Elasticsearch | http://localhost:9200 | No auth (dev mode) |
| Kibana | http://localhost:5601 | No auth |
| Neo4j Browser | http://localhost:7474 | user: `neo4j`, pass: from env |
| Neo4j Bolt | `bolt://localhost:7687` | user: `neo4j`, pass: from env |
| FastAPI Service | http://localhost:8000 | None |

## Manual Commands

### Start services
```bash
docker-compose up -d
```

### Stop services
```bash
docker-compose down
```

### View logs
```bash
docker-compose logs -f postgres
docker-compose logs -f elasticsearch
```

### Reset database
```bash
docker-compose down -v  # Warning: deletes all data
docker-compose up -d
./scripts/init-services.sh
```

## Testing

### Test PostgreSQL
```bash
docker-compose exec postgres psql -U agent -d personal_agent -c "\dt"
```

### Test Elasticsearch
```bash
curl http://localhost:9200/_cluster/health?pretty
curl http://localhost:9200/_cat/indices?v
```

### Test FastAPI Service
```bash
# Health check
curl http://localhost:8000/health | jq

# Create session
curl -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"channel": "CLI", "mode": "NORMAL"}' | jq

# Chat
curl -X POST "http://localhost:8000/chat?message=Hello" | jq
```

## Troubleshooting

### Services not starting
```bash
# Check docker status
docker ps -a

# Check service logs
docker-compose logs elasticsearch
docker-compose logs postgres
```

### Elasticsearch index issues
```bash
# Delete and recreate indices
curl -X DELETE http://localhost:9200/agent-logs-*
./scripts/setup-elasticsearch.sh
```

### PostgreSQL connection issues
```bash
# Check if database is ready
docker-compose exec postgres pg_isready -U agent -d personal_agent

# Connect to database
docker-compose exec postgres psql -U agent -d personal_agent
```
