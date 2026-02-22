#!/bin/bash
# Setup Elasticsearch index templates and ILM policies

set -e

ES_URL="${ES_URL:-http://localhost:9200}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=== Setting up Elasticsearch at $ES_URL ==="

# Wait for Elasticsearch to be ready
echo "Waiting for Elasticsearch to be ready..."
until curl -s "$ES_URL/_cluster/health" > /dev/null 2>&1; do
  echo "  Waiting for Elasticsearch..."
  sleep 2
done
echo "✓ Elasticsearch is ready"

# Create ILM policy
echo "Creating ILM policy..."
curl -X PUT "$ES_URL/_ilm/policy/agent-logs-policy" \
  -H 'Content-Type: application/json' \
  -d @"$PROJECT_ROOT/docker/elasticsearch/ilm-policy.json"
echo ""
echo "✓ ILM policy created"

# Create index template
echo "Creating index template..."
curl -X PUT "$ES_URL/_index_template/agent-logs-template" \
  -H 'Content-Type: application/json' \
  -d @"$PROJECT_ROOT/docker/elasticsearch/index-template.json"
echo ""
echo "✓ Index template created"

# Create Captain's Log index template
echo "Creating Captain's Log index template..."
curl -X PUT "$ES_URL/_index_template/agent-captains-template" \
  -H 'Content-Type: application/json' \
  -d @"$PROJECT_ROOT/docker/elasticsearch/captains-index-template.json"
echo ""
echo "✓ Captain's Log index template created"

# Create initial index with alias
echo "Creating initial index..."
curl -X PUT "$ES_URL/agent-logs-000001" \
  -H 'Content-Type: application/json' \
  -d '{
    "aliases": {
      "agent-logs": {
        "is_write_index": true
      }
    }
  }'
echo ""
echo "✓ Initial index created"

echo ""
echo "=== Elasticsearch setup complete ==="
echo "View logs at: http://localhost:5601 (Kibana)"
