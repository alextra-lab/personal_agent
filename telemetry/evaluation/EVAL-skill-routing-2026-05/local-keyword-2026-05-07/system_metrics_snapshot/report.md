# Prompt: `system_metrics_snapshot`

_Ask for current resource utilization. Should route to system-metrics skill.
_

Tags: diagnostics, metrics

## Turn 1

- session_id: `49200829-327e-4615-8dda-03e8f453a5ca`
- trace_id:   `7d391f81-7f6c-4f9f-b845-31c521058730`
- duration:   20.70s

**User**

```
How much memory is the agent currently using? What is the CPU load
and how much disk space is left?

```

**Assistant**

```
Here's the current system snapshot:

| Metric | Value |
|--------|-------|
| **Agent Memory (RSS)** | **~377 MB** (main uvicorn process, PID 26) |
| **Parent `uv` process** | ~88 MB (PID 1) |
| **CPU Load Average** | **0.70 / 0.37 / 0.37** (1m / 5m / 15m) |
| **Disk Space** | **125 GB free** of 197 GB total (34% used) |

The agent is using about **377 MB of RAM** and the CPU is mostly idle (load well below 1 core). There's plenty of disk headroom with **125 GB remaining**. The system has been up for nearly 4 days.
```

**ES log summary**

- log_count: 82
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
| `tool_call_completed` | 3 |
| `tool_result_skill_hint_appended` | 3 |
| `bash_completed` | 3 |
| `skill_index_assembled` | 2 |
| `tools_passed_to_llm` | 2 |
| `llm_call_messages_debug` | 2 |
| `history_sanitised` | 2 |
| `chat_completions_payload` | 2 |
| `raw_llm_response` | 2 |
| `request_received` | 1 |
| `intent_classified` | 1 |
| `proactive_memory_suggest_start` | 1 |
