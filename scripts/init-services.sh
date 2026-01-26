#!/bin/bash
# Initialize all Phase 2.1 services (PostgreSQL, Elasticsearch, Neo4j)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=== Phase 2.1 Service Initialization ==="
echo ""

# Check if docker-compose is running
if ! docker ps | grep -q "personal_agent"; then
  echo "Starting docker-compose services..."
  cd "$PROJECT_ROOT"
  docker-compose up -d
  echo "✓ Docker services started"
  echo ""

  # Wait for services to be healthy
  echo "Waiting for services to be healthy (this may take 30-60 seconds)..."
  sleep 10

  # Wait for PostgreSQL
  echo "Checking PostgreSQL..."
  until docker-compose exec -T postgres pg_isready -U agent -d personal_agent > /dev/null 2>&1; do
    echo "  Waiting for PostgreSQL..."
    sleep 2
  done
  echo "✓ PostgreSQL is ready"

  # Wait for Elasticsearch
  echo "Checking Elasticsearch..."
  until curl -s http://localhost:9200/_cluster/health > /dev/null 2>&1; do
    echo "  Waiting for Elasticsearch..."
    sleep 2
  done
  echo "✓ Elasticsearch is ready"

  # Wait for Neo4j
  echo "Checking Neo4j..."
  until curl -s http://localhost:7474 > /dev/null 2>&1; do
    echo "  Waiting for Neo4j..."
    sleep 2
  done
  echo "✓ Neo4j is ready"
  echo ""
else
  echo "Docker services already running"
  echo ""
fi

# Setup Elasticsearch templates
echo "Setting up Elasticsearch..."
"$SCRIPT_DIR/setup-elasticsearch.sh"
echo ""

# Note: PostgreSQL tables are created automatically via init.sql on first start
echo "✓ PostgreSQL tables initialized (via init.sql)"
echo ""

echo "=== All services initialized ==="
echo ""
echo "Service URLs:"
echo "  PostgreSQL:    localhost:5432 (user: agent, db: personal_agent)"
echo "  Elasticsearch: http://localhost:9200"
echo "  Kibana:        http://localhost:5601"
echo "  Neo4j Browser: http://localhost:7474"
echo "  Neo4j Bolt:    bolt://localhost:7687"
echo ""
echo "Next steps:"
echo "  1. Install dependencies: uv sync"
echo "  2. Start FastAPI service: uvicorn personal_agent.service.app:app --reload"
echo "  3. Test health check: curl http://localhost:8000/health"
