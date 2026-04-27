# query-elasticsearch — Query ES indices, inspect schema, read self-telemetry

**Category:** `system_read` · **Risk:** low · **Approval:** `bash curl` auto-approved (NORMAL); `run_python` auto-approved (NORMAL/ALERT/DEGRADED)

## Querying Elasticsearch (ES|QL)

ES|QL is the preferred query language. Send a POST to `_query`:

```bash
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-* | WHERE @timestamp > NOW()-1hour | LIMIT 50"}' \
  | jq .
```

Common patterns:

```bash
# Errors in the last hour
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE level == \"ERROR\" AND @timestamp > NOW()-1hour | LIMIT 100"}' \
  | jq '.values'

# Event counts by task_type
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-events-* | WHERE @timestamp > NOW()-24hours | STATS count=COUNT(*) BY task_type | SORT count DESC"}' \
  | jq '.values'

# p95 latency
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-traces-* | WHERE @timestamp > NOW()-1hour | STATS p95=PERCENTILE(duration_ms, 95) BY task_type"}' \
  | jq '.values'

# Find a trace by ID
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-* | WHERE trace_id == \"<trace_id>\" | SORT @timestamp ASC | LIMIT 200"}' \
  | jq '.values'
```

## Index / schema inspection

```bash
# List all indices (name, status, health, doc count, size)
curl -s 'http://elasticsearch:9200/_cat/indices?format=json&h=index,status,health,docs.count,store.size' | jq .

# Get field mappings for an index
curl -s 'http://elasticsearch:9200/agent-logs-2026.04/_mapping' | jq '.[] | .mappings.properties | keys'

# Shard allocation
curl -s 'http://elasticsearch:9200/_cat/shards?format=json' | jq '[.[] | {index, shard, state, node}]'

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

## Governance

- `bash curl` to ES: auto-approved in all non-LOCKDOWN modes. No auth required — local cluster only. 30 s timeout.
- LOCKDOWN: `bash` disabled. Use `read` primitive to read raw JSONL directly from log files (e.g. `read path="/opt/seshat/telemetry/events.jsonl"`).
- `run_python` for self-telemetry: available in NORMAL/ALERT/DEGRADED; disabled in LOCKDOWN/RECOVERY.
- Output cap: pipe large responses through `| head -c 50000` or use `LIMIT` in ES|QL.

See also: [bash — Shell Command Executor](bash.md) · [run_python — Python Docker Sandbox](run-python.md)
