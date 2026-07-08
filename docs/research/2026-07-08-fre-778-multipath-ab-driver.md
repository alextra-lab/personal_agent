# FRE-778 — ADR-0104 Multipath Recall A/B: the FRE-724 Proof Instrument

**Date:** 2026-07-08 · **ADR:** ADR-0104 · **Ticket:** FRE-778 · **Backing:** FRE-724 (seam owner, Awaiting Deploy), FRE-706 (floor sign-off), FRE-489/670 (gate sets), FRE-658 (window semantics)
**Scope of this doc:** a **test-substrate proof instrument** for FRE-724 to consume — not itself FRE-724's master-owned, deploy-gated live/prod graduation proof. All numbers below are from the isolated test substrate (Neo4j :7688 / ES :9201 / Postgres :5433), local 0.6B embedder/reranker, co-resident haystack. No production read or write.

## Method

Driver: `scripts/eval/fre435_memory_recall/ab_multipath.py`. Each gate set's full case list is co-seeded into the test graph once, with **no wipe between cases** (adapted from `ab_relevance_bounded.py`'s `calibrate()` mode) — every query ranks against a real haystack of the other cases, not an empty graph. For each case, entity-path and broad-path recall are driven twice: multipath OFF, then ON (`multipath_recall_enabled` + `lexical_arm_enabled` + `multiquery_arm_enabled` toggled together; `recall_similarity_floor` pinned at the FRE-706 owner-confirmed `0.60` for both states; `relevance_bounded_recall_enabled` pinned `False` throughout so the ADR-0100 flag can't confound this ADR-0104 measurement).

**Environment note:** the multi-query arm's paraphrase-generation call needs the `sub_agent` SLM role, which runs on a separate Mac host (reached in production over the `slm.frenchforet.com` Cloudflare Access tunnel). That tunnel wasn't wired into this test-substrate run, so the multi-query arm's call fails, is caught, and contributes nothing (a graceful degrade, confirmed in the logs — `multiquery_paraphrase_generation_failed` / `paraphrase_count=0`, never a crash). The results below are the dense + lexical arms' combined lift; multi-query's marginal contribution is not measured in this run.

## Results

| metric | lexical (FRE-489, n=21) | semantic (FRE-670, n=54) |
|---|---:|---:|
| recall@5 OFF | 0.1111 | 0.0455 |
| recall@5 ON | 1.0 | 0.8523 |
| **lift** | **+0.8889** | **+0.8068** |
| recovered (AC-3 tail-win: denied OFF, surfaced ON) | 16/21 | 37/54 |
| broad-path hit OFF / ON | 14/21 / 18/21 | 25/54 / 44/54 |
| p50 latency, ON state | 24.3s | 30.2s |
| latency ceiling (FRE-724 AC-6b) | 17.0s | 17.0s |
| **within ceiling?** | **No** | **No** |
| dense-arm floor invariant: min positive cosine | 0.658 | 0.656 |
| floor (FRE-706) | 0.60 | 0.60 |
| **invariant holds?** | **Yes** | **Yes** |

**FRE-658 window check** (multipath ON, direct `MemoryService.query_memory()` call): a marker turn seeded 40 days in the past, queried with `hard_recency_days=7` (expect empty) and with the window omitted (expect the turn). `in_window_hit=False`, `omitted_window_hit=True` — **passed**.

## Findings for FRE-724

1. **The instrument discriminates.** Both gate sets have a genuinely non-trivial OFF baseline (0.11 and 0.045, both well below 1.00) — the co-resident haystack works as intended, unlike the FRE-435 per-case-isolation harness (which scored every case 1.00 off and on, byte-identical).
2. **Multipath (dense + lexical) delivers a large, real recall lift** on both gate sets — 0.89 and 0.81 — with a substantial share of cases (16/21, 37/54) showing the AC-3 tail-win pattern: denied under dense-only, recovered once the lexical arm is added.
3. **The dense-arm floor invariant holds** on both gate sets: the lowest true-positive cosine (0.656–0.658) clears the FRE-706-confirmed 0.60 floor with room to spare.
4. **The FRE-658 explicit-window contract holds under multipath ON**, live: an explicit hard window correctly excludes an older-than-window turn; an omitted window still surfaces it.
5. **p50 latency exceeds the 17s ceiling on both gate sets (24.3s, 30.2s) in this test-substrate environment.** This is a measured result, not a pass/fail judgment call — it is reported honestly rather than tuned to clear the bar. Plausible contributors: the local CPU reranker (already the dominant cost in the FRE-679 ~17s prod baseline) scoring a larger fused candidate set from the co-resident haystack (up to 21/54 candidate entities vs. a single-entity graph), and this run measuring wall-clock end-to-end rather than the isolated reranker-only pass FRE-679 measured. This number should not be read as a verdict on the *deployed* flag's latency (which is separately, and already, live and functioning per the 2026-07-07 flag-flip) — it is this specific test-substrate configuration's measurement, offered to FRE-724 as a data point, not a gate.

## Reproduce

```bash
docker start cloud-sim-embeddings   # local embedder; stopped by default (managed-embedder profile is live)
PYTHONPATH=. uv run python scripts/eval/fre435_memory_recall/ab_multipath.py \
    --run-id fre778-$(date +%Y%m%d) --gate-set both
docker stop cloud-sim-embeddings    # restore managed-only posture
```

Raw JSON report: `telemetry/evaluation/fre778-multipath-ab/ab-{run_id}.json` (gitignored).
