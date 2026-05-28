# FRE-398 — Bubble up useful, actionable errors to the user (PR1: backend)

**Linear:** FRE-398 (Approved · Tier-2 · High) — `[Thread] Bubble up useful, actionable errors to the user`
**Related:** FRE-389 / ADR-0076 (DecisionCard + typed WS event pattern, the model to mirror). Sibling FRE-399 owns retry/auto-fallback-to-Cloud + SLM telemetry — **out of scope here.**
**Branch:** `starry-plaza-1s/fre-398-thread-bubble-up-useful-actionable-errors-to-the-user`

## Context

Motivating incident (trace `73efd74a…`, 2026-05-28): a turn read 5 files and drafted a 15K-char artifact, then the **final primary LLM call hit a Cloudflare 524** (Mac SLM origin timeout). The executor has no handling for `LLMServerError` mid-turn, so it propagated through `task_failed` → the generic *"An error occurred while processing your request. Please try again."* — discarding all gathered work and telling the user nothing actionable.

This is the FRE-389 principle ("no silent/abrupt firing; surface state + give agency") applied to **infra failures**: recover-or-explain. Two failures today: (a) the reply is generic instead of classified + actionable, and (b) gathered tool results are thrown away.

This PR is **backend-only** (confirmed split). PWA `ClassifiedErrorCard` rendering is a follow-on PR2; FRE-398 stays **In Progress** until PR2 lands (per multi-phase ticket policy).

## Scope of PR1

1. Classify failures into a structured, guidance-bearing result (reason + concrete next step + action ids).
2. Don't discard work: on a primary-call failure with gathered tool results, salvage a synthesis from them.
3. Emit a typed `ClassifiedErrorEvent` → wire `RUN_ERROR` over the existing AG-UI WS transport (PWA renders in PR2).
4. Unit tests per error class + the gathered-results fallback path.

## Design

### A. Error classification — new module `src/personal_agent/error_classification.py`

Frozen dataclass + isinstance-based classifier (more robust than the existing substring matcher). Reuses `sanitize_error_message` (`security.py:291`) only for the generic-reason fallback.

```python
@dataclass(frozen=True)
class ClassifiedError:
    category: Literal["model_server", "timeout", "connection", "rate_limit",
                      "budget_denied", "tool_failure", "generic"]
    reason: str        # what happened, user-facing, no internals
    next_step: str     # concrete guidance
    actions: tuple[str, ...]  # action ids for PR2 buttons, e.g. ("retry","switch_to_cloud","stop")
    partial: bool = False     # set by caller when a partial reply was salvaged

def classify_error(error: Exception) -> ClassifiedError: ...
```

Mapping (isinstance, in priority order — import classes from `llm_client/types.py` and `cost_gate/types.py`):

| Exception | category | reason | next_step | actions |
|---|---|---|---|---|
| `LLMServerError` | model_server | "The local model server hit an error (it may have timed out on a large request)." | "Retry, switch to Cloud, or shorten the request." | retry, switch_to_cloud, stop |
| `LLMTimeout`, `InferenceSlotTimeout` | timeout | "The local model timed out — the request was large." | "Retry, switch to Cloud, or shorten it." | retry, switch_to_cloud, stop |
| `LLMConnectionError` | connection | "Couldn't reach the local model server." | "Check the SLM server is running, then retry or switch to Cloud." | retry, switch_to_cloud, stop |
| `LLMRateLimit` | rate_limit | "The model server is rate-limiting requests right now." | "Wait a moment, then retry." | retry, stop |
| `BudgetDenied` | budget_denied | derive from payload (`role`, `time_window`, `denial_reason`) | "Raise the budget for this window or wait for it to reset." | stop |
| else | generic | `sanitize_error_message(error)` | "Try rephrasing or retry." | retry, stop |

`InferenceSlotTimeout` lives at `llm_client/concurrency.py:371`; `BudgetDenied` at `cost_gate/types.py:71`. Action ids are stable strings consumed by PR2 (no behavior wired this PR).

### B. Don't discard work — `orchestrator/executor.py`

1. **New ctx field** (`orchestrator/types.py`, near `tool_results` at L166): `classified_error: ClassifiedError | None = None`.

2. **Generalize `_fallback_reply_from_tool_results`** (`executor.py:761`) — add optional `lead: str | None = None`; when provided, use it instead of the hardcoded "I reached my tool-use limit…" line. (Default preserves current behavior for the existing call site at L2399.)

3. **`step_llm_call` except block** (`executor.py:2402-2445`): after setting `ctx.error = e`, classify and salvage:
   ```python
   classified = classify_error(e)
   if ctx.tool_results:
       fallback = _fallback_reply_from_tool_results(
           ctx, lead="The model call failed before I could finish, but here's what I gathered:")
       ctx.final_reply = f"{fallback}\n\n---\n_{classified.reason} {classified.next_step}_"
       classified = replace(classified, partial=True)
   ctx.classified_error = classified
   ```
   Keep the existing `error_step` but enrich `metadata` with `error_category=classified.category`. Existing structlog lines already carry `trace_id`/`session_id` — keep them (identity gate).

4. **`execute_task_safe` `ctx.error` branch** (`executor.py:3142-3158`): stop overwriting with the generic string.
   ```python
   classified = ctx.classified_error or classify_error(ctx.error)
   log.warning(TASK_FAILED, trace_id=ctx.trace_id, session_id=ctx.session_id,
               error=classified.reason, error_type=type(ctx.error).__name__,
               error_category=classified.category)
   if not ctx.final_reply:                       # no salvaged work → classified message
       result["reply"] = f"{classified.reason} {classified.next_step}"
   # else: result["reply"] is already ctx.final_reply (partial work preserved, L3137)
   result["steps"].append({"type": "error", "description": classified.reason,
       "metadata": {"error_category": classified.category,
                    "error_type": type(ctx.error).__name__}})
   await _emit_classified_error(ctx, classified)   # best-effort, see C
   ```

5. **`execute_task_safe` top-level fatal except** (`executor.py:3186-3220`): classify `e`, set `result["reply"] = f"{classified.reason} {classified.next_step}"`, enrich the error step metadata, and best-effort `await _emit_classified_error(ctx, classified)` when `ctx.session_id` is set. (No tool-result salvage here — ctx may be partially built.)

### C. Typed transport event — mirror FRE-389 `CancelledEvent` (one-way, no round-trip)

1. **`transport/events.py`** — add frozen dataclass after `CancelledEvent` (L182) and to the `InternalEvent` union (L186):
   ```python
   @dataclass(frozen=True)
   class ClassifiedErrorEvent:
       session_id: str
       trace_id: str
       category: Literal["model_server","timeout","connection","rate_limit",
                         "budget_denied","tool_failure","generic"]
       reason: str
       next_step: str
       actions: Sequence[str]
       partial: bool
   ```

2. **`transport/agui/adapter.py`** — import the event (L21-32 block) + add a `case` before `case _:` (L143). Wire type `RUN_ERROR` (AG-UI standard name); lift `session_id`/`trace_id` to envelope top, domain under `data` (matches the rich-event convention):
   ```python
   case ClassifiedErrorEvent():
       envelope = {"type": "RUN_ERROR", "session_id": event.session_id,
                   "trace_id": event.trace_id,
                   "data": {"category": event.category, "reason": event.reason,
                            "next_step": event.next_step, "actions": list(event.actions),
                            "partial": event.partial}}
   ```

3. **`transport/agui/transport.py`** — import (L36-47 block) + `async def emit_classified_error(*, session_id, trace_id, category, reason, next_step, actions, partial)` wrapping `_push_event(ClassifiedErrorEvent(...), session_id)` (mirror `emit_cancelled` at L143).

4. **`orchestrator/executor.py`** — `async def _emit_classified_error(ctx, classified)` near `_emit_turn_cancelled` (L128): best-effort `try/except` → `log.debug`, guarded by `ctx.session_id`; calls `emit_classified_error(...)` from the transport module (lazy import, same as the other `_emit_*` helpers).

No `ws_endpoint.py` change (one-way event). No `EventType` enum exists. PWA `types.ts` mirror + rendering = PR2.

## Files touched (PR1)

| File | Change |
|---|---|
| `src/personal_agent/error_classification.py` | **new** — `ClassifiedError` + `classify_error` |
| `src/personal_agent/orchestrator/types.py` | add `classified_error` field to `ExecutionContext` |
| `src/personal_agent/orchestrator/executor.py` | generalize fallback (L761); salvage in except (L2402); classified reply + emit in `execute_task_safe` (L3142, L3186); `_emit_classified_error` helper (L128) |
| `src/personal_agent/transport/events.py` | `ClassifiedErrorEvent` + union |
| `src/personal_agent/transport/agui/adapter.py` | import + `RUN_ERROR` case |
| `src/personal_agent/transport/agui/transport.py` | import + `emit_classified_error` |

## Tests (TDD — write first, confirm red, then implement)

- **`tests/personal_agent/test_error_classification.py`** (new) — one test per row in the mapping table → asserts `category`, that `reason`/`next_step` are non-empty and contain the expected guidance, and `actions`. Include the `BudgetDenied` payload-derived reason and the generic fallback.
- **`tests/personal_agent/orchestrator/`** — gathered-results fallback: build an `ExecutionContext` with populated `ctx.tool_results`, drive `step_llm_call` so the LLM raises `LLMServerError` (mock `llm_client.respond`), assert `ctx.final_reply` contains the salvaged tool summary **and** the classified reason, and `ctx.classified_error.partial is True`. Second case: empty `tool_results` → `execute_task_safe` reply equals classified reason + next_step (not the generic string). Mirror existing executor test setup.
- **`tests/personal_agent/transport/test_events.py`** — `ClassifiedErrorEvent` creation/frozen/equality + extend `TestInternalEventUnion.test_pattern_matching`.
- **`tests/personal_agent/transport/test_adapter.py`** — `RUN_ERROR` envelope shape + extend `test_all_event_types_serialize`.
- **`tests/personal_agent/transport/test_adapter_seq.py`** — `seq` passthrough for the new event.

## Quality gates (all before PR)

- `make test-file FILE=tests/personal_agent/test_error_classification.py` then transport + orchestrator test files, then `make test`
- `make mypy` · `make ruff-check` · `make ruff-format`
- `pre-commit run --all-files` — **identity-threading gate is live**: every new `log.*`/emit on the request path carries `trace_id` + `session_id` (all new sites above do; the transport `_push_event` persists with both).

## PR (pre-merge checklist only)

Open against `main` using `.github/PULL_REQUEST_TEMPLATE.md`. Title: `feat(orchestrator): classify turn failures + salvage partial work + RUN_ERROR event (FRE-398 PR1)`. No prod-verify/deploy items in the checklist — those go in a Linear comment.

## Post-merge (same session)

1. `make deploy`; capture output.
2. **Forced-failure verification** (PR1 has no PWA UI yet — verify the event + salvage at the transport/log layer):
   - Trigger a model failure (point `LLM_BASE_URL` at a dead port, or stop the SLM origin) and run a turn that first calls a tool (so `ctx.tool_results` is populated), e.g. via `uv run agent "read <file> then summarize"`.
   - Confirm the reply is the **salvaged tool synthesis + classified reason/next-step**, not the generic string.
   - Confirm a `RUN_ERROR` row lands in `session_events` (Postgres) with the classified `data`.
   - Confirm structlog `task_failed` carries `error_category` with `trace_id`/`session_id`.
3. If the emit path touches `session_events` joinability → run `scripts/monitors/joinability_probe.py` against prod; paste output.
4. Update `docs/plans/MASTER_PLAN.md` on `main` (header + Last updated); commit + push.
5. Linear: comment PR link + deploy timestamp + verification snippet; **keep FRE-398 In Progress** (PWA PR2 outstanding). File/track PR2 (ClassifiedErrorCard) as the remaining acceptance item (AC3 PWA render).

## Acceptance criteria (FRE-398)

| # | Criterion | This PR |
|---|---|---|
| 1 | Classified, human-readable reason + next step (not generic) | ✅ A + B4/B5 |
| 2 | Primary-call failure mid-turn falls back to synthesis from gathered results | ✅ B2/B3 |
| 3 | Reason delivered as a typed transport event the PWA renders distinctly | ⏳ event emitted (C); PWA render = PR2 |
| 4 | Unit tests per error class → expected message/guidance + fallback path | ✅ Tests |

## Out of scope (FRE-399)

Retry / auto-fallback-to-Cloud (the `switch_to_cloud` action is surfaced but not wired), SLM telemetry. This thread surfaces the error well; it does not recover automatically.
