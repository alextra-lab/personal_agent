# SKILL: Seshat Observations & Performance Metrics

> **Tier:** 2 — CLI tool + HTTP API  
> **Immediate CLI:** Kibana at `http://localhost:5601`  
> **Gateway API:** `$SESHAT_API_URL/observations` (Phase C+)  
> **Auth:** None (Kibana local) / `SESHAT_API_TOKEN` (Gateway)  
> **ADR:** `docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md`

---

## What This Skill Does

Query execution traces, telemetry data, cost metrics, and performance observations. Observations capture every decision point, tool call, delegation event, and system health metric. Use this to understand system behavior, debug issues, analyze patterns, and measure performance.

---

## When to Use

- You need to understand why a request failed: "Show me traces where delegation failed"
- You want to analyze costs: "How much did last week's requests cost?"
- You're debugging a performance issue: "Which steps took longest?"
- You need to see tool call history: "What tools were called during request X?"
- You want to understand delegation patterns: "When does the system choose to decompose vs. delegate?"

---

## Commands

### Immediate CLI (Works Now)

#### Use Kibana for local observation queries

Open **Kibana** at `http://localhost:5601` (requires `./scripts/init-services.sh` running).

**To query recent observations:**

1. Click **Discover** in left sidebar
2. Select `elasticsearch-observations-*` index pattern
3. Add filters or search:
   - `status: "failed"` – Failed operations only
   - `tool_name: "linear_issue_create"` – Specific tool
   - `cost_dollars: [5 TO *]` – High-cost requests
4. Review logs with full context (trace_id, entity_id, timestamps, etc.)

**Sample Kibana queries:**

```
status: "error" AND type: "delegation"
```

```
tool_name: "mcp_gateway" AND duration_ms: [1000 TO *]
```

```
request_id: "req-abc123" AND level: "error"
```

---

### Gateway-Ready API (Available After Phase C Deployment)

> **Note:** Phase C deploys the Seshat API Gateway. For now, use Kibana above.

#### Set up authentication

```bash
export SESHAT_API_TOKEN="<your-api-token>"
export SESHAT_API_URL="http://localhost:9000"  # local or https://seshat.example.com after Phase C
```

#### Query recent observations

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/observations/recent?limit=20"
```

Response:
```json
{
  "observations": [
    {
      "id": "obs-5000",
      "timestamp": "2026-04-14T08:30:15Z",
      "trace_id": "tr-xyz789",
      "type": "tool_call",
      "tool_name": "linear_issue_create",
      "status": "success",
      "duration_ms": 245,
      "cost_dollars": 0.00012
    }
  ]
}
```

#### Query by trace ID

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/observations/{trace_id}"
```

Response:
```json
{
  "trace_id": "tr-xyz789",
  "observations": [
    {
      "step": 1,
      "type": "request_received",
      "user_message": "Create a Linear issue for bug X"
    },
    {
      "step": 2,
      "type": "intent_classification",
      "intent": "task_creation",
      "confidence": 0.98
    },
    {
      "step": 3,
      "type": "delegation_decision",
      "decision": "single_brain",
      "reason": "task_fits_in_context_window"
    }
  ]
}
```

#### Query observations by type and filters

```bash
# Failed delegations
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/observations?type=delegation&status=failed&limit=10"

# High-cost requests (over $0.01)
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/observations?cost_min=0.01&limit=10"

# Tool call history for a specific tool
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/observations?tool_name=mcp_gateway&limit=20"
```

#### Get system health metrics

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/observations/health"
```

Response:
```json
{
  "total_requests_24h": 1250,
  "avg_cost_per_request": 0.0042,
  "error_rate": 0.02,
  "avg_latency_ms": 1840,
  "delegation_rate": 0.35
}
```

---

## Authentication

**Kibana (CLI):**
No authentication required for local access.

**Gateway API (Phase C+):**
```bash
export SESHAT_API_TOKEN="<your-api-token>"

# Verify connection
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/observations/health"
```

---

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| Kibana not available | Elasticsearch not running | Run: `./scripts/init-services.sh` |
| `401 Unauthorized` | Missing `SESHAT_API_TOKEN` | Export: `export SESHAT_API_TOKEN="..."` |
| `Empty results` | No observations match filters | Try broader query; check timestamp range |
| `trace_id not found` | Trace doesn't exist or expired | Traces retain 30 days; check request_id instead |

---

## Notes

- All observations are indexed in Elasticsearch; queries are real-time
- Trace IDs are unique per request; use for full request replay/debugging
- Cost data includes LLM tokens, tool calls, and infrastructure
- Observations include timestamps in ISO 8601 UTC format
- Kibana provides advanced visualization; API provides programmatic access
- Dashboard: `http://localhost:5601/app/dashboards` (after Phase C deployment)
