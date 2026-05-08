# Prompt: `es_skill_routing_telemetry`

_Query the skill routing telemetry itself. Validates skill_index_assembled and skill_routing_call_completed events are visible in ES.
_

Tags: telemetry, es, meta

## Turn 1

- session_id: `0b9e74ce-2de6-4a4e-8f2e-737bfb3eaa65`
- trace_id:   `feb46c29-0a73-422e-8b44-6a100ba7ddce`
- duration:   27.04s

**User**

```
Show me the skill_index_assembled events from the last hour.
What routing mode has been used and how many chars were injected per turn?

```

**Assistant**

```
Here's a full summary of the `skill_index_assembled` events from the last hour:

---

## 📊 skill_index_assembled — Last Hour (50 events, capped at 50)

### Routing Mode
**100% `model_decided`** — every single event used the `model_decided` routing mode. No other mode (`always_inject`, `never_inject`, etc.) appeared at all.

### Injected Characters per Trace
Each trace has a **fixed `injected_chars` value** (the skill index payload is stable within a request). Here's the breakdown by unique trace:

| Trace ID (short) | `injected_chars` | Events |
|---|---|---|
| `ece81a1b` | **18,344** | 29 |
| `cf687e26` | **13,397** | 5 |
| `feb46c29` | **10,559** | 5 |
| `1b0c6806` | **10,559** | 2 |
| `57d4051c` | **5,793** | 2 |
| `747a7919` | **5,515** | 2 |
| `b6575e04` | **7,703** | 2 |
| `effa1d2d` | **4,835** | 2 |
| `a446c749` | **9,300** | 2 |
| `3a0d78e2` | **4,367** | 2 |
| `5d95bfa2` | **1,661** | 1 |

### Key Observations
- **Range:** 1,661 – 18,344 chars injected, depending on how many skills were deemed relevant by the model.
- **High repetition per trace:** `ece81a1b` fired 29 assembly events — this is the current long-running conversation (this session), which has many LLM turns.
- **`model_id` is null** across all events — the model identity isn't being logged into this field at the `skill_index_assembled` stage (it's likely logged at the `litellm_request_complete` stage instead).
- **No `session_id`** values are populated either — consistent with prior observations.
```

**ES log summary**

- log_count: 164
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 0
- capture_id: None
- extraction_outcome: None
- tool_calls: 5 requested / 5 executed (bash: 5)
- loop_gate_decisions: ['allow', 'allow', 'allow', 'allow', 'allow']

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
| `step_executed` | 5 |
| `tool_loop_gate` | 5 |
| `bash_allowlist_miss` | 5 |
| `tool_call_started` | 5 |
| `bash_started` | 5 |
| `tool_result_skill_hint_appended` | 5 |
| `bash_completed` | 5 |
| `tool_call_completed` | 5 |
| `tools_dispatched_parallel` | 5 |
