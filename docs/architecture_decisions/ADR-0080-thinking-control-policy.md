# ADR-0080: Model-Aware Thinking-Control Policy

**Status:** Implemented (FRE-417, PR #107; the `/no_think` gate shipped — deferred items, suffix retirement + cloud thinking, tracked in FRE-418)
**Date:** 2026-05-29
**Issue:** FRE-417
**Related:** ADR-0023 (thinking budget / disable_thinking), ADR-0079 (server-authoritative profile), ADR-0044 (execution profiles)

## Context

"Thinking" (chain-of-thought) for the local Qwen models is controlled by **two independent mechanisms** that have accumulated over time:

1. **Server-side `disable_thinking`** → injects `chat_template_kwargs {enable_thinking: False}` via `extra_body` (`llm_client/models.py`, `adapters.py:645`). Set per model role in `config/models.yaml` (e.g. `sub_agent.disable_thinking: true`).
2. **The `/no_think` prompt suffix** → `llm_no_think_suffix` appended to the last user message on tool-flow turns (`executor.py` `_append_no_think_to_last_user_message`), gated by `llm_append_no_think_to_tool_prompts` (default `True`).

The suffix exists because **LM Studio ignored `extra_body`**, so `chat_template_kwargs` had no effect and the prompt token was the only working control (comment at `executor.py:2204-2205`). Two facts have since changed:

- The SLM server is now **MLX-based**, not LM Studio.
- The primary model evolved **Qwen3.5 → Qwen3.6**.

Two problems surfaced (FRE-417, from the 2026-05-29 screenshot investigation):

- **Profile-blind injection.** The suffix was gated only on `tools` present + the global flag — never on the active model. On the **cloud** path it was appended to **Sonnet** prompts, where `/no_think` is meaningless noise (prompt pollution + a few wasted tokens).
- **Misattribution.** `/no_think` was informally read as a *routing* signal ("its presence means local"). It is **not** — it is a thinking-control token with no bearing on local/cloud routing (which is decided solely by the profile; see ADR-0079).

Current per-role thinking state: `primary` (orchestrator Qwen3.6) thinking **on** (`thinking_budget_tokens: 32768`); `sub_agent` (Qwen3.6 instruct) `disable_thinking: true`; cloud `claude_sonnet` — **no thinking control configured at all**.

## Decision

1. **Gate `/no_think` to Qwen-family models (shipped).** `_append_no_think_to_last_user_message` / `_append_no_think_synthesis_nudge` now no-op unless the active primary model id contains `qwen` (`_no_think_applies()`). Cloud/Sonnet prompts are no longer polluted. Default-on when the model can't be resolved (preserves prior local behaviour).

2. **`/no_think` is a thinking control, not a routing signal.** Recorded here so it is not conflated again. Routing = profile (ADR-0079).

3. **Server-side `disable_thinking` is the preferred mechanism going forward.** Now that the SLM server is MLX (which honours `chat_template_kwargs`), the prompt-suffix workaround is, in principle, redundant for the primary model. We **keep** the suffix for now (gated to Qwen) because it is verified working and removing it is a separate, testable change — but new thinking control should be expressed via `disable_thinking` / `thinking_budget_tokens` in `models.yaml`, not new prompt tokens.

4. **Cloud thinking is unconfigured — left as a deliberate gap, tracked.** Anthropic exposes extended thinking as a per-request parameter, not separate endpoints; the cloud path wires none today. Whether the cloud path should gain a per-role thinking budget (mirroring the local thinking/instruct split) is deferred to the delegation/optimization revisit (FRE-418).

## Consequences

**Positive**
- Cloud prompts are clean; no meaningless `/no_think` token on Sonnet.
- The two-mechanism history and the "not a routing signal" fact are documented.
- A clear direction (prefer server-side control) for future thinking changes.

**Negative / deferred**
- Two mechanisms still coexist; fully retiring the `/no_think` suffix (relying solely on MLX `chat_template_kwargs`) is **not** done here — it needs an isolated verification that MLX honours `enable_thinking: False` for the primary model under the agent loop. Tracked as a follow-up under FRE-418.
- Cloud thinking remains unconfigured (FRE-418).

## Alternatives considered

- **Remove the `/no_think` suffix entirely now**, relying on `disable_thinking`. Rejected for this ticket: the primary currently runs thinking **on** with a budget, not disabled; and removing the verified suffix without confirming MLX honours the server-side flag under load risks a silent regression. Gate-not-remove is the safe step.
- **Gate on `provider_type == "local"`** instead of the model id. Equivalent today (local == Qwen), but the model-id check is precise about *why* (`/no_think` is Qwen-specific) and survives a future non-Qwen local model.
