# ADR-0031: Model Configuration Consolidation

**Status**: Accepted  
**Date**: 2026-03-10  
**Deciders**: Project owner  
**Supersedes**: None (extends ADR-0007, ADR-0003)

---

## Context

Model configuration is currently split across two files with no clear ownership rule:

| Setting | Location | Rationale |
|---|---|---|
| Local model IDs, sampling params, timeouts | `config/models.yaml` | Model config |
| `entity_extraction_role` | `config/models.yaml` | Process-to-model assignment |
| `claude_model` (model ID) | `settings.py` / `.env` | Cloud API config |
| `claude_max_tokens` | `settings.py` / `.env` | Model call param |
| `claude_weekly_budget_usd` | `settings.py` / `.env` | Operational budget |

This creates a confusion: *"which file do I change to use a different model for entity extraction?"*. The answer is "both, depending on whether you're switching from local to cloud or between cloud models."

Additional problems discovered:

1. **Dead env var**: `AGENT_ENTITY_EXTRACTION_MODEL` in `.env` has no corresponding `settings.py` field and is never read. Config drift started.
2. **Magic string anti-pattern**: `entity_extraction_role: "claude"` is a special-cased sentinel that bypasses the model registry entirely — there is no `ModelDefinition` for Claude, so its parameters live outside the config system.
3. **Hardcoded pricing**: `ClaudeClient` hard-codes `$3.0/1M input, $15.0/1M output` and reads its own model name from `settings`. When switching to a different Claude model or adding OpenAI, this requires changes in both places.
4. **No process-to-model assignment for Captain's Log or Insights**: Only entity extraction has a config-driven model assignment; the others are hardcoded to `ModelRole.REASONING` in Python.

As the system moves toward using powerful cloud models for background processes (Captain's Log, Entity Extraction, Insights Engine) and plans to A/B test between providers (Anthropic vs OpenAI), this split will compound into a serious maintainability problem.

## Decision

**`config/models.yaml` is the single source of truth for all model identity and call parameters.**

**`settings.py` / `.env` holds only secrets (API keys) and operational runtime controls (budgets, feature flags).**

### Specific changes

**`models.yaml` gains:**
- Cloud model entries (e.g. `claude_sonnet`, `openai_o4_mini`) as first-class `ModelDefinition` entries alongside local models
- Two new top-level role assignment keys: `captains_log_role` and `insights_role`
- `provider` field on each model definition (`"anthropic"` | `"openai"` | absent = local)
- `max_tokens` field for output token limits (primarily useful for cloud models)

**`settings.py` removes:**
- `claude_model` → moves to `models.yaml` as `models.claude_sonnet.id`
- `claude_max_tokens` → moves to `models.yaml` as `models.claude_sonnet.max_tokens`
- `claude_weekly_budget_usd` → renamed to `cloud_weekly_budget_usd` (provider-agnostic)

**`settings.py` adds:**
- `openai_api_key: str | None` (secret, env-only)
- `cloud_weekly_budget_usd: float` (renamed from `claude_weekly_budget_usd`)

**`ClaudeClient` changes:**
- Constructor accepts `model_id: str` and `max_tokens: int` directly (no longer reads `settings.claude_model`)
- Budget check uses `settings.cloud_weekly_budget_usd`

**Entity extraction routing logic:**
- Replaces `entity_extraction_role == "claude"` sentinel check with `model_def.provider == "anthropic"` check
- Creates `ClaudeClient` internally when the model def says Anthropic provider (fixes existing bug where Claude was never actually invoked when consolidator is constructed without an explicit client)

### Rule of thumb

> **"Which model does X use?"** → `config/models.yaml` (role assignment + model definition)  
> **"What is my API key?"** → `.env` (secret, gitignored)  
> **"How much can I spend?"** → `.env` (operational runtime control)  
> **"Is this feature enabled?"** → `.env` (feature flag)

## Alternatives Considered

### Alternative A: Keep split but add documentation

Add a comment in `settings.py` pointing to `models.yaml` and vice versa. Does not solve the drift problem — the next time someone adds a cloud provider, they will instinctively add `openai_model` to `settings.py` because `claude_model` is there.

**Rejected**: Documentation does not prevent recurrence.

### Alternative B: Move everything to `settings.py` / `.env`

Put all model IDs into `.env` / `AppConfig`, abolish `models.yaml` for model identity. The problem is that model identity requires structured per-model sampling params, concurrency limits, endpoint overrides, and provider dispatch logic that doesn't fit well in flat key=value env vars.

**Rejected**: `models.yaml` provides necessary structure; env vars are not the right format.

### Alternative C: Use LiteLLM as the unified abstraction

Route all LLM calls (local and cloud) through LiteLLM, which normalizes providers. LiteLLM model strings (`anthropic/claude-sonnet-4-5`, `openai/o4-mini`) are self-contained and make the provider implicit in the model ID.

**Deferred**: LiteLLM is the right long-term direction but is a larger architectural change. DSPy already uses LiteLLM internally; extending this to direct calls is tracked separately. This ADR is a prerequisite step.

## Consequences

### Positive
- Single file to change when switching models for any process
- Cloud model parameters (context length, max tokens, timeout) are versioned alongside local model parameters
- Adding a new process-to-model assignment requires only a new key in `models.yaml` + a validator
- A/B testing is fully config-driven: change `captains_log_role` in `models.yaml` to switch models
- `ClaudeClient` becomes a pure API adapter, not a config reader

### Negative
- `ClaudeClient` callers must now pass `model_id` and `max_tokens` explicitly (breaking change for direct instantiation)
- The `entity_extraction_role: "claude"` magic string is removed; existing `models.yaml` files with that value need migration to a named cloud model entry
- `AGENT_CLAUDE_MODEL`, `AGENT_CLAUDE_MAX_TOKENS` env vars in existing `.env` files become dead config

### Migration
- `.env` files: remove `AGENT_CLAUDE_MODEL`, `AGENT_CLAUDE_MAX_TOKENS`, rename `AGENT_CLAUDE_WEEKLY_BUDGET_USD` → `AGENT_CLOUD_WEEKLY_BUDGET_USD`
- `config/models.yaml`: add `claude_sonnet` entry, update `entity_extraction_role` from `"claude"` to `"claude_sonnet"`, add `captains_log_role` and `insights_role`
- No database migration required

## Acceptance Criteria

- [ ] `config/models.yaml` contains at least one cloud model entry with `provider: "anthropic"`
- [ ] `settings.py` has no `claude_model` or `claude_max_tokens` fields
- [ ] `ClaudeClient` does not import or read `settings.claude_model`
- [ ] Entity extraction correctly dispatches to Claude when `model_def.provider == "anthropic"`
- [ ] `captains_log_role` and `insights_role` are recognized by `ModelConfig`
- [ ] Tests pass without modification (no test references to removed settings fields)
- [ ] `.env.example` is updated to reflect the new structure

## Related

- ADR-0007: Unified Configuration Management (established `settings.py` as config authority)
- ADR-0003: Model Stack (initial model selection decisions)
- ADR-0029: Inference Concurrency Control (uses `ModelDefinition.provider_type`)
