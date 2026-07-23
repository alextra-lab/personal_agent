# ADR-0077: Artifact Draft — Sub-Agent HTML Generation

**Status:** Implemented — 2026-05-27 (PR #84, merged; `tools/artifact_tools.py` + tests + governance/model config). *Status line corrected 2026-07-23: it had remained "Proposed" since authoring while the index already recorded the merge.*
**Date:** 2026-05-27
**Supersedes:** —
**Related:** ADR-0070 (Output Channel Model), ADR-0033 (Model Taxonomy), ADR-0044 (Execution Profiles), ADR-0069 (R2 Artifact Substrate), ADR-0074 (End-to-End Traceability)

## Context

When the primary model (Qwen3.6-35B-A3B, thinking mode, 32K thinking budget) creates an HTML artifact, it generates the entire HTML string inside `artifact_write`'s `content` parameter. This wastes thinking tokens on HTML serialization — a task that doesn't benefit from extended reasoning. A typical rich HTML artifact consumes 8-16K output tokens from the primary model.

HTML generation is a completion task, not a reasoning task. The model needs HTML/CSS fluency and clean markup — that's a capability frontier, not a reasoning frontier. The planning phase (deciding what to show, which data to include, how to structure it) benefits from thinking; the serialization phase does not.

## Decision

### D1 — Plan/Generate separation

The primary model calls `artifact_draft(slug, title, summary, plan, tags)` instead of generating HTML directly. A sub-agent (instruct mode, thinking disabled) generates the HTML from the plan. The primary model never writes raw HTML for artifacts.

### D2 — Profile-driven model selection

`get_llm_client(role_name="sub_agent")` resolves via the active ExecutionProfile (ADR-0044). Local profile uses Qwen3.6-35B-A3B instruct. Cloud profile uses claude_haiku. No fallback logic, no per-call model selection.

### D3 — Direct executor chaining with compensating telemetry

`artifact_draft_executor` calls `artifact_write_executor` directly (Python function call). No governance re-check on the inner call — the outer `artifact_draft` tool call already passed governance. `ctx` (TraceContext) passes through for identity and joinability.

Since bypassing `run_sub_agent()` and `ToolExecutionLayer` loses their structured events, `artifact_draft_executor` emits its own span-correlated events (see D8).

### D4 — HTML-only scope

`artifact_draft` always produces `text/html; charset=utf-8`. Other content types (CSV, JSON, markdown, images) use `artifact_write` directly — they don't benefit from plan/generate separation.

### D5 — Self-contained HTML

The sub-agent generates fully self-contained HTML with an inline `<style>` design system: CSS custom properties for colors, spacing, typography, and utility classes for layout. No `<script>` tags, no external CDN. Compliant with ADR-0070 D7 sandbox posture (`sandbox=""`).

### D6 — No retry on sub-agent failure

If the sub-agent produces empty or invalid HTML, the tool call fails with `ToolExecutionError`. The primary model sees the error and can retry with a refined plan or fall back to `artifact_write` directly.

### D7 — Token budget override

The sub-agent call uses `max_tokens=16384` (per-call override on `respond()`). The model config `context_length` bumps from 16384 to 32768 to accommodate system prompt + plan + output.

### D8 — Joinability and observability (ADR-0074)

The executor creates a child span via `ctx.new_span()` for the sub-agent inference call. All structured log events include `trace_id`, `session_id`, and `span_id`:

| Event | When |
|-------|------|
| `artifact_draft_start` | Entry |
| `artifact_draft_sub_agent_start` | Before `respond()` |
| `artifact_draft_sub_agent_complete` | After `respond()` (success or failure) |
| `artifact_draft_html_validated` | After validation passes |
| `artifact_draft_completed` | After write chain completes |

`trace_ctx=child_ctx` is threaded to `respond()` for cost attribution. `timeout_s` is passed explicitly since `ToolExecutionLayer` does not enforce tool definition timeouts.

### D9 — Input and output validation

Input: `plan` capped at 8000 characters to prevent context window overflow.

Output validation before writing (ADR-0070 D7 enforcement):
- Non-empty and >= 50 characters
- Contains `<!DOCTYPE html>`
- Contains `</html>` closing tag
- No `<script` tags
- No inline event handlers (`on*=`)

## Consequences

- Primary model output tokens for HTML artifacts drop from ~12K to ~200 (plan only). Thinking budget is preserved for reasoning.
- Sub-agent inference adds ~15-60s latency (local) or ~3-5s (cloud). Total artifact creation is slower but cheaper in thinking tokens.
- `artifact_write` remains available for pre-rendered content and non-HTML types.
- HTML quality depends on the sub-agent's capability. The system prompt maximizes quality with structural guidance, but this is ultimately model-constrained.
