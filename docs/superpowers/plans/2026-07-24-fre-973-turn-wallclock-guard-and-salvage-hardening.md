# FRE-973: turn-level wall-clock guard + salvage hardening

**Ticket:** [FRE-973](https://linear.app/frenchforest/issue/FRE-973/local-primary-turns-can-run-away-and-hard-fail-on-a-model-server-524) (Approved, Tier-2:Sonnet, stream:build2)
**Status:** In Progress — **revised after codex:rescue plan review** (verdict: needs-fix on v1; this is v2)

## Symptom (from ticket)

A HYBRID turn on the local qwen primary ran ~1013s (16.9 min) and hard-failed with an
HTTP 524 (Cloudflare/tunnel timeout). As tool-loop context grew (FRE-970), each
successive planning call got slower (216s → 251s) until it exceeded the tunnel
read-timeout. No duration ceiling existed — only an iteration-count gate — so the turn
ran to a provider timeout with no partial output salvaged.

## Research findings (corrects two premises in the ticket)

1. **No wall-clock guard exists.** `_resolve_max_iterations(ctx)` (`executor.py:105`)
   caps tool-loop iterations (default 25) but nothing tracks cumulative turn duration.
   At ~230s/iteration the incident spanned only ~4-5 iterations — nowhere near the
   ceiling. **Confirmed real gap.**
2. **ADR-0123 is not the salvage mechanism** — it's the turn-progress-visibility ADR
   (unrelated). The actual salvage is **FRE-398**, in `orchestrator/executor.py`.
3. **`classify_error` already handles `LLMServerError`/524 correctly** →
   `category="model_server"` (`error_classification.py:71-77`, already tested). The
   ticket's "isinstance/allowlist gap" hypothesis does not hold.
4. **The real, confirmed salvage gap:** `execute_task`'s outer `except Exception`
   (`executor.py:2401-2410`) and `execute_task_safe`'s outer `except Exception`
   (`executor.py:5050-5091`) never look at `ctx.tool_results` — unlike `step_llm_call`'s
   own local except (`:4332-4431`), which already does. Any exception reaching those
   two outer handlers permanently drops gathered tool results.

## v1 plan defects found by codex:rescue review, and how v2 fixes them

**Defect 1 — placement cannot bound an in-flight call (incorrect).** v1 only checked
wall-clock in `step_tool_execution`'s between-rounds gate. That gate runs *after* an LLM
response has already returned and requested tools — it can't stop a single call that is
itself running long (executor.py:4119, the actual `await llm_client.respond(...)`), and
if an overlong call returns a final answer with **no** tool calls, it goes straight to
`SYNTHESIS` and never revisits `step_tool_execution` at all (`:4321-4330`). **Fix (v2):**
enforce the deadline at the call seam itself — see §A below.

**Defect 2 — salvage centralization was incomplete.** `execute_task_safe`'s outer except
hardcodes its return (`ctx.final_reply` is never consulted, `ctx.steps` gets replaced by
a one-element list) and `execute_task`'s outer except needs to persist
`ctx.classified_error` (not just `ctx.final_reply`) so `execute_task_safe` doesn't
reclassify from `ctx.error` and lose the `partial=True` marker. **Fix (v2):** see §B below
for the exact corrected diffs.

**Verified non-issue:** the "re-entrant `_emit_classified_error`" hazard codex flagged
does not materialize — `_emit_classified_error` (`executor.py:145-170`) already wraps its
own body in `try/except Exception: log.debug(...)` and never raises, so it cannot trigger
the outer except a second time. No fix needed; noted here so it isn't re-litigated.

## Fix — v2

### A. Deadline enforcement at the call seam (the primary fix)

- `ExecutionContext` (`orchestrator/types.py`): add
  `turn_started_monotonic: float = field(default_factory=time.monotonic)` (needs
  `import time`). Stamped automatically at construction, which in production happens
  immediately before `execute_task_safe` is invoked (`orchestrator.py:117,134`) — no
  dependency on step ordering. Tests that construct a `ctx` well before driving it must
  set this field explicitly to avoid accidental "expired" state.
- New small helper in `executor.py`, next to `_resolve_max_iterations`:
  ```python
  def _turn_deadline_remaining(ctx: "ExecutionContext") -> float:
      """Seconds left in this turn's wall-clock budget (may be negative)."""
      return (
          ctx.turn_started_monotonic + settings.orchestrator_task_timeout_seconds
      ) - time.monotonic()
  ```
- **In `step_llm_call`, immediately before `response = await llm_client.respond(...)`**
  (`executor.py:4119`, inside the existing try that already spans `:3755-4332`):
  - Compute `_remaining = _turn_deadline_remaining(ctx)`.
  - If `_remaining <= 0`: build `ctx.final_reply` via `_fallback_reply_from_tool_results`
    with a "This turn was stopped early — it exceeded its Ns time budget" lead, append a
    `warning` step, and `return TaskState.SYNTHESIS` directly — **no LLM call attempted**.
  - Otherwise, wrap the call: `await asyncio.wait_for(llm_client.respond(...), timeout=_remaining)`.
    This bounds the **entire** call, including whatever internal retries
    `LocalLLMClient`/`LiteLLMClient` perform (`client.py:375-608`), to the remaining turn
    budget — no changes needed to the client's retry internals.
  - Add `except TimeoutError:` **before** the existing `except Exception as e:` on this
    same try (disjoint: `LLMClientError`/`LLMTimeout` subclass `Exception`, not
    `TimeoutError` — verified `llm_client/types.py:106-136`). On this branch: same
    "stopped early" salvage + `return TaskState.SYNTHESIS` as the pre-check branch.
  - This is a true zero-additional-inference stop: `step_synthesis` (`:4918-4968`) makes
    no further model call.
- **In `step_tool_execution`'s iteration-limit block** (`:4468-4507`), before calling
  `_maybe_pause_for_constraint`: if `_turn_deadline_remaining(ctx) <= 0`, skip the
  interactive pause (there's no time left to spend asking) and fall straight into the
  existing "force synthesis" `else` branch — i.e. treat an exhausted wall-clock budget
  as an automatic decline, not a question. This prevents the turn from spending its last
  moments waiting on a user decision it has no budget left to act on.
- **Bump `orchestrator_task_timeout_seconds` default 300 → 900** (`config/settings.py:187`):
  confirmed unused anywhere in current code and in the FRE-893 config audit
  (`never-read`, `docs/research/2026-07-16-fre-893-config-parameter-usage-audit.md:200`).
  300s would clip the codebase's own documented legitimate case (ADR-0123's "six-minute
  artifact build"); 900s sits above that and below this incident's ~1013s runaway.
  Update the field description to state explicitly it's a **total-turn wall-clock
  deadline**. Also update `.env.example:154` (currently shows `=300`) and regenerate
  `docs/reference/CONFIG_INVENTORY.md` §1 via
  `uv run python scripts/audit/config_inventory.py generate` (machine-generated section,
  do not hand-edit). The FRE-893 audit doc itself is a dated point-in-time snapshot —
  left as-is, noted stale in the PR for reviewer awareness.
  **Flagging this default explicitly for master/owner review** at the PR — it's a new
  behavioral ceiling where none effectively existed before, not a number the ticket
  specified. Correctly scoped claim: 900s **combined with the call-seam enforcement in
  this plan** would have stopped the incident before its 524; the setting alone (v1's
  claim) would not have.

### B. Harden salvage to cover every exit path (corrected per review)

- Extract the existing "if `ctx.tool_results` and not already set: build `final_reply`"
  logic (`executor.py:4394-4402`) into a shared helper:
  ```python
  def _salvage_partial_reply(
      ctx: ExecutionContext, classified: ClassifiedError, *, lead: str
  ) -> ClassifiedError:
      if ctx.tool_results and not ctx.final_reply:
          ctx.final_reply = (
              _fallback_reply_from_tool_results(ctx, lead=lead)
              + f"\n\n---\n_{classified.reason} {classified.next_step}_"
          )
          classified = with_partial(classified)
      return classified
  ```
- `step_llm_call`'s except: replace the inline block with a call to the helper
  (behavior-identical for that site).
- **`execute_task`'s outer except (`:2401-2410`):** classify `e`, call the helper, and
  **persist `ctx.classified_error = classified`** (the missing piece v1 didn't do) —
  before setting `ctx.state = TaskState.FAILED`.
- **`execute_task_safe`'s outer except (`:5050-5091`):** use
  `classified = ctx.classified_error or classify_error(e)`, call the helper (idempotent
  no-op if `execute_task` already salvaged — the helper's `not ctx.final_reply` guard),
  and **fix the hardcoded return**:
  - `"reply": ctx.final_reply or f"{classified.reason} {classified.next_step}"`
    (currently always the classified message, ignoring any salvaged reply).
  - `"steps": [*ctx.steps, {"type": "error", ...}]` (currently replaces `ctx.steps`
    outright with a single-element list, discarding prior turn steps).

## Non-goals (unchanged from v1, still deferred)

- Not touching `classify_error`'s isinstance branches — already correct.
- Not raising the tunnel/SLM read timeout — ticket marks this optional/stopgap only.
- Not touching ADR-0123 — confirmed unrelated to salvage.
- Not modifying `client.py`/`litellm_client.py` retry internals — the outer
  `asyncio.wait_for` bounds total wall-clock (including internal retries) without
  needing to touch them.

## Tests (TDD — written first, confirmed failing, then implemented)

All in `tests/personal_agent/orchestrator/test_error_fallback.py` unless noted:

1. **In-flight LLM deadline.** Mock `llm_client.respond` to hang past the remaining
   budget (e.g. `asyncio.sleep` longer than a small configured
   `orchestrator_task_timeout_seconds`); drive `step_llm_call` with `ctx.tool_results`
   pre-populated; assert it returns `TaskState.SYNTHESIS`, `ctx.final_reply` contains the
   "stopped early" + salvaged summary, and the call was cancelled at the deadline (not
   left running). **Must fail first** (today nothing bounds this call).
2. **Deadline-already-expired pre-check.** Backdate `ctx.turn_started_monotonic` so
   `_turn_deadline_remaining` is already `<= 0`; drive `step_llm_call`; assert
   `llm_client.respond` is **never called** and the same "stopped early" outcome results.
3. **Final-answer bypass closed.** Mock `llm_client.respond` to exceed the deadline but
   return a real no-tool-call answer; assert it's still bounded by the `asyncio.wait_for`
   wrap (doesn't silently bypass through the no-tools branch at `:4321-4330`).
4. **Concurrent ceilings — deadline wins, no interactive pause.** Set both the iteration
   count over its cap **and** the wall-clock deadline exceeded; drive
   `step_tool_execution`; assert `_maybe_pause_for_constraint` is **not** called and the
   turn proceeds straight to the force-synthesis branch.
5. **Outer-except salvage — `execute_task`.** Patch `step_tool_execution` (or another
   non-`step_llm_call` step) to raise `LLMServerError("524 origin timeout")` after
   `ctx.tool_results` has entries; drive `execute_task`; assert `ctx.final_reply` holds
   the salvaged summary and `ctx.classified_error.partial is True`. **Must fail first.**
6. **Outer-except salvage — `execute_task_safe` returned reply.** Force `execute_task`
   itself to raise with `ctx.tool_results` pre-populated and `ctx.final_reply` already
   set by the time the (mocked) exception fires; assert `execute_task_safe`'s **returned
   `result["reply"]`** (not just `ctx.final_reply`) equals the salvaged text, and
   `result["steps"]` contains the prior steps plus the new error step (not just the
   error step alone). **Must fail first** against the current hardcoded return.
7. **Empty tool_results — no false partial.** Both outer handlers, with
   `ctx.tool_results == []`: assert the classified-only message is returned and
   `classified.partial` stays `False` (the helper's existing no-op-when-empty behavior).
8. **Idempotence.** Call `_salvage_partial_reply` twice on the same `ctx`; assert
   `ctx.final_reply` is unchanged after the second call and `_fallback_reply_from_tool_results`
   is not invoked again (the `not ctx.final_reply` guard holds).
9. **Existing tests unchanged.** `test_error_classification.py::TestClassifyLLMServerError`
   and the existing `test_error_fallback.py` suite continue passing.

## Acceptance criteria mapping (ticket's "What to fix")

| Ticket ask | This plan |
|---|---|
| Turn-level guard, degrades gracefully | §A: deadline enforced at the call seam (pre-check + `asyncio.wait_for`), zero further inference once expired |
| Wire model-server-timeout into salvage | §B: salvage hardened at both outer catch-alls, covering `model_server`/524 (and every category) regardless of which layer raises it |
| Optional: raise tunnel/SLM read timeout | Explicitly deferred |

## Files touched

- `src/personal_agent/orchestrator/types.py` — `turn_started_monotonic` field + `import time`.
- `src/personal_agent/orchestrator/executor.py` — `_turn_deadline_remaining`, call-seam
  deadline enforcement in `step_llm_call`, pause-skip in `step_tool_execution`,
  `_salvage_partial_reply` helper wired into 3 except sites, `execute_task_safe` return fix.
- `src/personal_agent/config/settings.py` — default bump + description.
- `.env.example` — example value bump.
- `docs/reference/CONFIG_INVENTORY.md` — regenerated (machine-generated section only).
- `tests/personal_agent/orchestrator/test_error_fallback.py` — new tests (list above).
