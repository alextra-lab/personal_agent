# FRE-1477 — The serving embedder arm does not separate relevance from recency. No bound was set.

**Date:** 2026-09-10 · **Ticket:** FRE-1477 ("Memory Recall Quality") · **Backing:** ADR-0148 D4.
**Arm measured:** `Qwen3-Embedding-8B` @ 1024 dimensions (OVH managed) — the arm that serves production.
**Substrate:** the FRE-375 test stack (Neo4j :7688). No production row was read or written.
**Artifact:** `config/calibration/proactive_relevance_bound.json`.

## The question

ADR-0148 D4 requires that every item entering `memory_context` carry a relevance value, and that an
item below its path's calibrated bound is not admitted whatever its recency. For the proactive path
the bound is a number, and the ADR is explicit that the number comes from a measurement of the arm
that is actually serving, not from the document and not from FRE-694's June figures:

> **No FRE-694 number describes the deployed arm.** … The bound is set by measuring the arm that is
> actually serving.

So: **on `Qwen3-Embedding-8B` @ 1024, is there a bound that rejects the median top-ranked non-match
while still admitting at least 90% of the labelled positives?**

## Answer

**No.** The two constraints are not jointly satisfiable on this arm. The calibration reported the
incompatibility and stopped, which is what D4 requires of it, and **no bound is configured.**

| Quantity | Value |
|---|---|
| Labelled positives (per expected entity) | 57 |
| Negatives (per query, strongest non-match) | 54 |
| Positive median | 0.7547 |
| Negative median | **0.6798** |
| Negative maximum | 0.7897 |
| Lowest bound rejecting the negative median | 0.6808 |
| Share of the whole positive set it admits | **84.2%** (required: 90%) |

Admitted share falls monotonically as the bound rises, so the lowest admissible bound is also the
most permissive one. If it fails the positive constraint, every higher bound fails it too. One
candidate settles the question; there is no sweep to search.

Note the shape, not just the verdict: **the negative maximum (0.7897) exceeds the positive median
(0.7547)**. The strongest non-match out-scores a typical true match on embedding alone. That is
FRE-694's separation ceiling, reproduced on the deployed arm rather than transferred from the arms
FRE-694 tested.

## Method

The proactive path admits on `vector_score`, which is what `db.index.vector.queryNodes` returns
(`memory/service.py:1000-1016`, copied untransformed into the row at `:1049-1054`). So the
calibration measures that index, on that arm, through the production write and query path — not an
offline approximation of it.

The metric is FRE-694's: per expected entity a positive, and per query the **strongest non-match**
as the negative. That statistic is the right one because on a query whose answer is not in the
corpus, the nearest neighbour the index returns *is* the strongest non-match. It describes the item
admission is decided on. It does not describe the corpus at large, and nothing here claims it does.

Both score populations are committed whole, not as summary statistics. ADR-0148 AC-5 names the whole
labelled positive set as the denominator, and a stored share can be re-cut after the fact.

## Instrument validation — two checks, both passed

FRE-694's discipline is to validate the instrument against the production path before reporting any
number from it. Its literal reference is a 0.6B measurement and no 0.6B endpoint serves today, so
the *structure* transfers rather than the constant. Tolerance is FRE-694's own, 0.02.

**P1 — cross-implementation.** `scripts/eval/fre817_corpus_ab_embedder/corpus_ab.py` is an
independently authored offline pipeline with its own corpus build, entity text, query prefix, OVH
client and cosine. Its three aggregates against the Neo4j index's:

| Statistic | Neo4j index | Independent offline | Δ |
|---|---|---|---|
| pos_median | 0.7547 | 0.7440 | 0.0107 |
| neg_median | 0.6798 | 0.6712 | 0.0086 |
| neg_max | 0.7897 | 0.7912 | 0.0015 |

**P2 — index fidelity.** Per scored candidate, the index's score against the cosine computed
client-side over the vectors the index itself holds: 111 pairs, maximum Δ **0.0097**.

P2 closes a gap FRE-694 stated about its own check — *"an embedding-geometry test, not a
Neo4j-HNSW index-fidelity test"* — and one FRE-817 recorded for this arm specifically: *"The OVH-8B
arm has no equivalent live-Neo4j reference (there is no production index at 4096-dim today)."* The
served width is now 1024 and the production index runs at it, so the reference FRE-817 lacked
exists, and this is it.

### A first run whose parity passed while comparing the wrong thing

Worth recording, because the failure mode is the one a parity gate exists to catch and the pass/fail
bit did not reveal it.

The first run reported P1 deltas of 0.0147 / 0.0198 / 0.0157 — inside the tolerance, so it passed.
But all three leaned the same way, and a consistent offset is a bias, not noise. The cause:
`_embed_ovh` sends no `dimensions` parameter, so the offline side received the model's **native
4096** while production requests and stores **1024**. FRE-694 measured separation on this very model
as materially dimension-dependent (Youden J 0.550 at 1024 against 0.534 at native), so the two sides
were not measuring the same geometry at all.

The fix reduces the offline side client-side with the MRL truncate-and-renormalize FRE-694 validated
as equivalent to the server-side parameter (cosine ~0.999). Deltas fell to 0.0107 / 0.0086 / 0.0015.
The verdict was unchanged, because the verdict rests on the index side and the index side was the
production path throughout — but the parity gate had not yet validated anything, and a run reported
on that basis would have claimed evidence it did not have.

## What ships, and what does not

**Ships.** The gate itself (`memory/proactive.py`), ahead of `_combine_scores` so recency cannot
compensate for it, with its own `DropReason.RECALL_RELEVANCE_BOUND`; the calibration harness; the
committed artifact; the `config_guard` check binding a configured bound to it; and the repair of
FRE-1287's fixture.

**Does not ship: a number.** `proactive_memory_relevance_bound` is `None`, so the gate is inert and
the proactive path admits exactly as it did before. D4 is explicit that a missing calibration never
silently defaults to zero, and AC-5 fails a bound configured in the reserved case. Choosing one here
would be choosing, not measuring.

**So the defect FRE-1118 named is still open on this path.** At the measured median top-ranked
non-match, with zero entity overlap and zero topic hits, a candidate still clears the 0.30 bar on
recency. `TestTheMeasuredNonMatchStillClearsTheBar` asserts exactly that, in FRE-1287's own test
file, so the gap is recorded in code and not only in prose.

## The decision this hands back

D4 names the response to an incompatibility, and it is not a lower bar:

> the response is a reranker-side bound or a different arm, not a quietly chosen number.

Three courses, for the owner:

1. **A reranker-side bound.** FRE-695 measured the reranker separately, and a cross-encoder discriminates
   where a bi-encoder does not. This is the option ADR-0148 itself points at, and it is already the
   mechanism FRE-1479 must build for broad recall.
2. **A different embedder arm.** FRE-694 tested four and none opened a clean floor, with Voyage best at
   J 0.59 against the 8B arm's 0.53. Better, but the clouds still overlap; this would raise the
   admissible share without guaranteeing it clears 90%.
3. **Revisit the 90%.** It is ADR-0148's own figure. Lowering it to 84% would admit the measured bound
   — and would accept suppressing roughly one labelled positive in six. That is a recall cost the
   ADR deliberately refused to let an implementer accept silently, which is why this document
   reports the number instead of taking the decision.

The harness re-runs against whatever arm is chosen: it reads the serving configuration rather than a
hardcoded arm, and it commits its own answer.

## Fold-in found while building this — a constant standing in for a score

`_augment_proactive_with_lexical` (`memory/service.py:1080-1178`, FRE-724) appends lexical-arm entity
hits to the proactive rows with `vector_score = recall_similarity_floor` — a configuration constant,
not a measurement. Production runs this path (`MULTIPATH_RECALL_ENABLED=true`,
`RECALL_SIMILARITY_FLOOR=0.60`).

Two consequences. Today, such a candidate already carries an embedding term of
`max(0, 2×0.60 − 1) = 0.20` on no embedding evidence — a surviving floor of the kind FRE-1287
removed, which FRE-1287 did not reach because it enters upstream, in the row. And for this gate,
whether such a candidate passed would have depended on where a calibrated bound fell relative to an
unrelated constant.

The row now carries `vector_score_measured`, and the gate treats an unmeasured score as no relevance
evidence, exactly as it treats a zero entity overlap. This changes admission only for a lexical-only
candidate that *also* has zero overlap and zero topic hits — the population D4 forbids admitting.

## References

- ADR-0148 D4 — `docs/architecture_decisions/ADR-0148-absence-must-be-reachable-and-sayable.md`
- FRE-694 — `docs/research/2026-06-29-fre-694-embedder-separation.md`
- FRE-695 (reranker separation) — `docs/research/2026-06-30-fre-695-reranker-separation.md`
- FRE-1287 — the shipped half and its fixture defect
- Harness — `scripts/eval/fre1477_relevance_calibration/`
