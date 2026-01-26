# ADR-0003: Local Model Stack for Personal Agent MVP

- Status: Proposed
- Date: 2025-12-28
- Owners: Project Owner

## 1. Context

The personal agent runs **fully locally** on a MacBook Pro (Apple Silicon M4 Max, 128 GB RAM). It must:

- Assist with **coding**, including multi-file reasoning and tool usage.
- Perform **deep reasoning and research**, including self-questioning and reflection.
- Act as a **router/utility brain** for light tasks, classification, and fast decisions.
- Operate **offline** where possible, with no hard dependency on cloud LLMs.
- Fit into the existing architecture:
  - A **Local LLM Client** component abstracts model calls.
  - The rest of the system addresses models by **roles/capabilities**, not specific IDs.

Recent research and model ecosystem surveys indicate that:

- Modern small/medium open-weight models from **Mistral** (Ministral, Mistral Small, Magistral, Devstral) and **Qwen** (Qwen3, Qwen3-Coder) run comfortably on this hardware using MLX / GGUF via LM Studio or similar local runners.
- Newer **Qwen3-Next** MoE models (e.g., Qwen3-Next-80B-A3B) offer strong reasoning performance and can be used in 8-bit quantized form on the M4 Max, at the cost of higher latency than 8B–24B dense models.
- A **three-role model stack** (router/utility, reasoning/generalist, coding) is a good fit for an agentic system that needs both speed and depth.
- Mixed stacks (Qwen + Mistral) often outperform single-vendor stacks in practice for local setups, provided the integration layer is clean and model selection is configuration-driven.

This ADR defines the **initial model strategy and roles** for the MVP. It does **not** lock in a single vendor forever; it specifies a baseline and how those choices are represented.

## 2. Decision

### 2.1 Logical roles, not hard-wired models

We define **three logical model roles** in the system:

1. **Router / Utility Model**
   - Fast, small model for:
     - intent classification,
     - simple Q&A,
     - **routing decisions** (which model to delegate to),
     - cheap "sanity checks" and meta-questions.
   - **Key capability:** Acts as intelligent dispatcher, either:
     - Handling simple queries directly (fast path), OR
     - Delegating complex queries to specialized models (intelligent routing)

2. **Reasoning / Generalist Model**
   - Medium model for:
     - multi-step reasoning and planning,
     - research tasks and world-model building,
     - high-quality inner-dialogue / self-questioning.

3. **Coding / SWE Model**
   - Medium model for:
     - code understanding and generation,
     - refactors, multi-file reasoning,
     - agentic code-editing workflows.

The **Local LLM Client** addresses models by these roles:

```text
models.router
models.reasoning
models.coding
```

Concrete model names, quantization levels, and serving backends are specified in configuration (e.g. `config/models.yaml`), not hard-coded.

### 2.2 Initial concrete model choices (MVP baseline)

Given the current ecosystem and hardware, the MVP will start with a **mixed Qwen + Mistral stack**:

- **Router / Utility (models.router)**
  - Primary: **Qwen3-1.7B-Instruct** *or* **Ministral 3 3B Instruct**.
  - Rationale:
    - Both are very fast, small models suitable for always-on, low-latency tasks.
    - They are strong at light reasoning, routing, and classification-style tasks.
    - Choice between them is treated as a configuration toggle.

- **Reasoning / Generalist (models.reasoning)**
  - Primary: **Qwen3-Next-80B-A3B-Thinking** (8-bit quantized, MoE reasoning-tuned variant).
  - Alternates:
    - **Magistral Small** (~24B reasoning-tuned Mistral model),
    - **Mistral Small 3.x** (generalist "small" frontier model),
    - **Qwen3-8B-Instruct** as a lighter Qwen3 dense fallback.
  - Rationale:
    - Qwen3-Next-80B-A3B-Thinking provides stronger multi-step reasoning and generalist performance than smaller dense models, while remaining locally runnable in 8-bit form on the M4 Max.
    - Magistral Small and Mistral Small 3.x remain strong medium-sized local options when lower latency or simpler deployment is required.
    - Qwen3-8B-Instruct offers a vendor-consistent fallback within the Qwen3 family if MoE deployment becomes inconvenient.

- **Coding / SWE (models.coding)**
  - Primary options (choose one based on task/availability):
    - **Qwen3-Coder-30B** (8-bit quantized, LM Studio)
    - **Devstral-Small-2-2512** (Mistral's agentic SWE model, strong instruction-following)
  - Fallback:
    - **Qwen3-1.7B** only for fallback lightweight coding tasks (not preferred for serious work).
  - Rationale:
    - Qwen3-Coder-30B: Most capable Qwen coding model locally available (~32 GB footprint, 8-bit). Strong multi-file reasoning and SWE-agent alignment.
    - Devstral-Small-2-2512: Mistral's latest agentic coding model with extended context (2512 tokens). Excellent for tool-augmented coding workflows.
    - Choice between them is configuration-driven; both validated locally.

### 2.3 Serving and integration

- Models are served by an **external local runner** (e.g. LM Studio, Ollama, or a custom MLX-based server).
- The personal agent interacts with models via a **stable local API**, abstracted in the Local LLM Client:
  - HTTP(S) or IPC calls with clearly defined request/response schemas.
  - The agent never depends on the runner's UI; it only talks to the API.
- Quantization:
  - MVP targets **4–8 bit quantizations** for all models, balancing quality and performance.

  - Exact quantization level is a configuration choice per model, not hard-coded.

**API Preference:** The Local LLM Client SHOULD prefer a *Responses-style* interface that supports tool calls, explicit reasoning traces, and richer conversation state. When the local runner only exposes a traditional `/v1/chat/completions` or similar endpoint, the client MUST provide an adapter layer that emulates Responses semantics so that higher layers of the agent do not depend on a specific runner API shape.

### 2.4 Configurability

- All model identifiers, endpoints, and quantization details live in configuration files, e.g.:

```yaml
models:
  router:
    id: "Qwen3-1.7B-Instruct"
    endpoint: "http://localhost:8001/v1/chat/completions"
  reasoning:
    id: "Magistral-Small-2506"
    endpoint: "http://localhost:8002/v1/chat/completions"
  coding:
    id: "Qwen3-Coder-30B"
    endpoint: "http://localhost:8003/v1/chat/completions"
```

- The agent will be built to tolerate **model swaps** as long as the new models:
  - are instruction-following,
  - support the required context length,
  - meet minimum quality for their role.

## 3. Decision Drivers

- **Offline-first**: no hard dependency on external APIs or cloud LLMs.
- **Hardware utilization**: M4 Max 128 GB is powerful enough to run multiple small/medium models concurrently at useful speeds.
- **Role separation**: small router model for speed, medium reasoning model for depth, medium coding model for SWE tasks.
- **Flexibility for experimentation**: enable swapping models without rewriting core logic.
- **Security & governance**: local-only by design, easier to reason about data flows.
- **Pedagogical value**: Owner wants to study and compare model behaviors, and understand how different model roles interact within an agentic system.
- **Local feasibility**: Mistral Large 3 is considered too large for this setup; Qwen3-Next-80B in 8-bit quantized form is the upper bound for reasoning capacity that remains locally practical.
- **Currency of stack**: Older Qwen2.5-Coder references are deprecated; Qwen3‑Coder‑30B reflects the currently deployed, actively maintained coding stack in our environment and is already locally validated in LM Studio.

## 4. Considered Alternatives

### 4.1 Single-model strategy

Use a single medium/large model for all roles (router, reasoning, coding).

- Pros:
  - Simpler configuration and integration.
  - One model to tune and monitor.
- Cons:
  - Overkill for routing and light tasks; wastes compute and increases latency.
  - Harder to specialize behaviors (coding vs reasoning vs utility).
  - Less pedagogical insight into multi-model orchestration.

Given the hardware headroom and the experimental nature of the project, the **multi-role stack** is preferred.

### 4.2 All-Mistral stack

- Router: Ministral 3 3B.
- Reasoning: Magistral Small or Mistral Small 3.x.
- Coding: Devstral Small.

Pros:

- Single-vendor stack, tighter conceptual cohesion.
- Strong support for agentic SWE workflows (Devstral).

Cons:

- Misses the strong Qwen3-Coder-30B line, which is already deployed and evaluated locally in LM Studio.
- Reduces diversity of behaviors for research purposes.

### 4.3 All-Qwen stack

- Router: Qwen3-1.7B-Instruct.
- Reasoning: Qwen3-8B-Instruct.
- Coding: Qwen3-Coder-30B.

Pros:

- Strong models across roles.
- Single-vendor ecosystem simplifies updates.

Cons:

- Misses Mistral's reasoning-focused Magistral and Devstral SWE agent tuning.
- Less diversity for experimentation.

The mixed stack is chosen to maximize **coverage, diversity, and learning value** while staying fully local.

### 4.4 Cloud-hosted models

- Use frontier cloud models (OpenAI, Anthropic, etc.) for one or more roles.

Rejected for MVP due to:

- Desire for **fully local, offline-capable** agent.
- Simpler threat model and governance for a personal project.
- Clearer focus on local orchestration and observability.

## 5. Consequences

### 5.1 Positive

- The agent can:
  - route light tasks quickly via the small router model,
  - perform deep reasoning with a reasoning-optimized model,
  - offer strong coding assistance through a dedicated coding model.
- The Local LLM Client becomes a natural place to:
  - implement **tool selection and routing**,
  - log per-role performance metrics,
  - experiment with different stacks over time.
- The configuration-driven approach makes it easy to:
  - benchmark alternative models for each role,
  - adjust to future local model improvements without redesign.

### 5.2 Negative / Trade-offs

- Higher **complexity** than a single-model setup:
  - more endpoints to configure,
  - more processes to monitor.
- Higher **resource usage**:
  - multiple models may be resident at once.
  - requires careful consideration of VRAM and concurrency.
- Evaluation becomes more complex:
  - need to attribute performance differences to models vs orchestration.

## 6. Implementation Notes & Follow-ups

- The Local LLM Client should normalize runner differences: prefer `/v1/responses` when available; when only `/v1/chat/completions` exists, wrap it to expose Responses-like behavior (tool calls, traces, state).
- Define a **model configuration schema** (e.g. `config/models.yaml`) with fields for:
  - `id`, `endpoint`, `context_length`, `role`, `quantization`, `max_concurrency`.
- Integrate model selection into the **Local LLM Client** with clear interfaces:

```python
class ModelRole(str, Enum):
    ROUTER = "router"
    REASONING = "reasoning"
    CODING = "coding"


class LocalLLMClient:
    def chat(self, role: ModelRole, messages: list[dict]) -> dict:
        """Send a chat-style request to the model for the given role."""
        ...
```

- **Implement intelligent routing logic** (Day 11.5):
  - Router model makes **explicit routing decisions** via structured prompts
  - Routing decision format:
    ```json
    {
      "routing_decision": "HANDLE|DELEGATE",
      "target_model": "REASONING|CODING" (if DELEGATE),
      "confidence": 0.0-1.0,
      "reason": "explanation"
    }
    ```
  - Orchestrator implements **multi-model coordination**:
    ```
    INIT → LLM_CALL(router) → LLM_CALL(reasoning) → SYNTHESIS
             ↓                      ↓
        routing decision       generate response
    ```
  - Full telemetry of routing decisions with `trace_id` correlation
  - See: `../plans/router_routing_logic_implementation_plan.md`
  - Research: `../research/router_prompt_patterns_best_practices_2025-12-31.md`

- Connect this ADR with:
  - ADR-0002 (orchestrator style),
  - ADR-0004 (telemetry and metrics),
  - ADR-0005 (governance config and modes),
  - ADR-0008 (model stack course correction, routing patterns).
- Define initial LM Studio (or equivalent) profiles matching the chosen models and expose them via local HTTP APIs.

## 7. Open Questions

- Do we need **long-context** (≥128K) models for specific workflows (large document analysis, huge codebases), or is a shorter context acceptable for MVP?
- How aggressively should we quantize models (4-bit vs 8-bit) before quality becomes noticeably problematic for reasoning and coding?
- Should the agent support **dynamic model selection** based on runtime signals (e.g. Brainstem mode, task complexity), or is static per-role mapping sufficient for MVP?
- When and how should we re-evaluate this stack in light of new local models (e.g. newer Qwen3 variants, future Magistral/Devstral releases)?
