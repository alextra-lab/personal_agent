# Local LLM Client – MVP Specification (v0.1)

## 1. Purpose & Responsibilities

The **Local LLM Client** is the only component allowed to talk directly to local model runners (LM Studio, Ollama, custom MLX servers, etc.). Its job is to:

- Expose a **clean, role-based interface** to the rest of the system (`router`, `reasoning`, `coding`).
- Normalize differences between runners and endpoints:
  - Prefer a **Responses-style** interface (tools, traces, state).
  - When only `/v1/chat/completions` exists, emulate Responses semantics via an adapter.
- Handle **timeouts, retries, backoff, and basic circuit breaking**.
- Emit **structured telemetry** for every call (for Brainstem, observability, and evaluation).
- Enforce **governance hooks** (modes, budgets, simple safety checks) at call time.

The orchestrator, tools, and UI never talk to LM Studio (or any runner) directly; they only talk to the Local LLM Client.

---

## 2. External Interface (to the Rest of the Agent)

### 2.1 Model roles

Roles are defined at the architecture level as:

```python
class ModelRole(str, Enum):
    ROUTER = "router"
    REASONING = "reasoning"
    CODING = "coding"
```

These map to configured models in `config/models.yaml`.

### 2.2 Core methods

MVP methods (Python-style, conceptual):

```python
class LocalLLMClient(Protocol):
    def respond(
        self,
        role: ModelRole,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_s: float | None = None,
        trace_ctx: "TraceContext | None" = None,
    ) -> "LLMResponse":
        """Single-turn 'responses-style' call for a given model role."""
        ...

    def stream_respond(
        self,
        role: ModelRole,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_s: float | None = None,
        trace_ctx: "TraceContext | None" = None,
    ) -> "Iterable[LLMStreamEvent]":
        """Streaming variant, yielding events (tokens, tool calls, traces)."""
        ...
```

### 2.2.1 When to Use Streaming vs Non-Streaming

**Design Decision** (ADR-0009): Use streaming responses for **user-facing output** where progressive display improves UX, but use non-streaming (complete responses) for **internal agent communication** where full responses are necessary for validation, parsing, and instruction processing.

**See ADR-0009** (`../architecture_decisions/ADR-0009-streaming-vs-non-streaming-responses.md`) for complete rationale, consequences, and implementation plan.

**Use `stream_respond()` when:**

- The output is directly displayed to the end user (e.g., final chat response in CLI/UI)
- Progressive token display improves perceived responsiveness
- The complete response is not needed before display begins

**Use `respond()` when:**

- Internal agent communication (routing decisions, tool calls, planning)
- Response must be parsed/validated before proceeding (e.g., JSON routing decisions)
- Response is used as input to subsequent steps (e.g., tool arguments, instructions)
- Complete response is required for error handling or retry logic

**Examples:**

- **Streaming**: Final user-facing response in `Channel.CHAT` → use `stream_respond()` and display tokens as they arrive
- **Non-streaming**: Router making routing decision → use `respond()` to get complete JSON for parsing
- **Non-streaming**: Tool call generation → use `respond()` to extract complete tool calls before execution
- **Non-streaming**: Internal reasoning/planning → use `respond()` to get complete plan before validation

Messages follow OpenAI-style chat semantics:

```python
{"role": "system" | "user" | "assistant" | "tool", "content": "..."}
```

### 2.3 Response types

High-level response type (language-agnostic):

```python
class ToolCall(TypedDict):
    id: str
    name: str
    arguments: str  # JSON string


class LLMResponse(TypedDict):
    role: str              # "assistant"
    content: str           # final natural language content
    tool_calls: list[ToolCall]
    reasoning_trace: str | None  # e.g. <think>...</think> or model-specific trace
    usage: dict            # token counts, wall time, etc.
    raw: dict              # raw runner response for debugging
```

Streaming events:

```python
class LLMStreamEvent(TypedDict):
    type: str  # "token" | "tool_call" | "trace" | "done" | "error"
    data: Any
```

The orchestrator only needs to understand:

- `content` for normal answers,
- `tool_calls` to dispatch tools,
- `reasoning_trace` for logging / optional display,
- `usage` for metrics and budgets.

---

## 3. Responses vs Chat Completions

### 3.1 Design principle

- The client API is **Responses-shaped**, regardless of which backend endpoint we hit.
- The client MUST normalize the following concepts:
  - tools & tool calls,
  - reasoning traces / "thinking" content,
  - usage and token accounting,
  - multi-turn message history.

### 3.2 Backend modes

Two primary backend modes are anticipated:

1. **Responses-native runner** (e.g. LM Studio `/v1/responses`)
   - Direct mapping:
     - client → `/v1/responses`.
     - `tools` → `tools` field.
     - `reasoning_trace` → model-specific extension (e.g. metadata or special content type).
   - Minimal adaptation required.

2. **Chat-completions-only runner** (`/v1/chat/completions`)
   - Adaptation layer:
     - `tools` → tools/functions field supported by the runner.
     - no explicit reasoning field → `reasoning_trace = None`, or extracted from tagged segments if configured.
     - mimic Responses layout in `LLMResponse` (content + optional tool calls + usage), so the orchestrator does not care that the backend is older.

**API Preference (from ADR-0003):**

> The Local LLM Client SHOULD prefer a *Responses-style* interface that supports tool calls, explicit reasoning traces, and richer conversation state. When the local runner only exposes a traditional `/v1/chat/completions` or similar endpoint, the client MUST provide an adapter layer that emulates Responses semantics so that higher layers of the agent do not depend on a specific runner API shape.

---

## 4. Configuration & Model Routing

### 4.1 Config schema (conceptual)

The client reads configuration from a file such as `config/models.yaml`:

```yaml
models:
  router:
    id: "qwen/qwen3-4b-2507"
    endpoint: "http://localhost:8001/v1/responses"
    context_length: 8192
    quantization: "8bit"
    max_concurrency: 4
    api_type: "responses"       # "responses" | "chat_completions"
  reasoning:
    id: "deepseek-r1-distill-qwen-14b"
    endpoint: "http://localhost:8002/v1/responses"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 2
    api_type: "responses"
  coding:
    id: "mistralai/devstral-small-2-2512"
    endpoint: "http://localhost:8003/v1/responses"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 2
    api_type: "responses"
```

### 4.2 Resolution rules

- Lookup by `ModelRole` → config entry.
- The client enforces per-role defaults (e.g. timeouts, max tokens) but allows per-call overrides.
- Future: mode-aware overrides (e.g. in Conservative mode, use a smaller reasoning model or cap max tokens).

---

## 5. Error Handling & Timeouts

### 5.1 Error classes

The client exposes a small error hierarchy to the orchestrator:

```python
class LLMClientError(Exception):
    ...


class LLMTimeout(LLMClientError):
    ...


class LLMConnectionError(LLMClientError):
    ...


class LLMRateLimit(LLMClientError):
    ...


class LLMServerError(LLMClientError):
    ...


class LLMInvalidResponse(LLMClientError):
    ...
```

The orchestrator maps these to:

- retries (idempotent cases, within limits),
- user-visible errors,
- or escalation to the Brainstem (for circuit-breaking, mode changes, or downgrading model roles).

### 5.2 Timeouts

Per-call timeouts:

- default per-role, e.g. (tunable in config):
  - `router`: 5s,
  - `reasoning`: 60s,
  - `coding`: 45s.
- overridable per call via `timeout_s`.
- all timeouts must be enforced on the client side, regardless of backend defaults.

---

## 6. Telemetry & Tracing

Every model call must emit a **span** (OpenTelemetry-style), with at least:

- `role`: `router` | `reasoning` | `coding`.
- `model_id`: configured model ID.
- `endpoint`: URL.
- `latency_ms`.
- `usage.prompt_tokens` and `usage.completion_tokens` (if available).
- `error_type` (if any).
- `trace_id` / `span_id` (for end-to-end correlation).

The `trace_ctx` parameter allows upstream components (orchestrator / Brainstem) to pass a parent trace context into the client, so that telemetry can stitch LLM calls together with tool calls and decisions.

---

## 7. Governance Hooks (MVP)

The Local LLM Client enforces **per-call governance hooks** before and after contacting the runner:

- **Mode-aware limits**:
  - In Conservative mode:
    - disallow certain tools in `tools`,
    - cap `max_tokens` and temperature,
    - potentially forbid the heaviest reasoning model if not strictly necessary.
- **Budget-aware limits** (MVP, simple):
  - log when cumulative usage for a session exceeds soft thresholds,
  - hard-stop further calls when explicit limits are configured and exceeded.

The Brainstem defines the policy; the Local LLM Client enforces it.

---

## 8. Implementation Notes (Python, MVP)

- Implementation language: **Python 3.12**.
- HTTP client: simple, explicit library (e.g. `httpx`) with:
  - timeouts,
  - retry policies,
  - structured logging.
- Type system: use `TypedDict` / `Protocol` or Pydantic models for internal structures.
- Runner abstraction layer:
  - one implementation for LM Studio,
  - others (Ollama, custom MLX) can plug in behind the same interface.

The first implementation can be synchronous; later versions can add async support if needed.

---

## 9. Out of Scope (for v0.1)

- Dynamic model auto-selection within a role based on task difficulty or context size.
- Cross-runner failover (e.g. "if LM Studio is down, try another backend").
- Cloud-hosted models as fallback; these require a separate ADR and explicit governance.

This spec is intentionally narrow: it defines a stable spine between the higher-level agent orchestration and the local model runners, while keeping room for future evolution.
