# ADR-0033: Multi-Provider Model Taxonomy & Adaptive Concurrency Bounds

**Status**: Proposed
**Date**: 2026-03-25
**Deciders**: Project owner
**Extends**: ADR-0031 (model config consolidation), ADR-0029 (inference concurrency)

---

## Context

After completing the Slice 2 evaluation phase — 25 conversation paths run against both Qwen3.5 (local) and Claude Sonnet (cloud baseline) — three architectural gaps surfaced that block further progress toward Slice 3.

### Gap 1: Role Overloading Prevents Sub-Agent Optimization

The `standard` role serves double duty as both the "primary agent fast-path" and the default model for HYBRID sub-agents. These are fundamentally different workloads:

| Concern | Primary Agent (Orchestrator) | Sub-Agent (Focused Task) |
|---------|------------------------------|--------------------------|
| **Purpose** | Multi-turn reasoning, tool orchestration, decomposition | Single-task completion, bounded output |
| **Thinking** | Enabled (3000 token budget) | Disabled |
| **Temperature** | 0.6 (balanced creativity + accuracy) | 0.3–0.4 (deterministic, factual) |
| **Presence penalty** | 1.5 (anti-repetition in long outputs) | 0.3–0.5 (focused, allow some repetition for coherence) |
| **Max output tokens** | 8192+ | 2048 (summary-length) |
| **Timeout** | 180s (deep reasoning) | 60–90s (fast completion) |
| **Typical model** | 35B (full reasoning) | 9B (fast inference) |
| **Concurrency** | 1 (GPU-limited) | 2–3 (smaller footprint) |

The current `ModelRole` enum (`ROUTER`, `STANDARD`, `REASONING`, `CODING`) doesn't express this distinction. Sub-agents inherit `ModelRole.STANDARD` by default, which maps to `models["standard"]` — the 9B model with parameters tuned for conversational use, not focused sub-task completion.

**Evaluation evidence**: In Run 3, sub-agents used the 35B model (the same `llm_client` instance passed from the primary agent call), consuming 4× the GPU time per sub-agent. In the Sonnet baseline, sub-agents used Claude Sonnet at $3/$15 per million tokens — 10× more expensive than Haiku would be for simple sub-tasks.

### Gap 2: Provider Lock-in and Misleading Naming

The system has two LLM clients:

| Client | Name | Actual Scope |
|--------|------|-------------|
| `LocalLLMClient` | Implies "local only" | Actually handles **any** OpenAI-compatible endpoint: local SLM, OpenAI API, Groq, Together, Fireworks, Mistral |
| `ClaudeClient` | Anthropic-specific | Correct |
| *(missing)* | — | Google Gemini (different SDK, different tool format, different message structure) |

The `LocalLLMClient` name creates cognitive friction: developers assume it's only for local models and create separate clients for cloud OpenAI-compatible providers. The factory dispatch is currently binary (`if anthropic → Claude, else → Local`), with no path for Google.

**Coverage analysis**: Three provider clients cover the entire major-model landscape:

| Client | Providers Covered | Protocol |
|--------|------------------|----------|
| OpenAI-compatible | OpenAI, local SLM (llama.cpp, vLLM, LM Studio, Ollama), Groq, Together, Fireworks, **Mistral** | OpenAI Chat Completions API |
| Anthropic | Claude family (Opus, Sonnet, Haiku) | Anthropic Messages API |
| Google Gemini | Gemini family (Pro, Flash, Ultra) | Google GenAI SDK |

Mistral's API is OpenAI-compatible and does not warrant a dedicated client.

### Gap 3: Static Concurrency with No Adaptive Bounds

`max_concurrency` in `ModelDefinition` is a single ceiling value. There is no floor (`min_concurrency`). The brainstem already collects system metrics (CPU, memory, GPU) and computes expansion budgets (`compute_expansion_budget()`), but has no mechanism to feed those signals back into per-model concurrency limits.

For sub-agents, this matters: on a quiet system with ample GPU headroom, we could run 3 concurrent 9B sub-agents. Under load, we should contract to 1. The brainstem has the signals; the model config has no bounds to constrain them.

### Gap 4: No Delegation Tier in the Model Taxonomy

Stage B delegation (DelegationPackage/DelegationOutcome) exists as data structures, but delegation targets are not represented in `models.yaml`. When the agent needs to delegate to Claude Code, Codex, or a SOTA research model, there's no config-driven way to select which model, set its parameters, or track its costs alongside the primary and sub-agent models.

---

## Decision

### D1: Three-Tier Model Taxonomy

All models are classified into three tiers reflecting their architectural role:

```
┌──────────────────────────────────────────────────────────┐
│  TIER 1: PRIMARY                                          │
│  The orchestrator brain. Reasoning, tool calling,         │
│  decomposition planning. Thinking enabled. One active.    │
│  models.yaml key: "primary"                              │
│  ModelRole.PRIMARY                                       │
├──────────────────────────────────────────────────────────┤
│  TIER 2: SUB_AGENT                                        │
│  Focused single-task completion. No thinking, low temp,   │
│  bounded output. Multiple concurrent. Fast inference.     │
│  models.yaml key: "sub_agent"                            │
│  ModelRole.SUB_AGENT                                     │
├──────────────────────────────────────────────────────────┤
│  TIER 3: DELEGATION (structure now, implementation Slice 3)│
│  External model targets with different invocation          │
│  interfaces. Claude Code, Codex, Gemini Deep Research.    │
│  models.yaml section: "delegation_targets"               │
│  Invoked via DelegationPackage, not respond()            │
└──────────────────────────────────────────────────────────┘
```

**Specialist roles** (`coding`) remain as entries in the flat `models` dict but are understood to belong to a tier. The tier determines default behavior (thinking, concurrency, parameters). The `models.yaml` file documents tier membership via comments.

### D2: ModelRole Enum Update

```python
class ModelRole(str, Enum):
    """Model roles mapping to entries in config/models.yaml."""

    # ── Active roles ────────────────────────────────
    PRIMARY = "primary"          # Tier 1: orchestrator brain
    SUB_AGENT = "sub_agent"      # Tier 2: focused task completion
    CODING = "coding"            # Specialist: code generation

    # ── Deprecated (kept for deserialization) ───────
    REASONING = "reasoning"      # Migration: use PRIMARY
    STANDARD = "standard"        # Migration: use SUB_AGENT
    ROUTER = "router"            # Removed in Redesign v2
```

`ModelConfig` validation creates backward-compatible aliases so that `models["reasoning"]` resolves to `models["primary"]` and `models["standard"]` resolves to `models["sub_agent"]` when the old keys are absent.

### D3: models.yaml Restructure

```yaml
# ── Tier 1: Primary Agent ────────────────────────
primary:
  id: "unsloth/qwen3.5-35-A3B"
  context_length: 64000
  thinking_budget_tokens: 3000
  temperature: 0.6
  top_p: 0.95
  min_concurrency: 1
  max_concurrency: 1
  default_timeout: 180
  ...

# ── Tier 2: Sub-Agent ────────────────────────────
sub_agent:
  id: "unsloth/qwen3.5-9b"
  context_length: 32768
  disable_thinking: true
  temperature: 0.4        # Lower: deterministic focused output
  top_p: 0.8
  presence_penalty: 0.5   # Lower: allow coherent focused response
  min_concurrency: 1
  max_concurrency: 3      # Multiple 9B can run on same GPU
  default_timeout: 90
  max_tokens: 2048         # Bounded: sub-tasks produce summaries
  ...

# ── Specialist Roles ─────────────────────────────
coding:
  id: "unsloth/qwen3.5-9b"
  temperature: 0.2
  ...

# ── Cloud Models ─────────────────────────────────
claude_sonnet:
  id: "claude-sonnet-4-6"
  provider: "anthropic"
  ...

gemini_pro:
  id: "gemini-2.5-pro"
  provider: "google"
  ...

# ── Tier 3: Delegation Targets (Slice 3) ─────────
delegation_targets:
  claude_code:
    description: "Claude Code CLI for coding tasks"
    model_ref: "claude_sonnet"
    interface: "cli"
    capabilities: ["code_generation", "refactoring", "testing"]
  deep_research:
    description: "SOTA model for deep research and analysis"
    model_ref: "gemini_pro"
    interface: "api"
    capabilities: ["research", "analysis", "long_context"]
```

The `delegation_targets` section is **structural** in this ADR — parsed and validated but not invoked. Slice 3 (programmatic delegation) implements the invocation.

### D4: Sub-Agent Client Isolation

**Bug fix**: Currently, `execute_hybrid()` receives the primary agent's `llm_client` instance. When the primary is `ClaudeClient`, sub-agents also call Claude — ignoring `spec.model_role` entirely (because `ClaudeClient.respond()` always uses `self.model`).

**Fix**: `execute_hybrid()` creates its own client via the factory:

```python
async def execute_hybrid(specs, trace_id, max_concurrent):
    from personal_agent.llm_client.factory import get_llm_client
    sub_agent_client = get_llm_client(role_name="sub_agent")
    # All sub-agents use the sub_agent model config
```

This ensures sub-agents always use the model designated for their tier, regardless of what the primary agent uses.

### D5: Concurrency Bounds

Add `min_concurrency` to `ModelDefinition`:

```python
min_concurrency: int = Field(
    default=1,
    ge=1,
    description="Floor for adaptive concurrency (brainstem cannot go below this).",
)
```

The existing `max_concurrency` becomes the ceiling. The brainstem's `compute_expansion_budget()` adjusts within `[min_concurrency, max_concurrency]` based on system load.

**Now**: Static bounds enforced by `InferenceConcurrencyController`.
**Slice 3**: Adaptive controller that uses brainstem sensor data to adjust the effective concurrency level within bounds.

### D6: Three Provider Clients

| Provider field | Client class | Status |
|---------------|-------------|--------|
| `None` / `"openai"` | `OpenAICompatibleClient` (renamed from `LocalLLMClient`) | Rename |
| `"anthropic"` | `ClaudeClient` | Exists |
| `"google"` | `GeminiClient` | New |

The factory dispatch becomes:

```python
match model_def.provider:
    case "anthropic":
        return ClaudeClient(...)
    case "google":
        return GeminiClient(...)
    case _:  # None, "openai", or any OpenAI-compatible
        return OpenAICompatibleClient(...)
```

`LocalLLMClient` is retained as a backward-compatible alias:
```python
LocalLLMClient = OpenAICompatibleClient
```

---

## Alternatives Considered

### A1: Keep flat roles, just add "sub_agent" entry

Keep `REASONING`, `STANDARD`, `CODING` as-is. Just add `sub_agent` to the enum and YAML.

**Rejected**: Perpetuates the confusing `standard`/`reasoning` naming. The evaluation showed that the primary agent role is always `reasoning` (not `standard`), making `standard` a misleading name for a role that's barely used directly. Renaming to `primary` + `sub_agent` is clearer.

### A2: Use LiteLLM for all provider routing

Route everything through LiteLLM, which normalizes providers.

**Deferred** (same as ADR-0031): LiteLLM is the right long-term direction but introduces a dependency and abstraction layer. Building native clients first gives us deep understanding of each provider's tool calling and message format quirks, which is valuable for a research project. LiteLLM can replace our clients later without architectural change.

### A3: Dynamic model loading (multiple models per GPU)

Instead of separate model configs, dynamically load/unload models on the SLM server based on demand.

**Deferred to Slice 3**: Requires SLM server API changes (model loading endpoints). The current architecture assumes one model loaded per SLM server process. This is a valid optimization but orthogonal to the taxonomy decision.

### A4: tier field on ModelDefinition instead of naming convention

Add `tier: Literal["primary", "sub_agent", "delegation"]` to ModelDefinition and use it for dispatch instead of the role name.

**Considered but deferred**: Adds complexity without clear benefit now. The naming convention (`primary`, `sub_agent`) is sufficient. A `tier` field could be added later if we need runtime tier-based dispatch (e.g., "find me any sub_agent tier model that's available").

---

## Consequences

### Positive

- **Sub-agents get dedicated optimization**: Lower temperature, bounded output, appropriate timeouts — immediately improves HYBRID expansion quality
- **Clear mental model**: 2 (eventually 3) tiers with distinct purposes; `models.yaml` is self-documenting
- **Sub-agent client isolation**: Sub-agents always use the sub_agent model, never accidentally inherit the primary's expensive cloud model
- **Provider coverage**: OpenAI-compatible + Anthropic + Google covers all major model providers
- **Concurrency bounds**: `[min, max]` range enables future adaptive control without config changes
- **Delegation readiness**: `delegation_targets` section is ready for Slice 3 programmatic delegation

### Negative

- **Migration churn**: Renaming `reasoning` → `primary` and `standard` → `sub_agent` touches executor, routing, tests, and possibly external scripts
- **Three LLM clients to maintain**: Each provider has different tool formats, message structures, and error semantics
- **GeminiClient requires Gemini SDK dependency**: New dependency in `pyproject.toml`
- **Backward compatibility burden**: Deprecated `REASONING`/`STANDARD` enum values and model config aliases add code that exists only for migration

### Risks

- **Enum alias confusion**: Developers may use deprecated `ModelRole.REASONING` and not realize it maps to a model entry that may not exist in newer config files. **Mitigation**: `from_str()` logs a deprecation warning.
- **Sub-agent client creation per request**: Creating a new client in `execute_hybrid()` instead of reusing the primary's. **Mitigation**: Client construction is lightweight (no connections opened until first `respond()` call); cache if needed.

---

## Migration Path

### Phase 1: Taxonomy + Sub-Agent (this plan)

1. Add `PRIMARY` and `SUB_AGENT` to `ModelRole` enum
2. Restructure `models.yaml`: `primary` replaces `reasoning`, add `sub_agent`, remove `standard`
3. `ModelConfig` validator creates aliases: `reasoning` → `primary`, `standard` → `sub_agent`
4. Add `min_concurrency` to `ModelDefinition`
5. Wire sub-agent client isolation in `execute_hybrid()`
6. Update executor to use `ModelRole.PRIMARY`
7. Rename `LocalLLMClient` → `OpenAICompatibleClient` (with alias)
8. Build `GeminiClient` (skeleton with `respond()` implementing the `LLMClient` protocol)
9. Update factory for three-provider dispatch

### Phase 2: Delegation Targets (Slice 3)

1. Implement `delegation_targets` parsing in `ModelConfig`
2. Build delegation orchestrator that uses target config
3. Wire `DelegationPackage.target_agent` → `delegation_targets[name].model_ref`
4. Implement CLI interface for Claude Code delegation
5. Adaptive concurrency controller reading brainstem metrics

### Cleanup (post-migration)

1. Remove deprecated `REASONING`, `STANDARD`, `ROUTER` from `ModelRole`
2. Remove `ModelConfig` backward-compat aliases
3. Remove `LocalLLMClient` alias

---

## Acceptance Criteria

- [ ] `models.yaml` has `primary` and `sub_agent` entries with distinct parameters
- [ ] `models.yaml` has `delegation_targets` section (structural, validated, not invoked)
- [ ] `ModelRole.PRIMARY` and `ModelRole.SUB_AGENT` exist and map to correct config entries
- [ ] `ModelRole.REASONING` and `ModelRole.STANDARD` still work (backward compat)
- [ ] Sub-agents use `sub_agent` model config, never the primary's client
- [ ] `ModelDefinition` has `min_concurrency` field with default 1
- [ ] `OpenAICompatibleClient` is the canonical class name; `LocalLLMClient` is an alias
- [ ] `GeminiClient` exists with `respond()` matching the `LLMClient` protocol
- [ ] Factory dispatches `"google"` provider to `GeminiClient`
- [ ] All existing tests pass (backward compat aliases work)
- [ ] New tests cover: sub-agent client isolation, `min_concurrency` validation, provider dispatch for all three providers
- [ ] `mypy src/` passes
- [ ] `ruff check src/` passes
- [ ] Evaluation harness runs with updated `models.yaml` (no regression)

---

## Related

- **ADR-0031**: Model Configuration Consolidation (established `models.yaml` as single source of truth)
- **ADR-0029**: Inference Concurrency Control (per-model and per-endpoint semaphores)
- **ADR-0023**: Qwen3.5 Model Integration (thinking controls, sampling params)
- **ADR-0032**: Robust Tool Calling Strategy (strategy-aware tool filtering)
- **Evaluation Results**: `telemetry/evaluation/run-03-three-fixes/` and `telemetry/evaluation/run-foundation-baseline/`
- **Slice 3 Spec**: `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` Section 7 (Self-Improvement Loop)
