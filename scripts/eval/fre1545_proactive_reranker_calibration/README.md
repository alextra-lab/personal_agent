# FRE-1545 — proactive reranker relevance-bound calibration (ADR-0148 D4)

Measures the **serving reranker** on the **proactive path's own candidates**, commits the bound
the proactive gate compares against, and measures the latency the gate adds per proactive call.

Result of the 2026-10-04 run: bound **0.338891** on `rerank-2.5`, admitting 93.6% of 110
labelled positives. See `docs/research/2026-10-04-fre-1545-proactive-reranker-calibration.md`.

## Why this exists

FRE-1477 measured the serving embedder on this path and reported an incompatibility: no bound
satisfies D4's two constraints. D4 names the response, a reranker-side bound. FRE-1479 measured
the same reranker on broad recall, but over the multipath fused set. The proactive gate sees a
different population, so the bound is measured on it, not copied.

## Run it

```bash
make test-infra-up    # the FRE-375 test stack must be up
uv run python -m scripts.eval.fre1545_proactive_reranker_calibration.calibrate
```

The artifact lands at `config/calibration/proactive_rerank_relevance_bound.json`. If the bound
changes, set `proactive_memory_rerank_relevance_bound`'s default to the new value.
`config_guard.check_proactive_rerank_bound_calibration` fails CI until the two agree.

## What it measures

Production code end to end: `generate_embedding` → `MemoryService.suggest_proactive_raw` (a
session id no seeded turn carries, so every turn is cross-session) →
`memory.proactive.score_proactive_relevance`, the function the adapter calls on every turn.

| Pass | `top_k` | Purpose |
|---|---|---|
| Wide | 100 (covers the 49-entity corpus) | Every labelled positive enters. The bound is chosen here. |
| Production width | the deployed value (20) | A real retrieval, timed (AC-7), and the P3 reference. |

The production-width pass is a separate retrieval, not a cut of the wide one. A wide dense
retrieval suppresses lexical hits that sit in dense ranks 21–100, so a cut does not reproduce
production's candidate set.

Labels follow FRE-1479. An entity candidate is positive if its name is expected. An episode
candidate is positive if any entity its turn `DISCUSSES` is expected. The negative is each
query's strongest non-match over both kinds.

## The checks

All must pass before a number is reported.

- **P1** — the proactive document builder plus production `rerank()` over every corpus entity,
  against `separation_benchmark`'s independent Voyage arm. Entity documents only, for
  FRE-1479's reason. The shared report prints FRE-1479's caveat text with it.
- **P2R** — FRE-695's instrument-sanity gate.
- **P3** — per candidate present in both passes, the score movement between the wide set and the
  production-width set, over every query. The shared report labels it "input cap"; the harness
  prints what it compares just above it.

## Substrate and spend

Test substrate only (FRE-375), with the test stack's own Neo4j credentials pinned so an ambient
`.env` password is never sent to it. `wipe_substrate` refuses outside `Environment.TEST`. Keys
come from `pass`, never logged or persisted. About 160 rerank calls and about 250 embedding
calls per run.
