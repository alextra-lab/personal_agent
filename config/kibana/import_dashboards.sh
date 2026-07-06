#!/usr/bin/env bash
#
# Import all Kibana saved objects (dashboards, visualizations, index patterns).
# Requires a running Kibana instance.
#
# Usage:
#   ./config/kibana/import_dashboards.sh              # defaults to http://localhost:5601
#   KIBANA_URL=http://kibana:5601 ./config/kibana/import_dashboards.sh

set -euo pipefail

KIBANA_URL="${KIBANA_URL:-http://localhost:5601}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_DIR="${SCRIPT_DIR}/dashboards"

if [ ! -d "$DASHBOARD_DIR" ]; then
  echo "ERROR: Dashboard directory not found: $DASHBOARD_DIR"
  exit 1
fi

# Ordered so that index patterns load before visualizations before dashboards.
# data_views first (shared index-patterns + searches), then one file per dashboard.
FILES=(
  "data_views.ndjson"
  "system_health.ndjson"
  "task_analytics.ndjson"
  "request_timing.ndjson"
  "request_traces.ndjson"
  "self_improvement_funnel.ndjson"
  "extraction_retry_health.ndjson"
  "llm_performance.ndjson"
  "expansion_decomposition.ndjson"
  "intent_classification.ndjson"
  "prompt-cost-cache.ndjson"
  "cost_budget.ndjson"
  "traversal_gate.ndjson"
  "monitors_joinability_slm.ndjson"
  "turn_session_artifact.ndjson"
  "context_occupancy.ndjson"
)

echo "Importing dashboards into Kibana at ${KIBANA_URL} ..."
echo ""

for f in "${FILES[@]}"; do
  filepath="${DASHBOARD_DIR}/${f}"
  if [ ! -f "$filepath" ]; then
    echo "  SKIP  ${f} (file not found)"
    continue
  fi

  # Capture both the HTTP status and the response body. The _import endpoint
  # returns HTTP 200 even when individual objects fail (the failures are reported
  # in the JSON body), so trusting the status code alone hides broken imports.
  response=$(curl -s -w $'\n%{http_code}' \
    -X POST "${KIBANA_URL}/api/saved_objects/_import?overwrite=true" \
    -H "kbn-xsrf: true" \
    -F "file=@${filepath}")
  status=$(printf '%s' "$response" | tail -n1)
  body=$(printf '%s' "$response" | sed '$d')

  if [ "$status" = "200" ] && printf '%s' "$body" | grep -q '"success":true' \
     && ! printf '%s' "$body" | grep -q '"errors"'; then
    echo "  OK    ${f}"
  else
    echo "  FAIL  ${f} (HTTP ${status})"
    echo "        ${body}"
    fail=1
  fi
done

if [ "${fail:-0}" = "1" ]; then
  echo ""
  echo "ERROR: one or more files failed to import (see above)."
  exit 1
fi

echo ""
echo "Done. Visit ${KIBANA_URL}/app/dashboards to verify."
