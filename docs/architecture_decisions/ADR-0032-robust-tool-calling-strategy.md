# ADR-0032: Robust Tool Calling Strategy Across Model Families

**Status**: Accepted
**Date**: 2026-03-13
**Deciders**: Architecture Team
**Supersedes**: Partially supersedes ADR-0008 (Hybrid Tool Calling Strategy)
**Related**: ADR-0008, ADR-0015 (Tool Call Performance), ADR-0031 (Model Config SSOT)

## Context

ADR-0008 introduced a hybrid tool calling strategy to support both native function calling (OpenAI `tool_calls`) and text-based tool calls (`[TOOL_REQUEST]...[END_TOOL_REQUEST]`). This was designed around DeepSeek-R1, which lacked native function calling.

As the model roster has grown (Qwen3.5, LFM/Liquid, Mistral, etc.), three fragility patterns emerged:

### Problem 1: LM Studio Chat Template Opacity

When `tools` are sent in the OpenAI API request, LM Studio applies the model's bundled Jinja2 chat template to render them into the prompt tokens the model actually sees. If the template lacks tool-aware rendering blocks (e.g. `{% if tools %}`), the tools array is **silently dropped** — the model never sees the tool definitions. The `supports_function_calling: true` config flag is a developer assertion, not a verified capability.

### Problem 2: Prompt Teaches Both Formats

`TOOL_USE_SYSTEM_PROMPT` instructs models to use native function calling *or* the `[TOOL_REQUEST]` text format. Models that support native calling sometimes generate the text format instead because the prompt taught them both, leading to inconsistent parsing.

### Problem 3: Error Message Poisoning

Failed tool calls append verbose error messages (`"Invalid arguments JSON: Expecting value..."`) to conversation history. Small models (4B-9B) overweight these negative signals and become reluctant to attempt tool calls on subsequent turns. The text-parser adds 4 regex strategies as patches for model-specific failures — a whack-a-mole pattern.

## Decision

### 1. ToolCallingStrategy Enum

Add an explicit `tool_calling_strategy` field to `ModelDefinition` in `models.py`:

- **`NATIVE`**: Pass tools in the OpenAI `tools` API parameter. Expect structured `tool_calls` in the response. Used for models with verified native tool support (e.g. Qwen3.5 via LM Studio).
- **`PROMPT_INJECTED`**: Do NOT pass `tools` in the API. Instead, render tool definitions as structured text in the system prompt. Parse tool invocations from free-text output via `tool_call_parser.py`. Used for models whose chat templates don't support tools.
- **`DISABLED`**: No tool calling at all (e.g. the router model).

### 2. Split System Prompts

Replace the single `TOOL_USE_SYSTEM_PROMPT` with two strategy-specific variants:

- `TOOL_USE_NATIVE_PROMPT`: Tells the model to use native `tool_calls` only. Does NOT teach the text fallback format.
- `TOOL_USE_PROMPT_INJECTED`: Teaches the `[TOOL_REQUEST]` text format. Includes rendered tool definitions inline.

### 3. Tool Prompt Renderer

New module `tool_prompt_renderer.py` converts OpenAI-format tool definitions into a compact text block suitable for system prompt injection, listing each tool's name, description, and parameters.

### 4. Strategy-Aware Client Filtering

`LocalLLMClient._do_request()` strips the `tools` API parameter for any model whose strategy is not `NATIVE`, as a safety net even if the orchestrator already handled it.

### 5. Error Message Depoisoning

- **Compress error messages**: Replace verbose errors with concise, neutral hints (e.g. `{"status": "retry", "hint": "..."}`). Full details stay in structured logs.
- **Context window eviction**: Old tool error messages from previous turns are evicted before general truncation, reducing stale negative signal.

## Consequences

### Positive

- Model swaps only require changing `tool_calling_strategy` in `models.yaml` — no code changes
- Native models no longer see confusing text-format instructions
- Error messages no longer dominate small model attention
- Clear extension point for future constrained decoding support

### Negative

- Two code paths for tool setup in the orchestrator (but cleanly separated by strategy)
- Prompt-injected path has lower reliability than native (inherent to text parsing)

## Files Changed

- `src/personal_agent/llm_client/models.py` — `ToolCallingStrategy` enum, new field
- `src/personal_agent/orchestrator/prompts.py` — Split prompts
- `src/personal_agent/orchestrator/executor.py` — Strategy-aware tool setup, error compression
- `src/personal_agent/llm_client/client.py` — Strategy-aware safety net
- `src/personal_agent/orchestrator/context_window.py` — Error eviction
- `config/models.yaml` — Explicit strategy per model

## Files Created

- `src/personal_agent/llm_client/tool_prompt_renderer.py` — Prompt-injection renderer

## Future Enhancements

1. **Startup capability probe**: Auto-detect tool calling support at first use
2. **Constrained decoding**: Grammar-guided generation via vLLM/Outlines
3. **Per-model telemetry**: Track tool call success rates to detect strategy misconfigs
