# Prompt: `system_metrics_snapshot`

_Ask for current resource utilization. Should route to system-metrics skill.
_

Tags: diagnostics, metrics

## Turn 1

- session_id: `555074b0-f606-4c7b-bf6b-25b40053d809`
- trace_id:   `7d0f600f-121f-47b8-94be-1bf553e91941`
- duration:   22.23s

**User**

```
How much memory is the agent currently using? What is the CPU load
and how much disk space is left?

```

**Assistant**

```
Here's the current system snapshot:

**Agent Memory (RSS)**
| Process | RSS | % Memory |
|---|---|---|
| uvicorn (PID 24) | **~383 MB** | 1.6% |
| uv run (PID 1) | ~90 MB | 0.3% |

The main agent process (uvicorn) is using about **383 MB** of memory.

**CPU Load Average**
| 1 min | 5 min | 15 min |
|---|---|---|
| 1.54 | 0.65 | 0.47 |

Load is elevated on the 1-minute window but trending down toward the 15-minute average — fairly normal activity.

**Disk Space**
| Total | Used | Available | Use% |
|---|---|---|---|
| 197 GB | 64 GB | **126 GB** | 34% |

Plenty of room — **126 GB free** on the root filesystem.
```

**ES log summary**

- log_count: 84
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 0
- capture_id: None
- extraction_outcome: None
- tool_calls: 3 requested / 3 executed (bash: 3)
- loop_gate_decisions: ['allow', 'allow', 'allow']

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `request_trace_step` | 11 |
| `state_transition` | 5 |
| `model_call_started` | 4 |
| `model_call_completed` | 4 |
| `tool_loop_gate` | 3 |
| `bash_auto_approved` | 3 |
| `tool_call_started` | 3 |
| `bash_started` | 3 |
| `bash_completed` | 3 |
| `tool_result_skill_hint_appended` | 3 |
| `tool_call_completed` | 3 |
| `skill_index_assembled` | 2 |
| `tools_passed_to_llm` | 2 |
| `llm_call_messages_debug` | 2 |
| `history_sanitised` | 2 |
| `chat_completions_payload` | 2 |
| `raw_llm_response` | 2 |
| `request_received` | 1 |
| `intent_classified` | 1 |
| `decomposition_assessed` | 1 |
