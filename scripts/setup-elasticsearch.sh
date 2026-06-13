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

delete_resource() {
  # Args: <label> <url-path>
  # DELETEs a resource; a 404 (already absent) is treated as success so the
  # script stays idempotent. Used to tear down retired templates whose index
  # patterns would otherwise collide at equal priority with their replacements.
  local label="$1"
  local path="$2"
  local resp
  resp=$(curl -sS -w '\nHTTP_STATUS:%{http_code}' -X DELETE "$ES_URL$path" 2>&1) || true
  local http_status="${resp##*HTTP_STATUS:}"
  if [[ "$http_status" =~ ^2[0-9][0-9]$ || "$http_status" == "404" ]]; then
    echo "  ✓ $label"
  else
    echo "  ✗ $label: HTTP $http_status — ${resp%$'\n'HTTP_STATUS:*}"
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

# 3. Captain's Log index templates (FRE-534/A2 — split from the single
#    straddling agent-captains-template into three non-colliding shapes).
#    The retired template's patterns (captures-* + reflections-*) overlap the
#    split captures/reflections templates at the SAME priority (110), so it must
#    be DELETEd first or the PUTs below fail with an equal-priority conflict.
delete_resource "Retire template: agent-captains-template" \
  "/_index_template/agent-captains-template"
put_resource "Index template: agent-captains-captures-template" \
  "/_index_template/agent-captains-captures-template" \
  "$PROJECT_ROOT/docker/elasticsearch/captains-captures-index-template.json"
put_resource "Index template: agent-captains-reflections-template" \
  "/_index_template/agent-captains-reflections-template" \
  "$PROJECT_ROOT/docker/elasticsearch/captains-reflections-index-template.json"
# Sub-agent captures: priority 120 so it out-ranks the captures-* glob (110).
put_resource "Index template: agent-captains-subagents-template" \
  "/_index_template/agent-captains-subagents-template" \
  "$PROJECT_ROOT/docker/elasticsearch/captains-subagents-index-template.json"

# 3b. Insights engine template (FRE-534/A2 — family previously untemplated).
#     ILM (FRE-543): monthly agent-insights-YYYY-MM, delete at 365d (min_age).
put_resource "ILM policy: agent-insights-policy" \
  "/_ilm/policy/agent-insights-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/insights-ilm-policy.json"
put_resource "Index template: agent-insights-template" \
  "/_index_template/agent-insights-template" \
  "$PROJECT_ROOT/docker/elasticsearch/insights-index-template.json"

# 3c. SLM health probe template (FRE-534/A2 / ADR-0083 — previously untemplated;
#     trace_id was ES-default text, breaking exact-term joins).
#     ILM (FRE-543): monthly agent-monitors-slm-health-YYYY.MM, delete at 90d (min_age).
put_resource "ILM policy: agent-monitors-slm-health-policy" \
  "/_ilm/policy/agent-monitors-slm-health-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-slm-health-ilm-policy.json"
put_resource "Index template: agent-monitors-slm-health-template" \
  "/_index_template/agent-monitors-slm-health-template" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-slm-health-index-template.json"

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

# 7b. Execution-topology projection template (FRE-548 / ADR-0088). Projects the
#     Postgres route-trace ledger row (turn-level + per-sub-agent segments) to
#     agent-topology-*. dynamic:false with an explicit schema keeps join keys
#     (trace_id, task_id) as keyword, authoritative_cost_usd as double, and
#     latency_total_ms as float — the FRE-537 panel constraint. Unblocks the
#     execution-topology Kibana view deferred from FRE-537.
put_resource "Index template: agent-topology-template" \
  "/_index_template/agent-topology-template" \
  "$PROJECT_ROOT/docker/elasticsearch/topology-index-template.json"

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
