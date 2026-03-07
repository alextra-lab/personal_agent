# ADR-0029: Inference Concurrency Control (Air Traffic Controller)

**Status**: Proposed  
**Date**: 2026-03-07  
**Deciders**: Project owner  

---

## Context

The agent runs multiple concurrent processes that all require LLM inference:

| Consumer | Model Role | Trigger | Priority |
|----------|-----------|---------|----------|
| **Router** | `router` | Every user message | Critical (user-facing) |
| **Response generation** | `standard` / `reasoning` | Every user message | Critical (user-facing) |
| **Entity extraction** | `reasoning` | Second brain consolidation (idle trigger or background) | Background |
| **Captain's Log reflection** | `reasoning` | `run_in_background()` after task completion | Background |
| **InsightsEngine** | `reasoning` | Weekly scheduled job | Background |
| **ThresholdOptimizer** | `reasoning` | Brainstem scheduled job | Background |

All roles currently point to the same LM Studio server (`http://127.0.0.1:1234/v1`). The `max_concurrency` field in `config/models.yaml` is **advisory only** — there is no semaphore or queue enforcing it in `llm_client/client.py`. The Elasticsearch handler has a `Semaphore(10)`, but the LLM client does not.

### Observed failures

On 2026-03-07, the following errors were observed when multiple agent subsystems competed for the local inference server simultaneously:

1. **HTTP 400** on `qwen3.5-35b-a3b` — LM Studio rejects a second concurrent request for the same model
2. **90-second timeout** — request queued internally by LM Studio but never served in time
3. **Entity extraction fallback** — `consolidation_extraction_fallback_skip` triggered because extraction failed
4. **Silent data loss** — failed reflections and extractions produce no Captain's Log entries and no knowledge graph updates

### Root cause hypothesis

Local SLM inference servers (LM Studio, Ollama, vLLM single-GPU) are fundamentally **single-request-at-a-time** for large models. A 35B parameter model cannot serve parallel requests on consumer hardware. The server either rejects, queues, or crashes when concurrent requests arrive.

This is **not** a problem with cloud Foundation Model providers (OpenAI, Anthropic, Google) which have massive inference clusters and handle concurrency server-side. The agent architecture must therefore be **endpoint-aware**: local endpoints need strict concurrency control; remote cloud endpoints need little or none.

### Current architecture gap

The `models.yaml` config already defines per-model `max_concurrency` and per-model `endpoint`. Multiple models may share the same endpoint (LM Studio instance), or use different endpoints (separate inference servers). The missing piece is a **runtime enforcement layer** that:

1. Enforces `max_concurrency` per model via semaphore
2. Enforces a global concurrency limit per endpoint (since LM Studio is the bottleneck, not the model role)
3. Prioritizes user-facing requests over background tasks
4. Adapts behavior based on whether the endpoint is local or remote

---

## Decision

Implement an **Inference Concurrency Controller** (internally nicknamed "Air Traffic Controller") in the LLM client layer that manages request scheduling based on endpoint type and capacity.

### Design

#### 1. Endpoint classification

Add an optional `provider_type` field to model config in `models.yaml`:

```yaml
models:
  reasoning:
    id: "qwen3.5-35b-a3b"
    endpoint: "http://127.0.0.1:1234/v1"
    provider_type: local        # local | cloud | managed
    max_concurrency: 1          # Enforced, not advisory
    # ...

  standard:
    id: "qwen3.5-4b-mxfp8-mlx"
    endpoint: "http://127.0.0.1:1234/v1"
    provider_type: local
    max_concurrency: 2
    # ...

  # Future: cloud provider example
  reasoning_cloud:
    id: "claude-sonnet-4-20250514"
    endpoint: "https://api.anthropic.com/v1"
    provider_type: cloud        # No throttling needed
    max_concurrency: 10         # High concurrency OK
    # ...
```

Provider types and their default throttling policy:

| `provider_type` | Per-model semaphore | Per-endpoint semaphore | Priority queue | Notes |
|-----------------|--------------------|-----------------------|----------------|-------|
| `local` | Yes (`max_concurrency`) | Yes (configurable, default 2) | Yes | Single-GPU inference servers |
| `managed` | Yes (`max_concurrency`) | Optional | Optional | Self-hosted multi-GPU, vLLM clusters |
| `cloud` | Optional (rate-limit protection) | No | No | OpenAI, Anthropic, Google — they manage concurrency |

If `provider_type` is omitted, default to `local` for `localhost`/`127.0.0.1` endpoints, and `cloud` for all others.

#### 2. Request priority

Define priority tiers to ensure user-facing requests are never starved by background work:

```python
class InferencePriority(IntEnum):
    CRITICAL = 0    # Router classification (must be fast)
    USER_FACING = 1 # Response generation (user is waiting)
    ELEVATED = 2    # Tool calls during active request
    BACKGROUND = 3  # Captain's Log reflection, entity extraction
    DEFERRED = 4    # Scheduled insights, optimizer, weekly jobs
```

The controller uses a **priority queue** (not FIFO) so that when a semaphore slot opens, the highest-priority waiting request proceeds first.

#### 3. Concurrency controller

```python
class InferenceConcurrencyController:
    """Manages concurrent access to inference endpoints.

    Enforces per-model and per-endpoint concurrency limits with
    priority-based scheduling. Local endpoints get strict control;
    cloud endpoints pass through with minimal overhead.
    """

    def __init__(self, model_configs: dict[str, ModelConfig]) -> None:
        # Per-model semaphores (from max_concurrency)
        self._model_semaphores: dict[str, asyncio.Semaphore]

        # Per-endpoint semaphores (shared across models on same server)
        self._endpoint_semaphores: dict[str, asyncio.Semaphore]

        # Priority queues per endpoint
        self._endpoint_queues: dict[str, asyncio.PriorityQueue]

    async def acquire(
        self,
        model_role: str,
        priority: InferencePriority = InferencePriority.USER_FACING,
        timeout: float | None = None,
    ) -> None:
        """Acquire an inference slot, respecting priority and concurrency limits."""

    def release(self, model_role: str) -> None:
        """Release the inference slot."""

    @asynccontextmanager
    async def request_slot(
        self,
        model_role: str,
        priority: InferencePriority = InferencePriority.USER_FACING,
    ) -> AsyncIterator[None]:
        """Context manager for inference slot acquisition."""
```

#### 4. Integration into LLM client

The controller wraps every `respond()` call in `llm_client/client.py`:

```python
async def respond(self, messages, role, priority=InferencePriority.USER_FACING, **kwargs):
    async with self._concurrency_controller.request_slot(role, priority):
        # Existing request logic
        return await self._do_request(messages, role, **kwargs)
```

#### 5. Caller annotations

Each subsystem passes its priority when calling the LLM:

| Caller | Priority | Rationale |
|--------|----------|-----------|
| `orchestrator/routing.py` | `CRITICAL` | User is waiting, sub-second target |
| `orchestrator/executor.py` (response) | `USER_FACING` | User is waiting |
| Tool execution (mid-task LLM calls) | `ELEVATED` | Part of active user request |
| `captains_log/reflection.py` | `BACKGROUND` | Non-blocking, can wait |
| `second_brain/entity_extraction.py` | `BACKGROUND` | Non-blocking, can retry |
| `insights/engine.py` | `DEFERRED` | Scheduled, no urgency |
| `brainstem/optimizer.py` | `DEFERRED` | Scheduled, no urgency |

#### 6. Backpressure for background tasks

When background tasks can't acquire a slot within a configurable timeout (default 30s for `BACKGROUND`, 60s for `DEFERRED`), they **skip gracefully** rather than queue indefinitely:

- Captain's Log reflection: falls back to basic (no-LLM) reflection
- Entity extraction: logs `extraction_deferred_busy` and retries next consolidation cycle
- Insights/Optimizer: skip this run, try next scheduled window

This prevents background work from accumulating unbounded queue pressure.

---

## Alternatives Considered

### A. Global singleton semaphore (simple)

A single `asyncio.Semaphore(1)` for all local LLM calls.

*Pros*: Trivial to implement, solves the immediate 400 errors.  
*Cons*: No priority — a deferred insights query could block a user-facing response. No per-endpoint awareness. Doesn't scale to mixed local/cloud.

### B. External queue (Redis, RabbitMQ)

Offload request scheduling to an external message broker.

*Pros*: Robust, battle-tested, supports distributed setups.  
*Cons*: Massive overkill for a single-machine agent. Adds infrastructure dependency. Latency overhead for what should be an in-process operation.

### C. Model-level semaphore only (no endpoint awareness)

One semaphore per model role, matching `max_concurrency`.

*Pros*: Simple, uses existing config.  
*Cons*: Doesn't account for multiple models sharing the same endpoint. Two models with `max_concurrency: 2` each could send 4 concurrent requests to the same LM Studio instance.

**Option C is the closest alternative** but fails the "shared endpoint" case. The chosen design adds endpoint-level coordination with minimal additional complexity.

---

## Consequences

**Positive:**
- Eliminates HTTP 400 and timeout errors from local inference contention
- User-facing requests are never starved by background work
- Cloud endpoints pass through with minimal overhead (no unnecessary throttling)
- Mixed local/cloud deployments work correctly (e.g., router local, reasoning cloud)
- `max_concurrency` becomes a real guarantee, not an aspiration
- Background tasks degrade gracefully instead of failing with cryptic errors

**Negative:**
- Adds a new layer of complexity to the LLM client
- Priority inversion edge cases need careful testing (e.g., many BACKGROUND tasks arriving just before a CRITICAL one)
- `provider_type` config adds a field that users must set correctly for non-default endpoints
- Queue depth monitoring needed to detect pathological backpressure

---

## Acceptance Criteria

- [ ] `InferenceConcurrencyController` enforces per-model and per-endpoint semaphores
- [ ] Priority queue ensures `CRITICAL` > `USER_FACING` > `ELEVATED` > `BACKGROUND` > `DEFERRED`
- [ ] Local endpoints (`provider_type: local` or localhost auto-detect) use strict concurrency control
- [ ] Cloud endpoints (`provider_type: cloud`) pass through with no blocking
- [ ] Background tasks (reflection, extraction) skip gracefully on timeout instead of failing with 400
- [ ] Unit tests for priority ordering, semaphore enforcement, and timeout backpressure
- [ ] Integration test: concurrent user request + background reflection on local endpoint — user request completes first
- [ ] `models.yaml` updated with `provider_type` field (backward compatible: defaults to auto-detect)
- [ ] No regression in single-request latency (controller overhead < 5ms)

---

## Implementation Timing

**When**: Early Phase 2.3 — this should be implemented **before** the Insights Engine and ThresholdOptimizer scheduled jobs go live, as those add more background LLM consumers. It is also a prerequisite for reliable Captain's Log reflections and entity extraction, which are already producing silent failures.

**Dependency**: None — this is a standalone improvement to `llm_client` that doesn't depend on other Phase 2.3 work.

**Priority**: High — the bug is active and causing data loss now.

---

## Links and References

- `config/models.yaml` — model configuration with `max_concurrency` and `endpoint`
- `src/personal_agent/llm_client/client.py` — LLM client (no current concurrency enforcement)
- `src/personal_agent/captains_log/background.py` — background task runner (fire-and-forget)
- `src/personal_agent/telemetry/es_handler.py` — example of semaphore pattern (`Semaphore(10)`)
- `docs/architecture/HOMEOSTASIS_MODEL.md` — homeostasis principle: every variable needs a control loop
- ADR-0023: Qwen3.5 model integration (defines current model config structure)
