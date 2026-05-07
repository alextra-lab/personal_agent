# Prompt: `system_metrics_snapshot`

_Ask for current resource utilization. Should route to system-metrics skill.
_

Tags: diagnostics, metrics

## Turn 1

- session_id: `90211b51-0330-447b-904f-fa2fc9db4a3f`
- trace_id:   `96855e9f-78c6-44b3-b66f-3f6fe97ce1d0`
- duration:   0.62s

**User**

```
How much memory is the agent currently using? What is the CPU load
and how much disk space is left?

```

**Assistant**

```
An error occurred while processing your request. Please try again.
```

**ES log summary**

- log_count: 38
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
| `intent_classified` | 1 |
| `decomposition_assessed` | 1 |
| `proactive_memory_suggest_start` | 1 |
| `proactive_memory_budget_trimmed` | 1 |
| `proactive_memory_suggest_complete` | 1 |
| `gateway_output` | 1 |
| `context_assembled` | 1 |
| `context_budget_applied` | 1 |
| `request_monitor_started` | 1 |
| `task_started` | 1 |
| `memory_enrichment_completed` | 1 |
| `step_init_gateway_path` | 1 |
| `step_llm_call_gateway_model` | 1 |
| `skill_routing_call_completed` | 1 |
| `skill_index_assembled` | 1 |
| `model_call_started` | 1 |
