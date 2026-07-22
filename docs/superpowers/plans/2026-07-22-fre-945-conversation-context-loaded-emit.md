# FRE-945 — The conversation-context-loaded emit is dark for the same reason

**Ticket:** [FRE-945](https://linear.app/frenchforest/issue/FRE-945) · **Root cause:** identical to
[FRE-944](https://linear.app/frenchforest/issue/FRE-944) (same commit: `4c38b21b`)
**Scope:** visibility only. No change to compaction/truncation behaviour.

---

## 1. Root cause (inherited from FRE-944, not re-derived)

`step_init`'s gateway-driven branch (`src/personal_agent/orchestrator/executor.py`, the
`if ctx.gateway_output is not None:` block) ends in an unconditional `return TaskState.LLM_CALL`
(now at the end of the branch, ~line 3092). FRE-944 already named and evidenced this: everything
below that return — `apply_context_window`, `_maybe_frozen_reset`, and the
`conversation_context_loaded` emit — is unreachable on gateway-driven turns, which is all of them
(157/157 observed over 30 days). FRE-944 restored the `cache_reset_decision` emit (now
`_emit_cache_reset_decision`, called at the top of the gateway branch, line ~2921). This ticket
restores the sibling `conversation_context_loaded` emit the same way.

No new evidence-gathering needed — FRE-944's ES/AST proof already covers this emit by name (its own
commit message lists it as one of the three casualties).

---

## 2. The change

**2a.** Extract the existing `log.info("conversation_context_loaded", ...)` call (currently the sole
call site, at the end of the legacy branch, after `apply_context_window` + `_maybe_frozen_reset`) into
a small private helper, `_emit_conversation_context_loaded(ctx, *, total_messages_in_db,
messages_loaded, messages_truncated, estimated_tokens) -> None`. Behaviour-identical extraction — the
legacy call site passes exactly the values it computes today.

**Note on the FRE-944 comparison (codex plan-review correction):** this is not the same shape as
`_emit_cache_reset_decision`. That helper *evaluates* scheduler state and returns data consumed by its
caller; this helper only *centralizes the log schema* so the legacy and gateway call sites can't drift
apart on field names. Calling it "mirroring FRE-944 exactly" overclaims — the shared idea is placement
(top of the gateway branch, covering every sub-path), not the helper's internal shape.

**2b.** At the **top** of the gateway branch — alongside `_emit_cache_reset_decision(ctx)`, not before
the branch's final return (the same placement rule FRE-944's self-review established, for the same
reason: the enforced-expansion sub-path returns from the middle of the branch and would otherwise stay
silent) — call the helper with:

- `total_messages_in_db=session_message_count` — already computed at the top of `step_init` (before
  the branch), in scope.
- `messages_loaded=len(ctx.messages)` — `ctx.messages` at this point already holds the loaded session
  history plus this turn's appended user message (the same state `_emit_cache_reset_decision` reads).
- `messages_truncated=0` — **not a placeholder.** On the gateway path `apply_context_window` (the only
  code in `step_init` that truncates) never runs — that call site sits below the branch's return, same
  as the emit itself. The count of messages **`step_init`'s own `apply_context_window` call** truncated
  on this path is therefore deterministically zero, always, by construction — a structural fact, not a
  per-turn measurement standing in for missing data. Scope this precisely in the code comment and the
  docstring: this field describes only step_init's own truncation action, never "how much truncation
  happened to this conversation anywhere in the pipeline." Gateway Stage 7
  (`request_gateway/budget.py::apply_budget`) already trims its own independent copy of history
  (`gw.context.messages`) before `step_init` ever runs, entirely separately from `ctx.messages` (which
  `step_init` rebuilds from the raw session at lines ~2743/2867) — that upstream trimming is real and
  is not what this field reports on, and the comment must say so explicitly so a future reader doesn't
  misread `0` as "no trimming happened anywhere."
- `estimated_tokens=estimate_messages_tokens(ctx.messages)` — same function, same `ctx.messages`, same
  point in the branch as `_emit_cache_reset_decision`'s `accumulated_tokens`. The two will read
  identically on gateway turns; that is expected, not a bug (both measure the same untrimmed history at
  the same point), and is the same untrimmed-vs-trimmed caveat FRE-944 already documented for
  `accumulated_tokens` — no new caveat text needed, just a pointer comment to avoid duplicating prose.
- **Branch-entry timing (codex plan-review finding).** The emit fires at branch *entry*, describing
  `ctx.messages` as loaded-session-history-plus-this-turn's-user-message — not the final message list
  the turn ends up sending to the LLM. On the enforced-expansion sub-path specifically, a synthesis
  message is appended to `ctx.messages` *after* this point (around line 3049–3060, before the sub-path's
  own early return at ~3082). So the emit's `messages_loaded`/`estimated_tokens` on that sub-path do not
  include the synthesis message. This is the correct scope per the ticket (it mirrors FRE-944's
  placement rationale — evaluate once, at the top, covering every sub-path uniformly) but must be
  documented as "state at branch entry," not implied to be the final per-turn state.

**2c.** Legacy call site (`_maybe_frozen_reset` branch) becomes a call to the same helper with its
existing computed values — zero behaviour change there.

No message list is mutated, no truncation is added, no threshold moves — this branch already contains
zero calls that could do any of those (only `_emit_cache_reset_decision`, memory-context wiring, and
the delegation/expansion dispatch, none of which this change touches).

---

## 3. Tests (TDD — extend the existing FRE-944 harness, not a new file)

`tests/test_orchestrator/test_frozen_reset_emit.py` already drives `step_init` on the real gateway path
with a capturing module-logger patch (`_gateway_ctx`, `_drive_gateway_turn`, `_capturing_log`) — the
exact harness this needs. Add:

1. `test_gateway_path_emits_conversation_context_loaded_exactly_once` — drive a `SINGLE`-strategy
   gateway turn; assert exactly one `conversation_context_loaded` event, asserting the **full payload
   schema** (`trace_id`, `session_id`, `total_messages_in_db`, `messages_loaded`, `messages_truncated`,
   `estimated_tokens`) is present with the right types — not just two fields, so schema drift between
   the legacy and gateway call sites is actually caught (codex plan-review gap). **Must fail on current
   code (0 events)** — quote the failure in the handoff (AC-1).
2. `test_enforced_expansion_subpath_also_emits_conversation_context_loaded` — same HYBRID/enforced-mode
   harness as `test_enforced_expansion_subpath_also_emits_exactly_once`; assert exactly one
   `conversation_context_loaded` event on that sub-path too (AC-2).
3. `test_conversation_context_loaded_messages_truncated_is_zero_on_gateway_path` — pins the documented
   structural-zero semantics from §2b, so a future edit can't silently start reporting a fabricated
   non-zero value.
4. `test_conversation_context_loaded_total_vs_loaded_count_asymmetry` — pins the pre-existing asymmetry
   that must carry unchanged through the refactor: `total_messages_in_db` == `session_message_count`
   (excludes this turn's just-appended user message) while `messages_loaded` == `len(ctx.messages)`
   (includes it) — codex plan-review flagged this as worth an explicit pin so the extraction in 2a
   doesn't "fix" it by accident.
5. Extend `test_gateway_path_never_compacts_even_when_reset_worthy` (or add a sibling assertion) to also
   confirm the new emit's presence adds no mutation. **Correction from codex plan-review:** the existing
   guard does not assert `ctx.messages` identity — it asserts history stayed a strict forward extension
   (`ctx.messages[: len(messages)] == messages` and length grew by exactly one) and that
   `ctx.salient_highlights` stayed empty. Reuse that same forward-extension assertion style for AC-3;
   do not claim it checks list identity.
6. `test_conversation_context_loaded_helper_emits_full_schema` — **new, not assumed** (codex
   plan-review: no pre-existing test asserts this emit directly by name). Rather than driving the full
   legacy (non-gateway) `step_init` path end-to-end — which pulls in memory-graph queries and session
   repository plumbing well outside this ticket's touch — call `_emit_conversation_context_loaded`
   directly with representative values and assert the log call carries the exact full schema, proving
   the 2a extraction is behaviour-identical to the original inline call without over-mocking an
   unrelated code path.

Generalize the file's local `_decisions(calls)` filter (currently hardcoded to
`CACHE_RESET_DECISION`) into a `_events(calls, event_name)` helper so both emits can be asserted with
the same utility, and update `_decisions`'s one call site accordingly — this is the only touch to
existing code in the test file beyond additions.

Commands: `make test-file FILE=tests/test_orchestrator/test_frozen_reset_emit.py`, then
`make test-file FILE=tests/test_orchestrator/test_frozen_reset_wiring.py`, then `make test`.

---

## 4. Acceptance criteria

| # | Criterion | How proven |
|---|-----------|-----------|
| 1 | Emit fires exactly once on the gateway path | Test 1; verified red-before-green against unmodified main |
| 2 | Enforced-expansion sub-path also emits, own regression test | Test 2 |
| 3 | No behaviour change | §2 — no new mutation/truncation/threshold call added; existing no-op guard covers it |
| 4 | Post-deploy: non-zero event count correlated to turns | Post-deploy runbook: ES phrase-match on `message` (text field, no keyword subfield), keyword-match on `module`/`function` — same field mechanics FRE-944 recorded |

## 5. Deploy

Rides FRE-944's pending gateway rebuild per the ticket — no separate deploy request.
