# Prompt: `es_skill_routing_telemetry`

_Query the skill routing telemetry itself. Validates skill_index_assembled and skill_routing_call_completed events are visible in ES.
_

Tags: telemetry, es, meta

## Turn 1

- session_id: `18c2c1fd-1b9e-4c95-a0e5-9aa5a2300950`
- trace_id:   `00a1cdd9-07e3-47be-918f-ffa37d5d9a6a`
- duration:   153.89s

**User**

```
Show me the skill_index_assembled events from the last hour.
What routing mode has been used and how many chars were injected per turn?

```

**Assistant**

```
An error occurred while processing your request. Please try again.
```

**ES log summary**

- log_count: 72
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 0
- capture_id: None
- extraction_outcome: None
- tool_calls: 2 requested / 2 executed (bash: 2)
- loop_gate_decisions: ['allow', 'allow']

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `request_trace_step` | 9 |
| `state_transition` | 4 |
| `model_call_started` | 4 |
| `chat_completions_payload` | 3 |
| `skill_index_assembled` | 2 |
| `tools_passed_to_llm` | 2 |
| `llm_call_messages_debug` | 2 |
| `history_sanitised` | 2 |
| `model_call_completed` | 2 |
| `tool_loop_gate` | 2 |
| `bash_auto_approved` | 2 |
| `tool_call_started` | 2 |
| `bash_started` | 2 |
| `bash_completed` | 2 |
| `tool_call_completed` | 2 |
| `tool_result_skill_hint_appended` | 2 |
| `model_call_error` | 2 |
| `task_failed` | 2 |
| `request_received` | 1 |
| `intent_classified` | 1 |
