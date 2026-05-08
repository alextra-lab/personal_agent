# ADR-0066: Skill Routing Defaults, Library-Size Threshold, and Missing-Skill Feedback Loop

**Status**: Accepted
**Date**: 2026-05-07
**Deciders**: Project owner
**Related**: ADR-0028 (CLI-first tool integration tiers — defines SKILL.md as Tier 2), ADR-0063 (primitive tools — created the bash/read/write/run_python primitives that depend on skill docs for guidance), FRE-326 (consolidation gate re-eval — same Captain's-Log feedback shape), FRE-244 / ADR-0056 (error pattern monitoring — same "agent named the gap" telemetry pattern)
**Linear**: FRE-328 (Phase-1 missing-skill telemetry follow-up)
**Eval data**: `telemetry/evaluation/EVAL-skill-routing-2026-05/` (run id `2026-05-07`, 6 cells × 10 prompts)

---

## Context

### The triggering incident

On 2026-04-29 the local Qwen agent was asked an ES diagnostic question. The system prompt at the time injected a single skill body chosen by a hardcoded `_KEYWORD_ROUTES` table in `src/personal_agent/orchestrator/skills.py`. The matched skill was `bash.md`, which contained no ES-index discipline. The agent inferred an index name from training-data priors (`logs-*`), looped 25 tool iterations against an empty result set, and returned a confabulated response. Two structural problems surfaced:

1. **Hardcoded routing**: the keyword table was patched once per skill. New skills required editing the table; misses were silent.
2. **Single-skill injection**: the routing returned exactly one body, so the agent never saw `query-elasticsearch.md`'s discipline alongside `bash.md`'s shell guidance for an ES-via-curl prompt.

A four-phase implementation landed across PRs #20–#23 (2026-05-04..2026-05-06):

- **Phase A** — replaced `_SKILL_FILES` / `_KEYWORD_ROUTES` with glob + YAML-frontmatter auto-discovery. 14 skill docs declare `name`, `description`, `when_to_use`, `tools`, `keywords`, `canonical_patterns`, `known_bad_patterns`. Adding a skill = drop a `.md` file.
- **Phase B** — added a compact skill index (frontmatter summaries only, no bodies), a `read_skill` tool that lazy-fetches full bodies, and a `hybrid` routing mode that injects both. Per-conversation dedup via `ctx.loaded_skills`.
- **Phase B.5** — pre-execution guards: `known_bad_patterns` in skill frontmatter are checked against tool arguments before dispatch. The `/logs-*` pattern fires before any ES call.
- **Phase C** — separate routing model. `skill_routing_model_key` (default `claude_haiku`) runs an LLM pre-flight that names which skills to pre-load. Independent of the primary model path because the local SLM is single-threaded and cannot run routing concurrently.
- **Phase D** — eval harness. 6-cell matrix `{local, cloud} × {keyword, hybrid, model_decided}`, 10 prompts per cell, per-request `skill_routing_mode` override on `/chat` so cells run without gateway restarts.

Implementation defaults are already in `src/personal_agent/config/settings.py`: `skill_routing_mode: str = "hybrid"`, `skill_routing_model_key: str = "claude_haiku"`. This ADR documents *why* those values, what the eval data showed, and what to do as the skill library grows.

### What the eval showed (run id `2026-05-07`) — original data †

| Cell | iter_limit | es_correct | read_skill | guard_blocks | routing_call | skill_chars/req |
|------|-----------|------------|------------|--------------|--------------|-----------------|
| cloud-keyword | 0% | **100%** | 0% | 0% | 0% | 5,193–14,096 |
| cloud-hybrid | 0% | **100%** | 0% | 0% | 0% | 6,856–15,759 |
| cloud-model-decided | 0% | **100%** | 40% | 0% | 100% | **1,661** |
| local-keyword | 0% | **100%** | 0% | 0% | 0% | 5,193–14,096 |
| local-hybrid | 0% | **100%** | 0% | 0% | 0% | 6,856–15,759 |
| local-model-decided | 0% | **100%** | 50% | 0% | 100% | **1,661** |

† **All `model_decided` rows above captured under two concurrent bugs** (D4 budget `KeyError` + duplicate event name). `model_decided` metrics reflect primary-model `read_skill` recovery, not router quality. See FRE-330 postfix data below.

### Postfix data — `cloud-model-decided` with both bugs fixed (run id `2026-05-08-postfix`) ‡

| Metric | 2026-05-07 (broken) | 2026-05-08-postfix (fixed) | Delta |
|--------|--------------------|-----------------------------|-------|
| `iter_limit_rate` | 0% | 0% | — |
| `routing_call_rate` | 100% | 100% | — |
| `read_skill_invoked_rate` | **40%** | **0%** | −40 pp (router now does the work) |
| `router_recall_mean` | ~0% (router returned []) | **94%** | +94 pp |
| `router_precision_mean` | n/a | **78%** | new metric |
| `router_empty_rate` | 100% | **10%** ¹ | −90 pp |
| `clean_success_rate` | n/a | **90%** ² | new metric |
| `routing_latency_p50` | ~50ms (fake; cached KeyError) | **~750ms** | real Haiku call |
| `skill_chars/req` (no-skill prompt) | 1,661 | **1,661** | unchanged |
| `skill_chars/req` (skill prompts) | 1,661 | **4,367–18,344** | router now injects skill bodies |

¹ The 10% router_empty is the `no_skill_needed` prompt (Fibonacci question) — **correct behaviour**; the router correctly identified no skills were needed.

² The 10% `failed` is `codebase_search` — a ground-truth calibration issue: the router returned `[bash, read-write]` vs expected `[bash, list-directory]`. The primary model completed the task correctly using `bash` alone. This is a false negative in the classification; the ground-truth label will be refined in FRE-334 (ambiguous/negative-control prompt expansion).

‡ `local-model-decided` not re-run: local SLM server offline at time of re-run. Previous 50% `read_skill` row retains the same caveat as cloud.

Three findings drive the rest of this ADR:

**Finding 1 — `hybrid` and `keyword` reach the same correctness target with no extra LLM calls.** Both produce 100% `es_first_call_correct` and 0% iteration-limit hits. `hybrid` injects ~10% more characters per request (the compact index is appended to the keyword bodies), but no skill needed lazy-loading because the keyword match was always sufficient.

**Finding 2 — `model_decided` with a working router reaches 90%+ clean_success with 0% `read_skill` fallback.** The router (Haiku) pre-loads the right skills in ~750ms, eliminating the 40% `read_skill` overhead observed when the router was broken. The injection cost adapts to the prompt (1,661 chars for no-skill requests, up to 18,344 for complex ES queries), making it the most token-efficient mode when the router is healthy.

**Finding 3 (amended) — the routing pre-flight now works.** Post-fix, `router_recall_mean = 0.94` and `read_skill_invoked_rate = 0%`. The broken-router finding from 2026-05-07 (Finding 3 in the original draft: "pre-flight doing zero useful work") is superseded. The 750ms Haiku latency is the new cost baseline for `model_decided`.

### Why this matters

The eval looked at a 14-skill library. At that size, every keyword-matched skill body fits in the system prompt with budget to spare. As the skill library grows — driven by `missing_skill_requested` feedback (FRE-328), Wave-F self-updating skills (FRE-226), and human-authored additions — the keyword/hybrid injection grows with it. At some library size the injection exceeds the budget headroom and the index-only approach has to take over. The ADR has to name that threshold so future-self knows when to flip the default.

The `missing_skill_requested` feedback loop is also live now in concept but unlogged: the agent already names what it needs when it calls `read_skill("nonexistent-name")` and the executor errors. Capturing that signal closes the loop from "agent can't find a skill" → "human approves a Linear ticket" → "skill is authored". That mechanism is in scope of this ADR because it directly determines how fast the library grows and therefore when the threshold is crossed.

---

## Decision

Lock `hybrid` as the default routing mode for both local and cloud profiles. Keep `model_decided` available behind a flag for when the skill library outgrows hybrid's injection budget. Investigate the latent routing-pre-flight bug separately. Wire the `missing_skill_requested` feedback loop in two phases.

### D1 — Default routing mode is `hybrid` for both profiles

`AGENT_SKILL_ROUTING_MODE=hybrid` ships as the default, confirmed by the eval data above. Rationale:

1. **Same correctness as `model_decided`** at the current library size (100% es_correct, 0% iteration_limit). The 50ms routing-call tax buys nothing observable.
2. **Same correctness as `keyword`** with a marginal injection-size increase that earns the dedup property (`ctx.loaded_skills` prevents the same skill body from being re-injected after a `read_skill` call).
3. **Self-describing** — adding a skill = drop a `.md` file with frontmatter. No code edits, no keyword-table patches. This is the property the user explicitly preferred (per memory `feedback_prefer_self_describing_over_harness_routing`).

`keyword` mode is retained as a legacy fallback (Phase A behavior) but is no longer the recommended starting point. `model_decided` is retained as the path forward when the library grows — see D2.

The default applies to both `local` (Qwen 35B via SLM tunnel) and `cloud` (Sonnet) profiles. The eval data showed Qwen and Sonnet behave identically on the routing dimension; the local profile does *not* need a different default. Skill content is injected into the system prompt regardless of which model handles it; the routing decision is upstream of model selection.

### D2 — Switch to `model_decided` when keyword/hybrid injection exceeds 6,000 tokens per request (≈24,000 chars)

Mechanism:

- **Threshold value**: 6,000 tokens of skill content per request, measured as the p95 of `skill_index_injected_chars` (already logged) divided by 4 (chars→token estimate). At the current library this is ~3,940 tokens (15,759 chars worst-case) — half the threshold.
- **Trigger source**: a Captain's-Log job reading `skill_index_assembled` events from ES, computing rolling p95 over the last 7 days, and filing a Linear `Needs Approval` ticket when the threshold is exceeded for two consecutive days.
- **Action on trigger**: ticket recommends flipping `AGENT_SKILL_ROUTING_MODE=model_decided`. The flip is a `.env` change + container restart, no code change. The compact-index path becomes the live serving path; keyword-matched bodies stop being injected; `read_skill` becomes the primary skill-fetch path; the routing pre-flight earns its keep at the larger library size.
- **Threshold rationale**: 6,000 tokens leaves ≥58,000 tokens of conversation budget against the smaller of the two primary models (Qwen 35B context_length 64,000). Cloud Sonnet has 200,000 tokens of context, so the threshold is not the binding constraint there — it's the local profile that bounds the choice.

This is "schema-ready, policy-deferred" — same shape as ADR-0065's per-user policy. The `model_decided` path is implemented and tested; flipping it is a config toggle, not a build.

### D3 — `missing_skill_requested` feedback loop, two phases

**Phase 1 (FRE-328)** — when `read_skill_executor` is called with a `name` not in the skill cache, emit a structured `missing_skill_requested` event with `requested_name`, the trace_id, the calling tool's prompt context (already in scope), and the list of currently-known skill names. This is one `log.warning` line. No prerequisites; ships under FRE-328.

**Phase 2** — Captain's Log subscribes to `missing_skill_requested` events. When the same `requested_name` appears 3+ times across distinct sessions in a rolling 7-day window, file a Linear `Needs Approval` ticket: *"Create skill: `<requested_name>` — requested N times across M sessions."* The ticket carries the calling prompts (truncated) so a human can decide whether the gap is real. Phase 2 runs reactively via the existing `consolidation.completed` event consumer (same path as ADR-0056 error pattern monitoring) — no new infrastructure.

**Why human-gated, not auto-authored**: skill authoring requires a write surface (`docs/skills/`) inside the agent's controlled workspace, owner identity (so we know which user authorized the write), and a quality bar (the skill has to be useful). All three are in flight as Wave-E/F items (FRE-213 → FRE-227 → FRE-226). Until those land, "agent files a ticket, human merges the PR" is the right loop.

**Why this loop is in scope of this ADR**: the threshold in D2 is meaningful only if the skill library grows. The growth mechanism is the feedback loop. Without D3, the library is frozen at 14 skills and D2 never trips.

### D4 — Routing pre-flight bug: ROOT-CAUSED AND FIXED at commit `178f664` (2026-05-07)

> **Amendment 2026-05-07** — the original D4 text below described this as "investigate later". An investigation in the same session as the ADR pinned the root cause and shipped a fix. Original text retained for the record; status update follows.

**Root cause (none of the three suspected):** `factory.get_llm_client_for_key()` defaulted `budget_role="skill_routing"`, but `config/governance/budget.yaml` never declared that role. The Cost Check Gate (ADR-0065) raised `KeyError('skill_routing')` on every reservation. The exception propagated into `route_skills()`, where a catch-all `except Exception` silently swallowed it and returned `[]`. The 50ms latency was the round-trip to the cost gate's role lookup, not the LLM — confirming the call never reached Haiku.

**Fix:**
- `config/governance/budget.yaml` — declared `skill_routing` role (default_output_tokens=64, safety_factor=1.2, on_denial=raise) + caps ($0.10/day, $0.50/week, user-confirmed 2026-05-07).
- `src/personal_agent/orchestrator/skills.py` — split the exception handler. `KeyError` now re-raises as `skill_routing_call_misconfigured` (logged at `error` level) so future budget-config gaps surface loudly. LLM-call failures (network, model error) still fall back to `[]` so the primary agent's `read_skill` path keeps working.

**Verification:** post-fix, a single ES prompt against Haiku returned `['bash', 'query-elasticsearch', 'seshat-observations']` in 1,177ms (was 50ms cached KeyError). Saved at `telemetry/evaluation/EVAL-skill-routing-2026-05/cloud-model-decided-2026-05-07-fixed/`.

**Implication for the eval data in this ADR:** every `model_decided` row in the results table was produced under the broken pre-flight. End-to-end correctness was preserved only because the primary model's `read_skill` fallback compensated. **The reported `model_decided` metrics measure primary-model recovery, not router quality.** A clean baseline must be re-captured (FRE-330) before any quantitative claim about `model_decided` performance is made.

#### Original D4 text (superseded — kept for posterity)

> Every `model_decided` trace in the eval shows `routing_skills_returned: []` from the Haiku pre-flight. The primary model still routes correctly via `read_skill`, so end-to-end correctness is unaffected, but the pre-flight is currently doing zero useful work — a 50ms tax with no return. Three plausible causes: prompt template too compressed; parser too strict; index format ambiguous. This ADR does not pick a cause; it files an investigation as a Wave-F follow-up to be opened *after* D2 trips. When `model_decided` becomes the live path, the bug becomes load-bearing and has to be fixed; until then it's a latent improvement, not a blocker.

### D5 — Local vs cloud asymmetries observed but not actioned

Two differences surfaced and are noted for future-self:

1. **Qwen pulls `read_skill` more aggressively (50%) than Sonnet (40%)** in `model_decided`. Qwen 35B is more conservative about acting without explicit context, so it prefers fetching the skill body before any tool call. This is a feature (more explicit grounding) not a bug; no action.
2. **One Qwen `es_incident_class` ReadTimeout** in `local-keyword` (10-minute harness limit). The 35B model on a single GPU produced a long thinking trace that exceeded the harness timeout. Not a routing-mode property; the prompt is genuinely complex. Mitigated downstream by `local-hybrid` and `local-model-decided` completing the same prompt in 3–7 minutes. No action in this ADR.

---

## Consequences

**Positive:**

- Default behavior matches the empirically best-validated mode for the current library size, with zero new infrastructure.
- Adding a skill remains a one-file operation (drop `.md` with frontmatter). The self-describing property is preserved.
- The `missing_skill_requested` loop names library gaps automatically; the agent can request what it needs without a human noticing the miss first.
- A clean trigger for the eventual `hybrid → model_decided` flip exists and is monitored. No human has to "remember to switch when the library grows."
- The `model_decided` implementation is preserved as a fully-tested fallback path. The flip is a `.env` toggle, not a build.

**Negative:**

- Hybrid injects 6,856–15,759 chars on every request to the primary model. At Qwen's 64,000-token context, this is 1.5–4% of the budget — non-trivial but well within headroom. Cloud Sonnet (200k context) is unaffected.
- The `read_skill` tool is still registered and visible to the model in `hybrid` mode even though it's effectively never called there (0% in the eval). This is a minor governance surface that has to be considered when adding new tools or governance modes.
- The latent routing-pre-flight bug (D4) means we currently cannot trust `routing_skills_returned` as a signal source. Captain's Log monitors that depend on it must wait for D4's fix.
- The threshold value in D2 (6,000 tokens) is judgment-based, not measured against an actual injection-budget overflow. If real-world prompts have larger context budgets in flight (proactive memory, conversation history, tool-result buffers), the threshold may need to drop.

**Neutral / explicit non-goals:**

- This ADR does not define how a skill is authored. That's FRE-226 / Wave F. The feedback loop only goes as far as filing a Linear ticket.
- This ADR does not change the per-tool skill linkage or the B.5 guard mechanism. Both ship as-is.
- This ADR does not change the `keyword` mode's behavior. It remains a supported fallback, just not the recommended default.
- The Haiku routing model (`skill_routing_model_key=claude_haiku`) is retained as the default routing model even though the routing call currently returns empty. The cost of the call is small ($0.0001/req), and fixing D4 is the path that makes the call useful — not switching the routing model.

---

## Implementation Notes

This ADR is largely retrospective: Phases A–D are already shipped (commits `08dc14b` and earlier on `main`). What this ADR adds is:

| Sub-task | Linear | Tier | Notes |
|----------|--------|------|-------|
| D1 — confirm `hybrid` as default in settings | inline | Sonnet | Already shipped; ADR-only docstring nudge if behavior diverges in the future |
| D2 — Captain's-Log threshold monitor for `skill_index_injected_chars` p95 | (to file) | Sonnet | Reuses the ADR-0056 / FRE-244 pattern monitor scaffolding |
| D3 Phase 1 — emit `missing_skill_requested` event from `read_skill_executor` | **FRE-328** | Sonnet | One-line `log.warning`; tests assert event shape |
| D3 Phase 2 — Captain's-Log subscriber that files Linear ticket at threshold | (folded into FRE-328) | Sonnet | Mirrors ADR-0056 cluster-then-file pattern |
| D4 — investigate Haiku routing returning `[]` | (to file) | Opus → Sonnet | Investigation first (Opus); fix likely Sonnet. Defer until D2 trips |

D2 and D4 will be filed as separate Linear tickets during the next planning loop; FRE-328 covers D3 Phase 1+2 and is the immediate next implementation.

---

## Status Tracking

| Phase | Linear | PR | Status |
|-------|--------|-----|--------|
| Phase A — frontmatter auto-discovery | (pre-ADR work) | #20 | ✅ Merged 2026-05-06 |
| Phase B — skill index + read_skill + hybrid | (pre-ADR work) | #22 | ✅ Merged 2026-05-06 |
| Phase B.5 — known_bad_patterns guards | (pre-ADR work) | #22 | ✅ Merged 2026-05-06 |
| Phase C — separate routing model | (pre-ADR work) | #23 | ✅ Merged 2026-05-06 |
| Phase D — eval harness + 6-cell matrix | (pre-ADR work) | commit `08dc14b` | ✅ Run completed 2026-05-07 (note: `model_decided` cells captured under D4 bug — see FRE-330 for clean baseline) |
| D3 Phase 1 — `missing_skill_requested` event | FRE-328 | (pending) | ✅ Approved 2026-05-07 |
| D3 Phase 2 — Captain's Log filing | FRE-328 (same) | (pending) | ✅ Approved 2026-05-07 |
| D2 — threshold monitor | FRE-335 | (pending) | ✅ Approved 2026-05-07 |
| **D4 — routing pre-flight `KeyError('skill_routing')` fix** | inline | commit `178f664` | ✅ **Shipped 2026-05-07 (mid-ADR session)** |
| Eval methodology — `es_first_call_correct_rate` `or`/`and` bug fix | FRE-329 | (pending) | ✅ Approved 2026-05-07 |
| Eval methodology — re-run model_decided cells post-router-fix | FRE-330 | PR pending | ✅ **cloud-model-decided re-run complete 2026-05-08** (local-model-decided skipped: SLM offline) |
| Eval methodology — split router-only vs end-to-end metrics | FRE-331 | PR pending | ✅ **Shipped 2026-05-08** — ground-truth labels + 7 new metrics |
| Eval methodology — ES polling instead of fixed sleep | FRE-332 | (pending) | ✅ Approved 2026-05-07 |
| Eval methodology — ES pagination past size=500 | FRE-333 | (pending) | ✅ Approved 2026-05-07 |
| Eval methodology — expand prompt set (ambiguous + neg-control + adversarial) | FRE-334 | (pending) | ✅ Approved 2026-05-07 |
