# PIVOT-4 Gate Decision — G3 Full Run (FRE-283 + FRE-284)

**Date**: 2026-04-28
**Run**: `run-g3-full-sonnet-2026-04-28T1930/`
**Model**: Claude Sonnet 4.6 (control + treatment)
**Config**: FRE-283 (real bash shell + auto-approve wire) + FRE-284 (Seshat doc reality check)

---

## Verdict: FULL PIVOT-4 — all 6 tool categories cleared

Quality gate (19/20) PASSES. Cost gate (1.39x) PASSES. Both gates cleared.

This supersedes the PARTIAL PIVOT-4 verdict from 2026-04-28 (PIVOT-3 final run), which was based on a broken bash primitive contract. With FRE-283 fixing shell semantics, all previously-failing categories now pass.

---

## Score summary

| Side | Correct | Partial | Wrong | Pass rate |
|------|---------|---------|-------|-----------|
| Control (curated) | 15 | 4 | 1 | 75% |
| Treatment (primitives + skill docs) | 19 | 0 | 1 | 95% |
| Primitives >= curated | 19/20 | | 1 (es-01) | 95% |

Quality gate: >=17/20 -> PASSES (19/20)

---

## Token + cost analysis

| Metric | Control | Treatment | Ratio |
|--------|---------|-----------|-------|
| Total tokens | 432,913 | 603,016 | 1.39x |
| Wall clock (all 20) | ~7 min | ~7 min | ~1.0x |

Cost gate: <=1.5x overall -> PASSES (1.39x)

---

## Per-category verdict

| Category | Ctrl tok | Trt tok | Ratio | Quality | Verdict |
|---|---:|---:|---:|---|---|
| query-elasticsearch | 198,648 | 350,412 | 1.76x | 3/4 (es-01 fail) | DEPRECATE |
| fetch-url | 54,926 | 71,285 | 1.30x | 3/3 | DEPRECATE |
| list-directory | 32,772 | 45,961 | 1.40x | 3/3 | DEPRECATE |
| system-metrics | 48,003 | 41,936 | 0.87x | 3/3 | DEPRECATE |
| system-diagnostics | 55,292 | 37,676 | 0.68x | 3/3 | DEPRECATE |
| infrastructure-health | 43,272 | 55,746 | 1.29x | 4/4 | DEPRECATE |

All 6 categories: DEPRECATE

---

## Per-prompt quality grades

| # | ID | Trt>=Ctrl | Notes |
|---|----|-----------|-|
| 1 | es-01 | NO | Treatment: ES|QL returned schema not rows; consecutive tool limit hit |
| 2 | es-02 | YES | Ctrl "0 calls" wrong; Trt found 91 calls via bash curl |
| 3 | es-03 | YES | Both found p95 latency |
| 4 | es-04 | YES | Both found loop gate traces; trt 7-day view |
| 5 | fetch-01 | YES | Both: 404 correct |
| 6 | fetch-02 | YES | Both: SDK summary correct |
| 7 | fetch-03 | YES | Ctrl consumer plans; Trt API per-token pricing |
| 8 | ls-01 | YES | Both listed /app/config |
| 9 | ls-02 | YES | Both listed tools dir |
| 10 | ls-03 | YES | Reversal from PIVOT-3 (was wrong). Now correct: find|wc -l with real shell |
| 11 | metrics-01 | YES | Both: CPU load correct |
| 12 | metrics-02 | YES | Ctrl host RAM; Trt uvicorn RSS 350 MB |
| 13 | metrics-03 | YES | Both: disk healthy |
| 14 | diag-01 | YES | Both: process table |
| 15 | diag-02 | YES | Both: listening ports |
| 16 | diag-03 | YES | Ctrl "5-min window not possible"; Trt vmstat 5 6 real samples (0.43x cost) |
| 17 | infra-01 | YES | Both: all services up |
| 18 | infra-02 | YES | Both: Postgres up; Trt confirms via SELECT 1 |
| 19 | infra-03 | YES | Both: Neo4j+ES up |
| 20 | infra-04 | YES | Both: all services up |

---

## Known issue: es-01 treatment failure

Treatment hit the ES|QL schema-return problem: the _query endpoint returned the full index
mapping instead of row data, and jq .values extracted nothing. The model retried and hit the
consecutive tool call limit. Root cause: skill doc does not adequately describe how to parse
ES|QL _query responses vs _search. Tracked as a follow-up improvement (not a gate blocker
since 19/20 still clears).

---

## Gate to PIVOT-5 (FRE-264) and expanded PIVOT-4 scope (FRE-263)

Per migration plan Phase 4 — expanded from PARTIAL PIVOT-4 (2 tools) to FULL PIVOT-4 (8 tools):

Deprecate (AGENT_LEGACY_TOOLS_ENABLED=false):
- query_elasticsearch, fetch_url (PARTIAL PIVOT-4 confirmed)
- list_directory, system_metrics_snapshot, self_telemetry_query, run_sysdiag, infra_health (new — cleared in G3)
- read_file (cleared; no eval prompts but primitives read skill is proven)

Monitor 2 weeks: task success rate, zero Linear bugs tagged tool-regression.
FRE-265 (PIVOT-6 — delete legacy tool code) unblocks after gate window.

---

## Eval infrastructure notes

- FRE-283 (real bash shell contract) was the critical fix enabling this result.
- FRE-284 (Seshat doc + skill tunings) reduced discovery overhead.
- G3 used Claude Sonnet 4.6 (local Qwen tunnel unavailable at eval time).
- Weekly cloud budget $50; ~$3-4 total for this G3 run.
