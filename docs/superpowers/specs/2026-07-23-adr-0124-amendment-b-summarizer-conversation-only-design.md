# Design — ADR-0124 Amendment B: the summarizer is conversation-only

**Date:** 2026-07-23
**Status:** Proposed — design agreed with the owner in session; to be formalized by the adrs seat as **Amendment B** to ADR-0124.
**Backing:** ADR-0124 (Phase 0 producer + Amendment A). Relocates verification to the *verification oracle* (research: `docs/research/2026-07-22-session-summary-kg-opportunity.md`, Lane 5 → Workstream 4).

## Context

ADR-0124 Phase 0 (the session-summary producer, i.e. the **summarizer**) is live. Amendment A
narrowed the summarizer's **input** to conversation text + tool *metadata* (name / status / error),
removing tool payloads. But it left two tool reaches in place:

- the **`tool_evidence` basis** — a digest item grounded in tool output; and
- the **`status_contradiction` correction** — the summarizer adjudicating "assistant narration denied
  by tool status/error."

Both contradict the amendment's own principle, and **AC-10 was deferred** as a result. This is the
loose end Amendment A left; Amendment B closes it thoroughly.

## Principle (owner)

The summary is built on **what the user actually received** — the assistant's responses plus the
user's own text. The assistant already consumed the tools and folded them into its reply, so that
reply *is* the record of what happened. Re-injecting tools lets the summarizer re-derive facts
differently than the assistant actually presented — a summary of something the user never received.
The summarizer must therefore neither **source content from** tools nor **adjudicate against** them.

## Decision

1. **Remove `tool_evidence` as a basis value.** Tools-as-source violates fidelity. Bases collapse to
   conversation-grounded values only: `user_statement`, `assistant_reasoning`, `mixed` (a combination
   of those two).
2. **Remove the `status_contradiction` correction.** Tools-as-adjudication is *verification* work, and
   verification is the job of the **verification oracle** (a later, downstream verifier that
   cross-checks facts against source summaries — Lane 5 / Workstream 4), **not** the summarizer.
   `self_correction` remains (see reconciliation).
3. **Do not add a tool-error flag.** Considered and rejected: most tool errors are *recovered from*, so
   a judgment-free "produced amid tool errors" flag is mostly false positives and would **pollute** a
   ~250-token digest; separating harmful from benign errors *is* the judgment that belongs to the
   oracle; and the raw tool status/error is **already durably captured in the turn records**, so the
   oracle can read it directly later. The summarizer carrying it preserves nothing and only adds noise.

Net: the summarizer faithfully records the conversation and nothing else. All tool-derived verification
is relocated to the future oracle, where the raw signal already waits.

## Consequences

- **AC-10 unblocks.** With nothing tool-sourced to label, the payload/tool-derived fixture problem
  dissolves. AC-10's discrimination check is redefined over the three conversation bases only.
- **Phase 1 (FRE-948) proceeds** on the simplest possible producer.

## Reconciliation surface for the adrs seat (do the thorough job Amendment A skipped)

- **`self_correction` evidence.** FRE-953 allowed its evidence to be "a tool error or the conversation."
  To be fully consistent with the principle, restrict it to the **conversation** (the assistant
  correcting itself in its own text). Decide and state explicitly.
- **Schema / enum / grammar.** Remove `tool_evidence` from the `basis` enum; remove
  `status_contradiction` from the correction `tier`; update the locator grammar (AC-11 — drop
  `tool_result[N].error` if `self_correction` becomes conversation-only).
- **Acceptance criteria.** AC-10 (redefine over the three bases), AC-11 (locator grammar), AC-12 / AC-13
  (corrections reduce to `self_correction` only). Rebuild the labelled fixtures accordingly.
- **Producer prompt.** Remove any instruction that lets tool status/error originate content or fire a
  contradiction; keep tool metadata in input only insofar as it is inert to the output.

## Non-goals

- Not building the verification oracle. Not adding embedding, hydration, or any Phase 1+ consumer here.
- Not re-opening Amendment A's payload removal (kept) or its conversation-scope input (kept).
