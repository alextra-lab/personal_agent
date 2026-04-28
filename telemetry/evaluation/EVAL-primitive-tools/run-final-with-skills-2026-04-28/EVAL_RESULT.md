# FRE-262 PIVOT-3 Gate Decision — Second Run (with skill docs loaded)

**Date**: 2026-04-28
**Run**: `run-final-with-skills-2026-04-28/`
**Context**: Re-run after discovering the first eval (`run-final-2026-04-28/`) ran without skill docs — `docs/skills/` was not in the Dockerfile.

---

## Verdict: **PIVOT-4 BLOCKED — skill block injection model must be redesigned first**

Both gate criteria failed. The full-block injection approach (all 9 skill docs concatenated, ~23K tokens, injected on every request) is counterproductive: it creates prohibitive cost overhead and produces new failures from context pressure.

---

## Score summary

| Side | ✅ Correct | ⚠️ Partial | ❌ Wrong | Pass rate |
|------|-----------|-----------|---------|-----------|
| **Control** (curated) | 17 | 1 | 2 | 85% (17/20) |
| **Treatment** (primitives + skill docs) | 14 | 1 | 5 | 70% (14/20) |
| **Primitives ≥ curated** | 15/20 prompts | | 5/20 (regressions) | 75% |

Gate thresholds: quality ≥17/20 → **FAIL (15/20)**; cost ratio ≤1.5× → **FAIL (4.13×)**

---

## Token + cost analysis

| Metric | Control | Treatment | Ratio |
|--------|---------|-----------|-------|
| Total tokens | 349,644 | 1,443,000 | **4.13×** |
| Baseline (simple 2-turn prompts) | ~11K | ~34K | **3.1× overhead from skill block alone** |
| Mean turns / prompt | 3.1 | 4.1 | 1.3× |
| Wall clock | 447s | 484s | 1.1× |

**Root cause of 4.13× cost**: The skill block injects all 9 skill docs (~23K tokens) as a fixed overhead on every request regardless of content. For simple 2-turn prompts, this triples the prompt token count before any tool call is made. For multi-turn prompts the overhead compounds.

---

## Per-prompt grades

| # | ID | Category | Ctrl | Trt | Trt ≥ Ctrl | Notes |
|---|----|-----------|----|-----|-----------|-------|
| 1 | es-01 | query-elasticsearch | ✅ | ✅ | ✅ | 0 errors correct |
| 2 | es-02 | query-elasticsearch | ❌ | ✅ | ✅ **IMPROVEMENT** | Discovery step worked — 79 tool calls found |
| 3 | es-03 | query-elasticsearch | ✅ | ✅ | ✅ | p95 97s (higher due to eval traffic) |
| 4 | es-04 | query-elasticsearch | ❌ | ✅ | ✅ **IMPROVEMENT** | Full top-10 loop trace table |
| 5 | fetch-01 | fetch-url | ✅ | ✅ | ✅ | 404 correct |
| 6 | fetch-02 | fetch-url | ✅ | ✅ | ✅ | SDK summary correct |
| 7 | fetch-03 | fetch-url | ⚠️ | ⚠️ | ✅ tie | Both partial |
| 8 | ls-01 | list-directory | ✅ | ✅ | ✅ | Correct |
| 9 | ls-02 | list-directory | ✅ | ✅ | ✅ | Correct; 8.8× tokens (6 turns) |
| 10 | ls-03 | list-directory | ✅ | ❌ | ❌ | Trt: "pipes not supported" (incorrect — pipes work); exhausted turns |
| 11 | metrics-01 | system-metrics | ✅ | ❌ | ❌ | Trt: bash command failures; gave up reporting system metrics |
| 12 | metrics-02 | system-metrics | ✅ | ❌ | ❌ | Trt: "ps flags not supported" (incorrect — procps-ng 4.0.4); gave up |
| 13 | metrics-03 | system-metrics | ✅ | ✅ | ✅ | Disk healthy |
| 14 | diag-01 | system-diagnostics | ✅ | ❌ | ❌ | Trt: "ps --sort not supported" (incorrect); gave up |
| 15 | diag-02 | system-diagnostics | ✅ | ✅ | ✅ | Port 9001 correct |
| 16 | diag-03 | system-diagnostics | ✅ | ✅ | ✅ | **1.1× tokens** — vmstat bounded correctly (6 × 5s samples) |
| 17 | infra-01 | infrastructure-health | ✅ | ✅ | ✅ | All 7 services |
| 18 | infra-02 | infrastructure-health | ✅ | ❌ | ❌ | Trt tried pg_isready (not installed), gave up; postgres IS reachable |
| 19 | infra-03 | infrastructure-health | ✅ | ✅ | ✅ | Neo4j + ES up |
| 20 | infra-04 | infrastructure-health | ✅ | ✅ | ✅ | All services |

---

## Two improvements, five regressions

**Fixed by skill docs** (2):
- es-02: discovery step → correct count
- es-04: explicit event_type example → found loop traces

**New regressions vs first eval** (5):
- ls-03, metrics-01, metrics-02, diag-01, infra-02: treatment gave up after bash command failures and incorrectly reported that tools don't work (pipes broken, ps flags missing, postgres unreachable)

**Why the regressions?** The 23K token skill block is injected across all 9 docs on every request. As context grows across tool-call turns, the LLM appears to misinterpret tool error responses or incorrectly conclude that tools are broken. The model reports that `ps --sort`, pipes, and pg_isready don't work — all of these were verified working in the container. This is a context-pressure / long-context reasoning failure, not a tool availability failure.

---

## Root cause: full-block injection is the wrong architecture

The current `get_skill_block()` function concatenates all 9 skill docs and injects the entire ~23K-token block on every request. This is wrong for two reasons:

1. **Cost**: Fixed 23K token overhead triples simple request costs (3.1× baseline ratio).
2. **Quality**: Large context causes LLM confusion about tool capabilities at high turn counts. The model starts reporting tool failures that don't exist.

**Required fix before PIVOT-4**: Intent-based skill injection — only inject the skill docs relevant to the detected task type:

| task_type / intent | Inject |
|---|---|
| `query_elasticsearch` | query-elasticsearch.md only |
| `list_directory` / filesystem | list-directory.md, read-write.md |
| `system_metrics` | system-metrics.md |
| `system_diagnostics` | system-diagnostics.md |
| `infrastructure_health` | infrastructure-health.md |
| `fetch_url` / web | fetch-url.md |
| general | run-python.md, bash.md |

This would reduce per-request overhead to 2-3K tokens (one skill doc) from 23K, bringing the cost ratio back toward the 1.19× seen in the first (no-skill-docs) run.

---

## Comparison across all three runs

| Run | Skill docs in image? | Cost ratio | Quality (trt ≥ ctrl) | Gate |
|-----|---------------------|-----------|---------------------|------|
| run-2026-04-27 (first eval) | ❌ No | 1.83× | 16/19 | Inconclusive |
| run-final-2026-04-28 | ❌ No (Dockerfile bug) | 1.19× | 19/20 | CLEARED (but invalid) |
| run-final-with-skills-2026-04-28 | ✅ Yes (full block) | 4.13× | 15/20 | **BLOCKED** |

No valid gate decision can be made until intent-based injection is implemented.

---

## PIVOT-4 pre-requisites

1. **Implement intent-based skill injection** in `orchestrator/skills.py` — inject only the matching skill doc based on `gateway_output.intent.task_type`
2. **Re-run this eval** — expected outcome: cost ratio back toward 1.2-1.5×, quality improvement on ES prompts preserved, system/diag tool confusion eliminated
3. **Then make the gate call**

PIVOT-4 remains blocked until a clean run with intent-based injection is available.

---

## References

- This run: `run-final-with-skills-2026-04-28/`
- First run (invalid): `run-final-2026-04-28/`
- Skills loader: `src/personal_agent/orchestrator/skills.py`
- Expected outputs: `expected_outputs.md`
