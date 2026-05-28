# Terminal tool failures short-circuit the reasoning loop

**Linear:** TBD — **needs a new ticket** (see "Ticket framing" below). NOT a reopen of FRE-398.
**Status:** Plan drafted (Opus). Implementation gated on Linear `Approved`.
**Related:** Builds directly on **FRE-398 (Done, PR #92 + #93)** — reuses the shipped `ClassifiedError` / `ClassifiedErrorEvent` / `RUN_ERROR` / `ClassifiedErrorCard` machinery. Sibling of FRE-399 (auto-retry/fallback — out of scope here).

## Context — the 3.5-minute hole

Observed 2026-05-28 (trace `b8cd1d53…`): the `artifact_draft` sub-agent timed out after 120s (`14:05:00`). The tool failure was returned to the orchestrator as an ordinary tool result, which sent the state machine **back to `LLM_CALL`**. The primary reasoning model (Qwen3.6-35B-A3B) was then called with a 7-message, ~22K-token context to "handle" the failure — and took **3.5 minutes** to generate prose amounting to "sorry, that timed out." The user saw nothing from `14:05:00` (timeout) until `~14:08:32` (turn complete).

**Root problem:** every tool failure — even terminal ones with nothing to decide — is routed back through the reasoning model. For a sub-agent timeout the model adds zero value; it's an expensive, slow, non-deterministic prose formatter for a known failure.

This is the FRE-398 principle ("surface errors fast and actionably") applied one layer up: at the **tool-execution layer**, not just the LLM-call layer.

## What's already shipped (FRE-398 — verified on `main`)

- `error_classification.py` — `ClassifiedError` frozen dataclass + `classify_error(exc)` (isinstance-based). **Literal omits `tool_failure`.**
- `executor.py:2456-2497` — `step_llm_call` except block: sets `ctx.error`, classifies, salvages `ctx.final_reply` from `ctx.tool_results`, returns `TaskState.FAILED`.
- `executor.py:3194-3222` — `execute_task_safe`: **for any turn with `ctx.error` set**, uses `ctx.classified_error or classify_error(ctx.error)`, surfaces the salvaged `ctx.final_reply` (or classified message if none), appends an error step, and calls `_emit_classified_error(ctx, classified)` → `RUN_ERROR`.
- `transport/events.py:186` — `ClassifiedErrorEvent`. **Literal omits `tool_failure`.**
- PWA `ClassifiedErrorCard.tsx` + `types.ts` — renders `RUN_ERROR`. `CATEGORY_TITLES` is an exhaustive `Record<category, string>`; `types.ts` category union **omits `tool_failure`**.

**Implication:** the emit-and-surface path already exists and works end-to-end for `FAILED` turns where `ctx.error` + `ctx.final_reply` are set. We do **not** need a new emission site. We need a second *trigger* for it, at the tool layer.

## Design

### Architectural decision — FAILED path, not SYNTHESIS (overrides Codex)

Codex (reviewing before it knew PR1 was merged) recommended transitioning `step_tool_execution → SYNTHESIS` with a prebuilt reply and *not* setting `ctx.error`. Given the shipped reality, the **FAILED path is strictly better**:

| | SYNTHESIS path (Codex) | **FAILED path (chosen)** |
|---|---|---|
| Emits `RUN_ERROR` | needs a **new manual emit call site** in `step_tool_execution` | **reuses shipped `execute_task_safe` emit** (zero new emit code) |
| Turn outcome record | records a failed turn as **success** | records it as **failed** (honest) |
| Reply surfacing | new code | **reuses shipped `ctx.final_reply` handling** |
| New code | more | less |

A terminal tool failure *is* a turn failure. Marking it `FAILED` with a deterministic `ctx.final_reply` mirrors exactly what the shipped `step_llm_call` already does for LLM exceptions.

### Self-describing terminality (not an executor allowlist)

Per the project preference for self-describing components over harness-side routing tables, the **tool declares its own terminality** by raising a dedicated exception. The executor stays generic: "if any dispatched result is marked terminal, short-circuit." Adding a future terminal-failure-capable tool = raise the exception in that tool; **no executor change**.

This also satisfies Codex's condition #2 (no brittle string-matching) and #6 (no premature termination): today exactly **one** site is terminal — `artifact_draft` timeout. Every other tool error still flows back to `LLM_CALL` for model recovery.

### Changes

**1. `tools/executor.py` — `TerminalToolError`**
```python
class TerminalToolError(ToolExecutionError):
    """A tool failure the model cannot recover from — short-circuit the turn.

    Carries user-facing guidance so the orchestrator can surface a classified
    error without a recovery LLM round-trip.
    """
    def __init__(self, message: str, *, reason: str, next_step: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.next_step = next_step
```

**2. `tools/executor.py:446` — preserve terminality through the flatten.**
The layer's `except Exception as e:` currently discards the exception type. Add:
```python
metadata: dict[str, Any] = {}
if isinstance(e, TerminalToolError):
    metadata = {"terminal": True,
                "terminal_reason": e.reason,
                "terminal_next_step": e.next_step}
return ToolResult(tool_name=tool_name, success=False, output={},
                  error=str(e), latency_ms=latency_ms, metadata=metadata)
```
(`ToolResult.metadata` already exists — `tools/types.py:59`.)

**3. `orchestrator/executor.py:2692` — propagate terminal flags** in the `_dispatch_tool_call` result dict (it currently propagates `tool_layer_error` but drops `metadata`):
```python
"terminal": bool(result.metadata.get("terminal")),
"terminal_reason": result.metadata.get("terminal_reason"),
"terminal_next_step": result.metadata.get("terminal_next_step"),
```
Mirror the same three keys in the bare `except Exception` dict at `:2715` (defaults: `False`/`None`/`None`).

**4. `orchestrator/executor.py` `step_tool_execution` — detect + short-circuit.**
After Phase 3 assembles dispatched results, **before** the `return TaskState.LLM_CALL`:
```python
terminal = next((d for d in dispatched_results if d.get("terminal")), None)
if terminal is not None:
    from personal_agent.error_classification import ClassifiedError
    classified = ClassifiedError(
        category="tool_failure",
        reason=terminal["terminal_reason"],
        next_step=terminal["terminal_next_step"],
        actions=("retry", "stop"),
    )
    ctx.classified_error = classified
    ctx.final_reply = f"{classified.reason} {classified.next_step}"
    ctx.error = ToolExecutionError(terminal.get("tool_layer_error") or "terminal tool failure")
    log.warning("tool_terminal_short_circuit",
                trace_id=ctx.trace_id, session_id=ctx.session_id,
                tool_name=terminal["tool_name"], error_category="tool_failure")
    return TaskState.FAILED
```
`ctx.error` must be truthy so the shipped `execute_task_safe` (`if ctx.error:`) emits `RUN_ERROR` and records the failure. `ctx.classified_error` takes precedence over `classify_error(ctx.error)`, so the `tool_failure` category is preserved. **No change to `execute_task_safe`.**

(Confirm the exact local variable name for the dispatched-results list in `step_tool_execution` at implementation time; the snippet assumes `dispatched_results`.)

**5. `tools/artifact_tools.py:1086` — raise terminal on timeout.**
```python
raise TerminalToolError(
    f"HTML generation sub-agent timed out after {_DRAFT_TIMEOUT_S}s.",
    reason="The artifact generator timed out — the document was too complex to build in time.",
    next_step="Try a simpler artifact, or switch to Cloud for more capacity.",
) from exc
```
**Only** the timeout path becomes terminal. The non-timeout `except Exception` sub-agent path (`:1098`) stays a normal `ToolExecutionError` (may be transient/retryable).

**6. Add `tool_failure` to the category Literals (3 + PWA):**
- `error_classification.py` — `ClassifiedError.category` Literal.
- `transport/events.py:207` — `ClassifiedErrorEvent.category` Literal.
- PWA `types.ts:216` — `ClassifiedErrorData['category']` union.
- PWA `ClassifiedErrorCard.tsx:30` — `CATEGORY_TITLES` (exhaustive `Record`, **must** add or TS fails to compile): `tool_failure: 'Artifact generation failed'`.

## Files touched

| File | Change |
|---|---|
| `src/personal_agent/tools/executor.py` | `TerminalToolError` class; map it to `ToolResult.metadata` in the except handler |
| `src/personal_agent/orchestrator/executor.py` | propagate terminal flags in `_dispatch_tool_call` (×2 dicts); detect + short-circuit in `step_tool_execution` |
| `src/personal_agent/error_classification.py` | add `tool_failure` to Literal |
| `src/personal_agent/transport/events.py` | add `tool_failure` to Literal |
| `src/personal_agent/tools/artifact_tools.py` | raise `TerminalToolError` on sub-agent timeout |
| `seshat-pwa/src/lib/types.ts` | add `tool_failure` to category union |
| `seshat-pwa/src/components/ClassifiedErrorCard.tsx` | add `tool_failure` title |

PWA delta is trivial (enum + title; card already renders `RUN_ERROR`), so **one PR** — no PR1/PR2 split.

## Tests (TDD — write first, confirm red)

- `tests/.../tools/test_executor.py` — `TerminalToolError` → `ToolResult(success=False, metadata.terminal is True, terminal_reason/next_step preserved)`.
- `tests/.../orchestrator/` — **the core test:** build an `ExecutionContext`, drive `step_tool_execution` where a dispatched tool result is `terminal=True`; assert it returns `TaskState.FAILED`, sets `ctx.classified_error.category == "tool_failure"`, sets `ctx.final_reply` to reason+next_step, and **the LLM is never called again** (mock `llm_client.respond`, assert 0 calls on the recovery path).
- **Negative test:** a non-terminal tool error (`success=False`, no terminal flag) still returns `TaskState.LLM_CALL` (recovery preserved — Codex condition #6).
- `tests/.../test_artifact_tools.py` — sub-agent timeout raises `TerminalToolError` with populated `reason`/`next_step`.
- `tests/.../transport/test_events.py` — `ClassifiedErrorEvent(category="tool_failure", …)` constructs/serializes.
- PWA — `ClassifiedErrorCard` renders the `tool_failure` title (extend existing card test if present).

## Quality gates

`make test` (new files first, then full) · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` (identity-threading: the new `log.warning("tool_terminal_short_circuit", …)` carries `trace_id` + `session_id`; the shipped `_emit_classified_error` already threads both).

## Post-merge verification (same session)

1. `make deploy`.
2. Force the failure: with the SLM slow/over-budget on a complex `artifact_draft` plan, run a turn. Confirm in gateway logs: `tool_terminal_short_circuit` fires at the moment of timeout (`+0s`), and there is **no second `model_call_started`** afterward (the 3.5-min gap is gone).
3. Confirm a `RUN_ERROR` row lands in Postgres `session_events` with `data.category == "tool_failure"`.
4. Confirm the PWA shows the `ClassifiedErrorCard` immediately (not after a multi-minute wait).
5. `task_failed` structlog carries `error_category="tool_failure"` + `trace_id`/`session_id`.

## Ticket framing (halt condition)

**FRE-398 is Done** (PR #92 + #93 merged). Per the multi-phase policy and the Linear gate, do **not** reopen it. This is **new scope discovered after FRE-398 shipped** → file a **new ticket** (sibling in the FRE-397/398/399/400 design thread), state `Needs Approval` + label `PersonalAgent`, Tier-2:Sonnet. **Do not implement until it is `Approved`.** The user said "fold it in" before we both knew FRE-398 had already shipped; surfacing this rather than silently reopening.

## Acceptance criteria

| # | Criterion |
|---|---|
| 1 | Terminal tool failure (artifact_draft timeout) ends the turn with **no recovery LLM call** |
| 2 | A classified `tool_failure` `RUN_ERROR` event reaches the PWA immediately |
| 3 | PWA renders the failure via the existing `ClassifiedErrorCard` |
| 4 | Non-terminal tool errors still loop back to the model (recovery preserved) |
| 5 | Tools declare terminality self-descriptively (`TerminalToolError`); no executor allowlist |

## Out of scope

- Auto-retry / auto-fallback-to-Cloud (FRE-399).
- Tuning the 120s `artifact_draft` timeout or making it adaptive (separate concern).
- A broad `ToolResult` metadata/schema redesign (Codex condition #4) — we ride the existing `metadata` field.
