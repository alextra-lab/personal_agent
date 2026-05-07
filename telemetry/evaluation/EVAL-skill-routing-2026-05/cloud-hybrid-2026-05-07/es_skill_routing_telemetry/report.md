# Prompt: `es_skill_routing_telemetry`

_Query the skill routing telemetry itself. Validates skill_index_assembled and skill_routing_call_completed events are visible in ES.
_

Tags: telemetry, es, meta

## Turn 1

- session_id: `efd700c8-4275-4a87-89f0-9e650fe31d6f`
- trace_id:   `edf3482e-d8aa-4ce3-b444-66ad4361df47`
- duration:   29.53s

**User**

```
Show me the skill_index_assembled events from the last hour.
What routing mode has been used and how many chars were injected per turn?

```

**Assistant**

```
The query returned no rows — meaning **there are zero `skill_index_assembled` events in the last hour**.

Here's what I confirmed along the way:

- ✅ The index `agent-logs-*` is reachable and healthy
- ✅ The correct field names are `routing_mode` and `injected_chars` (discovered from the schema)
- ❌ No `skill_index_assembled` events were found in the past 60 minutes

### Likely reasons:
1. **No agent turns ran in the last hour** — if there's been no conversation activity, no skill index would be assembled
2. **The event window is quiet** — prior memory search confirms events *do* exist historically (32 events were found in a previous session), just not in the last hour

### What we know from recent history (from memory):
- The routing mode previously observed was exclusively **`keyword`**
- Injected chars varied per turn (exact per-turn breakdown requires the data to be present)

If you'd like to widen the window (e.g. last 6 or 24 hours), I can rerun the query with a broader time range in the next session.
```

**ES log summary**

- log_count: 184
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 0
- capture_id: None
- extraction_outcome: None
- tool_calls: 6 requested / 6 executed (bash: 6)
- loop_gate_decisions: ['allow', 'allow', 'allow', 'allow', 'allow', 'allow']

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `request_trace_step` | 21 |
| `state_transition` | 15 |
| `skill_index_assembled` | 7 |
| `model_call_started` | 7 |
| `tools_passed_to_llm` | 7 |
| `llm_call_messages_debug` | 7 |
| `history_sanitised` | 7 |
| `cost_gate_reserved` | 7 |
| `litellm_request_start` | 7 |
| `litellm_request_complete` | 7 |
| `model_call_completed` | 7 |
| `tool_loop_gate` | 6 |
| `step_executed` | 6 |
| `tool_call_started` | 6 |
| `bash_started` | 6 |
| `bash_completed` | 6 |
| `tool_result_skill_hint_appended` | 6 |
| `tool_call_completed` | 6 |
| `tool_execution_completed` | 6 |
| `tools_dispatched_parallel` | 6 |
