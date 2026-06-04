# FRE-484 — Forced-synthesis call omits `tools=` on Anthropic with tool history

**Linear:** FRE-484 (Approved, High, Tier-2:Sonnet) · Project: Turn Cost & Latency Optimization (artifact builds)
**Related:** FRE-475 (intra-turn compression A/B — blocked by this on the `zero task_failed` gate)
**Refs:** `src/personal_agent/orchestrator/executor.py:2275` (forced synthesis) · `:2546` (tools=None) · `src/personal_agent/llm_client/litellm_client.py:406`

## Problem

The forced-synthesis path in `step_llm_call` fires when `ctx.force_synthesis_from_limit`
is set (tool-iteration limit hit). It sets `is_synthesizing=True`, skips tool setup so
`tools` stays `None`, and calls `llm_client.respond(..., tools=None)`. LiteLLM only sends
`tools` when truthy (`litellm_client.py:406`). On the **Anthropic** path, a transcript that
already contains `tool_use`/`tool_result` blocks requires `tools=` even for a no-tool
synthesis call, so the call dies with:

```
litellm.UnsupportedParamsError: Anthropic doesn't support tool calling without tools= param specified
```

→ `model_call_error` → `task_failed`; the artifact never builds.

## Fix direction

On the Anthropic path **only**, when the transcript already contains tool blocks, keep the
tool list and pin `tool_choice="none"` so the model synthesizes from gathered results
instead of calling more tools. Every other path (local SLM, or no tool history) keeps the
existing drop-tools behavior unchanged — the local path currently works and the bug is
Anthropic-specific.

**Provider detection:** `getattr(llm_client, "provider", None) == "anthropic"`.
`LiteLLMClient` sets `self.provider`; `LocalLLMClient` has no such attribute (→ `None`),
so this cleanly identifies the failing path without `isinstance`.

`respond()` already accepts `tool_choice` on both `LiteLLMClient` (litellm_client.py:318)
and `LocalLLMClient` (client.py:136). The executor's call site at 2546 currently omits it.

## Design — pure helper + small executor wiring

Extract the decision into two pure, unit-testable module-level helpers in `executor.py`.

### Helper 1 — `_transcript_has_tool_blocks`

```python
def _transcript_has_tool_blocks(messages: Sequence[Mapping[str, Any]]) -> bool:
    """Return True if the transcript already contains tool_use/tool_result blocks.

    Anthropic requires ``tools=`` on any request whose message history references
    tools (assistant ``tool_calls`` or ``role="tool"`` results), even for a no-tool
    synthesis call (FRE-484).

    Args:
        messages: Conversation messages in OpenAI format.

    Returns:
        True if any message carries assistant ``tool_calls`` or is a tool result.
    """
    for msg in messages:
        if msg.get("role") == "tool" or msg.get("tool_calls"):
            return True
    return False
```

### Helper 2 — `_forced_synthesis_tool_overrides`

> **Codex review fix (gap #4):** the first draft returned `(None, None)` when
> `tool_defs` was empty, which would still trip the LiteLLM raise on the
> Anthropic+tool-history path. The helper now falls back to a single placeholder
> tool when `tool_defs` is empty. Codex also confirmed (#3) that Anthropic does
> **not** require exact historical tool-name coverage — any non-empty `tools=`
> satisfies the constraint (LiteLLM's own `modify_params` fallback injects one
> dummy tool), so re-sending the full mode `tool_defs` (preferred for prompt-cache
> continuity) or a placeholder both work.

```python
# FRE-484: minimal placeholder so Anthropic accepts a forced-synthesis call whose
# history references tools, when the active mode currently exposes no tool defs.
# Never invoked — tool_choice is pinned to "none".
_SYNTHESIS_PLACEHOLDER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "noop",
        "description": (
            "Placeholder so Anthropic accepts a no-tool synthesis call whose "
            "history references tools. Never invoked (tool_choice is 'none')."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


def _forced_synthesis_tool_overrides(
    *,
    provider: str | None,
    messages: Sequence[Mapping[str, Any]],
    tool_defs: Sequence[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Resolve ``(tools, tool_choice)`` for a forced-synthesis model call.

    The forced-synthesis path normally drops ``tools=`` so the model answers from
    gathered results. On Anthropic, a transcript that already contains tool blocks
    makes LiteLLM reject the call with ``UnsupportedParamsError`` when ``tools=`` is
    absent (FRE-484). For that case only, keep a non-empty tool list and pin
    ``tool_choice="none"`` so the model synthesizes instead of calling more tools.
    Prefer the real mode ``tool_defs`` (best prompt-cache continuity); fall back to
    a single placeholder tool when none are available so the call still succeeds.

    Args:
        provider: Cloud provider name; ``"anthropic"`` triggers the workaround.
            ``None`` for the local SLM path.
        messages: Current conversation messages (OpenAI format).
        tool_defs: Tool definitions for the active mode, or ``None``.

    Returns:
        ``(tools, tool_choice)``. Every path except Anthropic-with-tool-history
        returns ``(None, None)`` — identical to the prior drop-tools behavior.
    """
    if provider == "anthropic" and _transcript_has_tool_blocks(messages):
        tools = list(tool_defs) if tool_defs else [dict(_SYNTHESIS_PLACEHOLDER_TOOL)]
        return tools, "none"
    return None, None
```

### Executor wiring (in `step_llm_call`, after the tools if/else block ~line 2353)

```python
# FRE-484: Anthropic rejects a synthesis call whose history already contains
# tool blocks unless tools= is present. Keep the tool list and pin
# tool_choice="none" so synthesis still happens. No-op on every other path.
tool_choice: str | dict[str, Any] | None = None
if is_synthesizing:
    _provider = getattr(llm_client, "provider", None)
    _synthesis_tool_defs = (
        get_default_registry().get_tool_definitions_for_llm(mode=ctx.mode)
        if _provider == "anthropic"
        else None
    )
    tools, tool_choice = _forced_synthesis_tool_overrides(
        provider=_provider,
        messages=ctx.messages,
        tool_defs=_synthesis_tool_defs,
    )
    if tools:
        log.info(
            "force_synthesis_tools_retained",
            trace_id=ctx.trace_id,
            provider=_provider,
            tool_count=len(tools),
        )
```

### Call-site change (line 2546)

```python
response = await llm_client.respond(
    role=model_role,
    messages=request_messages,
    system_prompt=system_prompt,
    tools=tools if tools else None,
    tool_choice=tool_choice,          # ← added (None on every non-Anthropic-synthesis path)
    trace_ctx=span_ctx,
    previous_response_id=ctx.last_response_id,
    max_retries=max_retries_override,
    priority=InferencePriority.USER_FACING,
    prompt_identity=_prompt_identity,
)
```

### Imports

Add to `executor.py` top: `from collections.abc import Mapping, Sequence`.

## Steps (atomic)

1. **Test first** — `tests/personal_agent/orchestrator/test_forced_synthesis_tools.py`:
   - `test_transcript_has_tool_blocks_*` — assistant `tool_calls`, `role="tool"`, and the
     negative (plain user/assistant) cases.
   - `test_anthropic_with_tool_history_retains_tools_and_pins_none` — provider="anthropic",
     transcript with a tool result, non-empty tool_defs → returns `(tool_defs, "none")`.
   - `test_anthropic_without_tool_history_drops_tools` → `(None, None)`.
   - `test_local_provider_none_drops_tools_even_with_history` (provider=None) → `(None, None)`.
   - `test_anthropic_with_history_but_no_tool_defs_uses_placeholder` (Codex gap #4) →
     returns `([_SYNTHESIS_PLACEHOLDER_TOOL], "none")`, i.e. a non-empty single-tool list
     so the LiteLLM raise is avoided.
   - Confirm the test fails (helpers don't exist yet): `make test-file FILE=tests/personal_agent/orchestrator/test_forced_synthesis_tools.py` → ImportError/collection error.
2. **Implement helpers** + import in `executor.py`. Re-run the file → green.
3. **Wire** the synthesis-override block + call-site `tool_choice=` in `step_llm_call`.
4. **Quality gates:** `make test-file FILE=tests/personal_agent/orchestrator/test_forced_synthesis_tools.py` → green;
   `make mypy`; `make ruff-check`; `make ruff-format`; then `make test` (full) and `pre-commit run --all-files`.

## Test commands & expected output

```bash
make test-file FILE=tests/personal_agent/orchestrator/test_forced_synthesis_tools.py
# → all tests pass (after impl); fails at collection before impl
make mypy            # → no new errors
make ruff-check      # → clean
make test            # → full suite green
```

## Out of scope / non-goals

- No change to the local SLM synthesis path (works today).
- No change to `litellm_client.py` — `tool_choice` plumbing already exists.
- Not adding a live A/B; FRE-475's A/B gate is a separate ticket. This unblocks it.

## Halt-condition check

- Single ticket, single PR, no ADR phase bundling.
- No historical-row drops.
- If `make mypy` shows >5 pre-existing errors → surface, separate ticket.
