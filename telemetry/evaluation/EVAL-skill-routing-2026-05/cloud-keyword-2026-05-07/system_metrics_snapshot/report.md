# Prompt: `system_metrics_snapshot`

_Ask for current resource utilization. Should route to system-metrics skill.
_

Tags: diagnostics, metrics

## Turn 1

- session_id: `5acef3e5-9355-4bf8-a6e8-674b29178db7`
- trace_id:   `844da311-a11d-496d-9312-c83a275797eb`
- duration:   11.68s

**User**

```
How much memory is the agent currently using? What is the CPU load
and how much disk space is left?

```

**Assistant**

```
Here's the current system snapshot:

---

### 🧠 Agent Memory Usage
| Process | RSS (RAM) | % of Host RAM |
|---|---|---|
| `uvicorn` (main agent) | **366 MB** (374,516 KB) | 1.6% |
| `uv` launcher | ~88 MB (90,248 KB) | 0.3% |

The primary agent process is consuming about **366 MB of RAM**.

---

### ⚙️ CPU Load
| Metric | Value |
|---|---|
| Load average (1m / 5m / 15m) | **0.57 / 0.49 / 0.34** |
| CPU usage breakdown | 9.6% user, 2.4% sys, **86.7% idle** |
| Agent (`uvicorn`) instantaneous CPU | ~20% |

The system is lightly loaded — the CPU is mostly idle with a gentle uptick from the agent itself.

---

### 💾 Disk Space
| Filesystem | Size | Used | Available | Use% |
|---|---|---|---|---|
| Root overlay (`/`) | 197 GB | 64 GB | **125 GB** | 34% |
| `/dev/shm` | 64 MB | 0 | 64 MB | 0% |
| `/run/docker.sock` | 2.3 GB | 1.9 MB | 2.3 GB | 1% |

Plenty of disk space remaining — **125 GB free** (66% of the 197 GB volume), and the system has been up for **3 days, 20 hours**.
```

**ES log summary**

- log_count: 81
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
| `tool_loop_gate` | 3 |
| `bash_auto_approved` | 3 |
| `tool_call_started` | 3 |
| `bash_started` | 3 |
| `bash_completed` | 3 |
| `tool_call_completed` | 3 |
| `tool_result_skill_hint_appended` | 3 |
| `skill_index_assembled` | 2 |
| `model_call_started` | 2 |
| `tools_passed_to_llm` | 2 |
| `llm_call_messages_debug` | 2 |
| `history_sanitised` | 2 |
| `cost_gate_reserved` | 2 |
| `litellm_request_start` | 2 |
| `litellm_request_complete` | 2 |
| `model_call_completed` | 2 |
| `request_received` | 1 |
| `intent_classified` | 1 |
