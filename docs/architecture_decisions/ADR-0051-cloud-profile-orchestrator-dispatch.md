# ADR-0051: Cloud Profile Orchestrator Dispatch via ContextVar

**Status**: Accepted  
**Date**: 2026-04-15  
**Deciders**: Project owner  
**Related**: ADR-0044 (Execution Profiles), ADR-0046 (AG-UI Transport), ADR-0033 (LLM Client Two-Path)

---

## Context

The Seshat PWA supports two execution profiles selectable at conversation start:

- **local** — Qwen3.5-35B on the home GPU via `LocalLLMClient`
- **cloud** — Claude Sonnet 4.6 via `LiteLLMClient` + Anthropic API

ADR-0044 defined the profile schema and `ExecutionProfile` model. ADR-0033 established the two-client LLM factory. What was missing was the **wiring**: how does the user's profile selection in the PWA reach the LLM factory deep inside the orchestrator call chain?

Two additional requirements constrained the design:

1. The PWA sends `POST /chat` as fire-and-forget (returns immediately; response arrives via SSE). The CLI uses `POST /chat` synchronously (returns `{"response": "..."}` directly). Both must coexist.
2. The orchestrator call chain is 6–8 frames deep; threading a `profile` parameter through every function signature would be invasive.

---

## Decision

### Profile propagation: `contextvars.ContextVar`

A single module-level `ContextVar` carries the active `ExecutionProfile` through the async orchestrator chain without modifying any function signature:

```python
# config/profile.py
_current_profile: ContextVar[ExecutionProfile | None] = ContextVar(
    "current_profile", default=None
)

def set_current_profile(profile: ExecutionProfile) -> Token: ...
def get_current_profile() -> ExecutionProfile | None: ...
```

`asyncio` tasks inherit context from their parent at creation time. Setting the profile at the start of the background processing coroutine makes it available to all downstream awaits, including the LLM factory.

### LLM factory: profile-aware model key resolution

`get_llm_client(role_name)` reads the active profile and resolves the model key before looking up `models.yaml`:

```
get_llm_client("primary")
  → profile active? → yes, cloud profile
  → profile.primary_model = "claude_sonnet"
  → models["claude_sonnet"].provider_type = "cloud"
  → return LiteLLMClient(model_id="claude-sonnet-4-6", provider="anthropic")
```

Without an active profile the factory behaves exactly as before (backward compatible).

### New endpoint: `POST /chat/stream`

Rather than modifying the existing synchronous `POST /chat` (used by the CLI with query params), a parallel endpoint accepts form-encoded data and is fire-and-forget:

```
POST /chat/stream
  Content-Type: application/x-www-form-urlencoded
  body: message=...&session_id=...&profile=cloud

→ 200 {"session_id": "...", "status": "streaming"}
  (background task running)

GET /stream/{session_id}
← text/event-stream
  data: {"type": "TEXT_DELTA", "data": {"text": "..."}, ...}
  data: {"type": "DONE"}
```

The background coroutine `_process_chat_stream_background`:
1. Loads and sets the `ExecutionProfile` ContextVar
2. Gets or creates the DB session with the client-provided UUID (so multi-turn history works without a prior `POST /sessions`)
3. Runs the Pre-LLM Gateway pipeline
4. Runs the orchestrator
5. Pushes `TextDeltaEvent` + `None` sentinel to the SSE queue
6. Persists the assistant message to PostgreSQL

### Infrastructure: full service app in gateway container

`Dockerfile.gateway` now runs `personal_agent.service.app:app` instead of `personal_agent.gateway.app:create_gateway_app`. This is required because:
- The thin gateway app has no orchestrator, memory service, or brainstem
- Wiring those into the gateway app would duplicate the service app's lifespan logic
- Running the service app directly gives access to all initialized subsystems (Neo4j, Elasticsearch, BrainstemScheduler, MCPGatewayAdapter)

The service app exposes all endpoints the gateway previously provided (via `gateway_mount_local` when that flag is set, or by including the gateway router directly).

### API key pass-through in LiteLLMClient

LiteLLM reads `ANTHROPIC_API_KEY` from environment. The service uses `AGENT_ANTHROPIC_API_KEY` (Pydantic settings with `env_prefix="AGENT_"`). LiteLLM never finds the key without explicit pass-through:

```python
# litellm_client.py
if self.provider == "anthropic":
    api_key = _settings.anthropic_api_key or None
if api_key:
    litellm_kwargs["api_key"] = api_key
```

---

## Alternatives Considered

### Alternative 1: Thread `profile` through every function signature

Pass `profile: ExecutionProfile | None` to `Orchestrator.handle_user_request()`, which passes it to `Executor`, which passes it to `get_llm_client()`, etc.

**Rejected**: 6+ function signatures to modify; breaks the executor–client interface protocol defined in ADR-0033 (`LLMClient.respond()` already has a fixed signature). ContextVar achieves the same result with zero interface changes.

### Alternative 2: Set profile in the endpoint handler before `asyncio.create_task()`

Call `set_current_profile()` in the FastAPI handler before launching the background task, relying on task context inheritance.

**Rejected (subtle)**: Works correctly, but feels fragile — the profile is set in the endpoint context, which could affect concurrent requests sharing an event loop if set/get semantics weren't perfectly understood. Setting it at the start of the background coroutine is self-contained and easier to reason about.

### Alternative 3: Modify `/chat` to accept both query params and form data

Detect content type in the handler and dispatch accordingly.

**Rejected**: FastAPI parameter binding doesn't gracefully support both `Query()` and `Form()` for the same field on the same endpoint. Would require raw `Request` parsing and manual type coercion. Adding `/chat/stream` is cleaner and preserves the CLI contract.

### Alternative 4: Run gateway app alongside service app (two processes)

Keep the thin gateway app for knowledge/observation APIs, and add a second service app process for orchestration.

**Rejected**: Two processes on a 24GB RAM VPS doubles the initialization footprint (two Neo4j connections, two Elasticsearch connections, two event bus consumers). Single process is simpler to operate.

---

## Consequences

### Positive

- **Zero orchestrator changes**: Profile dispatch is transparent to `Orchestrator`, `Executor`, and all tool implementations.
- **CLI unchanged**: `POST /chat` with query params continues to work exactly as before.
- **Multi-turn cloud conversations**: Background task creates DB sessions with client UUIDs, so history is addressable across turns.
- **Cost tracking preserved**: LiteLLM calls go through `LiteLLMClient.respond()` which already has budget enforcement and cost recording.
- **Extensible**: Adding a third profile (e.g., "deep-research" with Gemini 2.5 Pro) requires only a new YAML file in `config/profiles/` and a new `models.yaml` entry.

### Negative / Risks

- **ContextVar is implicit**: A developer unfamiliar with the pattern might not understand why `get_llm_client()` returns different clients without an explicit argument. Mitigated by clear docstrings in `factory.py` and `profile.py`.
- **Response is not streaming token-by-token**: The full orchestrator response arrives as one `TextDeltaEvent`. The UI shows the response appearing all at once rather than progressively. This is a known limitation to address when LiteLLM streaming is wired.
- **Full service app startup time**: The gateway container now takes ~60s to become healthy (vs ~15s for the thin gateway). Mitigated by `start_period: 60s` in the docker-compose healthcheck.
- **Memory**: `mem_limit` increased from 512MB to 768MB to accommodate the full service app's initialization.

---

## Implementation Notes

- `set_current_profile()` returns a `contextvars.Token` for test teardown: `_current_profile.reset(token)`.
- Profile `load_profile(name)` searches `config/profiles/{name}.yaml` relative to the working directory. In Docker this is `/app/config/profiles/`. The `config/` directory is `COPY`-ed into the image by `Dockerfile.gateway`.
- The `_process_chat_stream_background` coroutine catches all exceptions and always pushes a `None` sentinel so the SSE client stream closes cleanly even on error.
- Caddy's `path` directive does exact matching for paths without wildcards. `/chat/stream` must be listed explicitly alongside `/chat`.
