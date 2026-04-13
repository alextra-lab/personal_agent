# ADR-0044: Provider Abstraction & Dual-Harness Design

**Status**: Accepted
**Date**: 2026-04-13
**Deciders**: Project owner
**Extends**: ADR-0033 (Multi-Provider Model Taxonomy & LiteLLM), ADR-0029 (Inference Concurrency Control)
**Related**: ADR-0043 (Three-Layer Separation), ADR-0045 (Infrastructure), ADR-0050 (Remote Agent Harness Integration)

---

## Context

### ADR-0033 solved the provider interface — this ADR solves the harness

ADR-0033 established the two-client model:

| Client | Scope |
|--------|-------|
| `LocalLLMClient` | Local inference servers (llama.cpp, MLX, LM Studio) — GPU-aware concurrency, thinking budget, hardware optimization |
| `LiteLLMClient` | All cloud providers (Anthropic, OpenAI, Google, Mistral) — via `litellm.acompletion()` |

Both implement the `LLMClient` protocol (`factory.py`), and `get_llm_client(role_name)` dispatches based on `provider_type` in `models.yaml`. This works for single-profile operation: the agent starts with one model configuration and uses it.

What ADR-0033 does **not** address:

1. **Simultaneous operation**: Running a local agent session and a cloud agent session at the same time, each with its own model config, both sharing the same Knowledge Layer.
2. **Profile-based configuration**: Switching between "local" and "cloud" execution without editing `models.yaml` or restarting the service.
3. **Cross-profile delegation**: A local agent deciding mid-task that a sub-task needs cloud-tier reasoning (or vice versa), and seamlessly delegating across profiles.
4. **Profile-aware evaluation**: Running the same conversation through local and cloud profiles and comparing results — critical for the evaluation phase.

### Why this matters now

The fundamental limitation of a fully local agent is **concurrency, not just capability**.

A single GPU (M4 Max, 128GB unified memory) can run one inference at a time for a 35B model. This means:

| Limitation | Impact |
|-----------|--------|
| **No multi-tasking** | The primary agent blocks while a sub-agent runs. Tool-call loops are sequential: call → wait 10-30s → parse → call → wait. No parallelism. |
| **Not dynamic or reactive** | The agent can't monitor background events, process incoming messages, or update knowledge while generating a response. It's single-threaded by hardware constraint. |
| **Shared resource contention** | Loading a 35B primary model and a 4B sub-agent model simultaneously thrashes unified memory. Running them sequentially doubles response time. |
| **No concurrent sub-agents** | HYBRID expansion (Slice 2) spawns sub-agents, but they run one at a time on the same GPU. Three sub-agents = 3x the wait, not 1x. |

Cloud models solve this trivially: API calls are parallel, stateless, and horizontally scaled by the provider. Five concurrent Claude Haiku sub-agents cost dollars, not minutes.

The evaluation data (EVAL-01 through EVAL-08) confirmed both the concurrency gap and the capability gap: Claude Sonnet produced measurably better results on decomposition-heavy and code-centric tasks, AND completed them faster because sub-tasks ran in parallel.

**The bet**: Introducing cloud models now addresses the immediate concurrency and capability limitations. Over time, local models will become more capable and less resource-intensive (smaller MoE architectures, better quantization, future Apple Silicon improvements). The architecture should support a future where a purely local harness is viable — but not block on it.

The decision is not "local vs. cloud" — it's "both, simultaneously, with clean separation." The agent should:

- Use local models for quick, low-cost interactions (simple questions, tool routing, memory queries).
- Use cloud models for concurrent sub-agent execution, complex reasoning, and coding tasks.
- Let the user choose a profile per conversation (via UI, ADR-0048).
- Allow automated profile selection based on task characteristics (future Slice 3 capability).
- Preserve the fully-local path for when hardware and models catch up.

### What "harness" means

A **harness** is a complete execution configuration: which models to use, what concurrency limits apply, what cost constraints exist, and how delegation works. It is not just a model — it includes the full execution context.

```
Harness = Models + Concurrency Config + Cost Limits + Delegation Rules + Tool Access
```

Two harnesses can run simultaneously because they share the Knowledge Layer (ADR-0043) but maintain independent execution state.

---

## Decision

### D1: Profile-based execution configuration

Introduce **execution profiles** as first-class configuration objects. A profile defines a complete harness:

```yaml
# config/profiles/local.yaml
profile:
  name: local
  description: "Local-first execution on Apple Silicon"
  models:
    primary: qwen3.5-35b-a3b
    sub_agent: qwen3.5-4b
  provider_type: local
  concurrency:
    primary: 1
    sub_agent: 3
  cost_limit_per_session: null  # no cost for local
  delegation:
    allow_cloud_escalation: true
    escalation_provider: anthropic
    escalation_model: claude-sonnet-4-6
    escalation_triggers:
      - task_complexity: high
      - tool_call_failures: 3
      - user_explicit: true

# config/profiles/cloud.yaml
profile:
  name: cloud
  description: "Cloud execution via LiteLLM"
  models:
    primary: claude-sonnet-4-6
    sub_agent: claude-haiku-4-5
  provider_type: cloud
  concurrency:
    primary: 5
    sub_agent: 10
  cost_limit_per_session: 2.00  # USD
  delegation:
    allow_local_fallback: false
    external_agents:
      - claude_code
      - codex
```

**Single codebase, multiple profiles**. No repository cloning. The same service instance can run multiple profiles concurrently, each on its own set of models, each reading/writing the same Knowledge Layer.

**Relationship to existing `models.yaml`**: The current `models.yaml` defines model definitions (provider, endpoint, parameters). Profiles reference models by name. `models.yaml` remains the model registry; profiles are the execution configuration.

### D2: Dual-harness simultaneous operation

The service supports multiple active profiles simultaneously:

```
┌─────────────────────────────────────────────────┐
│                  Service (port 9000)             │
│                                                  │
│  ┌──────────────────┐  ┌──────────────────────┐ │
│  │  Profile: local   │  │  Profile: cloud       │ │
│  │                    │  │                        │ │
│  │  LocalLLMClient   │  │  LiteLLMClient        │ │
│  │  Qwen3.5-35B      │  │  Claude Sonnet        │ │
│  │  GPU concurrency  │  │  API concurrency      │ │
│  │  No cost limit    │  │  $2/session limit     │ │
│  └────────┬───────────┘  └────────┬───────────────┘ │
│           │                       │               │
│           └───────────┬───────────┘               │
│                       ▼                           │
│              Knowledge Layer (shared)             │
│              Neo4j · PostgreSQL · ES              │
└─────────────────────────────────────────────────┘
```

Each conversation is bound to a profile at creation time. The user selects the profile via the UI (ADR-0048) or CLI (`agent --profile cloud "question"`). Profile selection is per-conversation, not per-request — a conversation stays on its profile for coherence.

### D3: Cross-profile delegation (hybrid execution)

A local-profile conversation can delegate to cloud models when the task warrants it. This is not a profile switch — it's a scoped escalation:

1. The local primary agent identifies a sub-task that exceeds local model capability (complex reasoning, code generation, multi-file analysis).
2. It composes a `DelegationPackage` (ADR-0033 Slice 2 types) targeting the cloud escalation provider defined in the profile.
3. The orchestrator routes the delegation to a `LiteLLMClient` instance, using the escalation model.
4. The result flows back through the local orchestrator and into the Knowledge Layer.

This is **delegation**, not a profile switch. The primary conversation remains on the local profile. The cloud call is a scoped tool-like invocation with its own cost tracking and timeout.

**Key constraint**: Cloud escalation from a local profile always uses `LiteLLMClient`, never `LocalLLMClient`. The escalation model must be a cloud model. This prevents configuration loops.

### D4: Remote agent harnesses as delegation targets

External agent environments (Claude Code, Codex, Cursor) are modeled as **delegation targets**, not profiles. They have fundamentally different interfaces:

| Aspect | Profile (local/cloud) | Delegation Target (external agent) |
|--------|----------------------|-------------------------------------|
| Interface | `LLMClient.respond()` | CLI subprocess, API call, MCP |
| Model choice | Configured in profile | Owned by the external agent |
| Tool access | Seshat's tool registry | Agent's own tools + filesystem |
| State | Managed by Seshat | Managed by the external agent |
| Cost | Tracked per-completion | Tracked per-delegation |

Delegation targets are defined in profile config under `delegation.external_agents` and use the `DelegationPackage`/`DelegationOutcome` types from Slice 2. See ADR-0050 for the full integration design.

### D5: Profile-aware observation

Every execution trace, cost record, and performance metric is tagged with the profile that produced it:

```python
# In telemetry context
trace_context = TraceContext(
    trace_id="...",
    profile="local",       # ← new field
    session_id="...",
    request_id="...",
)
```

This enables:
- Per-profile cost dashboards ("cloud profile spent $14 this week")
- A/B comparison between profiles on the same task
- Profile-specific performance baselines

The collaboration log (what questions were asked, what knowledge was produced) is profile-agnostic — it's Knowledge Layer data (ADR-0043).

---

## Consequences

### Positive

- **Graceful transition from local to cloud**: No architectural break needed. Add a cloud profile, start using it, compare results. The local profile continues to work unchanged.
- **Per-conversation profile selection**: Users get explicit control over cost/quality tradeoffs. Quick questions on local, deep analysis on cloud.
- **Hybrid execution path exists from day one**: Local agents can escalate to cloud without user intervention, within configured bounds.
- **Evaluation becomes structured**: Run the same conversation through two profiles, compare traces. Profile-aware telemetry makes this queryable.
- **No config duplication**: Profiles reference models from the shared `models.yaml` registry. Adding a new provider means adding model definitions, not rewriting configuration.

### Negative

- **Complexity in the execution path**: The orchestrator must be profile-aware. `get_llm_client()` currently takes a role name; it will need to also take a profile (or the profile must be set in a context variable). This is a real refactor, not just plumbing.
- **Cost tracking across escalation**: When a local conversation escalates to cloud, the cost is attributed to... the local profile? The cloud provider? Both? This needs a clear attribution model. Decision: attribute to the profile that initiated the escalation (the local profile), with the cloud cost as a sub-item.
- **Profile proliferation risk**: Without discipline, profiles multiply. Mitigation: start with exactly two (`local`, `cloud`). Add new profiles only when there's a concrete need (e.g., `eval-comparison`, `low-cost-cloud`).

### Neutral

- **`models.yaml` is unchanged**: Profiles are a layer above model definitions. Existing model config continues to work. Profiles are additive.
- **FRE-145 (Implement ADR-0033) is prerequisite**: The LiteLLM integration and two-client architecture from ADR-0033 must be fully implemented before profiles can work. Profile dispatch builds on `get_llm_client()`.

---

## Alternatives Considered

| Option | Why rejected |
|--------|-------------|
| **Separate config repos per profile** | Violates single-codebase principle. Config divergence is inevitable. Merging changes across repos is painful. |
| **Environment-variable switching** | Fragile. No simultaneous operation. Can't bind a conversation to a profile at runtime. |
| **Single "adaptive" profile** | Too magical. Users lose control over cost/quality. Debugging becomes harder when the system silently switches models. An explicit profile with explicit escalation rules is more transparent. |
| **Docker Compose per profile** | Heavyweight. Multiple service instances means multiple Knowledge Layer connections, potential write conflicts, and no shared session state. The Knowledge Layer must be shared, not replicated. |
