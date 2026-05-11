# EVAL-2026-05-11-subagent-qwen36 — Comparison Report

**New run:** EVAL-2026-05-11-subagent-qwen36 (Qwen3.6-A3B-subagent replaces Qwen3.5-9B-8bit)
**Compared against:** EVAL-2026-05-11-clean (same config + old 9B sub-agent, this morning's 34/37)
**Baseline:** EVAL-10-post-enhance (2026-04-14)

---

## Headline

| Metric | EVAL-10 | clean | **qwen36-subagent** |
|---|---|---|---|
| Paths passed | 33/37 | **34/37** | 33/37 |
| Assertions passed | 175/181 (96.7%) | **177/181 (97.8%)** | 174/181 (96.1%) |
| Sub-agent success rate | n/a | 5/21 (24%) | **24/24 (100%)** |
| Sub-agent avg duration (success) | n/a | 39.1s | **21.8s** |
| Sub-agent p50/p90/p99 | n/a | n/a | 22.2s / 31.0s / 43.3s |

The MoE sub-agent is a **strict win on the layer it touches** — 4× more reliable, ~2× faster, no MLX failure modes. But overall path count dropped by one due to a temperature-1.0 sampling regression on CP-05.

---

## What changed vs `clean`

**Exactly one path differs:** CP-05.

| Path | clean (9B sub-agent) | qwen36 (MoE sub-agent) | Cause |
|---|---|---|---|
| CP-05 Delegation Intent | 5/5 in 838s | **2/5, turn 1 timeout @ 600s** | Primary's tool-use loop ran 16 calls in 10 min — not a sub-agent issue (CP-05 turn 1 made zero sub-agent calls); temp-1.0 sampling variance picked a longer-loop trajectory |

All other 36 paths are byte-identical pass/fail to the clean run.

---

## Sub-agent layer evidence

Today's 24 sub_agent_complete events, all successful:

```
total: 24
  success=true: 24
duration p50=22.2s p90=31.0s p99=43.3s
```

Compared to yesterday's clean run on the 9B (same eval, same paths):
- 21 events, 5 success (24%), 16 × 60s MLX timeouts
- Successful ones averaged 39.1s

The MoE sub-agent's effective throughput is roughly **93 tok/s** (avg 22s for `max_tokens: 2048`) — the bandwidth math (3 GB/token vs the 9B's 9 GB/token) plays out as predicted.

---

## CP-05 deep-dive: why a previously-passing path failed

The path makes only **primary** model calls during its hung turn 1:

```
events during CP-05 turn 1: 16 model_call_completed, role=primary, sub_agent: 0
```

Sub-agent didn't enter the picture for this specific failure. CP-05 ("Delegation Intent — Explicit and Implicit") prompts the model into a clarification + tool-call loop. With temperature 1.0:
- Yesterday's sample: 4-7 iterations, converged at 361s
- Today's sample: 16 iterations, hit 600s harness ceiling

This is the same failure mode CP-24 (Ambiguous Intent) exhibits every run — turn 1's tool-iteration count is a function of `(prompt, sampling seed)`, and temp 1.0 widens the distribution enough that CP-05 went from inside-the-fence to outside-the-fence.

---

## Persistent failures (same as clean, all temp-1.0 driven)

| Path | Failure | Hypothesis |
|---|---|---|
| CP-01 | 7/8 — spurious `tool_call_completed` on conversational turn | temp 1.0 → intent variance |
| CP-20 | 4/5 — intent_classified `expected=conversational, actual=tool_use` | temp 1.0 → intent variance |
| CP-24 | 2/4 — turn 1 600s timeout (23 model calls) + turn 2 context overflow from carry-over | temp 1.0 → tool-loop length variance + cross-turn history accumulation |
| **CP-05** (new) | 2/5 — turn 1 600s timeout (16 model calls) | temp 1.0 → tool-loop length variance |

**Three of four failures share the same root cause: temperature 1.0 widens the tool-iteration distribution enough that some paths cross the 600s harness wall-clock.**

---

## Recommendation: drop temperature 1.0 → 0.6

The Qwen card recommends 1.0 for "Thinking — General Tasks", but our gateway runs the model in a tightly-instrumented agent loop where deterministic tool-call sequencing is what matters. The Qwen3.5 carry-over of 0.6 was a better calibration for THIS workload, even though it's not the card's headline number for thinking.

Going 1.0 → 0.6 should:
- **Fix CP-01, CP-20** (intent classification stops drifting)
- **Likely fix CP-05** (tool-loop length distribution tightens)
- **Possibly improve CP-24** (the underlying ambiguity is still hard, but the loop should be shorter)
- Slight quality cost on free-form thinking (less exploration) — small price

If 0.6 turns out too cold (fewer creative paths), 0.7 is the middle ground the card calls "Instruct (Non-Thinking)".

---

## Suggested next steps

1. **Small PR**: `temperature: 1.0 → 0.6` on primary in both `models.yaml` and `models.cloud.yaml`. Plus keep all other PR #36 changes.
2. **Re-run**: full 37-path. Expect CP-01, CP-20 → pass; CP-05 → likely pass; CP-24 → probably still hard.
3. **If CP-24 still fails**: it's the genuine architectural issue (tool-loop runaway on ambiguous prompts). Then the next levers are `loop_gate` cap reduction or within-turn tool-history compression. Separate plan.

---

## Latency profile (for context)

| Metric | clean | qwen36 |
|---|---|---|
| Total eval wall time | 1:51 (h:mm) | 1:43 |
| Turn p50 | 21.7s | 25.3s |
| Turn p95 | 248s | 333s (CP-24 turn 2) |

Slightly higher p95 because CP-24's turn 2 still failed but completed (333s) instead of being abandoned. The MoE sub-agent didn't lengthen sub-agent latency at all — its p99 is 43s.

---

## Bottom line

- ✅ **Sub-agent swap: strict win.** Keep it.
- ❌ **Temperature 1.0: should revert to 0.6.** That's the next PR.
- 🔍 **CP-24 architectural issue remains.** Separate follow-up after the temperature fix isolates what's truly architectural vs sampling.
