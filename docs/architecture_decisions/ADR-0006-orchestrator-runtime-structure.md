# ADR-0006: Orchestrator Runtime Structure & Execution Model

**Status:** Proposed
**Date:** 2025-12-28
**Decision Owner:** Project Owner

---

## 1. Context

The **Orchestrator Core** is the deterministic "cortex" of the personal agent, responsible for:

- Receiving user requests and interpreting intent
- Planning and executing multi-step workflows
- Coordinating LLM calls, tool invocations, and safety checks
- Maintaining session state and conversation history
- Emitting structured telemetry for observability
- Respecting governance constraints (modes, tool permissions, rate limits)

The orchestrator must balance:

- **Determinism** (for safety, debuggability, and reproducibility)
- **Flexibility** (to support exploratory reasoning and adaptive workflows)
- **Simplicity** (to ship MVP quickly without overengineering)
- **Extensibility** (to evolve toward richer agentic capabilities)

ADR-0002 established the **hybrid orchestration model**: deterministic graph/state machine for control, with embedded LLM cognition inside bounded steps. This ADR makes that concrete:

- What does the graph/execution model look like?
- How is state managed?
- Sync vs async execution?
- Error handling and recovery?
- Session persistence?

---

## 2. Decision

### 2.1 Execution Model: Explicit State Machine with Step Functions

The orchestrator implements a **small explicit state machine** where:

- Each **state** represents a phase of task execution
- Each **transition** is triggered by a step function that returns a next state
- Step functions are **pure Python functions** (no framework magic)
- State is explicitly passed between steps (no hidden global state)

This gives us:

- **Transparency**: Can inspect current state at any time
- **Debuggability**: Can reconstruct execution by replaying state transitions
- **Testability**: Step functions are unit-testable in isolation
- **Simplicity**: No external graph library required for MVP

#### Example: Simple Q&A Flow

```python
from enum import Enum
from dataclasses import dataclass

class TaskState(str, Enum):
    INIT = "init"
    PLANNING = "planning"
    LLM_CALL = "llm_call"
    TOOL_EXECUTION = "tool_execution"
    SYNTHESIS = "synthesis"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class ExecutionContext:
    """Mutable state container passed through execution steps."""
    session_id: str
    trace_id: str
    user_message: str
    mode: Mode
    channel: Channel
    messages: list[dict]  # OpenAI-style chat history
    current_plan: Plan | None = None
    tool_results: list[ToolResult] = field(default_factory=list)
    final_reply: str | None = None
    error: Exception | None = None

def execute_task(ctx: ExecutionContext) -> ExecutionContext:
    """Main execution loop: iterate states until terminal."""
    state = TaskState.INIT

    while state not in {TaskState.COMPLETED, TaskState.FAILED}:
        logger.info("state_transition", state=state, trace_id=ctx.trace_id)
        state = step_functions[state](ctx)

    return ctx

# Step functions
def step_init(ctx: ExecutionContext) -> TaskState:
    """Initialize: determine intent and next action."""
    # Query governance for current mode constraints
    # Decide if tools are needed
    return TaskState.PLANNING if needs_planning(ctx) else TaskState.LLM_CALL

def step_planning(ctx: ExecutionContext) -> TaskState:
    """Use reasoning model to create an execution plan."""
    # Call LLM with planning prompt
    # Parse plan, store in ctx.current_plan
    return TaskState.LLM_CALL

def step_llm_call(ctx: ExecutionContext) -> TaskState:
    """Execute LLM call with or without tools."""
    # Call LocalLLMClient.respond(...)
    # If tool_calls returned, transition to TOOL_EXECUTION
    # Otherwise, transition to SYNTHESIS
    response = llm_client.respond(...)
    if response.tool_calls:
        return TaskState.TOOL_EXECUTION
    else:
        ctx.final_reply = response.content
        return TaskState.SYNTHESIS

def step_tool_execution(ctx: ExecutionContext) -> TaskState:
    """Execute tool calls, append results to context."""
    # For each tool call, check governance, execute, collect results
    # Append tool results to ctx.tool_results
    # Loop back to LLM_CALL for synthesis
    return TaskState.LLM_CALL

def step_synthesis(ctx: ExecutionContext) -> TaskState:
    """Finalize response."""
    # ctx.final_reply already set or needs final formatting
    return TaskState.COMPLETED

step_functions = {
    TaskState.INIT: step_init,
    TaskState.PLANNING: step_planning,
    TaskState.LLM_CALL: step_llm_call,
    TaskState.TOOL_EXECUTION: step_tool_execution,
    TaskState.SYNTHESIS: step_synthesis,
}
```

This is **simple**, **explicit**, and **traceable**. More complex flows (parallel branches, human approval checkpoints) can be added incrementally.

---

### 2.2 Session Management & State Persistence

#### Session Scope

- A **session** is a series of user interactions (multi-turn conversation)
- Each session has:
  - `session_id` (UUID)
  - `messages: list[dict]` (OpenAI-style chat history)
  - `metadata: dict` (mode, channel, user preferences)
  - `created_at`, `last_active_at`

#### Storage Strategy (MVP)

**In-memory first, optional persistence:**

- Active sessions stored in `dict[str, Session]` in Orchestrator process memory
- On graceful shutdown, serialize active sessions to `telemetry/sessions/<session_id>.json`
- On startup, load recent sessions (e.g., last 24 hours)
- After 24 hours of inactivity, sessions archived (moved to `telemetry/sessions/archive/`)

**Why in-memory first?**

- **Simplicity**: No database setup required
- **Fast**: No I/O overhead during execution
- **Sufficient for MVP**: Single user, single machine, sessions are relatively short
- **Evolution path**: Can add SQLite/DB later without changing Orchestrator interface

#### Session API

```python
class SessionManager:
    def create_session(self, mode: Mode, channel: Channel) -> str:
        """Create new session, return session_id."""
        ...

    def get_session(self, session_id: str) -> Session | None:
        """Retrieve active session."""
        ...

    def update_session(self, session_id: str, messages: list[dict]) -> None:
        """Update conversation history."""
        ...

    def list_active_sessions(self) -> list[Session]:
        """List all active sessions."""
        ...
```

---

### 2.3 Asynchronous Execution (First-Class)

**Decision: Use `asyncio` from the start.**

#### Rationale for Async-First

- **Migration cost is high**: Refactoring sync → async touches every component (orchestrator, LLM client, tools, tests). Better to build correctly from day one.
- **Enables key capabilities**:
  - **Parallel tool execution**: Run multiple independent tools concurrently (e.g., check CPU + check disk simultaneously)
  - **Streaming responses**: LLM tokens streamed to UI as they arrive (better UX)
  - **Background tasks**: Brainstem sensors, health checks can run without blocking user requests
  - **Concurrent sessions**: Handle multiple user sessions efficiently
- **Modern Python norm**: `asyncio` is mature, well-supported, and increasingly expected in Python 3.12+
- **Determinism preserved**: Async doesn't break determinism—execution order is still explicit, just non-blocking

#### Implementation Approach

- **Core orchestrator**:
  ```python
  async def execute_task(ctx: ExecutionContext) -> ExecutionContext:
      state = TaskState.INIT

      while state not in {TaskState.COMPLETED, TaskState.FAILED}:
          logger.info("state_transition", state=state)
          state = await step_functions[state](ctx)  # Await async step functions

      return ctx
  ```

- **Step functions are async**:
  ```python
  async def step_llm_call(ctx: ExecutionContext) -> TaskState:
      response = await llm_client.respond(...)  # Non-blocking LLM call
      if response.tool_calls:
          return TaskState.TOOL_EXECUTION
      else:
          ctx.final_reply = response.content
          return TaskState.SYNTHESIS
  ```

- **LLM Client async**:
  ```python
  class LocalLLMClient:
      async def respond(self, role: ModelRole, messages: list[dict], ...) -> LLMResponse:
          async with httpx.AsyncClient() as client:
              response = await client.post(endpoint, json=payload)
              return self._parse_response(response)
  ```

- **Tool Layer async**:
  ```python
  class ToolExecutionLayer:
      async def execute_tool(self, tool_name: str, arguments: dict, ...) -> ToolResult:
          # Permission checks (synchronous, fast)
          # Tool execution (potentially async for network/subprocess)
          result = await executor(**arguments)
          return ToolResult(...)
  ```

#### Async Best Practices

1. **Use `httpx.AsyncClient`** for HTTP calls (LLM endpoints, web search)
2. **Use `asyncio.subprocess`** for shell commands
3. **Use `asyncio.gather()`** for parallel operations (tool calls, multi-agent reasoning)
4. **Keep governance checks synchronous** (fast, no I/O, deterministic)
5. **Test with `pytest-asyncio`** (already in dependencies)

---

### 2.4 Error Handling & Recovery

#### Error Classification

1. **Recoverable errors** (retry or fallback):
   - LLM timeout → retry once or use smaller model
   - Tool execution failure → log, report to user, continue without result
   - Governance constraint violation → adjust plan or request approval

2. **Non-recoverable errors** (fail gracefully):
   - Invalid session ID → return error to UI
   - Critical safety violation → transition to LOCKDOWN, abort task
   - Orchestrator bug (unexpected exception) → log, return generic error, emit alert

#### Error Handling Strategy

**Within step functions:**

```python
def step_llm_call(ctx: ExecutionContext) -> TaskState:
    try:
        response = llm_client.respond(...)
        # ... process response
        return next_state
    except LLMTimeout:
        logger.warning("llm_timeout", trace_id=ctx.trace_id)
        # Retry once with lower timeout
        try:
            response = llm_client.respond(..., timeout_s=30)
            return next_state
        except LLMTimeout:
            ctx.error = "Model timeout, please try again"
            return TaskState.FAILED
    except Exception as e:
        logger.error("unexpected_error", exc_info=True, trace_id=ctx.trace_id)
        ctx.error = "Internal error occurred"
        return TaskState.FAILED
```

**Global orchestrator error handler:**

```python
def execute_task_safe(ctx: ExecutionContext) -> OrchestratorResult:
    """Wrapper with top-level error handling."""
    try:
        ctx = execute_task(ctx)
        return OrchestratorResult(
            reply=ctx.final_reply or "Task completed",
            steps=ctx.steps,
            trace_id=ctx.trace_id,
        )
    except Exception as e:
        logger.critical("orchestrator_fatal_error", exc_info=True)
        # Emit alert to user (macOS notification?)
        return OrchestratorResult(
            reply="Critical error occurred. The agent is recovering.",
            steps=[],
            trace_id=ctx.trace_id,
            error=str(e),
        )
```

#### State Checkpointing (Future)

For long-running tasks, periodically serialize `ExecutionContext` to disk:

- On error or crash, can resume from last checkpoint
- Enables "pause and resume" workflows
- Out of scope for MVP

---

### 2.5 Parallelism & Concurrency (Future)

**MVP limitation**: One task per session at a time, sequential execution.

**Future enhancements:**

1. **Parallel tool calls**:
   - Execute multiple independent tools concurrently (e.g., check CPU + check disk in parallel)
   - Requires async orchestrator

2. **Parallel branches of thought** (ADR-0002):
   - Run multiple LLM agents concurrently (e.g., Planner + Critic)
   - Merge results at a synthesis node
   - Implement as a special state with sub-state-machines

3. **Background tasks**:
   - Orchestrator spawns background workflows (e.g., periodic system health checks)
   - Background tasks run in separate sessions
   - Brainstem monitors and throttles background load

---

### 2.6 Observability Integration

Every state transition and step execution emits structured telemetry:

```python
def step_llm_call(ctx: ExecutionContext) -> TaskState:
    span_id = str(uuid.uuid4())
    logger.info(
        "step_started",
        event="llm_call",
        trace_id=ctx.trace_id,
        span_id=span_id,
        model_role=determine_role(ctx),
    )

    start_time = time.time()
    response = llm_client.respond(...)
    duration_ms = (time.time() - start_time) * 1000

    logger.info(
        "step_completed",
        event="llm_call",
        trace_id=ctx.trace_id,
        span_id=span_id,
        duration_ms=duration_ms,
        tokens_used=response.usage["total_tokens"],
    )

    return next_state
```

This integrates seamlessly with ADR-0004 (Telemetry) and enables:

- Trace reconstruction
- Performance profiling
- Policy compliance auditing

---

## 3. Decision Drivers

### Why Explicit State Machine over Graph Library?

- **Simplicity**: No external dependencies, no framework overhead
- **Control**: Exact behavior is readable in code
- **Testability**: Step functions are plain Python, easy to unit test
- **Evolution**: Can add graph library later (e.g., LangGraph) if complexity justifies it

### Why Synchronous First?

- **Faster MVP**: Async adds complexity without clear benefit for single-user scenario
- **Easier debugging**: Synchronous stack traces, no async footguns
- **Latency not bottleneck**: LLM inference takes seconds; async won't speed that up

### Why In-Memory Session State?

- **Fast**: No I/O during execution
- **Simple**: No schema migrations, no DB setup
- **Sufficient**: Single user, short sessions, graceful shutdown persistence

---

## 4. Implementation Plan

### Week 1: Core Orchestrator Skeleton

1. **Define core types**:
   - `src/personal_agent/orchestrator/types.py`:
     - `TaskState`, `ExecutionContext`, `OrchestratorResult`, `OrchestratorStep`

2. **Implement session manager**:
   - `src/personal_agent/orchestrator/session.py`:
     - `Session`, `SessionManager` classes

3. **Implement main execution loop**:
   - `src/personal_agent/orchestrator/executor.py`:
     - `execute_task()`, `execute_task_safe()`, step functions

4. **Wire to governance**:
   - Query mode constraints before execution
   - Filter tools, apply rate limits

### Week 2: Step Function Implementations

1. **Implement basic flows**:
   - Simple Q&A (no tools): `INIT → LLM_CALL → SYNTHESIS → COMPLETED`
   - Tool-using flow: `INIT → LLM_CALL → TOOL_EXECUTION → LLM_CALL → SYNTHESIS → COMPLETED`

2. **Integrate LLM client**: Call `LocalLLMClient.respond()` with trace context

3. **Integrate tool layer**: Call tool execution with governance checks

### Week 3: Error Handling & Observability

1. **Add error handling**: Implement retry logic, fallbacks, graceful failures

2. **Emit telemetry**: Log all state transitions, step completions, errors

3. **Session persistence**: Serialize/deserialize sessions on shutdown/startup

### Week 4: Integration Testing

1. **End-to-end tests**: Full request → response flows with telemetry validation

2. **Failure scenarios**: Test timeouts, tool errors, governance blocks

---

## 5. Consequences

### Positive

✅ **Simple, explicit execution model**: Easy to understand and debug
✅ **Testable**: Step functions unit-testable, full flows integration-testable
✅ **Observable**: Every step logged with trace correlation
✅ **Governable**: Mode constraints enforced before actions
✅ **Extensible**: Can add complexity (async, parallelism, richer graphs) incrementally

### Negative / Trade-offs

⚠️ **Async complexity**: More cognitive overhead than sync code (mitigated by clear patterns)
⚠️ **Testing complexity**: Need `pytest-asyncio`, async fixtures (learning curve)
⚠️ **Session state lost on crash**: In-memory state requires graceful shutdown (acceptable risk)
⚠️ **Manual state machine**: More boilerplate than graph library (acceptable for clarity)

### Positive (from Async Decision)

✅ **Parallel tool execution**: Multiple tools run concurrently
✅ **Streaming responses**: Tokens arrive as generated
✅ **Better resource utilization**: No thread-per-request overhead
✅ **Future-proof**: No costly refactor needed later

---

## 6. Open Questions & Future Work

- **Graph library integration**: At what complexity threshold do we adopt LangGraph or similar?
- **State persistence strategy**: Should we add SQLite for session storage in Phase 2?
- **Resumable workflows**: How do we checkpoint long-running tasks for pause/resume?
- **Distributed execution**: Will we ever need multi-process orchestration? (Unlikely for local-only)
- **Async performance**: How much latency improvement do we achieve with parallel tool execution? (Measure in experiments)

---

## 7. References

- `../architecture/ORCHESTRATOR_CORE_SPEC_v0.1.md` — High-level orchestrator design
- `ADR-0002` — Orchestration style decision
- `ADR-0004` — Telemetry integration
- `ADR-0005` — Governance enforcement
- `../architecture/HOMEOSTASIS_MODEL.md` — Control loop integration

---

## 8. Acceptance Criteria

This ADR is accepted when:

1. ✅ Core orchestrator types defined (`TaskState`, `ExecutionContext`, etc.)
2. ✅ Session manager implemented with in-memory storage
3. ✅ Basic execution loop (`execute_task()`) works end-to-end for simple Q&A
4. ✅ At least one tool-using flow executes successfully with telemetry
5. ✅ Error handling prevents crashes, returns graceful errors to UI
6. ✅ Session persistence (save/load on shutdown/startup) functional

---

**Next specs to complete**: Tool Execution Spec, UI/CLI Spec, Captain's Log Manager Spec
