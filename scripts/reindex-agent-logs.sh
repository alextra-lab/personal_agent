#!/usr/bin/env bash
# Reindex existing `agent-logs-*` daily indices through the (now-correct) index
# template so fields like `level`, `event_type`, `tool_name` end up as pure
# `keyword` instead of the legacy `text + .keyword` ES default.
#
# Per-index flow:
#   1. Skip indices already ending in `-v2` (idempotent re-runs).
#   2. Create empty `<idx>-v2` (inherits the registered template).
#   3. Reindex source → v2.
#   4. Verify doc count parity before destructive ops.
#   5. Delete source index.
#   6. Add alias <idx> → <idx>-v2 so existing queries still find data by the
#      familiar name.
#
# Total volume is small (~600 MB across ~30 indices); expect 2–5 minutes.
#
# Usage:
#   bash scripts/reindex-agent-logs.sh                   # against http://localhost:9200
#   ES_URL=http://localhost:9200 bash scripts/reindex-agent-logs.sh

set -euo pipefail

ES_URL="${ES_URL:-http://localhost:9200}"
PATTERN="${PATTERN:-agent-logs-*}"

echo "=== Reindex run ==="
echo "ES_URL: $ES_URL"
echo "Pattern: $PATTERN"
echo ""

# Wait for cluster to be reachable
until curl -fsS "$ES_URL/_cluster/health" >/dev/null 2>&1; do
  echo "Waiting for Elasticsearch..."
  sleep 2
done

# Discover indices, skip already-reindexed ones
mapfile -t INDICES < <(
  curl -fsS "$ES_URL/_cat/indices/$PATTERN?h=index" \
    | awk '{print $1}' \
    | grep -v -- '-v2$' \
    | sort
)

if [[ ${#INDICES[@]} -eq 0 ]]; then
  echo "No indices match $PATTERN that need reindexing."
  exit 0
fi

echo "Indices to reindex: ${#INDICES[@]}"
printf '  %s\n' "${INDICES[@]}"
echo ""

reindexed=0
skipped=0
failed=0

for idx in "${INDICES[@]}"; do
  v2="${idx}-v2"
  echo "── $idx → $v2 ──"

  # Source doc count
  src_count=$(curl -fsS "$ES_URL/$idx/_count" | python3 -c "import json,sys;print(json.load(sys.stdin)['count'])")
  echo "  source docs: $src_count"

  if [[ "$src_count" -eq 0 ]]; then
    echo "  empty index — deleting in place, no v2 needed"
    curl -fsS -X DELETE "$ES_URL/$idx" >/dev/null
    skipped=$((skipped + 1))
    echo ""
    continue
  fi

  # Create destination (inherits template)
  if curl -fsS -o /dev/null "$ES_URL/$v2"; then
    echo "  $v2 already exists — assuming prior run, will verify count"
  else
    curl -fsS -X PUT "$ES_URL/$v2" -H 'Content-Type: application/json' \
      -d '{}' >/dev/null
    echo "  created $v2"
  fi

  # Reindex (synchronous, wait_for_completion=true)
  echo "  reindexing..."
  reindex_resp=$(curl -fsS -X POST "$ES_URL/_reindex?refresh=true" \
    -H 'Content-Type: application/json' \
    -d "{\"source\":{\"index\":\"$idx\"},\"dest\":{\"index\":\"$v2\"}}")
  reindex_total=$(echo "$reindex_resp" | python3 -c "import json,sys;print(json.load(sys.stdin).get('total',0))")
  reindex_failures=$(echo "$reindex_resp" | python3 -c "import json,sys;print(len(json.load(sys.stdin).get('failures',[])))")

  # Confirm parity before destructive ops
  v2_count=$(curl -fsS "$ES_URL/$v2/_count" | python3 -c "import json,sys;print(json.load(sys.stdin)['count'])")
  echo "  reindex total=$reindex_total failures=$reindex_failures dest_count=$v2_count"

  if [[ "$reindex_failures" -ne 0 ]] || [[ "$v2_count" -ne "$src_count" ]]; then
    echo "  ✗ MISMATCH or failures — leaving source alone for inspection"
    failed=$((failed + 1))
    echo ""
    continue
  fi

  # Drop the original, swap in the alias
  curl -fsS -X DELETE "$ES_URL/$idx" >/dev/null
  curl -fsS -X POST "$ES_URL/_aliases" -H 'Content-Type: application/json' \
    -d "{\"actions\":[{\"add\":{\"index\":\"$v2\",\"alias\":\"$idx\"}}]}" >/dev/null

  echo "  ✓ swap complete: $idx is now an alias to $v2"
  reindexed=$((reindexed + 1))
  echo ""
done

echo "=== Summary ==="
echo "  reindexed: $reindexed"
echo "  skipped (empty): $skipped"
echo "  failed: $failed"

# Spot-check on the largest index that was reindexed
if [[ "$reindexed" -gt 0 ]]; then
  largest=$(curl -fsS "$ES_URL/_cat/indices/agent-logs-*-v2?h=index,docs.count&s=docs.count:desc" | head -1 | awk '{print $1}')
  if [[ -n "$largest" ]]; then
    echo ""
    echo "=== Verification — level field type on $largest ==="
    curl -fsS "$ES_URL/$largest/_mapping/field/level" \
      | python3 -c "
import json,sys
d=json.load(sys.stdin)
idx=list(d.keys())[0]
m=d[idx]['mappings'].get('level',{}).get('mapping',{}).get('level',{})
sub=list(m.get('fields',{}).keys())
print(f'  type={m.get(\"type\")} subfields={sub}')
print('  expected: type=keyword subfields=[]')
"
  fi
fi
