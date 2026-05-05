# Prompt: `loop_prone_query_refinement`

_A query that historically led the agent to repeat a tool call with slightly different parameters until the loop gate fired. Tests whether the gate hard-stops legitimate exploration.
_

Tags: loop-gate, exploration

## Turn 1

- session_id: `859670e8-e10e-40cc-83e5-08486de061d3`
- trace_id:   `0f60b2e2-cef6-43c4-8bc4-2c6c46353deb`
- duration:   133.17s

**User**

```
Search the codebase for the canonical implementation of context
compression. There may be more than one candidate (manager-level,
within-session, and async). Find each and tell me which one fires
first when the token budget is exceeded mid-orchestration.

```

**Assistant**

```
Here is the complete picture. There are **three** canonical context-compression implementations, each at a different layer and timing in the orchestration lifecycle:

---

## 1. Within-Session Hard Compression — `within_session_compression.py`

**File:** `/app/src/personal_agent/orchestrator/within_session_compression.py`

**What it does:** In-place hard compression of the working `ctx.messages` list when the token count *during* orchestration crosses a hard threshold (default 85% of `context_window_max_tokens`). It calls `compress_in_place()` which triggers the compression layers (Stage 1–7) synchronously.

**Where it fires — in `step_llm_call()`:**
```python
# ADR-0061 — within-session hard trigger.  Fires synchronously when the
# working messages list crosses the hard threshold (default 0.85 of the
# context window).  Layers above Stage 7 (which runs at request entry);
# this catches in-flight overflow caused by large tool responses.
if ctx.session_id and needs_hard_compression(
    ctx.messages, settings.context_window_max_tokens
):
    ctx.messages, _ = await compress_in_place(
        ctx.messages,
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        trigger="hard",
        bus=_bus,
    )
```

This is placed at the **top of `step_llm_call()`**, right before the LLM is invoked. It catches overflow caused by large tool responses that were appended during the previous `step_tool_execution` turn.

---

## 2. Manager-Level Async Compression — `compression_manager.py`

**File:** `/app/src/personal_agent/orchestrator/compression_manager.py`

**What it does:** The high-level `maybe_trigger_compression()` entry point that dispatches to the async compression pipeline (layers 1–7). It is request-scoped and fires *after* the orchestration loop completes.

**Where it fires — in `execute_task_safe()`:**
```python
# ADR-0038 + ADR-0061 §D1 soft trigger.  Bus threaded through so the
# within-session compression event lands on
# ``stream:context.within_session_compressed``.
if ctx.session_id:
    compression_manager.maybe_trigger_compression(
        session_id=ctx.session_id,
        messages=ctx.messages,
        trace_id=ctx.trace_id,
        bus=_soft_bus,
    )
```

This fires **after** `execute_task()` returns — i.e., once the task is COMPLETED or FAILED. It is the "soft trigger" that runs at the end of the request.

---

## 3. Context Window — `context_window.py`

**File:** `/app/src/personal_agent/orchestrator/context_window.py`

**What it does:** `apply_context_window()` — the pre-LLM context trimming function. It enforces the `context_window_max_tokens` budget via a strategy (e.g., "last_N", "sliding_window", "importance") and optionally applies a pre-computed compression summary.

**Where it fires — in `step_init()`:**
```python
# Apply context window controls before LLM usage to prevent overflow.
ctx.messages = apply_context_window(
    ctx.messages,
    max_tokens=settings.context_window_max_tokens,
    strategy=settings.conversation_context_strategy,
    trace_id=ctx.trace_id,
    session_id=ctx.session_id,
    compressed_summary=_summary,
)
```

This fires at the **beginning of `step_init()`**, before any memory queries or LLM calls. It is the first line of defense at the start of each request.

---

## Which fires first when the token budget is exceeded mid-orchestration?

**The within-session hard compression (`within_session_compression.py`) fires first.**

Here is the exact sequence when a large tool response pushes the context over budget *during* the orchestration loop:

| Order | Component | File | Trigger Point |
|-------|-----------|------|---------------|
| **1** | **Within-session hard compression** | `within_session_compression.py` | Top of `step_llm_call()` — checks `needs_hard_compression()` on the working message list, calls `compress_in_place()` |
| **2** | Manager-level async compression | `compression_manager.py` | After `execute_task()` returns in `execute_task_safe()` — the "soft t
... <truncated, 4664 chars total>
```

**ES log summary**

- log_count: 172
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 7
- capture_id: None
- extraction_outcome: None
- tool_calls: 9 requested / 9 executed (bash: 3)
- loop_gate_decisions: ['allow', 'allow', 'allow', 'allow', 'warn_consecutive', 'allow', 'warn_consecutive', 'warn_consecutive', 'advise_identity']

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `request_trace_step` | 17 |
| `state_transition` | 11 |
| `model_call_started` | 10 |
| `model_call_completed` | 10 |
| `tool_loop_gate` | 9 |
| `tool_call_started` | 9 |
| `tool_call_completed` | 9 |
| `read_executor_called` | 6 |
| `read_executor_success` | 6 |
| `tools_passed_to_llm` | 5 |
| `llm_call_messages_debug` | 5 |
| `history_sanitised` | 5 |
| `chat_completions_payload` | 5 |
| `raw_llm_response` | 5 |
| `step_executed` | 4 |
| `tools_dispatched_parallel` | 4 |
| `tool_execution_completed` | 4 |
| `within_session_compression_recorded` | 4 |
| `within_session_compression_completed` | 4 |
| `bash_auto_approved` | 3 |
