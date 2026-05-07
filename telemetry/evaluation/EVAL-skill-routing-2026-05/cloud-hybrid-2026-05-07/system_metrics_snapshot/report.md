# Prompt: `system_metrics_snapshot`

_Ask for current resource utilization. Should route to system-metrics skill.
_

Tags: diagnostics, metrics

## Turn 1

- session_id: `21cb467a-f1f5-45b9-93a4-692f943f9578`
- trace_id:   `080f6fad-c6e8-463a-a496-8cdf4fb604a0`
- duration:   11.34s

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
| Process | PID | RSS | % of Host RAM |
|---|---|---|---|
| **Uvicorn worker** (Python) | 26 | **371 MB** | 1.5% |
| `uv` launcher | 1 | **88 MB** | 0.3% |
| **Total agent footprint** | — | **~459 MB** | ~1.8% |

---

### ⚡ CPU Load
| Metric | Value |
|---|---|
| Load average (1m / 5m / 15m) | **1.02 / 0.78 / 0.55** |
| User CPU | 13.4% |
| System CPU | 3.7% |
| Idle | 79.3% |
| Wait (I/O) | 1.2% |

The host has **24 vCPUs** worth of headroom — a load average of 1.02 is quite light. CPU is mostly idle (~79%).

---

### 💾 Disk Space
| Mount | Size | Used | Available | Used% |
|---|---|---|---|---|
| `/` (overlay) | 197 GB | 64 GB | **125 GB** | 34% |

Plenty of disk space remaining — **125 GB free** with only 34% utilization.

---

**Summary:** The agent is healthy — memory is around **~460 MB total**, CPU load is light at **1.02**, and disk has **125 GB** available. No concerns.
```

**ES log summary**

- log_count: 80
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
| `litellm_request_start` | 2 |
| `cost_gate_reserved` | 2 |
| `litellm_request_complete` | 2 |
| `model_call_completed` | 2 |
| `request_received` | 1 |
| `intent_classified` | 1 |
