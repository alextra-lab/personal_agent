# FRE-672 — Rerank top-N candidates by vector score (recall-rollout latency unblocker)

**Backing:** ADR-0100; FRE-653 (de-gate), FRE-655 (A/B), FRE-656 (4.4 s measurement). Pairs with FRE-671 (reranker hosting — separate, evidence-gated). Tier-2:Sonnet.

## Problem (confirmed in code)

`rerank()` (`memory/reranker.py:112`) sends the **entire** `documents` list to the reranker
endpoint; `top_k`→`top_n` only caps the *returned* result count, not the cross-attention compute.
With the ADR-0100 de-gate on, `query_memory` builds up to `recall_candidate_cap` (500) candidates
and reranks all of them → FRE-656's ~4.4 s/recall. Recall runs most turns, so flipping the de-gate
on naively regresses latency. Fix: bound the reranker *input* to the top-N by vector score.

## Design

A conversation's vector score = `max(vector_scores[e] for e in conv.key_entities)` (0.0 if no hit),
mirroring `_calculate_relevance_scores` (service.py:2433-2441). Select the top-N such conversations,
rerank only those, map scores back by original index. Non-selected conversations get no
`reranker_scores` entry, so they fall through to the existing vector+recency scoring path unchanged.

## Atomic steps

### 1. Config — `reranker_input_cap` (`src/personal_agent/config/settings.py`, after `reranker_top_k` ~L534)
```python
reranker_input_cap: int = Field(
    default=50,
    ge=1,
    description=(
        "FRE-672: max candidates passed *into* the cross-attention reranker per "
        "recall. The reranker cross-attends over every document it receives, so its "
        "latency scales with this cap, not with recall_candidate_cap. Only the top-N "
        "candidates by vector score are reranked; the rest pass through on their "
        "vector+recency score. Small by design (positives sit in the high-vector-score "
        "head); calibrated against recall@5 in FRE-655's A/B."
    ),
)
```

### 2. Selector helper (`src/personal_agent/memory/service.py`, after `_rank_conversations_by_relevance` ~L108)
```python
def _select_rerank_candidates(
    conversations: Sequence[TurnNode],
    vector_scores: dict[str, float],
    input_cap: int,
) -> list[int]:
    """Select indices of the top-N candidates by vector score for reranking (FRE-672).

    The cross-attention reranker cross-attends over every document it is sent, so its
    latency scales with the candidate count (FRE-656: ~4.4 s over the 500-turn set).
    Most candidates are low-vector-score distractors the reranker will not promote.
    This bounds the reranker input to the ``input_cap`` candidates with the highest
    vector score — where cross-attention adjudication adds value — and lets the rest
    pass through on their existing vector+recency score.

    A conversation's vector score is the max cosine similarity across its
    ``key_entities`` (0.0 if none matched), mirroring ``_calculate_relevance_scores``.
    The sort is stable, so equal-score turns keep their input (candidate-query) order.

    Args:
        conversations: Candidate turns, in candidate-query order.
        vector_scores: Map of entity name to cosine similarity from the vector query.
        input_cap: Max number of candidate indices to return.

    Returns:
        Indices into ``conversations`` of the top-``input_cap`` candidates by vector
        score, in descending score order.
    """
    def _conv_vector_score(conv: TurnNode) -> float:
        hits = [vector_scores[e] for e in conv.key_entities if e in vector_scores]
        return max(hits) if hits else 0.0

    ranked = sorted(
        range(len(conversations)),
        key=lambda i: _conv_vector_score(conversations[i]),
        reverse=True,
    )
    return ranked[:input_cap]
```

### 3. Integrate into the reranker block (`service.py:1757-1781`)
Replace the `docs = [...]` build + `rerank(...)` call + back-map so the input is bounded:
```python
                # --- Reranker: re-score top-N candidates via cross-attention (FRE-672) ---
                # The reranker cross-attends over every document it receives, so its
                # cost scales with candidate count. Bound the input to the top-N by
                # vector score; the rest pass through on their vector+recency score.
                reranker_scores: dict[str, float] = {}
                if current_settings.reranker_enabled and query_text and len(conversations) > 1:
                    try:
                        from personal_agent.memory.reranker import rerank  # noqa: PLC0415

                        rerank_indices = _select_rerank_candidates(
                            conversations,
                            vector_scores_for_ranking,
                            current_settings.reranker_input_cap,
                        )
                        docs = [
                            conversations[i].summary or conversations[i].user_message or ""
                            for i in rerank_indices
                        ]
                        rerank_results = await rerank(
                            query=query_text,
                            documents=docs,
                            top_k=current_settings.reranker_top_k,
                        )
                        # rr.index is into the bounded docs list; map back to conversations.
                        if rerank_results:
                            max_score = max(r.score for r in rerank_results)
                            for rr in rerank_results:
                                if rr.index < len(rerank_indices):
                                    conv_idx = rerank_indices[rr.index]
                                    norm = rr.score / max_score if max_score > 0 else 0.0
                                    reranker_scores[conversations[conv_idx].turn_id] = norm
                    except Exception as rerank_exc:
                        log.warning(
                            "reranker_integration_failed",
                            error=str(rerank_exc),
                            trace_id=trace_id,
                        )
```

### 4. Tests (`tests/personal_agent/memory/test_hybrid_search.py`)
- **AC-2 selector**: `_select_rerank_candidates` returns highest-vector-score indices, respects cap,
  stable tie order, 0.0 for no-hit convs.
- **AC-1 bounded input**: patch `memory.reranker.rerank`/`service.rerank` to capture `documents`;
  drive `query_memory` (or the block) with M≫N candidates → asserts `len(documents) <= input_cap`.
- **AC-3 pass-through**: a candidate outside top-N has no `reranker_scores` entry → existing
  vector+recency path (covered by existing `_calculate_relevance_scores` tests staying green).

### 5. Quality gates
`make test-file FILE=tests/personal_agent/memory/test_hybrid_search.py` → green;
then `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

## Acceptance criteria → proof
| AC | Invariant | Proof |
|----|-----------|-------|
| AC-1 | M≫N → rerank() gets ≤ N docs | unit test captures `documents` arg |
| AC-2 | top-N are highest vector score, back-map correct | unit test on `_select_rerank_candidates` |
| AC-3 | non-reranked keep vector+recency score | existing scoring tests green |
| AC-4 | recall@5 preserved + reranker time drops | **post-deploy** FRE-655 A/B re-run (master runbook) |

AC-4 needs the live SLM reranker + eval substrate; documented as a post-deploy runbook for master,
not run in this build session.

## Risk register (codex plan-review, 2026-06-28)
- **Index back-map: CONFIRMED correct.** `RerankResult.index` is the index into the documents list
  passed to `rerank()`; mapping `rerank_indices[rr.index]` back to `conversations` is sound.
- **Mixed-mode scoring (not a regression):** once any reranker score exists, global `use_reranker`
  is True and vector-hit turns without a reranker score score under full-pipeline weights with
  `cw_reranker=0.0`. This already happens today — the old code reranked all 500 but `rerank()`
  returns only `reranker_top_k` (10) scored results, so ~490 already had `cw_reranker=0.0`. This
  change bounds the *input compute*; it does not change the weighting structure.
- **Graceful-degradation scope shrinks (minor):** on reranker endpoint failure, only the top-N docs
  get `_passthrough` scores (`1/(i+1)` by position) instead of all 500. Passthrough is near-meaningless
  ordering, and a turn outside the top-N would not be promoted by a working reranker either, so the
  effective fallback is unchanged for the turns that matter.
- **Pure entity-name matches (edge case):** a candidate matched only via explicit `entity_names`
  (not the vector query) has vector score 0.0 in the selector and competes at the tail. Acceptable —
  the vector-expanded positives carry the signal; AC-4's A/B is the safety net. N is config-driven so
  FRE-655 can raise it if the A/B shows recall loss.
