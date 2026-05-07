# Prompt: `es_skill_routing_telemetry`

_Query the skill routing telemetry itself. Validates skill_index_assembled and skill_routing_call_completed events are visible in ES.
_

Tags: telemetry, es, meta

## Turn 1

- session_id: `a8afdace-16a5-446c-818e-1659b85e0b74`
- trace_id:   `c2d3da7b-05e5-4da4-96ec-87a3998560d6`
- duration:   35.20s

**User**

```
Show me the skill_index_assembled events from the last hour.
What routing mode has been used and how many chars were injected per turn?

```

**Assistant**

```
Here's the full picture from the last hour — **141 `skill_index_assembled` events** across **3 routing modes**:

---

## 🗂️ Routing Modes & Injected Characters

| Routing Mode | Events | Avg Chars | Min Chars | Max Chars |
|---|---|---|---|---|
| **keyword** | 55 (39%) | 11,982 | 5,193 | 14,096 |
| **hybrid** | 50 (35%) | 13,434 | 6,856 | 15,759 |
| **model_decided** | 36 (26%) | 10,212 | 1,661 | 13,397 |

---

## Key Observations

- **All three routing modes are active** this hour — this is new compared to prior sessions where only `keyword` was observed. `hybrid` and `model_decided` have joined the mix.
- **`hybrid` injects the most context** on average (~13.4K chars/turn), suggesting it pulls from both keyword and semantic signals and errs on the side of completeness.
- **`keyword` is still the most frequent** mode (55 events, 39%), consistent with past history.
- **`model_decided` has the widest variance** (1,661–13,397 chars), which makes sense — the model chooses how much to inject dynamically, sometimes being very selective (1.6K) and sometimes verbose (13.4K).
- **Note:** The raw per-event query hit a schema error — fields `skills_injected` and `total_chars` don't exist in the index. The confirmed field is `injected_chars` (used in the aggregate above).
```

**ES log summary**

- log_count: 182
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 0
- capture_id: None
- extraction_outcome: None
- tool_calls: 8 requested / 8 executed (bash: 7)
- loop_gate_decisions: ['allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow']

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `request_trace_step` | 19 |
| `state_transition` | 13 |
| `tool_loop_gate` | 8 |
| `tool_call_completed` | 8 |
| `tool_call_started` | 8 |
| `bash_started` | 7 |
| `bash_completed` | 7 |
| `tool_result_skill_hint_appended` | 7 |
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
| `tool_execution_completed` | 5 |
| `tools_dispatched_parallel` | 5 |
