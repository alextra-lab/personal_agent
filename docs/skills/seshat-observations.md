# SKILL: Seshat Observations & Performance Metrics

> **Auth:** `SESHAT_API_TOKEN` (Gateway API)
> **ADR:** `docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md`

Query execution traces, telemetry data, cost metrics, and performance observations.

---

## Works Now

### Kibana (local, no auth)

Open **Kibana** at `http://localhost:5601` → **Discover** → select index pattern **`agent-logs-*`**.

> **Correct index:** `agent-logs-*` — **not** `elasticsearch-observations-*` (that pattern does not exist).

Key fields in `agent-logs-*`:

| Field | Type | Notes |
|-------|------|-------|
| `@timestamp` | date | Event time |
| `level` | keyword | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `trace_id` | keyword | Per-request trace |
| `event_type` | keyword | e.g. `tool_call_started`, `litellm_request_complete` |
| `tool_name` | keyword | Tool invoked |
| `cost_usd` | float | LLM call cost (**not** `cost_dollars`) |
| `prompt_tokens`, `completion_tokens` | long | Token counts |
| `success` | boolean | Operation outcome |

Sample Kibana queries:

```
level: "error"
tool_name: "query_elasticsearch" AND success: false
trace_id.keyword: "abc-123"
```

### Gateway API

```bash
export SESHAT_API_TOKEN="<your-api-token>"
export SESHAT_API_URL="http://localhost:9000/api/v1"
```

**Recent observations (GET /observations/recent)**

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/observations/recent?limit=20"
```

Response — **bare list** (not a wrapper object):

```json
[
  {
    "trace_id": "tr-xyz789",
    "timestamp": "2026-04-28T08:30:15Z",
    "event_type": "tool_call_started",
    "tool_name": "bash",
    "cost_usd": 0.00012
  }
]
```

**Observations by trace ID (GET /observations/{trace_id})**

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/observations/{trace_id}"
```

**Query observations (POST /observations/query)**

```bash
curl -X POST \
  -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"from": "2026-04-27T00:00:00Z", "limit": 50}' \
  "$SESHAT_API_URL/observations/query"
```

---

## 🚫 Planned — not implemented (do not call)

- `GET /observations?type=...` — filter-by-type query param (404)
- `GET /observations/health` — system health metrics endpoint (404)

Use `agent-logs-*` in Kibana or the ES API (`bash curl http://elasticsearch:9200/agent-logs-*/_search`) for filter-based queries.

---

## Error handling

| Error | Cause | Fix |
|-------|-------|-----|
| Kibana not available | Elasticsearch down | `./scripts/init-services.sh` |
| `401 Unauthorized` | Missing token | `export SESHAT_API_TOKEN="..."` |
| `404` on observations endpoint | Wrong path or unimplemented | Use `/api/v1/observations/recent` |
| Empty results | No data in time range | Widen the time window |
