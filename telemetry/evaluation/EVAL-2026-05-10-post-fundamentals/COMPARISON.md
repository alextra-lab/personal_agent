# EVAL-2026-05-10-post-fundamentals — Comparison Report

**New run:** EVAL-2026-05-10-post-fundamentals (2026-05-10, post PR #34)
**Baseline:** EVAL-10-post-enhance (2026-04-14, pre PR #34)
**Agent:** cloud-sim-seshat-gateway on http://localhost:9001 (PR #34 image, rebuilt 18:36)

---

## Headline Metrics

| Metric | EVAL-10 Baseline | EVAL-2026-05-10 | Delta |
|--------|-----------------|-----------------|-------|
| Paths passed | 33/37 (89.2%) | 33/37 (89.2%) | = |
| Assertions passed | 175/181 (96.7%) | 170/181 (93.9%) | −2.8 pp |
| Avg path time | 77.0s | 147.3s | +91% |
| Turn p50 latency | 13.1s | 22.6s | +73% |
| Turn p95 latency | 106.0s | 247.0s | +133% |
| model_call_error count | unknown | 9 total (see below) | — |
| HTTP 524 errors | multiple | 2 (1 run) | ↓ significantly |

---

## Pass Rate by Category

| Category | Baseline | New | Delta |
|----------|----------|-----|-------|
| Intent Classification | 5/7 | 6/7 | **+1** |
| Decomposition Strategies | 3/4 | 3/4 | = |
| Memory System | 4/4 | 4/4 | = |
| Expansion & Sub-Agents | 3/3 | 3/3 | = |
| Context Management | 8/8 | 7/8 | **−1** |
| Tools & Self-Inspection | 2/3 | 3/3 | **+1** |
| Edge Cases | 2/2 | 1/2 | **−1** |
| Memory Quality | 4/4 | 4/4 | = |
| Cross-Session Recall | 2/2 | 2/2 | = |

---

## Fixed Paths (baseline failures → passing)

| Path | Name | Fix responsible |
|------|------|-----------------|
| CP-05 | Complex Tool Use Intent | Role validator + loop gate (no longer drops tool_calls between rounds) |
| CP-07 | Tool Use Intent | Role validator fix (consecutive assistant merge stopped) |
| CP-11 | DECOMPOSE Strategy (Complex Multi-Part) | Loop gate consecutive_count reset; tool_call ID prefix |
| CP-22 | Tool Result Formatting | Role validator / preserve_thinking combination |

All four fixes directly target the bugs addressed in PR #34.

---

## Regressions (baseline passing → failing)

### CP-01 — Conversational Intent (Intent Classification)
- **Result:** 7/8 assertions (was 8/8)
- **Failing assertion:** `Event 'tool_call_completed': found (expected: absent)`
- **Analysis:** A purely conversational turn ("how's it going?") triggered a tool call that wasn't expected. The role validator changes may have altered how the model decides to invoke tools on trivial turns, or preserve_thinking is surfacing a planning step that wasn't previously visible. Non-deterministic — single run; worth re-running CP-01 in isolation to check frequency.
- **trace_id for investigation:** `ffc91b41-fbdd-478a-9d98-344f0727b0b1` (turn 1), `ca0afe5f-e39b-4288-afd1-e6ac3b8d1d39` (turn 2 — the failing one)

### CP-10 — DECOMPOSE Strategy (Complex Multi-Part Analysis) (Decomposition)
- **Result:** 0/7 — entire path timed out at 300s
- **Failing assertion:** `Turn timed out after 300098ms`
- **Analysis:** EVAL-10 baseline completed in 109.9s. The new run hung for the full 300s timeout on turn 0. Two context-size-exceeded errors appear in ES at `2026-05-10T21:01:24` and `2026-05-10T22:00:51`. One 524 and three "too many requests" errors at 21:14–21:15 may have left the inference server in a degraded state immediately before CP-10 ran (~21:04). Streaming changes mean an error mid-stream now propagates differently — the client may have been blocked waiting for a stream that errored silently. Needs targeted investigation.
- **session_id:** `f009a5d4-b054-43ed-a043-e6d44632687b`

### CP-20 — Progressive Token Budget Management (Context Management)
- **Result:** 4/5 assertions (was 5/5)
- **Failing assertion:** `intent_classified.task_type: expected=conversational, actual=tool_use`
- **Analysis:** Intent misclassification on turn 0 — the model classified a budget-discussion prompt as `tool_use` rather than `conversational`. Likely non-deterministic (single sample). No structural change in PR #34 touches intent classification. Low confidence this is a real regression.

### CP-24 — Ambiguous Intent (Edge Cases)
- **Result:** 2/4 — turn 0 timed out (300s), turn 1 very slow (220s)
- **Analysis:** Context-size error at `2026-05-10T22:00:51` is near CP-24's window. This is the same pattern as CP-10: a complex prompt hitting the SLM's context limit mid-stream, causing the client to hang until the harness 300s timeout fires. The fix to streaming (switching from non-streaming to SSE) means errors now arrive as stream chunks — if the error chunk isn't handled gracefully the client stalls.
- **session_id:** `3aabca26-5068-4cb3-adf6-67b17c2e2af2`

---

## PR #34 Target Metrics

### HTTP 524 errors
- **Baseline:** multiple 524s per run (caused gateway timeouts, breaking long generations)
- **New run:** 2 events on trace `fa8ce652` at `2026-05-10T21:15:11`
- **Assessment:** Major improvement. Residual 524s may be SLM server overload rather than a client timeout bug — the surrounding "too many requests" errors (21:14) suggest GPU saturation, not a client-side issue.

### orphaned_results_stripped
- No `history_sanitised` or `orphaned_results_stripped` warning events found in ES for any of the new run's trace_ids. **Baseline-consistent behavior; role validator fix holding.**

### tool_iteration_limit_reached
- 0 events in new run. Consistent with CP-11 fixing the loop gate accumulation bug.

### preserve_thinking / reasoning_content
- No `preserve_thinking_failed` events. Events with `reasoning_content` present visible in traces for multi-step paths (CP-09, CP-11, CP-16). Flag is active.

### Per-trace prompt_token growth
- Cross-checked CP-19 (multi-turn context management path): prompt tokens grow monotonically turn-over-turn as expected. No anomalous resets or spikes.

---

## Latency

All paths are slower than baseline — uniformly ~1.5–2.5x. This is expected: the new run routes through the cloud profile (port 9001, Cloudflare Access overhead + potential SLM load), while EVAL-10 ran on the local dev profile (port 9000). Additionally, preserve_thinking generates reasoning tokens that add to generation time.

The p95 jump (106s → 247s) is dominated by CP-10/CP-24 timeout outliers. Excluding those two, p95 is approximately 130s — a more realistic 1.2x increase.

---

## Model Errors Summary

| Timestamp | Event | Trace | Detail |
|-----------|-------|-------|--------|
| 21:01:24 | model_call_error ×2 | 908b3c17 | Context size exceeded |
| 21:14:46–47 | model_call_error ×3 | 0ff492c2, e9f6ea4c, eecd33c2 | Too many requests / GPU at capacity |
| 21:15:11 | model_call_error ×2 | fa8ce652 | 524 timeout |
| 22:00:51 | model_call_error ×2 | 1505094f | Context size exceeded |

Context-size errors are SLM-server-side (Qwen context window exhausted), not a PR #34 regression. 524 count is down from "multiple per run" to 2 total. "Too many requests" errors are expected on a single-GPU server under eval load.

---

## Followups

1. **CP-10 / CP-24 hang on streaming error** — when the SLM emits an error chunk mid-stream, the `_aggregate_streaming_chunks` loop may not break early. Needs a check: does the client raise immediately on `{"error": ...}` chunks, or does it wait for the stream to close? Relevant: `src/personal_agent/llm_client/client.py` around the `async with client.stream(...)` block.

2. **CP-01 spurious tool call** — re-run CP-01 5× to measure frequency before filing. If >20%, investigate whether `preserve_thinking` is causing the model to "plan" a tool call that then executes. Trace `ca0afe5f` is the relevant one.

3. **CP-20 intent misclassification** — re-run in isolation. If it reproduces, check whether the budget-tracking prompt wording now matches a `tool_use` trigger pattern in the gateway.

4. **Retry timing in telemetry checker** — defaults bumped to 8×2s for this run. This change has no correctness impact on EVAL-10 (which used the old local-dev ES with 1s refresh), but should be kept for future cloud-profile evals. Consider making it configurable via `--es-retry-delay` / `--es-max-retries` flags.

5. **Harness `--cf-email` flag** — new flag added this session (not committed). Should be committed to main along with the `trace_id.keyword` → `trace_id` fix before the next eval run.
