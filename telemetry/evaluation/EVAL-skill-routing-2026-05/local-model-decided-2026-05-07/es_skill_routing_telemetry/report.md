# Prompt: `es_skill_routing_telemetry`

_Query the skill routing telemetry itself. Validates skill_index_assembled and skill_routing_call_completed events are visible in ES.
_

Tags: telemetry, es, meta

## Turn 1

- session_id: `f2098c79-b420-46b6-9dbe-17ded3d06419`
- trace_id:   `ada3aa8a-a1dc-439e-827d-a5d39c5f8e72`
- duration:   68.31s

**User**

```
Show me the skill_index_assembled events from the last hour.
What routing mode has been used and how many chars were injected per turn?

```

**Assistant**

```
Based on the Elasticsearch queries:

## `skill_index_assembled` Events

**Zero events** found — both in the last hour and across the entire history of the `agent-logs-*` index. This event type does not appear to be emitted by any agent session.

## Routing Mode & Injected Chars

I was unable to complete the secondary queries to check for `routing_mode` and `injected_chars` fields due to tool call budget limits. However, from the **past conversation history** (session 2), we can see:

- The **previously observed routing mode was `keyword`**
- The relevant file was the skill index configuration
- There were **zero `skill_index_assembled` events** previously observed too

To get the current routing mode and injected chars per turn, I would need to:
1. Query the latest agent log entries for `routing_mode` and `injected_chars` fields
2. Check if these fields are populated on any event types that *are* being emitted

Would you like me to run those follow-up queries now?
```

**ES log summary**

- log_count: 201
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 0
- capture_id: None
- extraction_outcome: failed:BudgetDenied
- tool_calls: 9 requested / 9 executed (bash: 9)
- loop_gate_decisions: ['allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'block_consecutive', 'block_consecutive']

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `request_trace_step` | 19 |
| `state_transition` | 13 |
| `model_call_started` | 12 |
| `model_call_completed` | 12 |
| `tool_loop_gate` | 11 |
| `bash_auto_approved` | 9 |
| `tool_call_started` | 9 |
| `bash_started` | 9 |
| `tool_result_skill_hint_appended` | 9 |
| `bash_completed` | 9 |
| `tool_call_completed` | 9 |
| `skill_index_assembled` | 6 |
| `tools_passed_to_llm` | 6 |
| `llm_call_messages_debug` | 6 |
| `history_sanitised` | 6 |
| `chat_completions_payload` | 6 |
| `raw_llm_response` | 6 |
| `step_executed` | 5 |
| `tool_execution_completed` | 5 |
| `tools_dispatched_parallel` | 4 |
