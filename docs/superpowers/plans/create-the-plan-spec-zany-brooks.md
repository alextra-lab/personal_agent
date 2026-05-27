# ADR-0077: Artifact Draft — Sub-Agent HTML Generation

## Context

When the primary model (Qwen3.6-35B-A3B, thinking mode) creates an HTML artifact, it currently generates the entire HTML string inside `artifact_write`'s `content` parameter. This wastes the 32K thinking token budget on HTML serialization — a task that doesn't benefit from extended reasoning. A typical rich HTML artifact consumes 8-16K output tokens from the primary model.

The fix: separate **planning** (thinking primary) from **HTML generation** (instruct sub-agent). The primary calls `artifact_draft` with a structured plan + data. The executor spawns the sub-agent to generate HTML, then chains internally to `artifact_write_executor`.

Additionally, `config/profiles/local.yaml` has stale model references — `sub_agent_model: qwen3-8b` and `primary_model: qwen3.5-35b-a3b` don't match the current `models.yaml` keys (`sub_agent` and `primary`). These are prerequisite fixes.

The sub-agent `context_length` in `models.yaml` is currently 16384, which is tight for HTML generation (~500 tok system prompt + ~2K tok plan + ~8-16K tok HTML output). Bumping to 32768 (what it was before it was reduced).

---

## Decisions

### D1 — Plan/Generate separation
The primary model calls `artifact_draft(slug, title, summary, plan, tags)`. A sub-agent generates HTML from the plan. The primary model never writes raw HTML for artifacts.

### D2 — Profile-driven model selection (no fallback)
`get_llm_client(role_name="sub_agent")` resolves via the active profile. Local → Qwen3.6 instruct. Cloud → claude_haiku. No fallback, no per-call selection.

### D3 — Direct executor chaining with full observability
`artifact_draft_executor` calls `artifact_write_executor` directly (Python function call). No governance re-check on the inner call — the outer `artifact_draft` tool call already passed governance. `ctx` (TraceContext) passes through for identity and joinability.

**Compensating telemetry**: Since bypassing `run_sub_agent()` and `ToolExecutionLayer` loses their structured events, `artifact_draft_executor` emits its own span-correlated events that maintain the ADR-0074 joinability contract (see D8).

### D4 — HTML-only scope
`artifact_draft` always produces `text/html; charset=utf-8`. Other content types (CSV, JSON, markdown, images) use `artifact_write` directly — no plan/generate split needed.

### D5 — Self-contained HTML (no CDN)
The sub-agent generates fully self-contained HTML with an inline `<style>` design system. No `<script>` tags, no external CDN. Compliant with ADR-0070 D7 sandbox posture (`sandbox=""`).

The system prompt specifies the concrete CSS approach:
- A `<style>` block in `<head>` defining CSS custom properties (`--color-primary`, `--color-secondary`, `--color-accent`, `--color-bg`, `--color-surface`, `--color-text`, `--spacing-*`, `--font-*`)
- Utility classes for layout (`flex`, `grid`, `gap-*`, `p-*`, `m-*`, `text-center`, `font-bold`, `rounded`, `shadow`)
- Semantic HTML5 elements (`header`, `main`, `section`, `article`, `footer`)
- Responsive media queries for mobile/tablet/desktop

### D6 — No retry on sub-agent failure
If the sub-agent produces empty/invalid HTML, the tool call fails with `ToolExecutionError`. The primary model sees the error and can retry with a refined plan or fall back to `artifact_write` directly.

### D7 — Token budget override
The sub-agent call uses `max_tokens=16384` (per-call override on `respond()`), not the model config's default 2048. The model config `context_length` bumps from 16384 → 32768 to accommodate the full prompt + output.

### D8 — Joinability and observability (ADR-0074)

The `artifact_draft_executor` must maintain full trace joinability. The `ctx` parameter passed to tool executors is a `TraceContext` carrying `trace_id`, `session_id`, `user_id`, `profile`, and `kind`. Every log and LLM call must thread these through.

**Span structure**: The executor creates a child span via `ctx.new_span()` for the sub-agent inference call. This produces a `(child_trace_ctx, span_id)` tuple where the child context inherits `trace_id`, `session_id`, `user_id`, `profile`, and `kind` from the parent.

**Required structured events** (all include `trace_id`, `session_id`, `span_id`):

| Event | When | Key fields |
|-------|------|------------|
| `artifact_draft_start` | Entry | `slug`, `plan_length`, `trace_id`, `session_id` |
| `artifact_draft_sub_agent_start` | Before `respond()` | `task_id`, `model_role`, `max_tokens`, `timeout_s`, `trace_id`, `session_id`, `span_id` |
| `artifact_draft_sub_agent_complete` | After `respond()` | `task_id`, `success`, `duration_ms`, `html_length`, `trace_id`, `session_id`, `span_id` |
| `artifact_draft_html_validated` | After validation | `html_length`, `has_doctype`, `trace_id`, `session_id` |
| `artifact_draft_completed` | After write chain | `artifact_id`, `slug`, `size_bytes`, `sub_agent_duration_ms`, `trace_id`, `session_id` |

**TraceContext threading to `respond()`**: The `respond()` call receives `trace_ctx=child_trace_ctx` (the child span context). Both `LocalLLMClient` and `LiteLLMClient` use this for cost attribution (`litellm_client.py:262,421`) and telemetry correlation. This is non-negotiable — without it, the sub-agent inference is an orphaned cost event in ES.

**Timeout enforcement**: `ToolExecutionLayer.execute_tool()` does NOT enforce `timeout_seconds` from the tool definition. The executor must enforce its own timeout via `asyncio.wait_for()` around the `respond()` call, AND pass `timeout_s=120` to `respond()` so the LLM client's internal timeout aligns.

### D9 — Input and output validation

**Input guard**: `plan` parameter is capped at 8000 characters (~2K tokens). Larger plans risk exceeding the 32K context window when combined with system prompt + output. Raises `ToolExecutionError` with guidance to trim.

**Output validation** (before calling `artifact_write_executor`):
1. Non-empty and ≥50 characters (not just 20)
2. Contains `<!DOCTYPE html>` (case-insensitive)
3. Contains `</html>` closing tag
4. No `<script` tags (case-insensitive regex)
5. No inline event handlers (`on\w+=` pattern)

Violations raise `ToolExecutionError` with specific failure reason. This is the enforcement layer for ADR-0070 D7.

---

## Changes

### Phase 1: Config fixes (prerequisite)

**`config/profiles/local.yaml`** — Fix stale model references:
```yaml
primary_model: primary         # was: qwen3.5-35b-a3b (wrong key)
sub_agent_model: sub_agent     # was: qwen3-8b (wrong key)
```

**`config/models.yaml`** — Bump sub_agent context window:
```yaml
context_length: 32768   # was: 16384 — HTML generation needs ~18K+ total
```

### Phase 2: Core implementation

**`src/personal_agent/tools/artifact_tools.py`** — Add these components:

1. **`_HTML_GENERATION_SYSTEM_PROMPT`** (module-level constant) — Instructs the sub-agent to:
   - Output ONLY the HTML document (no explanation, no markdown fences)
   - Start with `<!DOCTYPE html>`, end with `</html>`
   - Define a `<style>` design system: CSS custom properties for colors (`--color-primary` through `--color-text`), spacing scale (`--spacing-1` through `--spacing-8`), typography scale, and utility classes for layout (flex, grid, gap, padding, margin, text-align, font-weight, rounded, shadow)
   - No external dependencies (no CDN links, no `<script>` tags)
   - No inline event handlers (`onclick`, `onload`, `onerror`, etc.)
   - Semantic HTML5 elements, responsive media queries, accessibility (ARIA, alt text, heading hierarchy)
   - Aim for under 200KB

2. **`_validate_html_output(html: str) -> None`** helper — Raises `ToolExecutionError` if output fails any D9 check (length, doctype, closing tag, script tags, event handlers).

3. **`artifact_draft_tool`** (ToolDefinition):
   - `name`: `"artifact_draft"`
   - `category`: `"artifact_write"`
   - Parameters: `slug` (required), `title` (required), `summary` (required), `plan` (required, max ~8000 chars), `tags` (optional)
   - `timeout_seconds`: 120 (sub-agent inference + R2 + Postgres)
   - `rate_limit_per_hour`: 20
   - No `content_type` param (always HTML)
   - No `content` param (sub-agent generates it)

4. **`artifact_draft_executor`** async function — full execution flow:
   ```
   1. Validate plan non-empty and ≤ 8000 chars
   2. Extract trace_id, session_id, user_id from ctx (TraceContext)
   3. Generate task_id = f"draft-{uuid4().hex[:12]}"
   4. Create child span: child_ctx, span_id = ctx.new_span()
   5. Log artifact_draft_start (trace_id, session_id, slug, plan_length)
   6. Acquire sub-agent client: get_llm_client(role_name="sub_agent")
   7. Build messages: [system: _HTML_GENERATION_SYSTEM_PROMPT, user: title+summary+plan]
   8. Log artifact_draft_sub_agent_start (task_id, span_id, trace_id, session_id)
   9. Call respond(role=SUB_AGENT, max_tokens=16384, trace_ctx=child_ctx, timeout_s=120)
      wrapped in asyncio.wait_for(timeout=120)
   10. Log artifact_draft_sub_agent_complete (task_id, span_id, duration_ms, success)
   11. Extract .content from LLMResponse dict
   12. Strip markdown code fences (defensive)
   13. Call _validate_html_output(html) — enforces D9 checks
   14. Log artifact_draft_html_validated (html_length, trace_id, session_id)
   15. Chain to artifact_write_executor(slug, "text/html; charset=utf-8", html, title, summary, tags, ctx)
   16. Augment result with generation_method="draft", sub_agent_duration_ms, task_id
   17. Log artifact_draft_completed (artifact_id, slug, size_bytes, trace_id, session_id)
   18. Return result
   ```

   Uses `respond()` directly (not `run_sub_agent()`) because `run_sub_agent` wraps `str()` around the full `LLMResponse` dict — we need clean `.content` extraction. The D8 structured events compensate for the observability that `run_sub_agent` would otherwise provide.

**`src/personal_agent/tools/__init__.py`** — Register inside the R2-gated block (lines 115-123):
```python
registry.register(artifact_draft_tool, artifact_draft_executor)
```

**`config/governance/tools.yaml`** — Add after `artifact_write` entry (~line 1229):
```yaml
artifact_draft:
    category: "artifact_write"
    allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED"]
    risk_level: "medium"
    requires_approval: false
    requires_approval_in_modes: ["ALERT", "DEGRADED"]
    timeout_seconds: 120
    rate_limit_per_hour: 20
    loop_max_per_signature: 3
    loop_max_consecutive: 3
```

### Phase 3: ADR

**`docs/architecture_decisions/ADR-0077-artifact-draft-subagent-generation.md`** — Record decisions D1-D9 above. Status: Proposed. Related: ADR-0070, ADR-0033, ADR-0044, ADR-0069, ADR-0074.

### Phase 4: Tests

**`tests/personal_agent/tools/test_artifact_tools.py`** — Extend with:

New fixture: `_FakeSubAgentClient` — mock `respond()` returning controlled `LLMResponse` dict with `.content` field. Captures all kwargs for assertion. Monkeypatch `get_llm_client` in `artifact_tools` module.

**Happy paths:**
- `test_artifact_draft_returns_expected_keys` — result has all artifact_write keys + `generation_method="draft"` + `sub_agent_duration_ms` + `task_id`
- `test_artifact_draft_chains_to_artifact_write` — R2 store receives the sub-agent HTML, Postgres INSERT fires
- `test_artifact_draft_content_type_is_always_html` — content_type is `text/html; charset=utf-8`
- `test_artifact_draft_strips_markdown_fences` — fences stripped before write
- `test_artifact_draft_passes_tags_through` — tags propagate

**Observability (D8):**
- `test_artifact_draft_passes_trace_ctx_to_respond` — assert `respond()` receives `trace_ctx` with matching `trace_id` and `session_id`; assert it's a child span (has `parent_span_id`)
- `test_artifact_draft_passes_timeout_to_respond` — assert `timeout_s=120` reaches `respond()`
- `test_artifact_draft_calls_respond_with_correct_max_tokens` — verify 16384

**Input validation (D9):**
- `test_artifact_draft_empty_plan_raises` — ToolExecutionError
- `test_artifact_draft_oversized_plan_raises` — plan >8000 chars → ToolExecutionError
- `test_artifact_draft_requires_user_id` — ToolExecutionError

**Output validation (D9):**
- `test_artifact_draft_rejects_missing_doctype` — sub-agent returns HTML without `<!DOCTYPE html>` → ToolExecutionError
- `test_artifact_draft_rejects_script_tags` — sub-agent returns `<script>...</script>` → ToolExecutionError
- `test_artifact_draft_rejects_event_handlers` — sub-agent returns `onclick="..."` → ToolExecutionError
- `test_artifact_draft_subagent_empty_html_raises` — ToolExecutionError
- `test_artifact_draft_subagent_timeout_raises` — ToolExecutionError with timeout message
- `test_artifact_draft_subagent_exception_raises` — ToolExecutionError with fallback guidance

**Static:**
- `test_system_prompt_prohibits_scripts` — assertion on `_HTML_GENERATION_SYSTEM_PROMPT` content
- `test_artifact_draft_registered_only_when_r2_configured` — tool absent from registry without R2 env vars

---

## Reusable existing code

| What | Where | How it's reused |
|------|-------|-----------------|
| `artifact_write_executor` | `tools/artifact_tools.py:300` | Called directly from `artifact_draft_executor` |
| `get_llm_client` | `llm_client/factory.py:56` | Obtains profile-aware sub-agent client |
| `resolve_model_key` | `config/profile.py:75` | Called internally by `get_llm_client` |
| `TraceContext.new_span()` | `telemetry/trace.py:82` | Creates child span for sub-agent call |
| `_resolve_user_id` / `_resolve_session_id` | `tools/artifact_tools.py:238-254` | Already called by `artifact_write_executor` in the chain |
| `_FakeStore` / `_FakeSession` / `_install_fakes` | `tests/.../test_artifact_tools.py:44-125` | Reused for all draft tests |
| `_ctx()` helper | `tests/.../test_artifact_tools.py:31` | Reused for all draft tests |
| `ToolDefinition` / `ToolParameter` | `tools/types.py` | Tool definition pattern |

---

## Verification

1. **Unit tests**: `make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py`
2. **Type check**: `make mypy`
3. **Lint**: `make ruff-check && make ruff-format`
4. **Governance loads**: `python -c "from personal_agent.governance import load_governance_config; load_governance_config()"` — no parse errors
5. **Tool registered**: `python -c "from personal_agent.tools import get_default_registry; print('artifact_draft' in [t.name for t in get_default_registry().list_tools()])"` — True (requires R2 env vars)
6. **Profile fix**: `python -c "from personal_agent.config.profile import load_profile; p = load_profile('local'); print(p.primary_model, p.sub_agent_model)"` — prints `primary sub_agent`
7. **Joinability check**: After a test run, grep ES logs for `artifact_draft_sub_agent_start` and verify `trace_id` + `session_id` + `span_id` are all present and that `trace_id` matches the parent `artifact_draft_start` event
8. **Manual smoke test** (post-deploy): Ask the agent to create an HTML comparison table. Verify it calls `artifact_draft`, the sub-agent generates HTML, artifact is accessible at public URL, HTML contains no `<script>` tags, and ES shows the full event chain with joined trace_id/session_id
