# Prompt: `es_skill_routing_telemetry`

_Query the skill routing telemetry itself. Validates skill_index_assembled and skill_routing_call_completed events are visible in ES.
_

Tags: telemetry, es, meta

## Turn 1

- session_id: `bdf8661b-a3b2-43e8-8774-b9a201c7c224`
- trace_id:   `cfe736a2-e493-442d-8a08-bada56556e40`
- duration:   113.70s

**User**

```
Show me the skill_index_assembled events from the last hour.
What routing mode has been used and how many chars were injected per turn?

```

**Assistant**

```
## `skill_index_assembled` Events — Last Hour

**Total events:** 193 in the past hour

### Routing Mode Distribution & Injected Chars per Turn

| Routing Mode | Events | Avg Injected Chars | Min | Max |
|---|---|---|---|---|
| **keyword** | 83 (43%) | 11,509 | 5,193 | 14,096 |
| **hybrid** | 64 (33%) | 13,247 | 6,856 | 15,759 |
| **model_decided** | 46 (24%) | 8,714 | 1,661 | 13,397 |

### Key Observations

- **`keyword`** is the dominant routing mode (83/193 events), with moderate injection volume (~11.5K chars/turn avg)
- **`hybrid`** has the highest average injection (~13.2K chars/turn) and the highest single-turn max (15,759 chars), suggesting it's pulling in more context from both vector and keyword search
- **`model_decided`** has the lowest average (~8.7K chars/turn) and the widest range (1.6K–13.4K), indicating more variable behavior depending on the model's own routing decision

The `injected_chars` field represents the number of characters injected into the prompt context during skill index assembly, which correlates with how much skill metadata/content was loaded for each turn.
```

**ES log summary**

- log_count: 179
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
| `request_trace_step` | 19 |
| `state_transition` | 13 |
| `model_call_started` | 12 |
| `model_call_completed` | 12 |
| `skill_index_assembled` | 6 |
| `llm_call_messages_debug` | 6 |
| `tools_passed_to_llm` | 6 |
| `history_sanitised` | 6 |
| `chat_completions_payload` | 6 |
| `raw_llm_response` | 6 |
| `tool_loop_gate` | 6 |
| `bash_auto_approved` | 6 |
| `tool_call_started` | 6 |
| `bash_started` | 6 |
| `bash_completed` | 6 |
| `tool_result_skill_hint_appended` | 6 |
| `tool_call_completed` | 6 |
| `step_executed` | 5 |
| `tools_dispatched_parallel` | 5 |
| `tool_execution_completed` | 5 |
