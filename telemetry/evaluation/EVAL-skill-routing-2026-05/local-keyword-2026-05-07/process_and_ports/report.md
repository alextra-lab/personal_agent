# Prompt: `process_and_ports`

_Ask for process and port information. Should route to system-diagnostics skill.
_

Tags: diagnostics

## Turn 1

- session_id: `e9eb66b7-cdd7-480f-92bd-ae610d79ecf6`
- trace_id:   `f3878a90-df7e-4c32-9b43-a33fb8e42d8d`
- duration:   22.55s

**User**

```
What processes are consuming the most memory right now?
Which ports is the agent listening on?

```

**Assistant**

```
## Top Memory Consumers

| Process | RSS | %MEM | Notes |
|---------|-----|------|-------|
| **uvicorn** (Python, PID 26) | **387 MB** | 1.6% | Main agent HTTP API |
| **uv** (PID 1) | 90 MB | 0.3% | Process manager / wrapper |
| **docker/mcp-gateway** (PID 50) | 29 MB | 0.1% | MCP server gateway |

The Uvicorn process dominates memory usage at ~387 MB RSS.

## Listening Ports

| Port | Protocol | Binding | Process |
|------|----------|---------|---------|
| **9001** | TCP | `0.0.0.0` (all interfaces) | **Uvicorn** — agent's HTTP API |
| 49554 | UDP | `127.0.0.11` only | Docker internal DNS |
| 40119 | TCP | `127.0.0.11` only | Docker internal DNS |

The agent's public-facing API is on **port 9001**, bound to all interfaces. The other two ports are Docker's embedded DNS resolver, only accessible locally.
```

**ES log summary**

- log_count: 75
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
| `model_call_started` | 4 |
| `model_call_completed` | 4 |
| `skill_index_assembled` | 2 |
| `tools_passed_to_llm` | 2 |
| `llm_call_messages_debug` | 2 |
| `history_sanitised` | 2 |
| `chat_completions_payload` | 2 |
| `raw_llm_response` | 2 |
| `tool_loop_gate` | 2 |
| `bash_auto_approved` | 2 |
| `tool_call_started` | 2 |
| `bash_started` | 2 |
| `tool_call_completed` | 2 |
| `bash_completed` | 2 |
| `tool_result_skill_hint_appended` | 2 |
| `request_received` | 1 |
| `intent_classified` | 1 |
| `decomposition_assessed` | 1 |
