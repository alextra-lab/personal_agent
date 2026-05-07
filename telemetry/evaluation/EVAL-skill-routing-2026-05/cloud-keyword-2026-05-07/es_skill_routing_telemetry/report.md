# Prompt: `es_skill_routing_telemetry`

_Query the skill routing telemetry itself. Validates skill_index_assembled and skill_routing_call_completed events are visible in ES.
_

Tags: telemetry, es, meta

## Turn 1

- session_id: `53fa91ab-b169-4910-90cc-38dfa7cfcaf8`
- trace_id:   `a789c162-1856-4211-bdf0-695d26eb5a6e`
- duration:   32.38s

**User**

```
Show me the skill_index_assembled events from the last hour.
What routing mode has been used and how many chars were injected per turn?

```

**Assistant**

```
Here's the full picture from the last hour's **32 `skill_index_assembled`** events:

---

## 🔍 `skill_index_assembled` — Last Hour Summary

### Routing Mode
**`keyword`** — used **100% of the time** across all 32 events and all 3 conversation traces. No other routing mode (e.g. `semantic`, `hybrid`) appeared at all.

---

### Injected Chars per Turn

| Trace ID | # Events | `injected_chars` values |
|---|---|---|
| `4e6e1674` | 25 | **14,096** on 24 turns · **5,193** on 1 turn (last turn of session) |
| `9ec3fb91` | 6 | **14,096** on 5 turns · **5,193** on 1 turn (last turn of session) |
| `a789c162` | 5 | **14,096** on all 5 turns (current session, still ongoing) |

**Pattern:**
- The dominant injection size is **14,096 chars** — this is the full skill index being injected.
- The smaller **5,193 chars** appears exactly once at the *end* of two completed sessions — likely a trimmed/partial index injected on the final tool-heavy turn (possibly after the budget was constrained or the skill index was narrowed).
- `loaded_skills_count` is **0** across all events, meaning no individual skill docs were loaded/expanded — only the index header was injected.
- `char_count` is `null` throughout (that field wasn't populated by this event type).

---

### Key Takeaways
1. **Skill routing is locked to `keyword` mode** — no semantic or hybrid routing has been triggered this hour.
2. **14,096 chars is the standard injection size** — consistently flat, suggesting the index hasn't grown or shrunk meaningfully between turns.
3. **5,193-char outlier** only appears at session end, likely reflecting a reduced context budget on the final turns before the session closed.
4. **No skill docs were expanded** (`loaded_skills_count = 0`) — every turn got the index only, no deep skill reads were logged at assembly time.
```

**ES log summary**

- log_count: 162
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
| `tool_loop_gate` | 5 |
| `step_executed` | 5 |
| `bash_allowlist_miss` | 5 |
| `tool_call_started` | 5 |
| `bash_started` | 5 |
| `bash_completed` | 5 |
| `tool_call_completed` | 5 |
| `tool_result_skill_hint_appended` | 5 |
| `tools_dispatched_parallel` | 5 |
