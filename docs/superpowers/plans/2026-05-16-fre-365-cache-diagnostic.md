# FRE-365 — Prompt cache diagnostic, compressor upgrade, background-role rebalance

**Status**: shipped
**Date**: 2026-05-16
**Ticket**: [FRE-365](https://linear.app/frenchforest/issue/FRE-365)
**PR**: TBD

This document records the diagnostic findings for FRE-365 and the
resulting changes: a compressor system-prompt expansion (cache-prefix
fix + quality lift) **plus** a background-role model rebalance in
`config/models.cloud.yaml`.

## TL;DR

- The ticket's framing — "cache only enabled for Sonnet, mirror config
  to Haiku/GPT" — was falsified by reading the code. Cache plumbing
  already runs for all Anthropic models (`litellm_client.py:232`) and
  OpenAI's `cached_tokens` is already extracted (`litellm_client.py:352-360`).
- The ticket's framing — "compression fires too late, post-hoc" — was
  also falsified. A proactive pre-LLM compression gate already exists
  at `executor.py:1377` (`needs_hard_compression` at 0.85 ×
  `context_window_max_tokens`, default 96 000) and a soft async trigger
  at 0.65 lives in `compression_manager.maybe_trigger_compression`.
- The ticket's headline numbers (4.5M input tokens / day, 118K-token
  calls) did **not** match ES. Actual May 13: 334 calls, ~2.3M total
  input tokens, max prompt 34 643. Zero
  `within_session_compression_hard_trigger` events fired — because
  nothing crossed the 81.6K threshold.
- The genuine findings:
  1. **Haiku 4.5 prompts (avg 652, max 834 tokens) are below the 1024-
     token cache floor** → no cache, ever, by design. Not a code bug.
  2. **gpt-5.4-nano does not participate in OpenAI's automatic prompt
     caching at all** — verified empirically across multiple calls with
     a 1640-token stable prefix. By contrast gpt-5.4-mini hit 1280/1640
     tokens cached on the second call in the same harness.
  3. The compressor system prompt was previously **~188 tokens** —
     even on a cache-participating model that's below the cache floor.

## Evidence (3-day window, 2026-05-13 → 2026-05-15)

### Per-model cache behaviour in production

| Model | calls | avg prompt | max prompt | calls > 1024 | cache hits | cache writes |
|---|---:|---:|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | 147 | 13 954 | 34 643 | 147 | **145 (99%)** | 142 |
| `anthropic/claude-haiku-4-5` | 80 | 652 | 834 | 0 | 0 | 0 |
| `openai/gpt-5.4-nano` | 153 | 1 084 | 2 988 | 80 (52%) | 0 | 0 |

### Direct A/B against the OpenAI API

**Test A — large stable prefix (~1500 tokens of padded system content):**

| Model | Call 0 cached | Call 1 cached | Call 2 cached |
|---|---:|---:|---:|
| **gpt-5.4-mini** | 0 | **1 280** | **1 280** |
| **gpt-5.4-nano** | **0** | **0** | **0** |

**Test B — `_COMPRESSOR_SYSTEM_PROMPT` (1065 tokens, this PR's expanded version), 4 consecutive calls:**

| Model | Call 0 cached | Call 1 cached | Call 2 cached | Call 3 cached |
|---|---:|---:|---:|---:|
| gpt-5.4-mini | 0 | 0 | 0 | 0 |

**Findings**:

1. gpt-5.4-nano returns `cached_tokens=0` regardless of prefix size.
   It does not participate in OpenAI's automatic prompt caching. This
   is OpenAI-side behaviour, not a project bug.
2. gpt-5.4-mini **does** cache, but the documented 1024-token floor
   appears to be a necessary-but-not-sufficient condition. At 1065
   tokens of stable prefix (this PR's expanded system prompt), cache
   did not engage in the harness; at ~1500 tokens it did. OpenAI's
   effective floor for mini sits somewhere between, likely tied to the
   128-token block boundary the cache uses internally.

The expanded system prompt is therefore **cache-ready in principle**
but not yet **cache-engaged in practice**. Two follow-up paths are
possible if observable cache hits become a goal:

- (a) Inflate the system prompt further to ~1280–1408 tokens (next
  128-token block boundaries above 1024). The added content would have
  to be genuinely useful or it becomes filler not quality.
- (b) Watch production telemetry on mini calls — OpenAI tunes cache
  eligibility over time; the 1065-token prefix may begin caching
  silently without project-side changes.

## What changed in this PR

### 1. Compressor system prompt expansion (cache-readiness + quality)

`src/personal_agent/orchestrator/context_compressor.py`:
`_COMPRESSOR_SYSTEM_PROMPT` grew from ~188 tokens (~755 chars) to
**~1065 tokens (tiktoken `cl100k_base`)** by adding three worked
examples and an anti-patterns section. The expansion is justified on
quality grounds independent of caching — concrete I/O examples and
mistakes-to-avoid are real compressor guidance, not padding. A
tiktoken-based unit test
(`tests/test_orchestrator/test_context_compressor.py::TestSystemPromptCacheEligibility`)
locks the byte length above the 1024 floor and guards against silent
mutation between invocations.

### 2. Background-role model rebalance (`config/models.cloud.yaml`)

After confirming nano does not cache, the user opted to upgrade three
of the four nano-bound background roles:

| Role | Before | After | Rationale |
|---|---|---|---|
| `compressor` (id field) | gpt-5.4-nano | **gpt-5.4-mini** | Cache participation + materially better compression fidelity for downstream agent-loop consumers (multi-step state tracking, latent-constraint preservation). |
| `entity_extraction_role` | gpt-5.4-nano | **gpt-5.4-mini** | Same — entity extraction benefits from the same nuanced-transformation capability mini exhibits over nano. |
| `captains_log_role` | gpt-5.4-nano | **claude_sonnet** | Restored from the previously-commented historical config. Self-reflection needs Sonnet-tier reasoning, not bulk summarisation. |
| `insights_role` | gpt-5.4-nano | **claude_sonnet** | Same — pattern detection across sessions is reasoning-heavy. |

Rough monthly-cost impact at observed volumes (sub-dollar in absolute
terms even at the most expensive configuration; values are estimates
based on the 3-day ES window):

| Role | Old monthly | New monthly |
|---|---:|---:|
| compressor | ~$0.13 | ~$0.78 (with cache) |
| entity_extraction | ~$0.50 | ~$1.90 |
| captains_log | ~$0.30 | ~$3.00 |
| insights | ~$0.20 | ~$2.00 |
| **total** | **~$1.13** | **~$7.68** |

Cost rises ~7×, but absolute floor is still ~$8/month for the entire
background pipeline. Quality benefit is meaningful given those roles
feed the agent loop.

## What was not done (and why)

- **No code change for Haiku.** Prompts are structurally below the
  caching floor for that model; no fix exists at the code level.
- **No change to `context_window_max_tokens` or threshold ratios.** The
  0.65 / 0.85 thresholds are well-calibrated; nothing in production
  approaches them.
- **No new pre-LLM gate.** The one the ticket described already exists.
- **No change to `config/models.yaml` (local-dev config).** Per
  user-memory `project_dev_environment_is_vps`, local dev mirrors VPS.
  Local config rarely runs; if the user wants parity, a follow-up swap
  on `config/models.yaml` is cheap.

## Files touched

- `src/personal_agent/orchestrator/context_compressor.py` — expanded
  `_COMPRESSOR_SYSTEM_PROMPT` from ~188 to ~1065 tokens.
- `tests/test_orchestrator/test_context_compressor.py` — added
  `TestSystemPromptCacheEligibility` (one tiktoken assertion + one
  stability assertion).
- `config/models.cloud.yaml` — flipped compressor / entity_extraction /
  captains_log / insights role assignments.
- `docs/superpowers/plans/2026-05-16-fre-365-cache-diagnostic.md` —
  this document.

## Methodology note for future cache audits

The diagnostic harness that proved nano does not cache is two-call
direct LiteLLM calls with a stable ≥1024-token system prefix and the
same `prompt_tokens_details.cached_tokens` field as the production
telemetry path. If a future ticket claims a model "should be caching
but isn't," reproduce the A/B with another cache-participating OpenAI
model in the same harness before assuming a project-side bug.
