# Prompt: `es_tool_error_analysis`

_Ask for a structured analysis of tool errors. Should route to ES skill.
_

Tags: telemetry, es

## Turn 1

- session_id: `c8b7836d-79a9-44ca-b3f3-d6b828375f05`
- trace_id:   `adc20cc8-9718-4f1a-a737-834668dc5f46`
- duration:   41.47s

**User**

```
Look at the recent traces and tell me: which tools have been failing most
often in the last 24 hours? Show me the top 3 by error count.

```

**Assistant**

```
I reached my tool-use limit before completing a synthesis. Here are the latest tool results:
- bash: success
- bash: success
- bash: success
```

**ES log summary**

- log_count: 143
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 0
- capture_id: None
- extraction_outcome: failed:BudgetDenied
- tool_calls: 6 requested / 6 executed (bash: 6)
- loop_gate_decisions: ['allow', 'allow', 'allow', 'allow', 'allow', 'allow']

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `request_trace_step` | 15 |
| `state_transition` | 9 |
| `model_call_started` | 8 |
| `model_call_completed` | 8 |
| `tool_loop_gate` | 6 |
| `bash_allowlist_miss` | 6 |
| `tool_call_started` | 6 |
| `bash_started` | 6 |
| `bash_completed` | 6 |
| `tool_call_completed` | 6 |
| `tool_result_skill_hint_appended` | 6 |
| `skill_index_assembled` | 4 |
| `tools_passed_to_llm` | 4 |
| `llm_call_messages_debug` | 4 |
| `history_sanitised` | 4 |
| `chat_completions_payload` | 4 |
| `raw_llm_response` | 4 |
| `step_executed` | 3 |
| `tools_dispatched_parallel` | 3 |
| `tool_execution_completed` | 3 |
