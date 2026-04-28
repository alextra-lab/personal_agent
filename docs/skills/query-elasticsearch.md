# query-elasticsearch — Query ES indices, inspect schema, read self-telemetry

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

Most important fields for queries:

| Field | Type | Purpose |
|-------|------|---------|
| `@timestamp` | date | Event time |
| `level` | keyword | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `message` | text | Log message |
| `trace_id` | keyword | Request trace (use `trace_id.keyword` in term filters) |
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
| `error` | keyword | Error message (when applicable) |
| `turn_count` | long | Number of LLM turns in request |
| `model` / `model_id` | keyword | LLM model used |

## Querying Elasticsearch (ES|QL)

ES|QL is the preferred query language. Send a POST to `_query`:

```bash
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE @timestamp > NOW()-1hour | LIMIT 50"}' \
  | jq .
```

Common patterns:

```bash
# Errors in the last hour
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE level == \"ERROR\" AND @timestamp > NOW()-1hour | LIMIT 100"}' \
  | jq '.values'

# Tool calls for a specific tool
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"tool_call_started\" AND tool_name == \"query_elasticsearch\" AND @timestamp > NOW()-24hours | STATS count=COUNT(*) | LIMIT 10"}' \
  | jq '.values'

# LLM token usage by task type
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE @timestamp > NOW()-24hours AND prompt_tokens IS NOT NULL | STATS total_tokens=SUM(prompt_tokens) BY task_type | SORT total_tokens DESC"}' \
  | jq '.values'

# Find a trace by ID
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE trace_id == \"<trace_id>\" | SORT @timestamp ASC | LIMIT 200"}' \
  | jq '.values'

# Count tool_call_started events for query_elasticsearch in the last day
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE event_type == \"tool_call_started\" AND tool_name == \"query_elasticsearch\" AND @timestamp > NOW()-24hours | STATS count=COUNT(*)"}' \
  | jq '.values'
```

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

Use `run_python` to call the Python telemetry API directly — these are not REST endpoints.

```python
from personal_agent.telemetry.metrics import query_events, get_trace_events, get_request_latency_breakdown
from personal_agent.captains_log.capture import read_captures
import json

# Recent events (last N events across all event types)
events = query_events(limit=50)
print(json.dumps(events, default=str, indent=2))

# Full event trace for a specific request
trace_events = get_trace_events(trace_id="<trace_id>")
print(json.dumps(trace_events, default=str, indent=2))

# Latency breakdown by stage for a trace
breakdown = get_request_latency_breakdown(trace_id="<trace_id>")
print(json.dumps(breakdown, default=str, indent=2))

# Captain's Log captures (self-improvement data)
captures = read_captures(limit=20)
print(json.dumps(captures, default=str, indent=2))
```

## Common mistakes

| Mistake | Fix |
|---------|-----|
| Using `agent-events-*` or `agent-traces-*` | These don't exist. Use `agent-logs-*` |
| `term` query on `trace_id` text field | Use `trace_id.keyword` for exact match in JSON queries |
| No LIMIT on large queries | Always add `\| LIMIT N` to ES|QL; default returns all rows |
| `_search` with Lucene syntax | Prefer ES|QL (`_query`) — simpler and more predictable |

## Governance

- `bash curl` to ES: auto-approved in all non-LOCKDOWN modes. No auth required — local cluster only. 30 s timeout.
- LOCKDOWN: `bash` disabled. Use `read` primitive to read raw JSONL directly from log files.
- `run_python` for self-telemetry: available in NORMAL/ALERT/DEGRADED; disabled in LOCKDOWN/RECOVERY.
- Output cap: pipe large responses through `| head -c 50000` or use `LIMIT` in ES|QL.

See also: [bash — Shell Command Executor](bash.md) · [run_python — Python Docker Sandbox](run-python.md)
