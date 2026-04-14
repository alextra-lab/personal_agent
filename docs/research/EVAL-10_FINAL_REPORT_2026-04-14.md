# EVAL-10 Final Report (FRE-187)

Date: 2026-04-14  
Issue: [FRE-187](https://linear.app/frenchforest/issue/FRE-187/eval-10-context-intelligence-final-verification-run)  
Spec: `docs/specs/CONTEXT_INTELLIGENCE_SPEC.md`

## Scope

This run validates Context Intelligence Phase 4 ENHANCE behavior and compares EVAL-10 outcomes against the EVAL-09 baseline.

## Commands Executed

```bash
# Category slice (optional fast feedback)
PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run \
  --categories context_management memory_quality cross_session \
  --output-dir telemetry/evaluation/EVAL-10-post-enhance/

# Full suite (37 paths) — responsiveness probe succeeds on current main (FRE-212)
PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run \
  --output-dir telemetry/evaluation/EVAL-10-post-enhance/
```

Targeted CP-30/CP-31 only (after fixes):

```bash
PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run \
  --paths CP-30 CP-31 \
  --output-dir telemetry/evaluation/EVAL-10-post-enhance/
```

Related regression/unit verification for Phase 4 components:

```bash
uv run pytest \
  tests/test_orchestrator/test_context_compressor.py \
  tests/test_orchestrator/test_async_compression.py \
  tests/personal_agent/request_gateway/test_state_document.py \
  tests/personal_agent/request_gateway/test_context.py -q
```

## Output Artifacts

- `telemetry/evaluation/EVAL-10-post-enhance/evaluation_results.json`
- `telemetry/evaluation/EVAL-10-post-enhance/evaluation_results.md`
- Runbook: `telemetry/evaluation/EVAL-10-post-enhance/README.md`

## Latest full-run metrics (2026-04-14T15:50:52Z)

| Metric | Value |
|--------|-------|
| Paths passed | 33 / 37 |
| Assertions passed | 175 / 181 |
| Assertion pass rate | **96.7%** |
| Avg turn response time | ~25.9 s |

## Baseline comparison (EVAL-09 vs EVAL-10)

- EVAL-09 assertion pass rate: `176/177` ≈ **99.4%**
- EVAL-10 assertion pass rate: `175/181` = **96.7%**
- EVAL-10 adds two cross-session paths (37 vs 35); remaining gap is six failed assertions across four paths.

## Paths that did not pass (`all_passed: false`)

| Path | Category | Primary failure mode |
|------|----------|----------------------|
| CP-05 | Intent Classification | Turn timeouts (300s); upstream LLM request timed out (~180s) in logs |
| CP-07 | Expansion & Sub-Agents | `query_elasticsearch` / ES ESQL error: unknown field `format` (HTTP 400) |
| CP-11 | Decomposition Strategies | Missing `decomposition_assessed` telemetry on two turns |
| CP-22 | Tool Reliability | Missing `tool_call_completed` (session still returned HTTP 200; see traces for tool/env issues) |

## Phase 4 evidence snapshot

- **Cross-session recall (CP-30, CP-31):** Passing after [FRE-210](https://linear.app/frenchforest/issue/FRE-210) (recall cue / intent classification). Confirmed in full run and targeted reruns.
- **Harness (FRE-211, FRE-212):** Post-path assertions awaited; responsiveness probe aligned with valid `session_id` — full run no longer requires `--skip-responsiveness-probe` as a workaround.
- **Memory quality:** FRE-189 stabilization (`get_user_interests` dynamic limit) supports memory-quality confidence checks in harness.

## Acceptance criteria traceability (FRE-187)

1. **Evaluation run completed with results documented** — **Met** (artifacts + this report).
2. **Pass rate ≥ EVAL-09 baseline (99.4%)** — **Not met** (96.7%); failures concentrated in CP-05, CP-07, CP-11, CP-22 as above.
3. **Phase 4 features validated** — **Partially met**: cross-session paths green; compression/KV-specific events still depend on harness telemetry surfacing for some checks (see runbook).
4. **Results written to `docs/research/` or evaluation output directory** — **Met**.

## Follow-up issues (resolved in this cycle)

- [FRE-210](https://linear.app/frenchforest/issue/FRE-210): CP-30/CP-31 recall regression — addressed.
- [FRE-211](https://linear.app/frenchforest/issue/FRE-211): Harness coroutine-not-awaited — addressed.
- [FRE-212](https://linear.app/frenchforest/issue/FRE-212): Responsiveness probe 422 — addressed.

## Closure note

FRE-187 verification **cycle is closed** with documented results. Assertion-rate parity with EVAL-09 was **not** achieved on the 2026-04-14 full run; remaining work is **operational and product** (LLM latency/timeouts, Elasticsearch query compatibility, telemetry completeness for decomposition and tool lifecycle), not unspecified Phase 4 implementation gaps. Track new issues for CP-05/CP-07/CP-11/CP-22 if re-baselining above 99.4% is required.
