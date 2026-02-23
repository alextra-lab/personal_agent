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
FILES=(
  "data_views.ndjson"
  "system_health.ndjson"
  "task_analytics.ndjson"
  "request_latency.ndjson"
  "request_timing.ndjson"
  "reflection_insights.ndjson"
  "insights_engine.ndjson"
)

echo "Importing dashboards into Kibana at ${KIBANA_URL} ..."
echo ""

for f in "${FILES[@]}"; do
  filepath="${DASHBOARD_DIR}/${f}"
  if [ ! -f "$filepath" ]; then
    echo "  SKIP  ${f} (file not found)"
    continue
  fi

  status=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${KIBANA_URL}/api/saved_objects/_import?overwrite=true" \
    -H "kbn-xsrf: true" \
    -F "file=@${filepath}")

  if [ "$status" = "200" ]; then
    echo "  OK    ${f}"
  else
    echo "  FAIL  ${f} (HTTP ${status})"
  fi
done

echo ""
echo "Done. Visit ${KIBANA_URL}/app/dashboards to verify."
