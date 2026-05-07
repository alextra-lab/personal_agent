# Prompt: `es_incident_class`

_The exact class of request that triggered the original agent diagnosis incident. The agent must query ES logs without hallucinating a wrong index name. Pass: first bash call uses agent-logs-* or guard fires (tool_call_blocked_known_bad_pattern).
_

Tags: incident, telemetry, b5-guard

## Turn 1

- session_id: `f34c59b2-2cae-4f0a-97ad-1da24581c0ed`
- trace_id:   `319b5d2c-5e6e-45eb-a8d8-fe57c10886b6`
- duration:   435.72s

**User**

```
Check the logs and show me any errors or warnings from the last 12 hours.
I want to understand what has been going wrong with the agent recently.

```

**Assistant**

```
I was unable to retrieve the live logs because I hit the consecutive tool-call limit during the query process. However, I can give you the exact, ready-to-run queries to pull the last 12 hours of errors and warnings, along with guidance on what to look for based on the system's logging schema.

### 🔍 Exact Queries to Run

You can run these via `bash curl` or paste them into **Kibana Discover** (index pattern: `agent-logs-*`).

**1. All Errors & Warnings (Combined, sorted newest first)**
```bash
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE level IN (\"ERROR\", \"WARNING\") AND @timestamp > NOW()-12hours | SORT @timestamp DESC | LIMIT 100 | PROJECT @timestamp, level, message, event_type, tool_name, task_type, trace_id"}' \
  | jq '.values[] | [.timestamp, .level, .message[0:150] // "", .event_type, .tool_name] | @tsv'
```

**2. Errors Only (with full error context)**
```bash
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE level == \"ERROR\" AND @timestamp > NOW()-12hours | SORT @timestamp DESC | LIMIT 50 | PROJECT @timestamp, level, message, error, tool_name, task_type, trace_id"}' \
  | jq '.values'
```

**3. Warnings Only (tool loops & gate decisions)**
```bash
curl -s -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE level == \"WARNING\" AND @timestamp > NOW()-12hours | SORT @timestamp DESC | LIMIT 50 | PROJECT @timestamp, level, message, event_type, tool_name, trace_id"}' \
  | jq '.values'
```

### 📊 What to Look For & How to Interpret

Based on the `agent-logs-*` schema and recent system patterns, here’s how to diagnose what’s been going wrong:

| Signal | Likely Cause | Action |
|--------|--------------|--------|
| `level: "ERROR"` + `event_type: "tool_call_completed"` + `success: false` | Tool execution failed (e.g., bash timeout, API rate limit, malformed response) | Check `error` field for stack trace or HTTP status. Look for repeated `tool_name` values. |
| `level: "ERROR"` + `event_type: "litellm_request_complete"` | LLM inference failure (timeout, rate limit, model down) | Check `model` field. If `cost_usd` is high but `success: false`, it's a billing/timeout leak. |
| `level: "WARNING"` + `event_type: "tool_loop_gate"` | Agent hit consecutive/identity block limits | Normal guardrail behavior. If frequent, the agent is stuck in a reasoning loop. Check `decision` field (`warn_consecutive` vs `block_consecutive`). |
| `level: "WARNING"` + `event_type: "gateway_request"` | Slow response or high latency threshold crossed | Check `elapsed_s` or `elapsed_ms`. If consistently >5s, the gateway or downstream service (Neo4j/ES) may be throttled. |
| `error` field contains `rate limit` or `429` | External API throttling (LLM provider, embeddings, or reranker) | Usually transient. If persistent, check `embeddings:8503` or `reranker:8504` health or provider quotas. |
| `error` field contains `connection refused` or `timeout` | Internal service unreachable (Postgres, Neo4j, Redis) | Run `infrastructure-health` or `curl http://<service>:<port>/health` to verify container DNS resolution. |

### 🛠 Next Steps
1. Run the combined query above to get the raw list.
2. If you paste the output here (or a few representative rows), I can immediately categorize them, identify root causes, and suggest fixes.
3. For any specific `trace_id` that looks critical, I can pull the full event chain using `get_trace_events(trace_id="<id>")` to see exactly where the failure occurred in the request lifecycle.

Let me know what the queries return, and I’ll help you drill into the root causes.
```

**ES log summary**

- log_count: 500
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 1
- capture_id: None
- extraction_outcome: None
- tool_calls: 20 requested / 20 executed (bash: 18)
- loop_gate_decisions: ['allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'advise_identity', 'advise_identity', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive', 'block_consecutive']

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `tool_loop_gate` | 66 |
| `state_transition` | 42 |
| `model_call_started` | 40 |
| `model_call_completed` | 40 |
| `skill_index_assembled` | 21 |
| `tools_passed_to_llm` | 20 |
| `llm_call_messages_debug` | 20 |
| `history_sanitised` | 20 |
| `chat_completions_payload` | 20 |
| `raw_llm_response` | 20 |
| `step_executed` | 20 |
| `tool_call_started` | 20 |
| `tool_call_completed` | 20 |
| `tool_execution_completed` | 20 |
| `bash_started` | 18 |
| `bash_completed` | 18 |
| `tool_result_skill_hint_appended` | 18 |
| `bash_allowlist_miss` | 14 |
| `tools_dispatched_parallel` | 9 |
| `bash_output_overflow` | 6 |
