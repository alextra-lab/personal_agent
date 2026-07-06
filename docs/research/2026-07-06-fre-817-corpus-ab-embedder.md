# FRE-817 — ADR-0112 AC-4 Corpus A/B: 0.6B (local) vs OVH-managed Qwen3-Embedding-8B

**Date:** 2026-07-06
**Backing:** ADR-0112 §D4 + AC-4. Decides the embedder for FRE-821 (the AC-5/AC-6 adoption ticket).

## What was measured

A fixed real-query corpus A/B, scored by nDCG@k, between the currently-deployed
local Qwen3-Embedding-0.6B and the OVH AI Endpoints-managed Qwen3-Embedding-8B
(the D4 candidate spine). Harness: `scripts/eval/fre817_corpus_ab_embedder/`.

- **Corpus:** `scripts/eval/fre435_memory_recall/semantic_probe.yaml` (FRE-670)
  — 54 vocabulary-divergent real-query cases over 49 notes, purpose-built so
  lexical/keyword shortcuts fail and the vector path must do real semantic
  matching (`bespoke_probe.yaml` was rejected as the corpus: 0.6B/4B already
  score identically on it per FRE-656, so it can't discriminate embedder
  quality at all).
- **Method:** offline embed → cosine-rank → nDCG@k (no substrate write), the
  same pattern `separation_benchmark.py` (FRE-694) established. Both arms
  embed notes as `"{name}: {description}"` (production text) and queries with
  the Qwen instruction-prefix (asymmetric query mode).
- **Pre-registered margin:** 0.05 nDCG, declared in `decision.py` before the
  run — grounded in the 54-case corpus's own granularity (one case flip ≈
  0.019; 0.05 is ≈2.6× that, so a margin "clear" can't be a single-case
  fluke). Only relevant if a *closed* candidate competes (neither arm here is
  closed — OVH-hosted Qwen3-Embedding-8B is open-weight per D4, just
  managed-hosted).

## Result

| Arm | nDCG@1 | nDCG@5 (decision metric) |
|---|---|---|
| 0.6b (local, currently deployed) | 0.8864 | 0.9303 |
| 8b-ovh (OVH-managed) | 0.9091 | 0.9566 |

**Decision: `8b-ovh` wins by measurement.** Both arms are open-weight, so the
margin gate never applied (`margin_cleared: null`) — the winner is simply the
higher-nDCG@5 arm. Full decision record (`decide_embedder`):

> no closed candidate competed; best open-weight arm '8b-ovh' (nDCG=0.9566)
> wins by measurement

Raw run record: `telemetry/evaluation/fre817-corpus-ab/corpus-ab-fre817-20260706.json`
(gitignored — this document is the committed, durable artifact).

## Instrument sanity (before the full-corpus spend)

A fixed known-relevant/known-irrelevant pair (cosmic microwave background vs.
ratatouille) was embedded through the OVH arm first: relevant cosine 0.5382 >
irrelevant cosine 0.1721 — confirms the model id and query-prefix format are
being interpreted correctly by the endpoint, not silently producing a
wrong-but-valid vector.

## Operational note: OVH batch limit discovered during this run

The OVH Qwen3-Embedding-8B endpoint rejects batches larger than 25 inputs
(`HTTP 400: "given batch size overflow maximal one", max=25`) — not
documented up front, found by running the real 49-note corpus through it.
`_embed_ovh` now chunks to 25 per request. Anyone else calling this endpoint
(including the eventual FRE-821 adoption work) should carry this limit
forward.

## Known limitation: offline geometry, not the live Neo4j retrieval path

This measures embedding-geometry ranking quality, not "does the production
Neo4j HNSW path retrieve this." The 0.6B arm's construction is the exact one
already validated against the FRE-670 Neo4j calibrate medians by
`separation_benchmark.py`'s `_parity_check` (Δ ≤ 0.02) — so its offline nDCG
numbers inherit that parity. The OVH-8B arm has no equivalent live-Neo4j
reference (no production index at 4096-dim today); standing one up would be
re-embed-adjacent work, explicitly deferred to FRE-821.

## What this does NOT decide

- Whether to adopt 8b-ovh into production (FRE-821 — AC-5/AC-6, including the
  same-model local-fallback vector-space proof).
- Any change to `config/substrate.yaml` / `settings.py` / the Neo4j vector
  index — none touched by this ticket.
- The closed/API-only-model margin gate is implemented and unit-tested
  (`decide_embedder`) but not exercised live here — no closed candidate
  (e.g. Voyage) was in this run's scope.

## Follow-up filed

A separate infrastructure question the owner raised during this ticket —
whether the OVH AI Endpoints call can/should route over the VPS's private
vRack network instead of the public internet — is tracked separately (see the
Linear follow-up ticket, filed Needs Approval) and did not block this
measurement (the live run used the existing public HTTPS endpoint,
owner-authorized).
