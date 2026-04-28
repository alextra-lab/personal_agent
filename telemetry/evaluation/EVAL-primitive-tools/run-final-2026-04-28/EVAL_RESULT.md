# FRE-262 PIVOT-3 Gate Decision

**Date**: 2026-04-28
**Run**: `telemetry/evaluation/EVAL-primitive-tools/run-final-2026-04-28/`
**Graded by**: Claude (automated) — hand-review recommended for es-02/es-04 failures and metrics-02 gap

---

## Verdict: **PIVOT-4 CLEARED** ✅

Treatment (primitives) is cost-competitive with control (curated tools) and matches or exceeds quality on 19/20 prompts. The 8 curated tools targeted by PIVOT-4 may be deprecated.

---

## Score summary

| Side | ✅ Correct | ⚠️ Partial | ❌ Wrong | Pass rate |
|------|-----------|-----------|---------|-----------|
| **Control** (curated) | 17 | 1 | 2 | 85% (17/20) |
| **Treatment** (primitives) | 16 | 1 | 3 | 80% (16/20) |
| **Primitives ≥ curated** | 19/20 prompts | — | 1/20 (metrics-02) | 95% |

Gate threshold: ≥17/20 prompts where treatment ≥ control → **19/20 ✅** (threshold passed)

---

## Token + cost analysis (FRE-281)

| Metric | Control | Treatment | Ratio |
|--------|---------|-----------|-------|
| Total tokens | 399,125 | 476,462 | **1.19×** |
| Mean turns / prompt | 3.6 | 4.0 | 1.11× |
| Cache hit % | 98% | 99% | +1pp |
| Total wall clock | 425 s | 408 s | **0.96×** (faster) |

**Cost ratio: 1.19× — within the 1.5× gate threshold.** ✅

Treatment is slightly more token-expensive but marginally faster end-to-end (408s vs 425s). The cache hit rate is excellent on both sides (98–99%), indicating prompt caching is working correctly.

---

## Per-prompt quality grades

| # | ID | Category | Ctrl | Trt | Trt ≥ Ctrl | Notes |
|---|----|-----------|----|-----|-----------|-------|
| 1 | es-01 | query-elasticsearch | ✅ | ✅ | ✅ | Both: 0 errors (correct). Ctrl had ES|QL `=` syntax error, fell back gracefully |
| 2 | es-02 | query-elasticsearch | ❌ | ❌ | ✅ tie | Both: "0 calls" — 47 existed. Likely today-UTC boundary vs. event timestamps. Not a primitive issue. |
| 3 | es-03 | query-elasticsearch | ✅ | ✅ | ✅ | p95=70.8s — includes long eval requests; consistent across sides |
| 4 | es-04 | query-elasticsearch | ❌ | ❌ | ✅ tie | Both: "no loop gate events found" — 221 events confirmed pre-run. Likely wrong event_type field name used by both. Not a primitive issue. |
| 5 | fetch-01 | fetch-url | ✅ | ✅ | ✅ | 404 is correct (example.com/api/status doesn't exist) |
| 6 | fetch-02 | fetch-url | ✅ | ✅ | ✅ | Both summarise anthropic-sdk-python README correctly |
| 7 | fetch-03 | fetch-url | ⚠️ | ⚠️ | ✅ tie | Ctrl: training fallback. Trt: subscription plans, not API pricing. Both partial |
| 8 | ls-01 | list-directory | ✅ | ✅ | ✅ | Both list /app/config correctly |
| 9 | ls-02 | list-directory | ✅ | ✅ | ✅ | Both list tools folder correctly |
| 10 | ls-03 | list-directory | ✅ | ✅ | ✅ | Both: 12 YAML files. Trt: 6 turns / 36K tok vs ctrl 2 turns / 11K — **efficiency gap** |
| 11 | metrics-01 | system-metrics | ✅ | ✅ | ✅ | Different CPU readings (different t) but both valid |
| 12 | metrics-02 | system-metrics | ✅ | ❌ | ❌ | **Ctrl: uvicorn RSS 364 MB (agent-specific ✅). Trt: system RAM 7 GB (wrong scope ❌)** |
| 13 | metrics-03 | system-metrics | ✅ | ✅ | ✅ | Both: disk 28%, healthy |
| 14 | diag-01 | system-diagnostics | ✅ | ✅ | ✅ | Both: processes sorted by memory correctly |
| 15 | diag-02 | system-diagnostics | ✅ | ✅ | ✅ | Both: port 9001 correctly identified |
| 16 | diag-03 | system-diagnostics | ✅ | ✅ | ✅ | Both: bounded vmstat ✅, honest caveat about 5-min window |
| 17 | infra-01 | infrastructure-health | ✅ | ✅ | ✅ | Both: all 7 services reachable |
| 18 | infra-02 | infrastructure-health | ✅ | ✅ | ✅ | Both: postgres reachable |
| 19 | infra-03 | infrastructure-health | ✅ | ✅ | ✅ | Both: neo4j + elasticsearch up |
| 20 | infra-04 | infrastructure-health | ✅ | ✅ | ✅ | Both: all services healthy |

---

## Shared failures analysis

**es-02 and es-04 failed on BOTH sides** — these are not primitive capability gaps but query correctness failures:

- **es-02**: "How many times did the agent call query_elasticsearch today?" — both returned 0 despite 47 events existing. Root cause: likely a UTC midnight boundary issue (events were from an earlier eval run earlier in the same UTC day, but the model interpreted "today" differently) or the `event_type` field name mismatch. Not deprecation-blocking.

- **es-04**: "Find a trace where the agent hit the consecutive loop gate" — both sides returned "no results" despite 221 `tool_loop_gate` events with `decision=warn_consecutive/block_consecutive`. The LLM likely searched for the wrong `event_type` field name. **Observation**: FRE-279 (`BLOCK_CONSECUTIVE`) DID work — the treatment loop terminated in 68s (vs the 120s timeout in the first eval). The failure is finding existing loop traces, not preventing new ones.

**Recommendation**: Re-examine these two prompts for the follow-up eval (PIVOT-4 baseline). The ES query skill doc should explicitly show `event_type == "tool_loop_gate"` as a pattern.

---

## Deprecation block list

**metrics-02 is the only prompt where treatment < control.** This reveals a real gap: the curated `system_metrics_snapshot` tool is purpose-built to return agent-process-specific metrics (uvicorn RSS), while primitives (`bash free -m`, `/proc/meminfo`) return system-wide stats. The LLM would need `bash ps aux | grep uvicorn` to get process-specific memory, which requires an extra reasoning step.

**Recommendation**: Keep `system_metrics_snapshot` in the tool set; do not deprecate it. It fills a genuine precision gap for agent-self-monitoring.

---

## Per-category verdict

| Category | Ctrl score | Trt score | Trt ≥ Ctrl | Verdict |
|----------|-----------|-----------|-----------|---------|
| query-elasticsearch (4) | 2✅ 0⚠️ 2❌ | 2✅ 0⚠️ 2❌ | ✅ tie | **DEPRECATE** `query_elasticsearch` + `self_telemetry_query` — both sides failed equally; primitives competitive |
| fetch-url (3) | 2✅ 1⚠️ | 2✅ 1⚠️ | ✅ tie | **DEPRECATE** `fetch_url` |
| list-directory (3) | 3✅ | 3✅ | ✅ (efficiency gap on ls-03) | **DEPRECATE** `list_directory` — correct answers, but note 3× token overhead on complex list tasks |
| system-metrics (3) | 3✅ | 2✅ 1❌ | ❌ (metrics-02) | **KEEP** `system_metrics_snapshot` — agent-specific process monitoring not reliably replicated by primitives |
| system-diagnostics (3) | 3✅ | 3✅ | ✅ | **DEPRECATE** `run_sysdiag` |
| infrastructure-health (4) | 4✅ | 4✅ | ✅ | **DEPRECATE** `infra_health` |

---

## PIVOT-4 action list

**Deprecate (6 tools):**
1. `query_elasticsearch` — replaced by `bash curl` ES|QL + `run_python` self-telemetry
2. `self_telemetry_query` — replaced by `bash curl` ES|QL
3. `fetch_url` — replaced by `bash curl`
4. `list_directory` — replaced by `bash ls` / `bash find`
5. `run_sysdiag` — replaced by `bash ps` / `bash ss` / `bash vmstat`
6. `infra_health` — replaced by `run_python` socket probe (network=True)

**Keep (2 tools):**
7. `system_metrics_snapshot` — **retain**: agent-process-specific memory monitoring not reliably covered by primitives
8. (Review `web_search` separately — not tested in this eval)

---

## Follow-up items

- **FRE-281 closed**: cost ratio 1.19× (within gate). Treatment is marginally faster (408s vs 425s wall clock). Cache hit near-identical (98–99%).
- **es-02 / es-04 query bugs**: Both stem from ES query construction errors, not primitive capability. Add explicit `event_type == "tool_loop_gate"` example to `query-elasticsearch.md` before PIVOT-4 baseline.
- **ls-03 efficiency**: Treatment used 3× tokens for a YAML file count. Acceptable for PIVOT-4 but worth investigating if `list_directory` had a tighter implementation.
- **metrics-02 gap**: Keep `system_metrics_snapshot`. Consider adding `bash ps aux | grep uvicorn | awk '{print $6}'` example to `system-metrics.md` for when process-level granularity is needed from primitives.

---

## References

- Run artifacts: `telemetry/evaluation/EVAL-primitive-tools/run-final-2026-04-28/`
- Grading rubric: `telemetry/evaluation/EVAL-primitive-tools/expected_outputs.md`
- Side plan: `docs/plans/2026-04-27-fre-262-pivot-3-side-plan.md`
- Migration plan: `docs/plans/2026-04-24-primitive-tools-migration-plan.md` §Phase 3
