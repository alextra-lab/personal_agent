---
name: query-elasticsearch
description: Query Elasticsearch indices for log analysis, telemetry, errors, and self-diagnosis. Use the JSON `_search` DSL — it's deterministic. ES|QL is reserved for ad-hoc analytics and has syntax gotchas. Primary path uses bash curl.
when_to_use: When you need to inspect agent telemetry, logs, errors, traces, or query ES directly. Always use agent-logs-* — never logs-*.
tools: [bash]
keywords:
  # Natural user phrasing — what a person actually types
  - logs
  - " log"
  - traces
  - telemetry
  - what happened
  - recent errors
  - app errors
  - check your logs
  - show me what
  # Technical / operator phrasing
  - agent-log
  - trace_id
  - kibana
  - query_elasticsearch
  - loop gate
  - litellm
  - tool_call
  - last hour
  - last day
  - 24 hour
  - p95
  - latency
  - errors in the
  - event_type
  - esql
  - agent-logs
  - loop trace
  - warn_consecutive
  - block_consecutive
  - query elasticsearch
  - search elasticsearch
canonical_patterns:
  - "agent-logs-*"
  - "agent-captains-captures-*"
  - "agent-captains-reflections-*"
known_bad_patterns:
  - pattern: "/logs-*"
    applies_to:
      tool: bash
      fields: [command]
    reason: "Generic '/logs-*' index does not exist in Seshat telemetry."
    suggestion: "Use '/agent-logs-*' — the correct index family starts with 'agent-'."
  - pattern: "agent-events-*"
    applies_to:
      tool: bash
      fields: [command]
    reason: "Index 'agent-events-*' does not exist."
    suggestion: "Use 'agent-logs-*' for events; filter by 'event_type' field."
  - pattern: "agent-traces-*"
    applies_to:
      tool: bash
      fields: [command]
    reason: "Index 'agent-traces-*' does not exist."
    suggestion: "Use 'agent-logs-*'; traces are logged events filtered by fields."
  - pattern: "agent-telemetry-*"
    applies_to:
      tool: bash
      fields: [command]
    reason: "Index 'agent-telemetry-*' does not exist."
    suggestion: "Use 'agent-logs-*' for all structured telemetry."
---

# query-elasticsearch — Query ES indices, inspect schema, read self-telemetry

**Status:** Primary path (FRE-263, 2026-04-28). Legacy `query_elasticsearch` tool is no longer registered in production (`AGENT_LEGACY_TOOLS_ENABLED=false`).

**Category:** `system_read` · **Risk:** low · **Approval:** `bash curl` auto-approved (NORMAL); `run_python` auto-approved (NORMAL/ALERT/DEGRADED)

## Actual indices (empirically verified 2026-04-28)

The cluster has four index families. **Do not guess index names** — use only these patterns:

| Pattern | Purpose | Example |
|---------|---------|---------|
| `agent-logs-YYYY.MM.DD` | All structured log events (primary telemetry) | `agent-logs-2026.04.28` |
| `agent-captains-captures-YYYY-MM-DD` | Captain's Log per-request captures | `agent-captains-captures-2026-04-28` |
| `agent-captains-reflections-YYYY-MM-DD` | DSPy self-reflection outputs | `agent-captains-reflections-2026-04-28` |
| `agent-insights-YYYY-MM-DD` | Cross-session insights and anomalies | `agent-insights-2026-04-28` |

**Wildcard queries:** `agent-logs-*` (all days), `agent-captains-captures-*`, `agent-captains-reflections-*`, `agent-insights-*`.

**Non-existent patterns** (404 errors): `agent-events-*`, `agent-traces-*`, `agent-telemetry-*` — these do not exist.

## Key fields in `agent-logs-*`

> **Mapping note (2026-05-10 cleanup):** All `keyword` fields below are now **pure `keyword`** (no `.keyword` subfield needed) on every daily index, including older snapshots — they were reindexed against a corrected template. ES|QL term equality (`WHERE level == "ERROR"`) works directly. If you ever see term equality silently returning null, double-check the field's mapping with `_mapping/field/<name>` — a regression in the template would manifest as `text + .keyword` again, and the query would need the `.keyword` suffix to work.

Most important fields for queries:

| Field | Type | Purpose |
|-------|------|---------|
| `@timestamp` | date | Event time |
| `level` | keyword | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `message` | text | Log message (full-text search; not for term equality) |
| `trace_id` | keyword | Request trace ID |
| `session_id` | keyword | Session identifier |
| `event_type` | keyword | What happened (e.g. `tool_call_started`, `litellm_request_complete`) |
| `action` | keyword | Sub-event action |
| `task_type` | keyword | Gateway intent classification |
| `mode` | keyword | Agent mode: `NORMAL`, `ALERT`, etc. |
| `tool_name` | keyword | Name of tool called |
| `prompt_tokens` | long | Input tokens to LLM |
| `completion_tokens` | long | Output tokens from LLM |
| `cache_read_input_tokens` | long | Cache-hit tokens (prompt caching) |
| `cache_creation_input_tokens` | long | Cache-miss tokens (new cache entry) |
| `cost_usd` | float | Cost of LLM call |
| `elapsed_s` | float | Elapsed wall time in seconds |
| `elapsed_ms` / `duration_ms` | long | Elapsed time in milliseconds |
| `success` | boolean | Whether the operation succeeded |
| `error` | text + `.keyword` | Free-form error message; use `error.keyword` for term equality / aggregations, `error` for full-text search |
| `turn_count` | long | Number of LLM turns in request |
| `model` / `model_id` | keyword | LLM model used |

## When to skip discovery

**For known-pattern questions** (counts, recent errors, cost by task type, trace lookup), use the one-shot canned recipes below directly — no `/_cat/indices` call needed. The index names and field names in this doc are empirically verified (2026-04-28).

**Run discovery only when:** the question involves an index pattern or field name not in this doc, or you're getting empty results/404s that suggest an assumption is wrong.

## Discovery step — for unfamiliar queries only

```bash
# 1. Verify index pattern exists and has data
curl -s 'http://elasticsearch:9200/_cat/indices?v&h=index,docs.count' | grep agent

# 2. Get exact field names
curl -s 'http://elasticsearch:9200/agent-logs-*/_mapping' \
  | jq 'to_entries[0].value.mappings.properties | keys | sort'
```

## Querying Elasticsearch — prefer `_search` (JSON DSL)

> **Use `_search` for everything in this skill.** ES|QL has subtle syntax gotchas (time-unit pluralisation, `KEEP` vs `PROJECT`, `COUNT_IF` vs ternaries) that send the agent into iteration loops trying variants that fail silently. The JSON DSL is verbose but deterministic — every field, query type, and aggregation is named explicitly. Post-2026-05-10 reindex, all named keyword fields are pure keyword so `term` filters work directly with no `.keyword` suffix.

### Canonical recipes for `agent-logs-*`

```bash
# Count events in the last 3 hours by level (errors / warnings / etc.)
curl -s -X POST 'http://elasticsearch:9200/agent-logs-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 0,
    "query": {"range": {"@timestamp": {"gte": "now-3h"}}},
    "aggs": {"by_level": {"terms": {"field": "level", "size": 10}}}
  }' | jq '.aggregations.by_level.buckets'

# Top N event_types in the last hour
curl -s -X POST 'http://elasticsearch:9200/agent-logs-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 0,
    "query": {"range": {"@timestamp": {"gte": "now-1h"}}},
    "aggs": {"by_event": {"terms": {"field": "event_type", "size": 20}}}
  }' | jq '.aggregations.by_event.buckets'

# Recent ERROR events with details
curl -s -X POST 'http://elasticsearch:9200/agent-logs-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 20,
    "query": {"bool": {"must": [
      {"term": {"level": "ERROR"}},
      {"range": {"@timestamp": {"gte": "now-3h"}}}
    ]}},
    "sort": [{"@timestamp": "desc"}],
    "_source": ["@timestamp","level","event_type","message","tool_name","trace_id","error"]
  }' | jq ".hits.hits[]._source"

# All events for one trace (by trace_id)
curl -s -X POST 'http://elasticsearch:9200/agent-logs-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 200,
    "query": {"term": {"trace_id": "<trace_id>"}},
    "sort": [{"@timestamp": "asc"}],
    "_source": ["@timestamp","event_type","level","tool_name","duration_ms","latency_ms","model_id"]
  }' | jq ".hits.hits[]._source"

# Total tokens / cost in last 24h, grouped by task_type
curl -s -X POST 'http://elasticsearch:9200/agent-logs-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 0,
    "query": {"range": {"@timestamp": {"gte": "now-24h"}}},
    "aggs": {
      "by_task": {
        "terms": {"field": "task_type", "size": 10},
        "aggs": {
          "input": {"sum": {"field": "prompt_tokens"}},
          "output": {"sum": {"field": "completion_tokens"}},
          "cost": {"sum": {"field": "cost_usd"}}
        }
      }
    }
  }' | jq '.aggregations.by_task.buckets'

# Loop-gate fires (consecutive or identity blocks) in last 7 days
curl -s -X POST 'http://elasticsearch:9200/agent-logs-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 0,
    "query": {"bool": {"must": [
      {"term": {"event_type": "tool_loop_gate"}},
      {"terms": {"decision": ["warn_consecutive","block_consecutive","block_identity"]}},
      {"range": {"@timestamp": {"gte": "now-7d"}}}
    ]}},
    "aggs": {"by_decision": {"terms": {"field": "decision", "size": 5}}}
  }' | jq '.aggregations.by_decision.buckets'
```

**Time math** uses ES date math: `now-1h`, `now-3h`, `now-24h`, `now-7d`, `now-30d`, etc. No spaces, no plurals.

**Term equality** uses `{"term": {"field": "value"}}` for keyword fields (most named fields). Use `{"match": ...}` for full-text in `message`, `user_message`, or `error` (the `error` field also has an `.keyword` subfield for term equality).

### ES|QL — for ad-hoc analytics only

ES|QL (`/_query`) can be useful for one-off analytics with `STATS … BY …` chains, but it has gotchas the JSON DSL doesn't:

| ES\|QL gotcha | Symptom | Workaround |
|---|---|---|
| Time units must be plural with space: `NOW()-3 hours`, NOT `NOW()-3hour` | Empty result, no error | Use the JSON DSL's `now-3h` instead |
| Column selection uses `KEEP`, not `PROJECT` | Parse error or unexpected columns | Use the JSON DSL's `_source` filter instead |
| Conditional aggregation: `COUNT_IF(condition)` (no ternaries) | Parse error | Use JSON `aggs` with `filter` instead |
| Term equality on `text` fields silently returns null | Empty result, no error | After 2026-05-10 reindex this only affects `error` and `message` — use `_search` |

If you must use ES|QL for an ad-hoc shape:

```bash
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE @timestamp > NOW()-3 hours | STATS count=COUNT(*) BY level | SORT count DESC"}' \
  | jq '.values'
```

If you get an empty `values` array, switch to `_search` rather than iterating ES|QL syntax.

## Index / schema inspection

```bash
# List all indices with doc count and size
curl -s 'http://elasticsearch:9200/_cat/indices?format=json&h=index,docs.count,store.size' | jq .

# Get field mappings for a specific index
curl -s 'http://elasticsearch:9200/agent-logs-*/_mapping' | jq 'to_entries[0].value.mappings.properties | keys | sort'

# Cluster health
curl -s 'http://elasticsearch:9200/_cluster/health' | jq '{status, active_shards, unassigned_shards}'
```

## Self-telemetry (agent logs + Captain's Log captures)

Use `bash` + `curl` to query ES. **Do NOT use `run_python` for project-module imports** — the
sandbox container does not have project source installed (`from personal_agent import ...` will
`ImportError`). Use the REST API instead.

```bash
# Recent agent log events (last 20, any type)
curl -s -X POST 'http://elasticsearch:9200/agent-logs-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 20,
    "query": {"match_all": {}},
    "sort": [{"@timestamp": "desc"}],
    "_source": ["@timestamp","level","event_type","message","tool_name","trace_id"]
  }' | jq ".hits.hits[]._source"

# Recent Captain's Log captures (last 10, newest first)
curl -s 'http://elasticsearch:9200/agent-captains-captures-*/_search' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 10,
    "sort": [{"timestamp": "desc"}],
    "_source": ["trace_id","user_message","outcome","total_tokens","duration_ms","timestamp"]
  }' | jq '.hits.hits[]._source'

# Recent reflections — recurring ones only (seen_count >= 2, most persistent first)
curl -s 'http://elasticsearch:9200/agent-captains-reflections-*/_search' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 10,
    "query": {"range": {"seen_count": {"gte": 2}}},
    "sort": [{"seen_count": "desc"}, {"created_at": "desc"}],
    "_source": ["rationale","proposed_change_what","seen_count","category","created_at","linear_issue_id"]
  }' | jq '.hits.hits[]._source'

# Full event trace for a specific request (by trace_id)
curl -s 'http://elasticsearch:9200/agent-logs-*/_search' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 100,
    "query": {"term": {"trace_id.keyword": "<trace_id>"}},
    "sort": [{"@timestamp": "asc"}],
    "_source": ["event_type","@timestamp","duration_ms","latency_ms","model_id","tokens"]
  }' | jq '.hits.hits[]._source'
```

## Common mistakes

| Mistake | Fix |
|---------|-----|
| Using `agent-events-*` or `agent-traces-*` | These don't exist. Use `agent-logs-*` |
| Iterating ES\|QL syntax variants when a query returns null | Switch to `_search` JSON DSL. ES\|QL gotchas (plural time units, `KEEP` vs `PROJECT`, `COUNT_IF`) waste turns. The JSON DSL is verbose but every name is explicit. |
| `term` query on `error` text field | Use `error.keyword`. All other named fields (`level`, `event_type`, `tool_name`, `trace_id`, etc.) are pure keyword post-2026-05-10 reindex — `term` works directly. |
| Wrong time boundary for "today" | "Today" is ambiguous. Use `@timestamp >= now-24h` (rolling window) instead of a midnight boundary — events from earlier in the day may be in yesterday's UTC index. |
| Empty `_source` filter | Omit `_source` to get the full doc (it's small). Or list explicit fields to keep results small. |
| Guessing `event_type` values | Run a `terms` agg on `event_type` to see what's actually emitted. Known values: `tool_call_started`, `tool_call_completed`, `litellm_request_complete`, `tool_loop_gate`, `session_created`, `gateway_request`, `state_transition`, `model_call_started`, `model_call_completed`, `history_sanitised`. |

## Governance

- `bash curl` to ES: auto-approved in all non-LOCKDOWN modes. No auth required — local cluster only. 30 s timeout.
- LOCKDOWN: `bash` disabled. Use `read` primitive to read raw JSONL directly from log files.
- `run_python` for self-telemetry: available in NORMAL/ALERT/DEGRADED; disabled in LOCKDOWN/RECOVERY.
- Output cap: pipe large responses through `| head -c 50000` or use `LIMIT` in ES|QL.

See also: [bash — Shell Command Executor](bash.md) · [run_python — Python Docker Sandbox](run-python.md)
