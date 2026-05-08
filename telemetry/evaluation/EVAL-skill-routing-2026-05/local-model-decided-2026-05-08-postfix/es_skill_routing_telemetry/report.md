# Prompt: `es_skill_routing_telemetry`

_Query the skill routing telemetry itself. Validates skill_index_assembled and skill_routing_call_completed events are visible in ES.
_

Tags: telemetry, es, meta

## Turn 1

- session_id: `956ab769-e197-4865-bdac-a6dba6633b0a`
- trace_id:   `256d73b6-74e1-4565-a3b2-0abe84a9ac11`
- duration:   406.78s

**User**

```
Show me the skill_index_assembled events from the last hour.
What routing mode has been used and how many chars were injected per turn?

```

**Assistant**

```
Here are the **skill_index_assembled** events from the last hour, along with routing mode and injected chars analysis.

---

## Summary

| Metric | Value |
|---|---|
| **Event count (last hour)** | **100** |
| **Time span** | ~06:07 UTC to 06:46 UTC |
| **Frequency** | ~1 event per minute (steady periodic process) |

---

## Routing Mode

| Routing Mode | Count | % of Total |
|---|---|---|
| **model_decided** | 99 | 99% |
| hybrid | 1 | 1% |

**Virtually all** skill index assemblies use the `model_decided` routing mode. The single `hybrid` outlier occurred at `2026-05-08T06:26:49Z` with 6,856 injected chars — the only event in the window that didn't use `model_decided`.

---

## Injected Characters (per event)

The `injected_chars` field contains **discrete token-count values** that repeat across events, suggesting different skill index sizes depending on what was being assembled:

| Injected Chars | Count | First Occurrence |
|---|---|---|
| **18,344** | 41 | 06:11:09 |
| **10,559** | 17 | 06:07:40 |
| **13,397** | 13 | 06:07:23 |
| **9,300** | 6 | 06:09:14 |
| **7,703** | 4 | 06:08:55 |
| **5,793** | 4 | 06:08:38 |
| **5,515** | 4 | 06:08:22 |
| **4,835** | 4 | 06:09:10 |
| **4,367** | 4 | 06:09:43 |
| **1,661** | 2 | 06:10:57 |
| **6,856** | 1 | 06:26:49 (hybrid mode) |

### Key observations:
- **Range**: 1,661 – 18,344 chars per event
- **Most common**: 18,344 chars (41% of events) — likely the full/default skill index
- **Second most common**: 10,559 chars (17%) — possibly a trimmed or partial index
- **Smallest**: 1,661 chars — a minimal index (likely during initial assembly or cleanup)
- **All events** have `model`, `turn_count`, `task_type`, and `session_id` as `null` — these are background/system events, not user-facing turns

---

## Interpretation

The `skill_index_assembled` events appear to be a **periodic background process** (likely a cron or scheduler) that continuously rebuilds/references the skill index. The varying `injected_chars` values suggest different index sizes depending on the current state of available skills — the 18,344-char value is the "full" index, while smaller values represent partial or incremental assemblies.
```

**ES log summary**

- log_count: 208
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 0
- capture_id: None
- extraction_outcome: None
- tool_calls: 8 requested / 8 executed (bash: 8)
- loop_gate_decisions: ['allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow', 'allow']

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `request_trace_step` | 21 |
| `state_transition` | 15 |
| `model_call_started` | 14 |
| `model_call_completed` | 14 |
| `tool_loop_gate` | 8 |
| `tool_call_started` | 8 |
| `bash_started` | 8 |
| `bash_completed` | 8 |
| `tool_result_skill_hint_appended` | 8 |
| `tool_call_completed` | 8 |
| `skill_index_assembled` | 7 |
| `tools_passed_to_llm` | 7 |
| `llm_call_messages_debug` | 7 |
| `history_sanitised` | 7 |
| `chat_completions_payload` | 7 |
| `raw_llm_response` | 7 |
| `bash_allowlist_miss` | 7 |
| `step_executed` | 6 |
| `tool_execution_completed` | 6 |
| `tools_dispatched_parallel` | 6 |
