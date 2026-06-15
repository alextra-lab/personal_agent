---
name: seshat-observations
description: Query execution traces, telemetry data, cost metrics, and performance observations via Kibana or API.
when_to_use: When you need to inspect performance metrics, cost data, or execution telemetry from an external agent context.
---

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

### Route-trace ledger (per-turn stimulus → model-path → result-type)

The route-trace ledger (FRE-452 / ADR-0088) records what the gateway decided (the
deterministic-shell `gateway_label`) vs what the harness actually did (`orchestration_event`),
joinable to `api_costs` on `trace_id`. A turn is **one turn-level row** (`task_id` null) plus
**one segment row per sub-agent** (`task_id` set), the ADR-0088 per-topology fan-out (FRE-517).
Rows are returned as a bare list of the seam-neutral `RouteTraceRow` shape. The `session` and
`recent` views are **turn-level only** (segments excluded). Scope: `observations:read`.

**All rows for a trace — turn-level + segments (GET /observations/route-traces/{trace_id})** —
returns a list (turn-level first); 404 if absent/malformed:

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/observations/route-traces/{trace_id}"
```

**By session, newest first (GET /observations/route-traces/session/{session_id}?limit=)**:

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/observations/route-traces/session/{session_id}?limit=50"
```

**Recent-N with boundary filters (GET /observations/route-traces/recent)** — filters compose
with AND; `limit` is clamped to 200:

| Query param | Effect |
|-------------|--------|
| `limit` | Max rows (default 50, max 200) |
| `label_lie=true` | Gateway-declared expansion disagrees with the actual orchestration event (*candidate* heuristic) |
| `fallback_triggered=true` | Turn escalated to the primary after a sub-agent/phase failure |
| `not_reconciled=true` | Live vs authoritative (`api_costs`) cost disagreed |

```bash
curl -H "Authorization: Bearer $SESHAT_API_TOKEN" \
  "$SESHAT_API_URL/observations/route-traces/recent?label_lie=true&limit=25"
```

### Execution-topology ES projection (`agent-topology-*`, FRE-548 / FRE-545)

The seam also projects each completed turn's route-trace row to a dedicated, Kibana-readable
index **`agent-topology-*`** (one doc per `(trace_id, task_id)`): the turn-level row
(`role: primary`, no `task_id`) plus one per sub-agent (`role: sub_agent`). Explicit schema
(`dynamic: false`): `trace_id`/`task_id`/`session_id`/`topology`/`role`/`gateway_label`/
`result_type`/`task_type`/`complexity` (keyword), `authoritative_cost_usd` (double),
`input_tokens`/`output_tokens` (long), `latency_total_ms` (float), `@timestamp` (date). The
**routing surface** (FRE-545) adds `model_role`/`decomposition_strategy`/`decomposition_reason`
(keyword) + `intent_confidence` (float) — `model_role` is the selected tier (always `primary`
today; the FRE-432 gap). Use it for topology distribution, primary-vs-sub-agent rows, authoritative
cost per topology, and the routing-decision surface.

```bash
curl "http://elasticsearch:9200/agent-topology-*/_search?q=topology:hybrid_fanout"
curl "http://elasticsearch:9200/agent-topology-*/_search?q=role:primary%20AND%20model_role:primary"
```

### Live-projector bus-delivery health (`agent-monitors-projector-health-*`, FRE-557)

One doc per trace at completion, measuring whether the live projector actually **received** the
best-effort `stream:turn.observed` events (ADR-0088 D6 sink 2). Fields (`dynamic: false`):
`trace_id`/`session_id`/`topology` (keyword), `events_received`/`model_calls_received` (long),
`projector_live_cost_usd`/`cost_authoritative_usd`/`cost_delta_usd` (double), `observation_complete`
(boolean), `@timestamp` (date).

**This is orthogonal to `not_reconciled`.** `cost_reconciled = FALSE` (on `route_traces`) means the
*durable per-loop accumulator* (`ctx.turn_cost_usd`) drifted from `SUM(api_costs)` — nothing to do
with the bus. The projector counter measures the *separate* bus/live path: when
`model_calls_received < COUNT(api_costs WHERE trace_id)` the live UI meter undercounted because bus
events were dropped. `observation_complete=false` ⇒ counters untrustworthy (eviction/late-attach).
A trace the projector saw *zero* times emits **no** doc — detect those by reconciliation: traces in
`api_costs` with no `agent-monitors-projector-health` doc.

```bash
curl "http://elasticsearch:9200/agent-monitors-projector-health-*/_search?q=observation_complete:true"
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
