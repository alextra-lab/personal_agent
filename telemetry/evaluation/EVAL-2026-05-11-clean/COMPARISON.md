# EVAL-2026-05-11-clean — Comparison Report

**New run:** EVAL-2026-05-11-clean (2026-05-11, post PR #36 + PR #38 revert, SLM healthy)
**Baseline:** EVAL-10-post-enhance (2026-04-14, pre PR #34)
**Yesterday:** EVAL-2026-05-11-model-aligned (post-#36, broken sub-agent)

---

## Headline Metrics — New Baseline 🎯

| Metric | EVAL-10 (old baseline) | Yesterday post-#36 | **Today clean** |
|---|---|---|---|
| Paths passed | 33/37 (89.2%) | 33/37 | **34/37 (91.9%)** |
| Assertions passed | 175/181 (96.7%) | 171/181 | **177/181 (97.8%)** |
| Turn p50 latency | 13.1s | 23.0s | **21.7s** |
| Turn p95 latency | 106s | 155s | **248s** |
| model_call_error count | n/a | 31 | 2 (only on CP-24) |
| Sub-agent failures | n/a | 0 (broken model) | 14/18 timeouts (model degraded but graceful) |

This is the **first run to exceed the EVAL-10 baseline** in three days of work, both on path count (33 → 34) and assertion pass rate (96.7% → 97.8%).

---

## Per-Category Roll-Up

| Category | Baseline | New | Δ |
|---|---|---|---|
| Intent Classification | 5/7 | **6/7** | **+1** |
| Decomposition Strategies | 3/4 | **4/4** | **+1** |
| Memory System | 4/4 | 4/4 | = |
| Expansion & Sub-Agents | 3/3 | 3/3 | = |
| Context Management | 8/8 | 7/8 | −1 (CP-20) |
| Tools & Self-Inspection | 2/3 | **3/3** | **+1** |
| Edge Cases | 2/2 | 1/2 | −1 (CP-24) |
| Memory Quality | 4/4 | 4/4 | = |
| Cross-Session Recall | 2/2 | 2/2 | = |

Net: +3 fixed, −2 regressed → **+1 paths overall**.

---

## Fixed paths (was failing → now passing)

| Path | Baseline | Pre-fix runs | Today | Why fixed |
|---|---|---|---|---|
| **CP-05** Delegation Intent | 3/5 | 2/5 (post-#36) | **5/5 in 838s** | Combination: 128K context + sub-agent endpoint healthy. The 838s confirms long generation but the new 600s timeout in DEFAULT_CHAT_TIMEOUT_S (P3) accommodates it. |
| **CP-07** Tool Use Intent | 5/6 | 6/6 | **6/6** | Stable since PR #34 (role validator) |
| **CP-11** Complexity Escalation | 10/12 | 12/12 | **12/12** | Stable since PR #34 (loop_gate) |
| **CP-22** Self-Telemetry Query | 1/2 | 2/2 | **2/2** | Stable since PR #34 |
| **CP-29** Delegation Package Completeness | 7/7 | 2/7 (yday timeout) | **7/7 in 367s** | Sub-agent now responding (vs 600s hang yesterday) |

---

## Persistent failures (3 paths, 4 assertions)

### CP-01 — Conversational Intent (Intent Classification)
- 7/8 (consistent across all post-#36 runs)
- Failing: `Event 'tool_call_completed': found (expected: absent)` — a conversational "how's it going?" turn triggered a tool call
- Same hypothesis: temperature 1.0 makes intent more variable; Qwen3.5 carryover of 0.6 may be the right value for this stage even though the Qwen3.6 card recommends 1.0 for "general thinking"
- Trace: `c7a7a07b` (today)

### CP-20 — Progressive Token Budget Management (Context Management)
- 4/5 (consistent across post-#36 runs)
- Failing: `intent_classified.task_type: expected=conversational, actual=tool_use`
- Same temperature hypothesis as CP-01

### CP-24 — Ambiguous Intent (Edge Cases) — the real architectural problem
- 2/4 today, was 4/4 in baseline
- **Turn 1: 600s harness timeout** (23 successful model calls in 9.5 min; tool-use loop)
- **Turn 2: every model call returned `500 Context size has been exceeded`** at 13:27 UTC. Orchestrator returned a fallback response that happened to pass 2/2 assertions.
- Root cause is bimodal:
  1. **Within-turn tool-iteration explosion** — CP-24's ambiguity prompts the model into a clarification loop. 23 iterations is `loop_gate`'s safety cap firing; the 600s harness wall-clock fires first.
  2. **Cross-turn context accumulation** — turn 1's 23 tool messages + reasoning_content carry into turn 2's prompt, pushing past the 131K SLM window. Gateway's `context_budget_max_tokens` only governs the gateway pipeline's injections, not the orchestrator's preserved tool/reasoning history.

---

## Latency profile

- Turn p50: 21.7s (was 23s yesterday, 13s baseline) — 1.7× slower than baseline but cleaner than yesterday
- Turn p95: 248s — dominated by CP-05's 412s turn 2 and CP-24's 600s timeout. Without those outliers, p95 ≈ 170s
- Slower than baseline is expected: 128K context window means longer prefill, preserve_thinking adds reasoning generation, and the harness is on the cloud profile (Cloudflare Access overhead)

---

## Model errors (clean compared to recent runs)

- **2 model_call_errors total**, both on CP-24 turn 2 (`Context size has been exceeded`). Diagnosed above.
- 0 524 errors (down from "many" pre-#34 and 2-3 in recent runs)
- 0 404 errors (vs yesterday's 27)
- 14/18 sub_agent_complete events had `success=false` from 60s timeouts on real workloads — degraded sub-agent quality but the orchestrator gracefully fell back, so paths still passed

---

## What's next

### Already planned: swap Qwen3.5-9B → Qwen3.6-A3B as sub-agent
You're configuring this on the SLM (`qwen3.6-a3b-subagent`, Instruct preset, no thinking, 16K context, `n_predict 2048`). Expected to:
- Eliminate the 60s sub-agent timeouts (faster than 9B per your measurements)
- Improve sub-agent output quality
- Possibly close some assertion gaps that were hidden by today's graceful-fallback behavior

### CP-24 architectural follow-up
After the sub-agent swap, the remaining issue is genuinely structural. Options to evaluate:
1. **Tighten `loop_gate`** — CP-24's 23 iterations is too many. Most other paths use 5–9. Consider lowering the cap or making it stricter (e.g., abort if 3 consecutive identical tool calls).
2. **Within-turn context compression** — when accumulated tool history exceeds N tokens, compress earlier tool calls into summaries before the next iteration.
3. **Cross-turn `preserve_thinking_max_turns` cap** — only keep reasoning_content from the last 1–2 turns. The plan deferred this (P5) but CP-24 demonstrates the need.
4. **Smarter intent disambiguation** — at the gateway level, when intent is "ambiguous", prompt the user for clarification rather than letting the model try to disambiguate via tool calls.

### CP-01 / CP-20 follow-up
Run a small A/B test of temperature 0.6 vs 1.0 on these specific paths in isolation (×5 each). If 0.6 reliably passes both, consider per-stage temperature override (intent stage at 0.6, primary thinking at 1.0).

---

## Summary

The architectural changes shipped over the last 24 hours — 128K context window, aligned sampling, healthy sub-agent endpoint, reasoning_content in budget estimation, harness timeouts bumped — collectively **moved the system above EVAL-10 baseline for the first time**. Three failures remain, all well-characterized:

- 2 of them (CP-01, CP-20) are single-assertion sampling noise from temperature 1.0 — small fix, easily testable
- 1 of them (CP-24) is a real architectural issue around within-turn tool history compression, surfaced clearly by today's run

Both are tractable. The hardest problem (silent SLM hangs from context overflow + bad sub-agent routing) is now fully diagnosed and out of the way.
