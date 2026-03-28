# EVAL-04: Context Budget Behavior Review

**Date:** 2026-03-28
**Issue:** FRE-149
**Script:** `scripts/eval_04_context_budget.py`
**Raw data:** `telemetry/evaluation/eval-04-context-budget/results.json`
**Harness report:** `telemetry/evaluation/eval-04-context-budget/harness_results.md`

---

## Executive Summary

Stage 7 (`budget.py`) **never triggers** under realistic usage. Token counts peaked at **1,645** across a 12-turn stress-test conversation — 2.5% of the 65,536 ceiling. The trimming priority order (`history → memory → tools`) is sound and requires no change. However, two structural gaps emerged: (1) the budget stage's token estimate excludes significant context that the executor adds before the LLM call, meaning the estimate understates actual token usage; and (2) a systematic intent classification gap (`conversational` vs `memory_recall`) causes CP-19 and CP-28 to fail at their final recall turns.

| Metric | Result |
|--------|--------|
| Budget trimming triggered | **No** |
| Max tokens observed (12-turn, verbose) | **1,645** (2.5% of 65K ceiling) |
| CP-20 harness result | **PASS** (5/5) |
| CP-19 harness result | **FAIL** (2/3) — intent classification gap |
| CP-28 harness result | **FAIL** (3/4) — same gap |
| Trimming priority order assessment | **Sound — no change needed** |

---

## Phase 1: Stress-test token progression

A 12-turn "Distributed Systems Deep Dive" conversation was run, with each turn adding distinct technical context (Kubernetes, Kafka, observability, CI/CD, ML platform, incident management, FinOps, vector search). Token counts were read from `context_budget_applied` events in Elasticsearch.

| Turn | Tokens | % of 65K | Notes |
|------|--------|----------|-------|
| 1 | 31 | 0.0% | Single user message |
| 2 | 165 | 0.3% | 3 messages accumulated |
| 3 | 353 | 0.5% | |
| 4 | 621 | 0.9% | |
| 5 | 850 | 1.3% | |
| 6 | 1,121 | 1.7% | |
| 7 | 1,309 | 2.0% | Message count plateaus at 11 |
| 8 | 1,430 | 2.2% | |
| 9 | 1,467 | 2.2% | |
| 10 | 1,563 | 2.4% | |
| 11 | **1,645** | **2.5%** | Peak — foundational recall turn |
| 12 | 1,536 | 2.3% | Synthesis turn |

**No trimming triggered.** `overflow_action = None` on all turns.

### Message count plateau at Turn 7

`message_count` in the budget event rises from 1 to 11 across Turns 1–6, then stays at 11 for Turns 7–12 despite the conversation continuing. This indicates `apply_context_window` in the executor (not Stage 7) is the active context governor — it caps the conversation window before the assembled context reaches the budget stage.

---

## Phase 2: Harness results (CP-19, CP-20, CP-28)

### CP-20: PASS (5/5)

All tool call assertions passed. The `context_budget_applied` event was confirmed present on Turn 4 (synthesis turn). The progressive tool-call accumulation pattern works correctly within the budget.

### CP-19: FAIL (2/3)

**Failing assertion:** `intent_classified.task_type = memory_recall` on Turn 10.

The gateway classified "Going back to the beginning — what was our primary database again?" as `conversational`, not `memory_recall`.

The `context_budget_applied` event was **present** — budget tracking is working correctly. The failure is purely in intent classification, not budget management.

### CP-28: FAIL (3/4)

**Failing assertion:** `intent_classified.task_type = memory_recall` on Turn 10.

The gateway classified "Given everything we've discussed about our stack, what is our primary database and why did we choose it?" as `conversational`, not `memory_recall`.

Same root cause as CP-19.

---

## Finding 1: Budget stage token estimate understates actual LLM token usage

The `context_budget_applied` event reports tokens from `messages + memory_context + tool_definitions` in the assembled `AssembledContext`. However:

- `has_tools = False` on all observed turns — tool definitions are not passed through the assembled context into the budget estimate. They are added by the executor separately before the LLM call.
- The system prompt is also added by the executor, not by the gateway pipeline.
- The actual LLM call includes: system prompt + session messages + tool definitions + memory context. The budget estimate only includes: session messages (windowed to ~11 by `apply_context_window`) + memory context.

**Implication:** The 1,645-token estimate for Turn 11 is the session-history portion only. The actual tokens sent to the LLM for that same turn (including system prompt + tool definitions) would be significantly higher. The budget ceiling of 65,536 may or may not be appropriate relative to the true LLM token footprint.

**Recommendation for EVAL-07:** The budget stage and the executor context window management are **two independent systems** that don't coordinate. This should be documented as a known gap. Slice 3 context budget work should consider whether to unify these into a single token accounting layer.

---

## Finding 2: Systematic intent classification gap — `conversational` vs `memory_recall`

Both CP-19 and CP-28 use a similar recall phrasing pattern on their final turns:

- CP-19: "Going back to the beginning — what was our primary database again?"
- CP-28: "Given everything we've discussed about our stack, what is our primary database and why did we choose it?"

Both are classified as `conversational`. The intent classifier does not detect the backward-reference framing ("going back", "everything we discussed") as a `memory_recall` signal.

This is a **systematic gap**, not a one-off failure. FRE-155 (ADR: Recall controller) documents the proposed remediation: a Stage 4b post-classification reclassifier that detects in-session backward-reference patterns. This finding directly supports the case for that ADR.

**Note:** Even when intent is misclassified, the agent's response quality is not necessarily degraded — the conversational model may still answer from in-context history. The classification gap affects routing and telemetry accuracy more than end-user experience at short conversation lengths.

---

## Finding 3: Trimming priority order is sound

The three-phase trimming order in `budget.py`:

1. **Drop oldest history** — preserves system + last user message; allows conversations to continue indefinitely
2. **Drop memory context** — Seshat enrichment is session-level and can be re-queried; less destructive than losing tools
3. **Drop tool definitions** — last resort; losing tools breaks agent functionality entirely

This ordering is **correct** for the intended use case. No change recommended.

---

## Finding 4: Token estimation heuristic is adequate but limited

`word_count * 1.3` is a lightweight proxy. It underestimates tokens for code/JSON content (higher token density) and overestimates for long URLs (single token). For the session message content observed in these evaluations, it produces reasonable relative estimates. The absolute accuracy doesn't matter much given that the budget never approaches the ceiling in practice.

---

## Answers to EVAL-04 investigation questions

| Question | Answer |
|----------|--------|
| What gets trimmed first? | Oldest history (`dropped_oldest_history`) |
| What gets trimmed second? | Memory context (`dropped_memory_context`) |
| What gets trimmed third? | Tool definitions (`dropped_tool_definitions`) |
| Is the budget too aggressive or too loose? | **Too loose** — ceiling never approached. But this is intentional conservatism for the 35B model's large context window. |
| For CP-19: is PostgreSQL trimmed by Turn 10, or under-attended? | **Neither** — it is in-context and not trimmed. The question is whether the model attends to Turn 2 context when answering Turn 10. |
| Are 32K/65K thresholds appropriate? | They are safe upper bounds. The budget stage only sees session messages (~1.6K tokens for 12 turns), so the threshold is never a constraint. A tighter threshold on the session-history component alone would be more useful. |

---

## Feeds into EVAL-07

This report contributes the following data points to the `docs/research/EVALUATION_PHASE_FINDINGS.md` synthesis:

- **Context budget adequacy:** Budget is very conservative (2.5% utilisation observed). No trimming in normal usage.
- **Two-layer context management:** `apply_context_window` (executor) and `apply_budget` (gateway Stage 7) operate independently. The executor is the real governor.
- **Intent classification gap:** `conversational` / `memory_recall` boundary has a systematic blind spot for backward-reference questions. Directly supports the FRE-155 Recall Controller ADR.
- **CP-20 pass:** Tool-heavy synthesis is working correctly and budget tracking is auditable.
- **ES field naming:** Structlog events are indexed as `event_type` (not `event`) in Elasticsearch. Relevant for any future direct ES queries.
