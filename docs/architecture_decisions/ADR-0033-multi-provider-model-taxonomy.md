# ADR-0033: Multi-Provider Model Taxonomy, LiteLLM Integration & Delegation Architecture

**Status**: Accepted (implemented — March 2026)
**Date**: 2026-03-26
**Deciders**: Project owner
**Extends**: ADR-0031 (model config consolidation), ADR-0029 (inference concurrency)

---

## Context

After completing the Slice 2 evaluation phase — 25 conversation paths run against both Qwen3.5 (local) and Claude Sonnet (cloud baseline) — four architectural gaps surfaced that block further progress toward Slice 3.

### Gap 1: Role Overloading Prevents Sub-Agent Optimization

The `standard` role serves double duty as both the "primary agent fast-path" and the default model for HYBRID sub-agents. These are fundamentally different workloads:

| Concern | Primary Agent (Orchestrator) | Sub-Agent (Focused Task) |
|---------|------------------------------|--------------------------|
| **Purpose** | Multi-turn reasoning, tool orchestration, decomposition | Single-task completion, bounded output |
| **Thinking** | Enabled (3000 token budget) | Disabled |
| **Temperature** | 0.6 (balanced creativity + accuracy) | 0.3-0.4 (deterministic, factual) |
| **Presence penalty** | 1.5 (anti-repetition in long outputs) | 0.3-0.5 (focused, coherent) |
| **Max output tokens** | 8192+ | 2048 (summary-length) |
| **Timeout** | 180s (deep reasoning) | 60-90s (fast completion) |
| **Typical model** | 35B (full reasoning) | 9B (fast inference) |
| **Concurrency** | 1 (GPU-limited) | 2-3 (smaller footprint) |

The current `ModelRole` enum (`ROUTER`, `STANDARD`, `REASONING`, `CODING`) doesn't express this distinction. Sub-agents inherit `ModelRole.STANDARD` by default, which maps to the 9B model with parameters tuned for conversational use — not focused sub-task completion.

**Evaluation evidence**: In Run 3, sub-agents used the 35B model (the primary agent's `llm_client` instance leaked into `execute_hybrid()`), consuming 4x the GPU time per sub-agent. In the Sonnet baseline, sub-agents used Claude Sonnet at $3/$15 per million tokens — 10x more expensive than Haiku would be for simple sub-tasks.

### Gap 2: Provider Proliferation Without Abstraction

The system has two hand-written LLM clients with different internal architectures:

| Client | What it does | Maintenance burden |
|--------|-------------|-------------------|
| `LocalLLMClient` | OpenAI-compatible endpoints (local SLM, Groq, Together) | httpx, custom retry, concurrency control, thinking budget, tool filtering |
| `ClaudeClient` | Anthropic API | Anthropic SDK, manual message/tool format conversion, cost tracking |

Adding Google Gemini or OpenAI would require a third (and fourth) client, each with its own message format conversion, tool calling translation, error handling, and retry logic. This is the path to **three to four bespoke HTTP clients that each handle the same fundamental problem differently**.

LiteLLM solves this exactly: one `acompletion()` call that transparently converts messages, tools, and responses across 100+ providers. It handles:
- Message format conversion (OpenAI <-> Anthropic <-> Google <-> Mistral)
- Tool calling format translation
- Retries with provider-specific backoff
- Cost tracking per completion (`completion_cost()`)
- Streaming normalization

What LiteLLM does NOT handle (and we must keep):
- GPU-aware concurrency control (ADR-0029) — specific to local inference
- Thinking budget injection (ADR-0023) — Qwen3.5-specific `chat_template_kwargs`
- Strategy-aware tool filtering (ADR-0032) — our business logic
- Priority queuing — our scheduling logic

**Architecture implication**: Keep `LocalLLMClient` for local inference (it owns hardware-specific optimization). Replace `ClaudeClient` with a `LiteLLMClient` that wraps `litellm.acompletion()` for all cloud providers. Two clients, clear separation:

| Client | Scope | Provider dispatch |
|--------|-------|------------------|
| `LocalLLMClient` | Local inference servers (llama.cpp, vLLM, LM Studio) | `provider_type: "local"` |
| `LiteLLMClient` | All cloud providers (Anthropic, OpenAI, Google, Mistral) | `provider_type: "cloud"` |

### Gap 3: Static Concurrency with No Adaptive Bounds

`max_concurrency` in `ModelDefinition` is a single ceiling value. There is no floor (`min_concurrency`). The brainstem already collects system metrics and computes expansion budgets, but has no mechanism to feed those signals back into per-model concurrency limits.

For sub-agents: on a quiet system with ample GPU headroom, we could run 3 concurrent 9B sub-agents. Under load, we should contract to 1. The brainstem has the signals; the model config has no bounds to constrain them.

### Gap 4: Coding Is Delegation, Not a Local Model Role

The `CODING` enum member maps to a local 9B model with `temperature: 0.2`. This conflates two entirely different things:

1. **A local model with low temperature** — This is just a parameter tweak, not a separate role.
2. **What this system actually needs for coding** — Delegation to external agents (Claude Code, Codex) that have their own models, file access, tool sets, and execution environments.

When the agent needs code written, it should compose a `DelegationPackage` and hand it to Claude Code (which runs as a CLI process with its own Claude model) or Codex (which runs in an OpenAI sandbox). These are not `respond()` calls — they're fundamentally different invocations with different interfaces, auth, timeout characteristics, and cost models.

The current `delegation_targets` sketch in models.yaml was too shallow — it treated delegation targets as model entries with a `model_ref`. Delegation targets are **agents**, not models. They need interface contracts.

---

## Decision

### D1: Two-Tier Model Taxonomy + Delegation Agents

```
+---------------------------------------------------------------------+
|                     MODELS (respond() interface)                     |
|                                                                     |
|  +--------------------------+  +------------------------------+     |
|  | TIER 1: PRIMARY          |  | TIER 2: SUB_AGENT            |     |
|  |                          |  |                              |     |
|  | Orchestrator brain       |  | Focused task completion      |     |
|  | Reasoning + tool calling |  | No thinking, low temperature |     |
|  | Thinking enabled         |  | Bounded output (2048 tokens) |     |
|  | Single concurrent        |  | Multiple concurrent          |     |
|  | models.yaml: "primary"   |  | models.yaml: "sub_agent"     |     |
|  | ModelRole.PRIMARY        |  | ModelRole.SUB_AGENT          |     |
|  +--------------------------+  +------------------------------+     |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|                  DELEGATION AGENTS (NOT respond())                    |
|                                                                     |
|  Different invocation interface per agent type.                     |
|  Defined in models.yaml "delegation_targets" section.               |
|  Invoked via DelegationPackage, not LLM client.                     |
|                                                                     |
|  +----------------+  +----------------+  +---------------------+    |
|  | Claude Code    |  | Codex          |  | Deep Research       |    |
|  | interface: cli |  | interface: api |  | interface: api      |    |
|  | model: sonnet  |  | model: codex   |  | model: gemini-pro   |    |
|  | output: diffs  |  | output: code   |  | output: report      |    |
|  +----------------+  +----------------+  +---------------------+    |
+---------------------------------------------------------------------+
```

Models and delegation agents are architecturally distinct:
- **Models** are called via `respond()` through the LLM client factory. They take messages and return completions. Configuration is sampling parameters.
- **Delegation agents** are called via `DelegationPackage` through a delegation orchestrator (Slice 3). They take task descriptions and return artifacts. Configuration is interface contracts.

### D2: ModelRole Enum — Clean Break

Remove all deprecated roles. No aliases, no migration code, no deprecation warnings.

```python
class ModelRole(str, Enum):
    """Model roles mapping to entries in config/models.yaml.

    Tier 1 (Primary): The orchestrator brain — reasoning, tool calling, decomposition.
    Tier 2 (Sub-Agent): Focused single-task completion — no thinking, fast inference.
    """

    PRIMARY = "primary"          # Tier 1: orchestrator brain
    SUB_AGENT = "sub_agent"      # Tier 2: focused task completion
```

Removed:
- `ROUTER` — dead code since gateway redesign (Slice 1). Intent classification is deterministic.
- `REASONING` — renamed to `PRIMARY`. All call sites updated.
- `STANDARD` — renamed to `SUB_AGENT`. All call sites updated.
- `CODING` — coding is delegation (D5), not a local model role. `Channel.CODE_TASK` routes to `PRIMARY`; the primary agent decides whether to handle directly or compose a delegation package.

This is a clean break: every reference to the old enum values is updated in the same commit. No backward-compat code that exists only for migration.

### D3: models.yaml Restructure

```yaml
# ── Model Taxonomy (ADR-0033) ──────────────────────────────────────
#   PRIMARY:    Orchestrator brain (reasoning, tool calling, decomposition)
#   SUB_AGENT:  Focused task completion (no thinking, fast, bounded output)

models:
  # ── Tier 1: Primary Agent ────────────────────────────
  primary:
    id: "unsloth/qwen3.5-35-A3B"
    context_length: 64000
    thinking_budget_tokens: 3000
    temperature: 0.6
    top_p: 0.95
    min_concurrency: 1
    max_concurrency: 1
    default_timeout: 180
    provider_type: "local"
    ...

  # ── Tier 2: Sub-Agent ────────────────────────────────
  sub_agent:
    id: "unsloth/qwen3.5-9b"
    context_length: 32768
    disable_thinking: true
    temperature: 0.4
    top_p: 0.8
    presence_penalty: 0.5
    max_tokens: 2048
    min_concurrency: 1
    max_concurrency: 3
    default_timeout: 90
    provider_type: "local"
    ...

  # ── Cloud Models ─────────────────────────────────────
  claude_sonnet:
    id: "claude-sonnet-4-6"
    provider: "anthropic"
    provider_type: "cloud"
    ...

# ── Delegation Targets (Slice 3 — schema defined, not invoked) ────
delegation_targets:
  claude_code:
    ...  # See D5
```

Removed: `coding` (delegation, not a model), `router` (dead). `reasoning` and `standard` replaced by `primary` and `sub_agent`. Experimental entries (`reasoning_heavy`, `coding_large_context`) retained for A/B testing.

### D4: LiteLLM for Cloud Provider Abstraction

Two LLM clients with clear boundaries:

```
                    get_llm_client(role_name)
                           |
                    ┌──────┴──────┐
                    │   Factory    │
                    └──────┬──────┘
                           |
              provider_type == "local"?
                    /            \
                 YES              NO
                  |                |
         LocalLLMClient      LiteLLMClient
              |                    |
    ┌─────────┴─────────┐    litellm.acompletion()
    │ GPU concurrency   │         |
    │ Thinking budget   │    ┌────┴────────┐
    │ Tool filtering    │    │ Anthropic   │
    │ Priority queuing  │    │ OpenAI      │
    │ httpx → local SLM │    │ Google      │
    └───────────────────┘    │ Mistral ... │
                             └─────────────┘
```

**`LiteLLMClient`** (new, replaces `ClaudeClient`):
- Wraps `litellm.acompletion()` for all cloud providers
- LiteLLM model string format: `"anthropic/claude-sonnet-4-6"`, `"openai/o4-mini"`, `"gemini/gemini-2.5-pro"`
- Our wrapper adds: cost tracking via `CostTrackerService`, budget enforcement, telemetry emission, concurrency bounds from `ModelDefinition`
- Implements the same `LLMClient` protocol (same `respond()` signature)
- Returns normalized `LLMResponse` (litellm already returns OpenAI-format responses)

**`LocalLLMClient`** (existing, kept as-is):
- Stays for local inference servers
- Retains GPU-aware concurrency (ADR-0029), thinking budget (ADR-0023), tool filtering (ADR-0032)
- These are hardware-specific optimizations that LiteLLM doesn't handle

**Factory dispatch**:
```python
def get_llm_client(role_name: str = "primary") -> LLMClient:
    config = load_model_config()
    model_def = config.models.get(role_name)

    match model_def.provider_type:
        case "local":
            return LocalLLMClient()
        case _:  # "cloud" or any non-local
            return LiteLLMClient(
                model_id=model_def.id,
                provider=model_def.provider,
                max_tokens=model_def.max_tokens or 8192,
            )
```

**What happens to `ClaudeClient`**: Deleted. Its `_convert_tools_to_anthropic()` and `_convert_messages_to_anthropic()` methods are exactly what LiteLLM does transparently. The cost tracking moves to `LiteLLMClient`. No code is lost — it's replaced by a better abstraction.

### D5: Delegation Target Architecture

Delegation targets are **external agents** with their own runtimes, models, and execution environments. They are NOT model entries and are NOT invoked via `respond()`.

**Schema** (`delegation_targets` section in models.yaml):

```yaml
delegation_targets:
  claude_code:
    description: "Claude Code CLI for coding and refactoring tasks"

    # ── Interface Contract ──────────────────────────────
    interface: "cli"                      # cli | api | sdk
    command: "claude"                     # CLI entrypoint
    auth_method: "api_key"               # api_key | oauth | session
    auth_env_var: "ANTHROPIC_API_KEY"    # Env var for auth credential

    # ── Model & Budget ──────────────────────────────────
    model: "claude-sonnet-4-6"           # Model the agent uses internally
    max_turns: 20                        # Conversation budget per delegation
    timeout_seconds: 600                 # 10 min for complex tasks
    cost_model: "per_token"              # per_token | per_task | per_minute
    estimated_cost_per_task_usd: 0.50    # Budget estimation

    # ── Capabilities & I/O ──────────────────────────────
    capabilities:
      - code_generation
      - refactoring
      - testing
      - debugging
      - file_editing
    input_format: "text"                 # DelegationPackage → task prompt
    output_format: "text+diffs"          # What comes back
    requires_working_directory: true      # Needs filesystem access
    sandboxed: false                     # Runs in caller's environment

  codex:
    description: "OpenAI Codex for sandboxed code execution"
    interface: "api"
    base_url: "https://api.openai.com/v1"
    auth_method: "api_key"
    auth_env_var: "OPENAI_API_KEY"
    model: "codex-mini"
    timeout_seconds: 300
    cost_model: "per_task"
    capabilities:
      - code_generation
      - code_execution
    input_format: "text+files"
    output_format: "text+artifacts"
    requires_working_directory: false
    sandboxed: true

  deep_research:
    description: "SOTA model for deep research with grounding"
    interface: "api"
    auth_method: "api_key"
    auth_env_var: "GOOGLE_API_KEY"
    model: "gemini-2.5-pro"
    timeout_seconds: 900                 # Deep research can take 15 min
    cost_model: "per_token"
    capabilities:
      - research
      - analysis
      - long_context
      - grounding
    input_format: "text"
    output_format: "text+citations"
    requires_working_directory: false
    sandboxed: false
```

**Delegation flow** (Slice 3 implementation, schema defined now):

```
DelegationPackage                DelegationOrchestrator              External Agent
     |                                |                                    |
     |  task_description              |                                    |
     |  target_agent: "claude_code"   |                                    |
     |  acceptance_criteria           |                                    |
     |  context, memory_excerpt       |                                    |
     |------------------------------->|                                    |
     |                                |  1. Look up delegation_targets     |
     |                                |     ["claude_code"]                |
     |                                |  2. Validate auth_env_var set     |
     |                                |  3. Format input per interface    |
     |                                |  4. Invoke (CLI / API / SDK)      |
     |                                |----------------------------------->|
     |                                |                                    |
     |                                |  5. Monitor: timeout, cost budget  |
     |                                |                                    |
     |                                |<-----------------------------------|
     |                                |  6. Parse output per output_format |
     |                                |  7. Build DelegationOutcome        |
     |<-------------------------------|                                    |
     |  DelegationOutcome             |                                    |
     |  success, artifacts,           |                                    |
     |  cost, duration                |                                    |
```

**Key design principle**: The `delegation_targets` schema is **structural** in this ADR. The YAML is parsed and validated (Pydantic model), but the `DelegationOrchestrator` that invokes targets is a Slice 3 deliverable. This gives us:
- A validated schema now — we know the shape before we build the orchestrator
- Clear interface contracts — when we implement Claude Code delegation, the config already defines what parameters it needs
- Cost estimation data — the agent can estimate delegation cost before deciding whether to delegate

### D6: Concurrency Bounds

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
**Slice 3**: Adaptive controller reads brainstem sensor data to adjust effective concurrency within bounds.

### D7: Dead Router Code Removal

The `ROUTER` code path in `executor.py` (~15 conditional branches checking `model_role == ModelRole.ROUTER`) is dead since the Slice 1 gateway redesign. Intent classification is now deterministic via `request_gateway/intent.py`. This dead code:
- Adds cognitive load when reading the executor
- Creates false dependencies on the router model config
- Makes the executor appear more complex than it is

**Decision**: Remove all ROUTER conditional branches in the same commit as the enum rename. The gateway is the intent classifier; the executor is the orchestrator.

---

## Alternatives Considered

### A1: Three native provider clients (original ADR-0033 v1)

Build `OpenAICompatibleClient`, `ClaudeClient`, `GeminiClient` — each with hand-written format conversion.

**Rejected (Revision 2)**: Maintaining three bespoke clients for message/tool format conversion is exactly what LiteLLM automates. We evaluated the features we actually need from native clients:

| Feature | LiteLLM handles? | Need native? |
|---------|-----------------|-------------|
| Tool calling format conversion | Yes | No |
| Message format conversion | Yes | No |
| Streaming | Yes | No |
| Structured output / JSON mode | Yes | No |
| Extended thinking (Anthropic) | Yes (pass-through) | No |
| Retries + rate limiting | Yes (built-in) | No |
| Cost tracking | Yes (`completion_cost()`) | No |
| GPU concurrency control | No | Yes (local only) |
| Thinking budget injection | No | Yes (local only) |
| Priority queuing | No | Yes (local only) |

The only features requiring native code are local-inference-specific. LiteLLM covers all cloud providers.

### A2: LiteLLM for everything (including local)

Route local SLM calls through LiteLLM too (`openai/qwen3.5` with `api_base`).

**Rejected**: `LocalLLMClient` has three hardware-specific features that LiteLLM can't replicate: GPU-aware concurrency semaphores (ADR-0029), `chat_template_kwargs` for thinking budget (ADR-0023), and strategy-aware tool filtering (ADR-0032). Replacing it with LiteLLM would lose these optimizations. The two-client split (local vs. cloud) maps exactly to the boundary where our business logic ends and provider abstraction begins.

### A3: Backward-compatible enum aliases (original ADR-0033 v1)

Keep `REASONING`, `STANDARD`, `ROUTER` as deprecated enum members with migration aliases.

**Rejected (Revision 2)**: This is a solo research project. Migration aliases add permanent complexity to serve zero users. A clean break (rename + update all ~170 call sites in one commit) is 30 minutes of mechanical work vs. permanent code debt. The search found ~90 references in src/ and ~80 in tests/ — all mechanical renames.

### A4: Keep `coding` as a model role

Keep `ModelRole.CODING` mapping to a local model with low temperature.

**Rejected**: Coding tasks in this architecture should be delegated to agents that can actually write and verify code (Claude Code, Codex). A local 9B model with `temperature: 0.2` is not a coding specialist — it's a parameter tweak. The `Channel.CODE_TASK` path routes to `PRIMARY`; the primary agent decides whether to handle directly or delegate.

### A5: Use LiteLLM Proxy (server mode) instead of library mode

Run a LiteLLM proxy server and route all calls through it.

**Deferred**: Proxy mode adds operational complexity (another service to manage). Library mode (`litellm.acompletion()`) is simpler for a single-process application. If we later need multi-process or multi-service model routing, the proxy becomes valuable. The switch from library to proxy mode is non-breaking.

---

## Consequences

### Positive

- **Sub-agents get dedicated optimization**: Distinct parameters, model, timeout, concurrency bounds
- **One cloud client for all providers**: LiteLLM handles Anthropic, OpenAI, Google, Mistral — adding a provider is one line in models.yaml
- **Clean mental model**: Two model tiers (primary + sub_agent) with clear purpose; delegation is architecturally separate
- **Sub-agent client isolation**: Sub-agents always use the sub_agent model config via factory, never inherit the primary's client
- **Delegation contracts defined early**: When Slice 3 implements delegation, the interface schema is already validated
- **Dead code removed**: ~15 ROUTER conditional branches deleted from executor
- **No migration tax**: Clean enum break means no deprecated values, no alias code, no confusion

### Negative

- **Migration churn**: ~170 call sites renamed in one commit (mechanical but large diff)
- **LiteLLM dependency**: New runtime dependency (~50MB). If LiteLLM breaks, cloud calls break.
- **`ClaudeClient` deleted**: The Anthropic format conversion code we just wrote is removed. That's wasted work, but sunk cost.
- **ROUTER removal may have edge cases**: If any code path reaches the executor without going through the gateway, ROUTER fallback no longer exists. Mitigation: the gateway always runs; verify via tests.

### Risks

- **LiteLLM version incompatibility**: Provider format changes can lag in LiteLLM releases. **Mitigation**: Pin version in pyproject.toml; test cloud calls in evaluation harness.
- **Sub-agent client creation per request**: Creating a new `LiteLLMClient` in `execute_hybrid()` instead of reusing. **Mitigation**: Client construction is lightweight (no connections until first call).
- **Lost cost tracking granularity**: ClaudeClient had per-call cost tracking via `CostTrackerService`. **Mitigation**: LiteLLM provides `completion_cost()` per call; integrate into `LiteLLMClient`.

---

## Implementation Scope

### This Plan (Phase 1)

1. Add `litellm` dependency
2. Clean ModelRole enum (PRIMARY + SUB_AGENT only)
3. Restructure models.yaml (primary, sub_agent, delegation_targets schema)
4. Add `min_concurrency` to ModelDefinition
5. Build `LiteLLMClient` (wraps `litellm.acompletion()`)
6. Update factory (local → LocalLLMClient, cloud → LiteLLMClient)
7. Wire sub-agent client isolation
8. Remove ROUTER dead code from executor
9. Update all call sites (src + tests)
10. Delete `ClaudeClient` (replaced by LiteLLMClient)

### Slice 3 (Deferred)

1. `DelegationOrchestrator` — invokes delegation targets per interface contract
2. Claude Code delegation via CLI / Agent SDK
3. Adaptive concurrency controller reading brainstem metrics
4. `DelegationTarget` Pydantic model — parse and validate `delegation_targets` YAML
5. Cost tracking dashboard for delegated tasks

---

## Acceptance Criteria

- [ ] `litellm` in pyproject.toml dependencies
- [ ] `ModelRole` has exactly two members: `PRIMARY`, `SUB_AGENT`
- [ ] No references to `ROUTER`, `REASONING`, `STANDARD`, `CODING` in src/ or tests/
- [ ] `models.yaml` has `primary` and `sub_agent` entries with distinct parameters
- [ ] `models.yaml` has `delegation_targets` section (validated schema, not invoked)
- [ ] `ModelDefinition` has `min_concurrency` field with default 1
- [ ] `LiteLLMClient` exists, implements `LLMClient` protocol via `litellm.acompletion()`
- [ ] `ClaudeClient` deleted (replaced by `LiteLLMClient`)
- [ ] Factory dispatches `provider_type: "local"` → `LocalLLMClient`, else → `LiteLLMClient`
- [ ] Sub-agents use `sub_agent` model config via factory, never primary's client
- [ ] Dead ROUTER code removed from executor
- [ ] All tests pass with updated role names
- [ ] `mypy src/` clean
- [ ] `ruff check src/` clean
- [ ] Evaluation harness runs (no regression)

---

## Related

- **ADR-0031**: Model Configuration Consolidation (models.yaml as single source of truth)
- **ADR-0029**: Inference Concurrency Control (GPU-aware semaphores — kept for LocalLLMClient)
- **ADR-0023**: Qwen3.5 Model Integration (thinking budget — kept for LocalLLMClient)
- **ADR-0032**: Robust Tool Calling Strategy (tool filtering — kept for LocalLLMClient)
- **Evaluation Results**: `telemetry/evaluation/run-03-three-fixes/` and `telemetry/evaluation/run-foundation-baseline/`
- **Slice 3 Spec**: `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` Section 7 (Self-Improvement Loop)
