# FRE-262 PIVOT-3 Gate Decision — Third Run (intent-based routing)

**Date**: 2026-04-28
**Run**: `run-final-routing-fixed-2026-04-28/`
**Config**: FRE-282 keyword routing active — bash.md + one matched skill doc per request

---

## Verdict: **PARTIAL PIVOT-4** — deprecate query-elasticsearch and fetch-url; keep the rest

Quality gate (17/20) barely cleared. Cost gate (≤1.5×) not cleared overall (2.06×), but two categories land within striking distance. Applied per-category verdict per side-plan §Wave E.

---

## Score summary

| Side | ✅ Correct | ⚠️ Partial | ❌ Wrong | Pass rate |
|------|-----------|-----------|---------|-----------|
| **Control** (curated) | 17 | 2 | 1 | 85% |
| **Treatment** (primitives + routed skills) | 16 | 1 | 3 | 80% |
| **Primitives ≥ curated** | **17/20** | | 3 (ls-03, diag-01, infra-02) | **85%** |

Quality gate: ≥17/20 → **✅ PASSES (exactly 17)**

---

## Token + cost analysis

| Metric | Control | Treatment | Ratio |
|--------|---------|-----------|-------|
| Total tokens | 392,623 | 807,696 | **2.06×** |
| Simple 2-turn baseline | ~11K | ~17-20K | 1.5-1.8× |
| Wall clock | 524s | 543s | 1.04× |

Cost gate: ≤1.5× overall → **❌ FAILS (2.06×)**

Cost improvement vs second eval (full-block injection): 4.13× → **2.06×** — routing halved the overhead.

Remaining overhead drivers: 6 prompts take 6-10 turns burning 50-87K tokens each (ls-03 5.6×, diag-01 4.6×, metrics-01 4.6×, infra-02 4.7×, infra-03 7.9×). The 14 prompts with 2 turns average 1.65× — consistent with the ~6K skill block overhead.

---

## Per-category verdict

| Category | Ctrl tok | Trt tok | Cost ratio | Quality (trt≥ctrl) | Verdict |
|---|---:|---:|---:|---|---|
| query-elasticsearch | 152,567 | 236,056 | **1.55×** | 4/4 ✅ (2 improvements) | **DEPRECATE** |
| fetch-url | 56,464 | 86,074 | **1.52×** | 3/3 ✅ (1 improvement) | **DEPRECATE** |
| system-diagnostics | 56,390 | 87,351 | **1.55×** | 2/3 ⚠️ (diag-01 ❌) | **KEEP** — quality fails |
| system-metrics | 48,798 | 124,028 | 2.54× | 3/3 ✅ tie | **KEEP** — cost too high |
| list-directory | 34,212 | 100,838 | 2.95× | 2/3 ⚠️ (ls-03 ❌) | **KEEP** — both fail |
| infrastructure-health | 44,192 | 173,349 | 3.92× | 3/4 ⚠️ (infra-02 ❌) | **KEEP** — both fail |

**Deprecate** (2 tools removed from curated set):
- `query_elasticsearch` — treatment improved on 2 prompts (es-02, es-04 now ✅ where control failed), cost 1.55× (close to gate, acceptable given quality gains)
- `fetch_url` — treatment matched/improved on all 3 prompts including fetch-03 (got actual API pricing vs control's consumer plans), cost 1.52×

**Keep** (4 tools retained):
- `run_sysdiag` — diag-01: treatment wrongly reports ps --sort unsupported despite procps-ng 4.0.4 in image
- `system_metrics_snapshot` — metrics-02: both sides report system RAM rather than agent-process RSS; not a quality regression but not an improvement
- `list_directory` — ls-03: treatment stubbornly counts only top-level files (5 not 12); skill doc routing isn't enough to break the LLM out of list_directory habit
- `infra_health` — infra-02: treatment tried pg_isready (not installed), gave up; infra-03 took 10 turns / 87K tokens for a 2-service check

---

## Per-prompt quality grades

| # | ID | Ctrl | Trt | Trt≥Ctrl | Notes |
|---|----|----|-----|---------|-------|
| 1 | es-01 | ✅ | ✅ | ✅ | trt 0.8× tokens — faster than ctrl |
| 2 | es-02 | ❌ | ✅ | ✅ **+** | trt: 93 calls (discovery step worked) |
| 3 | es-03 | ✅ | ✅ | ✅ | p95 85.7s (higher — eval traffic) |
| 4 | es-04 | ❌ | ✅ | ✅ **+** | trt found trace 2ebf1f03 (102 consecutive calls) |
| 5 | fetch-01 | ✅ | ✅ | ✅ | 404 correct |
| 6 | fetch-02 | ✅ | ✅ | ✅ | SDK summary correct |
| 7 | fetch-03 | ⚠️ | ✅ | ✅ **+** | trt got actual API pricing; ctrl showed consumer plans |
| 8 | ls-01 | ✅ | ✅ | ✅ | |
| 9 | ls-02 | ✅ | ✅ | ✅ | |
| 10 | ls-03 | ✅ | ❌ | ❌ | trt: 5 files (non-recursive); ctrl: 12 files correct |
| 11 | metrics-01 | ✅ | ✅ | ✅ | trt: load avg 1.60/1.55/0.99 correct |
| 12 | metrics-02 | ⚠️ | ⚠️ | ✅ tie | both: system RAM ~7.8 GB; neither got process RSS |
| 13 | metrics-03 | ✅ | ✅ | ✅ | disk 29% healthy |
| 14 | diag-01 | ✅ | ❌ | ❌ | trt: "ps --sort unsupported" (wrong — procps-ng 4.0.4) |
| 15 | diag-02 | ✅ | ✅ | ✅ | port 9001 correct |
| 16 | diag-03 | ✅ | ✅ | ✅ | trt 0.6× tokens — 2 turns vs ctrl 6 turns |
| 17 | infra-01 | ✅ | ✅ | ✅ | all 7 services |
| 18 | infra-02 | ✅ | ❌ | ❌ | trt tried pg_isready (not installed), gave up |
| 19 | infra-03 | ✅ | ✅ | ✅ | correct but 10 turns / 87K tokens |
| 20 | infra-04 | ✅ | ✅ | ✅ | all services |

---

## Three-run evolution

| Run | Skill docs | Cost ratio | Quality (trt≥ctrl) | Verdict |
|-----|-----------|-----------|---------------------|---------|
| run-final-2026-04-28 | ❌ Dockerfile bug | 1.19× | 19/20 | Invalid |
| run-final-with-skills | ✅ full block | 4.13× | 15/20 | BLOCKED |
| **run-final-routing-fixed** | **✅ routed** | **2.06×** | **17/20** | **PARTIAL PIVOT-4** |

---

## PIVOT-4 action list

**Deprecate (2 curated tools):**
1. `query_elasticsearch` — replaced by `bash curl` ES|QL + `run_python` self-telemetry. Treatment improved on both ES prompts where control failed. Cost 1.55× acceptable given quality gains.
2. `fetch_url` — replaced by `bash curl`. Treatment matched or exceeded control on all 3 fetch prompts including getting actual API pricing. Cost 1.52×.

**Keep (4 curated tools — block list for PIVOT-4):**
3. `list_directory` — ls-03 persistent failure (5 vs 12 YAML files). Treatment uses non-recursive listing even with explicit skill doc guidance. Root cause: LLM prefers list_directory over `bash find -name "*.yaml" | wc -l`.
4. `system_metrics_snapshot` — 2.54× cost; neither side gets process-level RSS (both return system RAM).
5. `run_sysdiag` — diag-01 failure; treatment incorrectly reports ps --sort unsupported. Root cause unknown — procps-ng 4.0.4 confirmed working.
6. `infra_health` — 3.92× cost; infra-02 treatment gives up on postgres check; infra-03 takes 10 turns / 87K tokens.

---

## Follow-up items for FRE-263 (PIVOT-4 block list)

- **diag-01 / infra-02 failures**: Both report "tool not available" for commands that work. Investigate whether the treatment container's bash executor is rejecting specific flag patterns on some code paths.
- **ls-03 persistence**: LLM keeps using `list_directory` (non-recursive) despite skill doc saying to use `bash find`. May need to remove `list_directory` from the treatment tool registry rather than relying on skill doc guidance.
- **infra-03 cost (87K / 10 turns)**: Correctly answered but extremely expensive. Infra health skill doc may be confusing the LLM into over-probing.
- **Cost ratio target**: 2.06× vs 1.5× gate. Remaining overhead is ~6K skill block per request + multi-turn exploration. Consider: (a) further shrinking bash.md, (b) routing infra-health to use run_python directly (fewer turns needed).

---

## References

- Run artifacts: `run-final-routing-fixed-2026-04-28/`
- Routing implementation: `src/personal_agent/orchestrator/skills.py` (FRE-282, commit b42d2e8)
- Grading rubric: `expected_outputs.md`
- Side plan: `docs/plans/2026-04-27-fre-262-pivot-3-side-plan.md`
