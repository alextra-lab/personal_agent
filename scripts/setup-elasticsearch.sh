#!/usr/bin/env bash
# Setup Elasticsearch index templates and ILM policies.
#
# This script is **idempotent** — safe to re-run after every container restart.
# PUTs for templates and policies replace existing definitions in place.
# Failures on each step do not bring down the whole script — we surface a
# clear summary at the end so a missing piece doesn't go silent.
#
# FRE-1036 (2026-07-31): every family here now writes monthly, ILM-managed
# indices (client picks the YYYY-MM bucket name; ILM does retention-only
# delete, no rollover action anywhere). This replaced a half-built
# rollover-alias scaffold for agent-logs (an unused agent-logs-000001
# bootstrap index behind an alias no writer ever used — removed) and fixed a
# live-broken rollover policy for agent-monitors-joinability (its ILM policy
# required index.lifecycle.rollover_alias, which no template set, so every
# index was permanently stuck in hot and never reached its 180d delete phase).
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

apply_live_index_mapping() {
  # Args: <write-alias> <template-file>
  #
  # Idempotently patches the current live write index with the explicit
  # properties declared in the template (additive only — ES rejects type
  # conflicts, which we surface as hard errors so they are never silently
  # skipped). Families without a write alias (date-partitioned indices) 404
  # on the alias lookup and are skipped cleanly.
  #
  # Intentionally applies only .template.mappings.properties, NOT
  # dynamic_templates or dynamic mode — those govern new-index creation only
  # and cannot be meaningfully back-applied to a live index.
  local alias="$1"
  local template_file="$2"
  local resp http_status resp_body live_index mapping_body

  # 1. Resolve the live write index for this alias.
  if ! resp=$(curl -sS -w '\nHTTP_STATUS:%{http_code}' "$ES_URL/_alias/$alias" 2>&1); then
    echo "  ✗ apply_live_index_mapping [$alias]: curl failed: $resp"
    failures=$((failures + 1))
    return 1
  fi
  http_status="${resp##*HTTP_STATUS:}"
  resp_body="${resp%$'\n'HTTP_STATUS:*}"

  if [[ "$http_status" == "404" ]]; then
    echo "  → [$alias] no write alias yet — skipping live-index mapping"
    return 0
  fi
  if [[ ! "$http_status" =~ ^2[0-9][0-9]$ ]]; then
    echo "  ✗ apply_live_index_mapping [$alias]: GET /_alias HTTP $http_status — $resp_body"
    failures=$((failures + 1))
    return 1
  fi

  live_index=$(python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
for idx, idata in data.items():
    for aname, ainfo in idata.get('aliases', {}).items():
        if ainfo.get('is_write_index', False):
            print(idx)
            sys.exit(0)
" <<< "$resp_body")

  if [[ -z "$live_index" ]]; then
    echo "  → [$alias] no is_write_index:true entry — skipping"
    return 0
  fi

  # 2. Extract properties from the template (single source of truth).
  mapping_body=$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    tpl = json.load(f)
props = tpl.get('template', {}).get('mappings', {}).get('properties', {})
print(json.dumps({'properties': props}))
" "$template_file")

  # 3. PUT mapping — additive only; ES rejects type conflicts with a 4xx so
  #    they surface here as hard errors (increment failures, don't swallow).
  echo "  → Patching live write index: $live_index"
  if ! resp=$(curl -sS -w '\nHTTP_STATUS:%{http_code}' -X PUT "$ES_URL/$live_index/_mapping" \
      -H 'Content-Type: application/json' \
      -d "$mapping_body" 2>&1); then
    echo "  ✗ apply_live_index_mapping [$alias]: PUT /_mapping curl failed: $resp"
    failures=$((failures + 1))
    return 1
  fi
  http_status="${resp##*HTTP_STATUS:}"
  resp_body="${resp%$'\n'HTTP_STATUS:*}"

  if [[ ! "$http_status" =~ ^2[0-9][0-9]$ ]]; then
    echo "  ✗ apply_live_index_mapping [$alias]: PUT /$live_index/_mapping HTTP $http_status — $resp_body"
    failures=$((failures + 1))
    return 1
  fi
  echo "  ✓ Mapping patched on $live_index"

  # 4. _field_caps assertion — every templated scalar leaf field must resolve
  #    to its declared type. Skip fields absent from _field_caps (no docs yet).
  #    Object/nested containers are excluded; they do not appear as typed leaves.
  if ! resp=$(curl -sS -w '\nHTTP_STATUS:%{http_code}' \
      "$ES_URL/$live_index/_field_caps?fields=*" 2>&1); then
    echo "  ✗ apply_live_index_mapping [$alias]: _field_caps curl failed: $resp"
    failures=$((failures + 1))
    return 1
  fi
  http_status="${resp##*HTTP_STATUS:}"
  resp_body="${resp%$'\n'HTTP_STATUS:*}"

  if [[ ! "$http_status" =~ ^2[0-9][0-9]$ ]]; then
    echo "  ✗ apply_live_index_mapping [$alias]: _field_caps HTTP $http_status — $resp_body"
    failures=$((failures + 1))
    return 1
  fi

  local validation_result
  if ! validation_result=$(python3 -c "
import json, sys

with open(sys.argv[1]) as f:
    tpl = json.load(f)
props = tpl.get('template', {}).get('mappings', {}).get('properties', {})
caps = json.loads(sys.stdin.read()).get('fields', {})

def check(props, prefix=''):
    mismatches = []
    for name, defn in props.items():
        path = (prefix + '.' + name) if prefix else name
        ftype = defn.get('type')
        sub = defn.get('properties')
        if sub:
            mismatches.extend(check(sub, path))
        elif ftype and ftype not in ('object', 'nested') and path in caps:
            actual = list(caps[path].keys())
            if ftype not in actual:
                mismatches.append(f'{path}: declared={ftype} actual={actual}')
    return mismatches

mm = check(props)
if mm:
    for m in mm:
        print('MISMATCH ' + m)
    sys.exit(1)
print('OK')
" "$template_file" <<< "$resp_body" 2>&1); then
    echo "  ✗ _field_caps type mismatch on $live_index:"
    echo "$validation_result" | sed 's/^/    /'
    failures=$((failures + 1))
    return 1
  fi
  echo "  ✓ _field_caps verified on $live_index"
}

put_and_apply_template() {
  # Args: same as put_resource — <label> <url-path> <template-file>
  #
  # PUTs the index template then idempotently applies its explicit field
  # mappings to the current live write index (see apply_live_index_mapping).
  # Uses 'if put_resource ...' rather than a bare call so that a template PUT
  # failure increments failures without exiting the script under set -e, then
  # skips the live-index patch (no point patching if the template didn't update).
  if put_resource "$1" "$2" "$3"; then
    local alias
    alias=$(python3 -c "
import sys
pattern = open(sys.argv[1]).read()
import json
tpl = json.loads(pattern)
p = tpl.get('index_patterns', [''])[0]
print(p.rstrip('*').rstrip('-'))
" "$3")
    apply_live_index_mapping "$alias" "$3"
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
put_and_apply_template "Index template: agent-logs-template" \
  "/_index_template/agent-logs-template" \
  "$PROJECT_ROOT/docker/elasticsearch/index-template.json"

# 3. Captain's Log index templates (FRE-534/A2 — split from the single
#    straddling agent-captains-template into three non-colliding shapes).
#    The retired template's patterns (captures-* + reflections-*) overlap the
#    split captures/reflections templates at the SAME priority (110), so it must
#    be DELETEd first or the PUTs below fail with an equal-priority conflict.
#    ILM (FRE-1036): monthly agent-captains-captures-YYYY-MM / agent-captains-
#    reflections-YYYY-MM, delete at 90d/180d (min_age). PUT the policy before the
#    template so new indices bind on creation. Previously undeleted by any ILM
#    policy — the client-side lifecycle_manager.py sweep (retired by FRE-1036)
#    was the only deleter.
put_resource "ILM policy: agent-captains-captures-policy" \
  "/_ilm/policy/agent-captains-captures-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/captains-captures-ilm-policy.json"
put_resource "ILM policy: agent-captains-reflections-policy" \
  "/_ilm/policy/agent-captains-reflections-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/captains-reflections-ilm-policy.json"
delete_resource "Retire template: agent-captains-template" \
  "/_index_template/agent-captains-template"
put_and_apply_template "Index template: agent-captains-captures-template" \
  "/_index_template/agent-captains-captures-template" \
  "$PROJECT_ROOT/docker/elasticsearch/captains-captures-index-template.json"
put_and_apply_template "Index template: agent-captains-reflections-template" \
  "/_index_template/agent-captains-reflections-template" \
  "$PROJECT_ROOT/docker/elasticsearch/captains-reflections-index-template.json"
# Sub-agent captures: priority 120 so it out-ranks the captures-* glob (110).
# Shares agent-captains-captures-policy (same retention as the parent family).
put_and_apply_template "Index template: agent-captains-subagents-template" \
  "/_index_template/agent-captains-subagents-template" \
  "$PROJECT_ROOT/docker/elasticsearch/captains-subagents-index-template.json"

# 3a-ii. Self-improvement funnel events (ADR-0105 D6, FRE-719). Monthly index
#        (FRE-1036), no write alias — apply_live_index_mapping skips cleanly
#        via its 404 path. ILM (FRE-1036): delete at 90d (min_age).
put_resource "ILM policy: agent-captains-funnel-events-policy" \
  "/_ilm/policy/agent-captains-funnel-events-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/captains-funnel-events-ilm-policy.json"
put_and_apply_template "Index template: agent-captains-funnel-events-template" \
  "/_index_template/agent-captains-funnel-events-template" \
  "$PROJECT_ROOT/docker/elasticsearch/captains-funnel-events-index-template.json"

# 3b. Insights engine template (FRE-534/A2 — family previously untemplated).
#     ILM (FRE-543): monthly agent-insights-YYYY-MM, delete at 365d (min_age).
put_resource "ILM policy: agent-insights-policy" \
  "/_ilm/policy/agent-insights-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/insights-ilm-policy.json"
put_and_apply_template "Index template: agent-insights-template" \
  "/_index_template/agent-insights-template" \
  "$PROJECT_ROOT/docker/elasticsearch/insights-index-template.json"

# 3c. SLM health probe template (FRE-534/A2 / ADR-0083 — previously untemplated;
#     trace_id was ES-default text, breaking exact-term joins).
#     ILM (FRE-543): monthly agent-monitors-slm-health-YYYY.MM, delete at 90d (min_age).
put_resource "ILM policy: agent-monitors-slm-health-policy" \
  "/_ilm/policy/agent-monitors-slm-health-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-slm-health-ilm-policy.json"
put_and_apply_template "Index template: agent-monitors-slm-health-template" \
  "/_index_template/agent-monitors-slm-health-template" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-slm-health-index-template.json"

# 4. Joinability probe ILM policy (ADR-0074 Phase 5 / FRE-376).
put_resource "ILM policy: agent-monitors-joinability-policy" \
  "/_ilm/policy/agent-monitors-joinability-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-joinability-ilm-policy.json"

# 5. Joinability probe index template.
put_and_apply_template "Index template: agent-monitors-joinability-template" \
  "/_index_template/agent-monitors-joinability-template" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-joinability-index-template.json"

# 5b. Per-substrate joinability flat projection template (FRE-550). priority 200
#     so it strictly outranks the dynamic:false parent agent-monitors-joinability-*
#     (priority 100) template for the -substrate-* indices. Shares the joinability
#     ILM policy registered above.
put_and_apply_template "Index template: agent-monitors-joinability-substrate-template" \
  "/_index_template/agent-monitors-joinability-substrate-template" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-joinability-substrate-index-template.json"

# 6. SLM request telemetry ILM policy (FRE-1106). Mirror the application-log
#    policy (hot at zero, delete at 30d, no warm phase). PUT the policy before
#    the template so new indices bind on creation.
put_resource "ILM policy: slm-requests-policy" \
  "/_ilm/policy/slm-requests" \
  "$PROJECT_ROOT/docker/elasticsearch/slm-requests-ilm-policy.json"

# 6a. SLM request telemetry template (FRE-411). The slm_server shipper has no
#     template of its own, so without this the daily slm-requests-* index gets
#     default dynamic mapping (text join keys) and exact-match term joins on
#     trace_id/span_id silently return nothing — the exact failure mode this
#     script's header warns about. Now lifecycle-managed (FRE-1106).
put_and_apply_template "Index template: slm-requests-template" \
  "/_index_template/slm-requests-template" \
  "$PROJECT_ROOT/docker/elasticsearch/slm-requests-index-template.json"

# 7. Per-turn user value ratings template (FRE-407). dynamic:false keeps
#    prompt_component_ids as keyword (array) so mean-by-component aggregations
#    work. Re-rate overwrites the doc (doc_id=trace_id).
#    ILM (FRE-559): monthly user-turn-ratings-YYYY.MM, delete at 365d (min_age) —
#    ground-truth labels kept to the agent-insights-* horizon. ILM is now the sole
#    deleter (the lifecycle_manager 90d sweep override was removed). PUT the policy
#    before the template so new indices bind on creation.
put_resource "ILM policy: user-turn-ratings-policy" \
  "/_ilm/policy/user-turn-ratings-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/user-turn-ratings-ilm-policy.json"
put_and_apply_template "Index template: user-turn-ratings-template" \
  "/_index_template/user-turn-ratings-template" \
  "$PROJECT_ROOT/docker/elasticsearch/user-turn-ratings-index-template.json"

# 7c. Live-projector bus-delivery health template (FRE-557 / ADR-0088 D6). One doc per
#     trace at turn completion; model_calls_received vs COUNT(api_costs) detects
#     stream:turn.observed delivery loss to the projector (orthogonal to cost_reconciled
#     accumulator drift). dynamic:false explicit schema — join key keyword, *_usd double.
#     ILM (FRE-1036): monthly, delete at 90d (min_age) — family had no policy before.
put_resource "ILM policy: agent-monitors-projector-health-policy" \
  "/_ilm/policy/agent-monitors-projector-health-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-projector-health-ilm-policy.json"
put_and_apply_template "Index template: agent-monitors-projector-health-template" \
  "/_index_template/agent-monitors-projector-health-template" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-projector-health-index-template.json"

# 7b-ii. Cache-reset cadence monitor (ADR-0092 §D7, FRE-572). One doc per
#     frozen-reset firing: actual_turns vs l_star (ADR-0081 computed optimum).
#     dynamic:false — explicit double for l_star/deviation_turns guards the
#     first-value-0.0-to-long trap; l_star/deviation_turns are null when
#     optimal_run_length=inf (no hold-cost pressure).
#     ILM (FRE-1036): monthly, delete at 90d (min_age) — family had no policy before.
put_resource "ILM policy: agent-monitors-cache-reset-cadence-policy" \
  "/_ilm/policy/agent-monitors-cache-reset-cadence-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-cache-reset-cadence-ilm-policy.json"
put_and_apply_template "Index template: agent-monitors-cache-reset-cadence-template" \
  "/_index_template/agent-monitors-cache-reset-cadence-template" \
  "$PROJECT_ROOT/docker/elasticsearch/monitors-cache-reset-cadence-index-template.json"

# 7b. Execution-topology projection template (FRE-548 / ADR-0088). Projects the
#     Postgres route-trace ledger row (turn-level + per-sub-agent segments) to
#     agent-topology-*. dynamic:false with an explicit schema keeps join keys
#     (trace_id, task_id) as keyword, authoritative_cost_usd as double, and
#     latency_total_ms as float — the FRE-537 panel constraint. Unblocks the
#     execution-topology Kibana view deferred from FRE-537.
#     ILM (FRE-1036): monthly, delete at 90d (min_age) — family had no policy before.
put_resource "ILM policy: agent-topology-policy" \
  "/_ilm/policy/agent-topology-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/topology-ilm-policy.json"
put_and_apply_template "Index template: agent-topology-template" \
  "/_index_template/agent-topology-template" \
  "$PROJECT_ROOT/docker/elasticsearch/topology-index-template.json"

# 8. Caddy access-log evidence, shipped by the Filebeat sidecar (FRE-1146 / ADR-0132 D3).
#    caddy.logger distinguishes egress-block traffic (http.log.access.egress_slm/.egress_artifacts)
#    from inbound. ILM (FRE-1036): monthly, warm forcemerge at 32d, delete at 90d (min_age) — the
#    default for a new family with no existing precedent. PUT the policy before the template so
#    new indices bind on creation.
put_resource "ILM policy: caddy-access-policy" \
  "/_ilm/policy/caddy-access-policy" \
  "$PROJECT_ROOT/docker/elasticsearch/caddy-access-ilm-policy.json"
put_and_apply_template "Index template: caddy-access-template" \
  "/_index_template/caddy-access-template" \
  "$PROJECT_ROOT/docker/elasticsearch/caddy-access-index-template.json"

echo ""
if [[ "$failures" -gt 0 ]]; then
  echo "=== Elasticsearch setup completed with $failures failure(s) ==="
  echo "Review the messages above. Re-run is safe."
  exit 1
fi

echo "=== Elasticsearch setup complete ==="
echo "View logs at: http://localhost:5601 (Kibana)"
