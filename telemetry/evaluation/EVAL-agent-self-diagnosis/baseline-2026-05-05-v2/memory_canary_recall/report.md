# Prompt: `memory_canary_recall`

_Two-turn canary. Turn 1 plants a unique fact tagged with a UUID. Turn 2 opens a fresh session (new_session: true) and asks for it back — tests whether memory writes + retrieval round-trip succeeds via the memory graph (not session history).
_

Tags: canary, memory, multi-turn

## Turn 1

- session_id: `a02a03d5-98b5-4bc4-9f86-02d50f96cf3c`
- trace_id:   `1cdefbc4-88b6-4a96-b978-16be4c77670a`
- duration:   12.30s

**User**

```
Memory canary 11111111-1111-1111-1111-111111111111: the diagnostic
color for Recovery Plan 2026-05-05 is ultramarine. Acknowledge.

```

**Assistant**

```
Acknowledged. Memory canary `11111111-1111-1111-1111-111111111111` received — Recovery Plan 2026-05-05 diagnostic color: **ultramarine**.
```

**ES log summary**

- log_count: 45
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
| `request_trace_step` | 9 |
| `state_transition` | 3 |
| `model_call_started` | 2 |
| `model_call_completed` | 2 |
| `request_received` | 1 |
| `intent_classified` | 1 |
| `recall_controller_skipped` | 1 |
| `decomposition_assessed` | 1 |
| `proactive_memory_suggest_start` | 1 |
| `proactive_memory_budget_trimmed` | 1 |
| `proactive_memory_suggest_complete` | 1 |
| `context_assembled` | 1 |
| `context_budget_applied` | 1 |
| `gateway_output` | 1 |
| `task_started` | 1 |
| `request_monitor_started` | 1 |
| `memory_enrichment_completed` | 1 |
| `step_init_gateway_path` | 1 |
| `step_llm_call_gateway_model` | 1 |
| `tools_passed_to_llm` | 1 |

## Turn 2

- session_id: `153b4ee9-da4b-43f1-9276-9f4813ebabb6`
- trace_id:   `61fd9b47-08eb-465a-8d4e-a3935d9ca5a7`
- duration:   13.10s

**User**

```
What is the diagnostic color for the recovery plan with UUID
11111111-1111-1111-1111-111111111111?

```

**Assistant**

```
I reached my tool-use limit before completing a synthesis. Here are the latest tool results:
- search_memory: success
```

**ES log summary**

- log_count: 67
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 0
- capture_id: None
- extraction_outcome: None
- tool_calls: 1 requested / 1 executed (bash: 0)
- loop_gate_decisions: ['allow']

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `request_trace_step` | 11 |
| `state_transition` | 5 |
| `model_call_started` | 4 |
| `model_call_completed` | 4 |
| `tools_passed_to_llm` | 2 |
| `llm_call_messages_debug` | 2 |
| `history_sanitised` | 2 |
| `chat_completions_payload` | 2 |
| `raw_llm_response` | 2 |
| `request_received` | 1 |
| `intent_classified` | 1 |
| `recall_controller_skipped` | 1 |
| `decomposition_assessed` | 1 |
| `proactive_memory_suggest_start` | 1 |
| `proactive_memory_budget_trimmed` | 1 |
| `context_assembled` | 1 |
| `proactive_memory_suggest_complete` | 1 |
| `context_budget_applied` | 1 |
| `gateway_output` | 1 |
| `request_monitor_started` | 1 |
