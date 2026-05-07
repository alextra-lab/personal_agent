# Prompt: `es_tool_error_analysis`

_Ask for a structured analysis of tool errors. Should route to ES skill.
_

Tags: telemetry, es

## Turn 1

- session_id: `446a3d98-72aa-4b25-8079-5a87a6223cd4`
- trace_id:   `9ec3fb91-65a9-4838-9d57-bfd4cb663edb`
- duration:   32.85s

**User**

```
Look at the recent traces and tell me: which tools have been failing most
often in the last 24 hours? Show me the top 3 by error count.

```

**Assistant**

```
Based on the queries I've run, here's what the data shows:

---

## 🔍 Tool Failure Analysis — Last 24 Hours

**The data returned zero tool-level error records.** Here's what I found and why:

### What the logs contain
The `agent-logs-*` index is heavily populated (34K+ events in 24h), but the **event types logged are infrastructure/system events** — things like `metrics.sampled`, `sensor_poll`, `mode_controller_evaluated`, `tool_loop_gate`, etc. — not tool execution outcomes with a `tool_name` + `success=false` structure.

Specifically:
- Querying `event_type == "tool_call_completed" AND success == false` → **0 results**
- Querying `level == "ERROR" AND tool_name IS NOT NULL` → **0 results** (only 1 log with a `tool_name` field existed, for `read_skill`)

### What this likely means
Tool call outcomes are either:
1. **Not instrumented at the event level** — failures may be embedded in message content rather than structured fields
2. **Logged under a different schema** — e.g. errors captured in `model_call_completed` events or in a separate index not covered by `agent-logs-*`
3. **Genuinely error-free** — the system may have had no hard tool failures in the last 24h (consistent with [prior log reviews](memory) which found errors were mostly transient rate-limit/connection issues at the *model call* level, not tool level)

### Recommendation
To get accurate tool failure data going forward, structured logging of tool outcomes (tool name, success/failure, error type, latency) into a dedicated field would make this query trivial. Want me to file a Linear issue to add that instrumentation?
```

**ES log summary**

- log_count: 168
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
| `request_trace_step` | 19 |
| `state_transition` | 13 |
| `skill_index_assembled` | 6 |
| `model_call_started` | 6 |
| `tools_passed_to_llm` | 6 |
| `llm_call_messages_debug` | 6 |
| `history_sanitised` | 6 |
| `cost_gate_reserved` | 6 |
| `litellm_request_start` | 6 |
| `litellm_request_complete` | 6 |
| `model_call_completed` | 6 |
| `tool_loop_gate` | 6 |
| `bash_allowlist_miss` | 6 |
| `tool_call_started` | 6 |
| `bash_started` | 6 |
| `bash_completed` | 6 |
| `tool_call_completed` | 6 |
| `tool_result_skill_hint_appended` | 6 |
| `step_executed` | 5 |
| `tools_dispatched_parallel` | 5 |
