#!/usr/bin/env bash
# FRE-380 Stage 1 soak measurement script.
#
# Run once daily between 2026-05-24 and 2026-05-30 (or on-demand). Captures
# the metrics that feed FRE-381 (Stage 2) design decisions, plus the
# joinability probe trend that gates ADR-0074 → Accepted.
#
# Outputs to stdout; redirect to a dated file when collecting baselines:
#     bash scripts/soak/fre-380-stage1-soak.sh > soak-$(date -u +%Y%m%d).txt
#
# Read-only — no writes, no mutations.

set -uo pipefail

ES_URL="${ES_URL:-http://localhost:9200}"
PG_URL="${PG_URL:-postgresql://agent:agent_dev_password@localhost:5432/personal_agent}"
NEO4J_HTTP="${NEO4J_HTTP:-http://localhost:7474}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-$(docker exec cloud-sim-seshat-gateway env 2>/dev/null | sed -n 's/^NEO4J_PASSWORD=//p')}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-neo4j_dev_password}"

section() {
  echo
  echo "════════════════════════════════════════════════════════════════"
  echo "$1"
  echo "════════════════════════════════════════════════════════════════"
}

es_query() {
  # $1 = index pattern, $2 = body
  curl -fsS "$ES_URL/$1/_search" -H 'Content-Type: application/json' -d "$2" \
    || echo '{"error":"es_unreachable"}'
}

pg_query() {
  # $1 = SQL
  docker exec -e PGPASSWORD=agent_dev_password cloud-sim-postgres \
    psql -U agent -d personal_agent -A -c "$1" 2>/dev/null \
    || echo "pg_unreachable"
}

neo4j_query() {
  # $1 = Cypher
  curl -fsS -u "$NEO4J_USER:$NEO4J_PASSWORD" \
    -H 'Content-Type: application/json' -H 'Accept: application/json' \
    "$NEO4J_HTTP/db/neo4j/tx/commit" \
    -d "{\"statements\":[{\"statement\":\"$1\"}]}" \
    2>/dev/null || echo '{"error":"neo4j_unreachable"}'
}

echo "FRE-380 Stage 1 soak — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Host: $(hostname)  Branch: $(git -C /opt/seshat rev-parse --short HEAD)"

# ─── 1. Stub-Turn vs entity-attached Turn ratio ──────────────────────────────
section "1. Consolidation outcome distribution (last 24h)"
pg_query "
SELECT outcome,
       COUNT(*) AS attempts,
       COUNT(DISTINCT trace_id) AS distinct_traces
FROM consolidation_attempts
WHERE started_at > now() - interval '1 day'
GROUP BY outcome
ORDER BY attempts DESC;
"
echo "DECISION: if extraction_capped > 10% of total, root-cause before Stage 2"

section "1b. Consolidation outcome distribution (last 7d)"
pg_query "
SELECT date_trunc('day', started_at) AS day,
       outcome,
       COUNT(*) AS attempts
FROM consolidation_attempts
WHERE started_at > now() - interval '7 days'
GROUP BY day, outcome
ORDER BY day DESC, attempts DESC;
"

# ─── 2. Attempt distribution at the point of capping ─────────────────────────
section "2. Attempt distribution leading up to extraction_capped"
pg_query "
WITH capped AS (
  SELECT trace_id FROM consolidation_attempts
   WHERE outcome = 'extraction_capped'
     AND started_at > now() - interval '7 days'
)
SELECT a.attempt_number, a.outcome, COUNT(*) AS n
FROM consolidation_attempts a
JOIN capped c USING (trace_id)
WHERE a.role = 'entity_extraction'
GROUP BY a.attempt_number, a.outcome
ORDER BY a.attempt_number, n DESC;
"
echo "DECISION: identifies which fail-mode dominates capping (fallback / budget / model_error)"

# ─── 3. Consolidator window coverage (codex open question #6) ────────────────
section "3. Consolidator window coverage check"
echo "── Captures (FS — telemetry/captains_log/captures/) ──"
echo -n "  total .json files: "
find /opt/seshat/telemetry/captains_log/captures -name '*.json' 2>/dev/null | wc -l
echo -n "  oldest: "; find /opt/seshat/telemetry/captains_log/captures -name '*.json' 2>/dev/null | sort | head -1
echo -n "  newest: "; find /opt/seshat/telemetry/captains_log/captures -name '*.json' 2>/dev/null | sort | tail -1
echo
echo "── Captures (ES agent-captains-captures-*) ──"
es_query "agent-captains-captures-*" '{"size":0,"aggs":{"oldest":{"min":{"field":"timestamp"}},"newest":{"max":{"field":"timestamp"}},"total":{"value_count":{"field":"trace_id"}}}}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); a=d.get('aggregations',{}); print('  total:', a.get('total',{}).get('value','?')); print('  oldest:', a.get('oldest',{}).get('value_as_string','?')); print('  newest:', a.get('newest',{}).get('value_as_string','?'))"
echo "── Turns Neo4j has ──"
neo4j_query 'MATCH (t:Turn) RETURN min(t.timestamp) AS oldest, max(t.timestamp) AS newest, count(t) AS total' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); r=d.get('results',[{}])[0].get('data',[{}])[0].get('row',[]); print(r if r else d)"
echo
echo "── Captures without a Turn (anti-join via union) ──"
# Captures from FS (the durable source — ES is a backfilled mirror). Get most-recent 200.
ls -t /opt/seshat/telemetry/captains_log/captures/*/*.json 2>/dev/null \
  | head -200 \
  | xargs -n1 basename 2>/dev/null \
  | sed 's/\.json$//' \
  > /tmp/soak-captures.tsv
# Neo4j turns → trace_ids
neo4j_query 'MATCH (t:Turn) RETURN t.trace_id AS trace_id LIMIT 5000' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); rows=d.get('results',[{}])[0].get('data',[]); [print(r['row'][0]) for r in rows if r.get('row') and r['row'][0]]" \
  > /tmp/soak-turns.tsv 2>/dev/null
echo "  recent captures: $(wc -l < /tmp/soak-captures.tsv)"
echo "  neo4j turns:     $(wc -l < /tmp/soak-turns.tsv)"
echo "  captures-without-turn (set diff, recent 200):"
sort /tmp/soak-captures.tsv -u > /tmp/soak-captures-sorted.tsv
sort /tmp/soak-turns.tsv -u    > /tmp/soak-turns-sorted.tsv
comm -23 /tmp/soak-captures-sorted.tsv /tmp/soak-turns-sorted.tsv | head -20
echo "DECISION: if non-empty AND timestamps fall outside consolidator window, file backfill ticket"

# ─── 4. Stub Turn UX impact ──────────────────────────────────────────────────
section "4. Stub Turns surfacing in recall queries"
neo4j_query 'MATCH (t:Turn) WHERE t.properties.extraction_outcome = "capped_after_retries" RETURN count(t) AS stubs' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); r=d.get('results',[{}])[0].get('data',[{}])[0].get('row',[]); print('  stub Turns in graph:', r if r else d)"
echo
echo "  Recent stub Turns (5 most recent):"
neo4j_query 'MATCH (t:Turn) WHERE t.properties.extraction_outcome = "capped_after_retries" RETURN t.turn_id, t.session_id, t.timestamp ORDER BY t.timestamp DESC LIMIT 5' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); rows=d.get('results',[{}])[0].get('data',[]); [print('   ', r['row']) for r in rows]"
echo "DECISION: if stubs surface prominently in PWA recall, add filter or flag in Stage 2"

# ─── 5. Joinability probe outcome trend (7d) ─────────────────────────────────
section "5. Joinability probe outcomes — last 7 days"
es_query "agent-monitors-joinability-*" '{
  "size": 0,
  "query": {"range": {"started_at": {"gte": "now-7d"}}},
  "aggs": {
    "by_day": {
      "date_histogram": {"field": "started_at", "calendar_interval": "1d"},
      "aggs": {"by_outcome": {"terms": {"field": "outcome"}}}
    }
  }
}' | python3 -c "
import sys, json
d = json.load(sys.stdin)
buckets = d.get('aggregations', {}).get('by_day', {}).get('buckets', [])
print(f'  {\"day\":12} {\"green\":>5} {\"yellow\":>6} {\"red\":>4} {\"skipped\":>7} {\"total\":>5}')
for b in buckets:
    outcomes = {x['key']: x['doc_count'] for x in b.get('by_outcome', {}).get('buckets', [])}
    day = b['key_as_string'][:10]
    print(f'  {day:12} {outcomes.get(\"green\",0):>5} {outcomes.get(\"yellow\",0):>6} {outcomes.get(\"red\",0):>4} {outcomes.get(\"skipped\",0):>7} {b[\"doc_count\"]:>5}')
"
echo "DECISION: 7 green days, each ≥12 runs → flip ADR-0074 → Accepted, FRE-376 Done"

# ─── 6. Orphan kind distribution (where the probe is still finding bugs) ─────
section "6. Orphan kinds — last 7 days"
es_query "agent-monitors-joinability-*" '{
  "size": 0,
  "query": {"range": {"started_at": {"gte": "now-7d"}}},
  "aggs": {
    "orphan_kinds": {
      "nested": {"path": "orphans"},
      "aggs": {"kinds": {"terms": {"field": "orphans.kind", "size": 10}}}
    }
  }
}' | python3 -c "
import sys, json
d = json.load(sys.stdin)
for b in d.get('aggregations', {}).get('orphan_kinds', {}).get('kinds', {}).get('buckets', []):
    print(f'  {b[\"key\"]:30}  {b[\"doc_count\"]:>5}')
"

# ─── 7. Summary line ─────────────────────────────────────────────────────────
section "SUMMARY"
echo "Run date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo
echo "Feeds into:"
echo "  - FRE-381 PR description (Stage 2 design adjustments based on §1-#4)"
echo "  - ADR-0074 gate verdict (May 30 — §5)"
echo "  - Open question on backfill scope (§3 — implicit-catch-up vs explicit-script)"
echo "  - Stub Turn UX decision for FRE-381 AC-5 (§4)"
