# FRE-817 — ADR-0112 AC-4 corpus A/B embedder harness (nDCG, pre-registered margin)

Decides the embedder for FRE-821 (the adoption ticket): a fixed real-query
corpus A/B, scored by nDCG@k, between the currently-deployed local 0.6B
embedder and the OVH-managed Qwen3-Embedding-8B endpoint. Independent
measurement — no seam dependency on FRE-816's config work.

## Layout

| File | Role |
|------|------|
| `decision.py` | Pure ADR-0112 AC-4 margin decision — `EmbedderCandidate` / `EmbedderDecision` / `decide_embedder` / `PRE_REGISTERED_MARGIN_NDCG` |
| `corpus_ab.py` | The driver — pure `score_arm` (nDCG@k aggregation) + embed I/O (`_embed_local`, `_embed_ovh`) + CLI |
| `run_corpus_ab.sh` | Preflight-gated shell wrapper; pins the 0.6b arm's config |

## Why nDCG, not recall@k

ADR-0112 D4/AC-4 both specify nDCG@k as the deciding metric — it rewards
*ranking* quality (a relevant note found by cosine at rank 1 beats one found
at rank 5), where recall@k only asks "is it in the top-k at all." Reuses
`scripts/eval/fre435_memory_recall/metrics.py::ndcg_at_k` directly (binary
gains, `None` when nothing relevant — excluded from aggregates).

**The decision metric is nDCG@5, not @1** — both are computed and reported,
but only @5 feeds `decide_embedder`, so there is one designated "measured
winner" number even if @1 and @5 happen to disagree.

## The fixed real-query corpus: `semantic_probe.yaml`, not `bespoke_probe.yaml`

`bespoke_probe.yaml` (the FRE-489 gate set) is documented as "lexical masked
as semantic" — its own README records that 0.6B and 4B embedders **already
score identically on it** (FRE-656), so it cannot discriminate embedder
quality at all. `semantic_probe.yaml` (FRE-670, 54 vocabulary-divergent
cases) is purpose-built so keyword/lexical shortcuts fail and the vector path
must do real semantic matching — the correct fixed real-query set for an
embedder-quality A/B.

## The pre-registered margin

`PRE_REGISTERED_MARGIN_NDCG = 0.05`, declared in `decision.py` before any run.
Grounded in the probe's own granularity: 54 cases means one case flipping
moves the aggregate mean nDCG by ≈ 1/54 ≈ 0.019; 0.05 is ≈2.6× that, so a
margin "clear" cannot be a single-case fluke — the "not a noise-level win"
bar AC-4 requires. The driver never exposes a `--margin` CLI override — the
constant is the only margin ever used end-to-end.

For *this* run neither arm is closed/API-only (OVH-hosted
Qwen3-Embedding-8B is open-weight per D4, just managed-hosted), so
`margin_cleared` records `None` and the winner is simply the higher-nDCG@5
arm. The closed-candidate branch is fully implemented and unit-tested
(`decide_embedder` supports it) so it's a real, extensible code path for a
future closed contender — not exercised live by this ticket.

## Known limitation: offline geometry, not the live Neo4j retrieval path

This harness embeds notes+queries and ranks by cosine directly — an offline,
no-substrate pattern (mirrors
`scripts/eval/fre435_memory_recall/separation_benchmark.py`). It answers
"which embedder ranks better," not "does the production Neo4j HNSW path
retrieve this." The 0.6B arm's construction (`_entity_text`, same
config/dimension) is the exact one already validated against the FRE-670
Neo4j calibrate medians by `separation_benchmark.py`'s `_parity_check`
(Δ ≤ 0.02) — so its offline nDCG numbers inherit that parity. The OVH-8B arm
has no equivalent live-Neo4j reference (there is no production index at
4096-dim today); standing one up to check would be re-embed-adjacent work,
out of scope here and explicitly deferred to FRE-821.

## OVH embeddings API

`_embed_ovh` posts to `{base_url}/embeddings` (OpenAI-compatible), reading
`AGENT_OVH_AI_BASE_URL` / `AGENT_OVH_EMBEDDING_TOKEN` from `pass` at run time
(never logged/persisted, mirrors `separation_benchmark.py::_voyage_key`).
Response rows are re-sorted by their `index` field before extraction (never
trust response order) and a cardinality check refuses a truncated/expanded
response. A rank-order sanity probe (a fixed known-relevant/known-irrelevant
pair) runs before any full-corpus spend — catches a wrong model id or a
misinterpreted query prefix that fail-loud length/non-zero checks alone
can't see.

## Run

```bash
scripts/eval/fre817_corpus_ab_embedder/run_corpus_ab.sh
```

Output: `telemetry/evaluation/fre817-corpus-ab/corpus-ab-<run-id>.json`
(gitignored raw run — the committed AC-4 artifact is the curated writeup at
`docs/research/2026-07-06-fre-817-corpus-ab-embedder.md`).

## Tests

```bash
make test-k K=fre817   # pure decision + scoring + OVH-transport tests (no live network)
```
