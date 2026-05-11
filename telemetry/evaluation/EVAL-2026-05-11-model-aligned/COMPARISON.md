# EVAL-2026-05-11-model-aligned — Comparison Report

**New run:** EVAL-2026-05-11-model-aligned (PR #36 deployed: 128K context, Qwen card sampling, budgets bumped, harness 600s timeout)
**Baseline:** EVAL-10-post-enhance (2026-04-14, pre PR #34)
**Yesterday:** EVAL-2026-05-10-post-fundamentals (post-#34, pre-#36)

---

## Headline Metrics

| Metric | EVAL-10 Baseline | Yesterday | New | Δ vs base |
|---|---|---|---|---|
| Paths passed | 33/37 (89.2%) | 33/37 | 33/37 | = |
| Assertions passed | 175/181 (96.7%) | 170/181 | 171/181 (94.5%) | −2.2 pp |
| Turn p50 | 13.1s | 22.6s | 23.0s | +75% |
| Turn p95 | 106s | 247s | 155s | +46% |
| 524 errors | many | 2 | 3 | ↓↓ |
| 404 errors | n/a | 0 | 27 (transient, recovered via retry) | new |

The PR #36 changes did what they were designed to do — see Fixes section — but the larger thinking budget introduced two **new** silent-hang failures. Net path count is unchanged from yesterday.

---

## Per-Category Roll-Up

| Category | Baseline | New | Δ |
|---|---|---|---|
| Intent Classification | 5/7 | 5/7 | = |
| Decomposition Strategies | 3/4 | **4/4** | **+1** |
| Memory System | 4/4 | 4/4 | = |
| Expansion & Sub-Agents | 3/3 | 3/3 | = |
| Context Management | 8/8 | 7/8 | −1 |
| Tools & Self-Inspection | 2/3 | **3/3** | **+1** |
| Edge Cases | 2/2 | 2/2 | = |
| Memory Quality | 4/4 | 3/4 | −1 |
| Cross-Session Recall | 2/2 | 2/2 | = |

---

## Fixes (yesterday's regressions → resolved)

| Path | Yesterday | New | Notes |
|---|---|---|---|
| **CP-10** DECOMPOSE Complex | 0/7 (300s timeout) | **7/7 in 85s** | The primary target; context overflow eliminated by 131K window. |
| **CP-24** Ambiguous Intent | 2/4 (300s timeout) | **4/4 in 465s** | Fixed; 465s is close to the 600s ceiling — see latency note below. |

Both fixes confirm the diagnosis: the model was silently grinding on prompts that exceeded its specified context. Running it at the card's stated thinking minimum eliminates the silent hang on these paths.

---

## New Regressions

### CP-29 — Delegation Package Completeness (Memory Quality)
- **Was** 7/7 in 28s (baseline) and 7/7 in 64s (yesterday) → **2/7, turn 3 timeout @ 600s** today.
- **No errors in ES for the CP-29 window.** Turn 1 (15s) and turn 2 (13s) ran cleanly. Turn 3 silently hung for the full 600s.
- Same silent-hang signature as the original CP-10/CP-24 issue, but on a different path and a different turn.

### CP-05 — Delegation Intent (Explicit and Implicit) (Intent Classification)
- **Was** 5/5 in 544s yesterday (very slow but passing) → **2/5, turn 0 timeout @ 600s** today.
- 524 errors observed at `07:33:29` (during CP-05's window), trace `2fad3f4f` — these are the gateway giving up on an already-stuck SLM request, not the root cause.

### CP-01 — Conversational Intent (Intent Classification)
- 7/8 (same single-assertion failure as yesterday).
- Failing: `Event 'tool_call_completed': found (expected: absent)` — a conversational "how's it going?" turn triggered a tool call.
- Hypothesis: now that I've raised temperature from 0.6 to 1.0 (per Qwen card "Thinking — General Tasks"), intent classification has more variance. The Qwen3.5 0.6 setting may have been right for *this* workload even if not the card's headline number.

### CP-20 — Progressive Token Budget Management (Context Management)
- 4/5 (same single-assertion failure as yesterday).
- Failing: `intent_classified.task_type: expected=conversational, actual=tool_use`.
- Same temperature hypothesis as CP-01.

---

## Root cause hypothesis for CP-05 / CP-29

I bumped `thinking_budget_tokens` from 3000 → **32768** (Qwen card "standard" output) in PR #36. On Apple Silicon, the Qwen3.6-35B-A3B generates at ~50 tok/s. That means:

| Thinking budget | Max generation time |
|---|---|
| 3K (pre-#36) | ~60s |
| 8K | ~160s |
| 16K | ~320s |
| **32K (new)** | **~640s — exceeds harness's 600s timeout** |
| 81K (Qwen "complex") | ~1620s — infeasible on this hardware |

Add 128K prefill time (~160s) and the worst-case turn easily exceeds 800s. The model is doing what we asked; the constraint is local hardware throughput, not the model spec.

CP-29 turn 3 likely used the full thinking budget because by turn 3 the conversation has accumulated context that warrants deeper reasoning. CP-05 turn 0 is a complex delegation prompt that also pulls deep thinking.

This is not a bug in PR #36 — it's the model now having the budget to think for longer than the harness allows. Two ways to resolve:

1. **Reduce `thinking_budget_tokens` to a value that fits the harness window.** 16K (~320s of thinking + prefill) fits comfortably under 600s and is still 5× the pre-#36 cap. 8K is safer still. The Qwen card's 32K is aspirational for hardware that generates faster than M-series.
2. **Raise the harness timeout to 900s+.** Buys headroom for the 32K budget but real-world UX won't tolerate 15-minute turns, so the budget cap is more honest.

I'd recommend **(1) — set `thinking_budget_tokens: 16384`** as the next calibration step.

---

## PR #36 target metrics

| Metric | Result |
|---|---|
| CP-10 / CP-24 hangs | ✅ Resolved |
| `orphaned_results_stripped` | 0 |
| `tool_iteration_limit_reached` | 0 |
| `preserve_thinking` flag active | yes |
| `model_call_error` count | 31 (27× transient 404, 3× 524, 1× context-size). The 404 burst at 07:40-07:55 was an SLM hiccup recovered via retry — paths in that window all passed. |

---

## Followups

1. **Calibrate `thinking_budget_tokens` to local hardware speed.** Drop from 32768 to **16384** in both `config/models.yaml` and `config/models.cloud.yaml`. Re-run; if CP-05 / CP-29 still hang, try 8192.
2. **Revisit `temperature: 1.0`.** Two stable single-assertion regressions (CP-01, CP-20) across both post-#36 runs suggest 1.0 hurts deterministic stages (intent classification). The Qwen card says 1.0 for "Thinking — General Tasks" but our intent stage isn't really general-thinking; it's structured classification. Options:
   - Revert to 0.6 (Qwen3.5 carryover that was passing CP-01 / CP-20 in EVAL-10).
   - Compromise at 0.7 (Qwen card's "instruct mode" temp).
   - Add per-stage temperature overrides (larger change; defer).
3. **Investigate why CP-29 silently hung with zero error chunks.** If the SLM is processing a request but never returns, the gateway has no client-side signal to abort. Consider an SLM-side `max_tokens` cap that's slightly below the gateway timeout to force a fail-fast at the model.
4. **Latency profile is now bimodal** — most paths run faster than yesterday (turn p95 247s → 155s), but the failures are at the wall-clock ceiling. Confirms hardware throughput, not architecture, is the new constraint.
5. **404 burst at 07:40-07:55** — 27 consecutive `404 Not Found` from the SLM tunnel. All recovered via retry, but worth a glance at the SLM-side logs to understand what happened (model reload? llama-server restart?).

---

## Recommended next action

Apply followup #1 (drop `thinking_budget_tokens` to 16384) in a small PR, rebuild, re-run. Expect CP-05 and CP-29 to pass, returning the run to baseline pass rate. Then evaluate followup #2 (temperature) based on whether CP-01 / CP-20 still fail.
