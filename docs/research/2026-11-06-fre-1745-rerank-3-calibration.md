# FRE-1745 — Voyage rerank-3 calibration of the three reranker relevance bounds

Date: 2026-11-06. Ticket: FRE-1745. Design basis: ADR-0148 D4.

## Decision and scope

The owner decided on 2026-11-06 to move the `reranker` role from Voyage `rerank-2.5` to
`rerank-3`. Voyage's docs list `rerank-3` as "Highest accuracy" with the same endpoint, the
same 32,000 context and the same price per token. This ticket adds no quality study. It
re-measures the three cut-offs that live in the reranker's own score scale, because a bound
belongs to the model that produced the scores.

## Method

* The existing harnesses ran once each. No new harness was built.
  * `scripts/eval/fre1479_reranker_calibration` wrote `broad_recall_relevance_bound.json`.
  * `scripts/eval/fre1545_proactive_reranker_calibration` wrote
    `proactive_rerank_relevance_bound.json`.
* `entity_match_relevance_bound.json` is a copy of the FRE-1479 run with a `reuse_note`. FRE-1480
  showed that the entity match gate and the broad recall gate use the same core and returned
  identical scores on `rerank-2.5`. Master accepted the reuse on 2026-11-06. The `reuse_note`
  field is outside the artifact schema, and the loader ignores it.
* The only harness change is a `voyage-rerank-3` arm in `RERANKER_ARMS`, and
  `INDEPENDENT_ARM` now names it. The FRE-1479 harness takes the model name in the artifact
  from that arm, so the arm and the serving binding must name the same model.
* The probe set is `scripts/eval/fre435_memory_recall/semantic_probe.yaml` (54 cases).

## Result

| Artifact | Model | Bound | Positives admitted | Negative median |
|---|---|---|---|---|
| broad recall | rerank-3 | 0.344176 | 93.3% of 119 | 0.343176 |
| broad recall | rerank-2.5 | 0.338891 | 93.3% of 119 | 0.337891 |
| entity match | rerank-3 | 0.344176 | 93.3% of 119 | 0.343176 |
| entity match | rerank-2.5 | 0.338891 | 93.3% of 119 | 0.337891 |
| proactive rerank | rerank-3 | 0.344176 | 93.6% of 110 | 0.343176 |
| proactive rerank | rerank-2.5 | 0.338891 | 93.6% of 110 | 0.337891 |

ADR-0148 D4 needs 90% or more. All three pass. On `rerank-3` the broad recall bound admits
94.7% of the entity documents and 91.9% of the turn documents. The proactive bound admits 94.7%
of the entity candidates and 92.5% of the episode candidates.

The admitted share is the same as on `rerank-2.5` and the bound moves up by 0.005. The count of
admitted positives is equal (111 and 103), but the positive and negative score lists differ in
all three artifacts. This note does not explain why the count is equal.

Parity checks passed on both runs: P1 deltas of at most 0.012 against the limit of 0.02, P2R
sanity 0.6945 (relevant) against 0.2482 (irrelevant), and P3 listwise movement of 0.000000
(broad recall) and 0.000057 (proactive). Added time per proactive call at `top_k` 20: p50 229 ms,
p95 395 ms. No score came from the fallback model or from passthrough.

## Spend

About 328 rerank calls (220 on the production path, 108 on the independent arm) against 271
projected, which is 1.21 times. The logged cost of the production path was 0.038 dollars. The
independent arm adds about 0.01 dollars. The preflight undercounted because the FRE-1545
independent check ran twice per case.

## Rollback

Revert the `reranker` binding in `config/models.yaml` and the three artifacts and the three
setting defaults together. `config_guard` fails if only some of them are reverted. The old
artifacts are in git history.

## Limits

* The bound moves by 0.005. This note does not claim that `rerank-3` ranks better on this
  probe set. The ticket did not ask for that study. AC-5 reads live recall for at least 3 days
  after the deploy.
* `calibrated_reranker_model` returns `None` when an artifact is missing or malformed, and the
  gate then skips the producer check. `config_guard` fails in that state when a bound is
  configured, so CI protects it. This ticket does not change that behaviour.
