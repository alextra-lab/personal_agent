# FRE-1479 — broad-recall relevance-bound calibration (ADR-0148 D4)

Measures the **serving reranker** and commits the bound the broad-recall admission gate
compares against. The reranker sibling of `scripts/eval/fre1477_relevance_calibration/`,
which did the same for the proactive path's embedder arm.

## Why this exists

FRE-1477 measured the serving embedder and reported an **incompatibility**: no bound rejects
the median top-ranked non-match while still admitting 90% of the labelled positives. ADR-0148
D4 names the response — *"a reranker-side bound or a different arm, not a quietly chosen
number"* — and this harness produces the first of those.

## Run it

```bash
make test-infra-up          # the FRE-375 test stack must be up
uv run python -m scripts.eval.fre1479_reranker_calibration.calibrate --run-id cal-$(date +%Y%m%d)
```

`--dry-run` reports the corpus shape without sending anything. The artifact lands at
`config/calibration/broad_recall_relevance_bound.json`.

## What it measures

The **production pair**, not a re-implementation of it: `MemoryService._resolve_item_texts`
builds the documents and `memory.reranker.rerank` scores them. Two document shapes reach the
admission gate and both are measured:

| Shape | Built as | Reaches the boundary as |
|---|---|---|
| entity | `name + ' ' + description` (`service.py:5255`) | itself |
| turn | `coalesce(summary, user_message, '')` (`service.py:5266`) | the entities it discusses (`service.py:5417`) |

One bound governs their union, because one bound governs the path. The artifact records the
two halves separately so a reader can see whether one shape drags the other.

Metric: FRE-694's and FRE-695's — per expected entity a positive, and per query the strongest
non-match as the negative.

## The three checks, and why they are these three

Every one must pass before a number is reported.

**P1 — cross-implementation, entity documents only.** `separation_benchmark.py`'s
`voyage-rerank-2.5` arm is an independently authored pipeline: its own corpus build
(`"{name}: {description}"`), its own HTTP client, its own response parser. Compared on
FRE-694's three aggregates at FRE-694's own 0.02 tolerance.

Restricted to entity documents **deliberately**. The independent harness builds no turn
documents at all, so comparing the full production population would put 93 candidates against
49 and call the difference a parity delta. The first run of this harness did exactly that and
failed — `neg_max` 0.0664, `pos_median` 0.0469. That is the gate working, not the number
being wrong, and the fix is to compare the population both sides actually build.

**P2R — instrument sanity.** FRE-695's own gate: a trivially relevant document must outrank a
trivially irrelevant one. It catches what P1 cannot — a failure both implementations share,
such as a wrong model id or an endpoint answering with a constant.

FRE-1477's P2 was a Neo4j index-fidelity check and **has no reranker analogue**: no index sits
between the model and the score. It is recorded as absent rather than replaced by a vacuous
substitute, which would report a validated instrument on no evidence.

**P3 — listwise sensitivity.** This harness scores the whole corpus per query; production
reranks a fused set capped at `reranker_input_cap`. A rerank request is listwise, so the wider
candidate set could move a pair's score. P3 measures how far it actually does, rather than
asserting that it does not.

## Substrate and spend

Test substrate only (FRE-375): `:7688` / `:9201` / `:5433`, pinned at module top before any
`personal_agent` import. `wipe_substrate` refuses to run outside `Environment.TEST`. The graph
is needed because turn documents and their `Turn-[:DISCUSSES]->Entity` edges must exist to be
built and labelled the way production builds them.

Two endpoints are pinned, for different reasons:

* the **reranker**, because it is the component under measurement and D4 requires the number
  to describe what actually serves;
* the **embedder**, because `seed_replay` writes entities through the production
  `create_entity`, which embeds each one. Nothing downstream reads those vectors — a reranker
  scores text — so the embedder plays no part in the result.

Roughly 54 rerank requests per implementation against a paid endpoint, plus the seed's
embedding calls. Keys are read from `pass` at run time, never logged, never persisted.

## Reading the outcome

The harness reports one of two things, and both are results:

* **A bound.** It is committed to the artifact and configured as
  `broad_recall_relevance_bound`'s default. `config_guard.check_broad_recall_bound_calibration`
  binds the two together from then on.
* **An incompatibility.** No bound satisfies D4's two constraints on this component. The
  artifact records that, no bound is configured, the gate ships inert, and the decision is the
  owner's. The harness never picks a number in that case.
