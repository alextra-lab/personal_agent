#!/usr/bin/env bash
# Setup Elasticsearch index templates and ILM policies.
#
# This script is **idempotent** — safe to re-run after every container restart.
# PUTs for templates and policies replace existing definitions in place; the
# initial write-alias index is created only when missing. Failures on each
# step do not bring down the whole script — we surface a clear summary at the
# end so a missing piece doesn't go silent.
#
# Background (2026-05-10):
#   The template was missing/wrong for an extended period; daily indices were
#   created with default ES dynamic mapping (text+keyword for every string).
#   ES|QL term equality silently returned null. The agent retried broken
#   queries and exposed downstream bugs. This script is the prevention path:
#   running it before any event hits ES guarantees correct mappings on every
#   future daily index.
#
# Usage:
#   bash scripts/setup-elasticsearch.sh                    # local
#   ES_URL=http://localhost:9200 bash scripts/setup-elasticsearch.sh

set -euo pipefail

ES_URL="${ES_URL:-http://localhost:9200}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

failures=0

put_resource() {
  # Args: <label> <url-path> <body-file>
  # PUTs JSON to ES; logs success or non-fatal failure.
  local label="$1"
  local path="$2"
  local body="$3"
  local resp
  if ! resp=$(curl -sS -w '\nHTTP_STATUS:%{http_code}' -X PUT "$ES_URL$path" \
      -H 'Content-Type: application/json' \
      --data-binary @"$body" 2>&1); then
    echo "  ✗ $label: PUT failed: $resp"
    failures=$((failures + 1))
    return 1
  fi
  local http_status="${resp##*HTTP_STATUS:}"
  local response_body="${resp%$'\n'HTTP_STATUS:*}"
  if [[ "$http_status" =~ ^2[0-9][0-9]$ ]]; then
    echo "  ✓ $label"
  else
    echo "  ✗ $label: HTTP $http_status — $response_body"
    failures=$((failures + 1))
    return 1
  fi
}

echo "=== Setting up Elasticsearch at $ES_URL ==="

# Wait for Elasticsearch to be ready (max 60s — fail fast if unreachable).
echo "Waiting for Elasticsearch..."
attempts=0
until curl -fsS "$ES_URL/_cluster/health" > /dev/null 2>&1; do
  attempts=$((attempts + 1))
  if [[ "$attempts" -gt 30 ]]; then
    echo "✗ Elasticsearch did not become reachable in 60 seconds — aborting"
    exit 1
  fi
  sleep 2
done
echo "✓ Elasticsearch is ready"

# 1. ILM policy (PUT replaces — idempotent)
put_resource "ILM policy: agent-logs-policy" \
  "/_ilm/policy/agent-logs-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/ilm-policy.json"

# 2. Index template for agent-logs-* (PUT replaces — idempotent)
put_resource "Index template: agent-logs-template" \
  "/_index_template/agent-logs-template" \
  "$PROJECT_ROOT/docker/elasticsearch/index-template.json"

# 3. Captain's Log index template (PUT replaces — idempotent)
put_resource "Index template: agent-captains-template" \
  "/_index_template/agent-captains-template" \
  "$PROJECT_ROOT/docker/elasticsearch/captains-index-template.json"

# 4. Joinability probe ILM policy (ADR-0074 Phase 5 / FRE-376).
put_resource "ILM policy: agent-monitors-joinability-policy" \
  "/_ilm/policy/agent-monitors-joinability-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-joinability-ilm-policy.json"

# 5. Joinability probe index template.
put_resource "Index template: agent-monitors-joinability-template" \
  "/_index_template/agent-monitors-joinability-template" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-joinability-index-template.json"

# 6. SLM request telemetry template (FRE-411). The slm_server shipper has no
#    template of its own, so without this the daily slm-requests-* index gets
#    default dynamic mapping (text join keys) and exact-match term joins on
#    trace_id/span_id silently return nothing — the exact failure mode this
#    script's header warns about.
put_resource "Index template: slm-requests-template" \
  "/_index_template/slm-requests-template" \
  "$PROJECT_ROOT/docker/elasticsearch/slm-requests-index-template.json"

# 7. Per-turn user value ratings template (FRE-407). dynamic:false keeps
#    prompt_component_ids as keyword (array) so mean-by-component aggregations
#    work. Retention: 90 days — ground-truth labels are worth keeping longer
#    than operational logs. Re-rate overwrites the doc (doc_id=trace_id).
put_resource "Index template: user-turn-ratings-template" \
  "/_index_template/user-turn-ratings-template" \
  "$PROJECT_ROOT/docker/elasticsearch/user-turn-ratings-index-template.json"

# 7. Initial write-alias index — only create if absent. The HEAD probe uses
#    `-f` so a 404 is reported as a non-fatal exit; we then PUT.
echo "Bootstrap write-alias index: agent-logs-000001"
if curl -fsS -I -o /dev/null "$ES_URL/agent-logs-000001" 2>/dev/null; then
  echo "  ✓ already exists — skipping"
else
  if curl -sS -fX PUT "$ES_URL/agent-logs-000001" \
      -H 'Content-Type: application/json' \
      -d '{"aliases":{"agent-logs":{"is_write_index":true}}}' >/dev/null 2>&1; then
    echo "  ✓ created"
  else
    echo "  ✗ create failed"
    failures=$((failures + 1))
  fi
fi

echo ""
if [[ "$failures" -gt 0 ]]; then
  echo "=== Elasticsearch setup completed with $failures failure(s) ==="
  echo "Review the messages above. Re-run is safe."
  exit 1
fi

echo "=== Elasticsearch setup complete ==="
echo "View logs at: http://localhost:5601 (Kibana)"
