# Prompt: `infra_health_check`

_Probe all backend services. Should route to infrastructure-health skill.
_

Tags: infra, health

## Turn 1

- session_id: `b886cc4b-c953-4e73-bfe3-eedac508a01c`
- trace_id:   `bb67544e-0400-45d5-ba46-832022e2e26d`
- duration:   0.54s

**User**

```
Run a health check on all backend services — Postgres, Elasticsearch,
Neo4j, Redis. Which ones are reachable and which are not?

```

**Assistant**

```
An error occurred while processing your request. Please try again.
```

**ES log summary**

- log_count: 39
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 0
- capture_id: None
- extraction_outcome: None
- tool_calls: 0 requested / 0 executed (bash: 0)

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `request_trace_step` | 7 |
| `state_transition` | 2 |
| `task_failed` | 2 |
| `request_received` | 1 |
| `recall_controller_skipped` | 1 |
| `intent_classified` | 1 |
| `proactive_memory_suggest_start` | 1 |
| `decomposition_assessed` | 1 |
| `proactive_memory_budget_trimmed` | 1 |
| `proactive_memory_suggest_complete` | 1 |
| `context_assembled` | 1 |
| `context_budget_applied` | 1 |
| `gateway_output` | 1 |
| `task_started` | 1 |
| `memory_enrichment_completed` | 1 |
| `request_monitor_started` | 1 |
| `step_init_gateway_path` | 1 |
| `step_llm_call_gateway_model` | 1 |
| `skill_index_assembled` | 1 |
| `skill_routing_call_completed` | 1 |
