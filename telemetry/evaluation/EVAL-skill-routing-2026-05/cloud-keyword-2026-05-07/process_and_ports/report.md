# Prompt: `process_and_ports`

_Ask for process and port information. Should route to system-diagnostics skill.
_

Tags: diagnostics

## Turn 1

- session_id: `35f4ff90-31c4-4d2f-9e97-2e52962d3703`
- trace_id:   `917a8d49-1416-4444-9443-660a0cc525cc`
- duration:   10.53s

**User**

```
What processes are consuming the most memory right now?
Which ports is the agent listening on?

```

**Assistant**

```
Here's the snapshot:

---

### 🧠 Top Memory Consumers

| PID | %MEM | RSS | Command |
|-----|------|-----|---------|
| **26** | **1.5%** | **~366 MB** | `uvicorn` (main agent worker — `/app/.venv/bin/python`) |
| **1** | **0.3%** | ~88 MB | `uv run uvicorn` (launcher / supervisor process) |
| **50** | **0.1%** | ~28 MB | `docker run` (MCP Gateway — `sequentialthinking` + `context7` servers) |

The **Uvicorn worker (PID 26)** is by far the largest consumer at ~366 MB RSS / 1.5% of host memory. The `uv` launcher (PID 1) holds ~88 MB, and the MCP Gateway container runner is lightweight at ~28 MB. Everything looks normal — no runaway processes.

---

### 🔌 Listening Ports

| Protocol | Port | Process |
|----------|------|---------|
| **TCP 9001** | `0.0.0.0:9001` | `uvicorn` (PID 26) — the main agent HTTP API |
| TCP 40119 | `127.0.0.11:40119` | Docker embedded DNS (internal) |
| UDP 49554 | `127.0.0.11:49554` | Docker embedded DNS (internal) |

The agent is publicly listening on **port 9001** (all interfaces). The other two entries are Docker's internal DNS resolver — not externally exposed.
```

**ES log summary**

- log_count: 73
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 0
- capture_id: None
- extraction_outcome: None
- tool_calls: 2 requested / 2 executed (bash: 2)
- loop_gate_decisions: ['allow', 'allow']

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `request_trace_step` | 11 |
| `state_transition` | 5 |
| `skill_index_assembled` | 2 |
| `model_call_started` | 2 |
| `tools_passed_to_llm` | 2 |
| `llm_call_messages_debug` | 2 |
| `history_sanitised` | 2 |
| `cost_gate_reserved` | 2 |
| `litellm_request_start` | 2 |
| `litellm_request_complete` | 2 |
| `model_call_completed` | 2 |
| `tool_loop_gate` | 2 |
| `bash_auto_approved` | 2 |
| `tool_call_started` | 2 |
| `bash_started` | 2 |
| `bash_completed` | 2 |
| `tool_call_completed` | 2 |
| `tool_result_skill_hint_appended` | 2 |
| `request_received` | 1 |
| `intent_classified` | 1 |
