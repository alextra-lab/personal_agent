# FRE-1480 — Giving the entity-match path a relevance value

**Date:** 2026-09-11 · **Ticket:** FRE-1480 ("Memory Recall Quality") · **Backing:** ADR-0148 D4.
**Component measured:** Voyage `rerank-2.5` — the arm the `reranker` role resolves to in production.
**Substrate:** the FRE-375 test stack (Neo4j :7688) for the calibration; the live capture corpus,
read-only, for the population measurement.
**Artifacts:** `config/calibration/entity_match_relevance_bound.json`,
`scripts/eval/fre1480_path_population/measure.py`.

## The question

ADR-0148 D4 left this path one open decision and was explicit that it was not the obvious one:

> The implementation ticket decides **which** value — routing the path through the multipath core
> for a reranker score, or computing a similarity over the `query_text` it already carries —
> against that path's measured population. It does not decide **whether**.

So: **which relevance value does the entity-match path acquire, and what does its live population
say about the choice?**

## The population, measured first

The instrument is `agent-captains-captures-*`, field `recall_admission` — the ADR-0125 evidence
record, which lists every candidate the recall layer produced, its kind, its score, and whether it
was admitted. The producing path is not recorded, so it is read off the item shape, with every
discriminator derived from the producing code rather than guessed.

**Window 2026-08-13 to 2026-09-10. 258 captured turns carrying a `recall_admission` record.**

| Quantity | Value |
|---|---|
| Entity-match path **reached** (proactive ran, admitted nothing) | **24 turns (9.3%)** |
| ...of which **admitted** at least one item | **8 turns (3.1%)** — 33% of reached |
| Items admitted across the window | **38** |
| ...entities | **28** |
| ...episodes | **10** |
| Entities carrying a relevance value | **0** |
| Items per admitting turn | mean 4.75, min 3, max 5 (the `limit=5` cap) |

Three readings, and two limits.

**The path is small but not rare.** It is reached on nearly one turn in ten, and it is reached in
exactly the condition the rest of this chain exists to make honest — proactive ran and offered
nothing. Every admitting turn sat at or near its own cap.

**The defect is wider than the ticket states.** The ticket names the 28 entities, which carry no
score at all. The 10 episodes are not better off: they carry `(total - position) / total`, the fused
rank normalised — rank order wearing the score field. That is the same defect FRE-1479 removed one
path over, and it was live here.

**One classifier correction, recorded because it changed a number.** The first attribution counted a
15-item `MEMORY_RECALL` turn as an entity match. This path caps `conversations + entities` at
`query.limit` (5) and cannot admit more, so the item budget disproves it. The committed script tests
the budget, and the figures above are post-correction.

**Limit 1.** Captures exist only for completed turns, and Elasticsearch loses events episodically
(FRE-1051). The absolute turn counts are a lower bound. The ratios and the per-item findings are read
off the item records themselves and do not depend on the corpus being complete.

**Limit 2, measured rather than argued.** A turn is attributed to this path only when proactive left
drops behind. Proactive can also fall through having produced no candidates *and* no discards, and
such a turn is indistinguishable from one where the recall layer never ran. That bucket is the
ceiling on what `reached_turns` may undercount; the script publishes it beside the figure it
qualifies, and over this window it is **0 turns**.

## The mechanism, and why it is not a close call

**Candidate 1 — route through the multipath core and use the reranker score — is already the
deployed route.** `query_memory` dispatches to `_multipath_query_memory` whenever `query_text` is
present and `multipath_recall_enabled` is on (`service.py:4224`). This path always passes
`query_text=user_message`, and production sets the flag. The path has been running the fused +
reranked core all along.

**The core does not distinguish the two paths.** `_multipath_fused_recall`'s `path` argument is
documented as *"telemetry only"*, and the source bears that out: arm selection, fusion, capping and
reranking never inspect it. `path="broad"` and `path="entity"` run the same arms, the same RRF, the
same `reranker_input_cap` and the same `rerank()` call. The score space is not merely comparable to
FRE-1479's — it is the same function of the same `query_text`.

**The score already existed, already survived one hop, and was thrown away at the next.** FRE-1479
added `rerank_score` / `rerank_model` to `FusedResult`. `_multipath_query_memory` walked those very
items and replaced the value with the rank expression above.

**Candidate 2 is not available.** A fresh similarity over `query_text` needs its own bound in
embedding space, and FRE-1477 measured the serving embedder and reported an **incompatibility**: no
bound rejects the median top-ranked non-match while admitting 90% of the labelled positives. D4's
named response to that is *"a reranker-side bound or a different arm"*. Candidate 2 would add a
second scorer and a second calibration to reach a score space already measured as having no
admissible bound, while a calibrated reranker score for the same items sat two lines away.

**Chosen: candidate 1.**

## The bound, and an honest account of what its artifact is worth

`entity_match_relevance_bound = 0.338891`, committed to
`config/calibration/entity_match_relevance_bound.json` and bound to it by
`config_guard.check_entity_match_bound_calibration`.

ADR-0148 AC-10 requires one artifact per configured bound and names this one outright — *"the
entity-match bound against whichever scorer that path acquires"* — so the artifact is produced by
**running** the harness, not by copying FRE-1479's number.

| Quantity | This run (2026-09-11) | FRE-1479 (2026-09-10) |
|---|---|---|
| Labelled positives | 119 (entity 57, turn 62) | 119 (entity 57, turn 62) |
| Negative median — the value the bound must reject | 0.3379 | 0.3379 |
| **Configured bound** | **0.338891** | 0.338891 |
| Share of the whole positive set admitted | **93.3%** | 93.3% |
| Entity-document positives admitted | 93.0% | 93.0% |
| Turn-document positives admitted | 93.5% | 93.5% |
| Negatives rejected | 50.0% | 50.0% |

The two artifacts are byte-identical apart from `measured_on`. **That is worth stating plainly in
both directions.**

**What it is.** A genuine independent re-run, a day apart, against the live paid endpoint,
reproduced all 119 positive scores, all 54 negative scores and all four parity aggregates exactly.
Voyage returns dyadic-fraction scores and is deterministic for a fixed (query, document) pair;
nobody had established that, and it means FRE-1479's bound is reproducible rather than a single
draw.

**What it is not.** It is **not** a statistically distinct measurement of this path's population.
The harness has no path-specific population selection, so it measured the same corpus with the same
component and the same document construction. The artifact's value here is provenance — AC-10's
requirement — not new evidence about the entity-match path. Claiming otherwise would be the kind of
ceremony-dressed-as-measurement this chain exists to remove.

**Why two bounds rather than one, given that.** The cores are free to diverge, D4 carries one bound
per path, and a recalibration of one path must never silently move the other's gate. The filename is
what distinguishes them, and `config_guard` binds each file to its own setting.

**Read the second number too.** The bound rejects **50.0%** of the top-ranked non-matches, which is
by construction — the lowest bound strictly above the negative median rejects exactly half. So the
honest statement is: *the gate removes half of the top-ranked non-matches while keeping 93.3% of
genuine matches.* That is a material improvement over admitting every one of them, and it is nothing
like clean separation. FRE-695 reached the same conclusion about every reranker it tested.

## What ships

**The plumbing.** The reranker's score now survives `_multipath_query_memory` to the admission
boundary, on `MemoryQueryResult.relevance_values` and then on the adapter's item dicts, in the same
two field names the broad-recall path already uses.

The map is **namespaced by kind** (`entity:<name>` / `turn:<turn_id>`). `EntityNode.entity_id` is
populated from the node's name while a turn's identity is its id, so the two spaces are not
disjoint; an unnamespaced key lets an entity whose name equals some turn's id take that turn's score
and provenance. Codex plan-review found this and it is pinned by a collision test.

**The gate.** One predicate, parameterised by bound, flag and calibrated component, serving both
reranker-scored paths — rather than a second copy of FRE-1479's, which would become a second rule
the moment either changed. `config_guard`'s five reportable states are shared the same way.

**The sibling map now carries relevance values, not rank.** This supersedes what FRE-1004 preserved
on this path: `relevance_scores` holds the fused-rank sort key, and publishing it as the turn's
score is what made the evidence record read as though relevance had been established. FRE-1004's
actual obligation — a score the path established reaches the record — is unchanged and still pinned.

**`UNAVAILABLE`, not `NOTHING_RELEVANT`, and the predicate that took two attempts.** A first draft
reported a failed stage only when the turn admitted nothing. Codex plan-review caught it, and
ADR-0148 settles it in its own words: the ordering is *"fixed and total"*, rule 1 is *"`UNAVAILABLE`
outranks everything … whatever else happened"*, and *"a partially degraded recall is therefore
`UNAVAILABLE`, never `NOTHING_RELEVANT`."* `_rerank_fused_items` can score some indices and omit
others from one response, so the weaker rule would have let a turn admit the scored item and report
a completed run — partial evidence presented as a complete record. The rule is now **any**
unavailable drop, with no admitted-count qualifier.

A turn whose every candidate *was* measured and fell below the bound still composes
`NOTHING_RELEVANT`. That companion case is tested, because a rule that reported `UNAVAILABLE` for
every empty result would make absence unreachable — the defect ADR-0148 exists to remove, not one to
introduce.

**A fold-in: broad recall gets the same composition.** FRE-1479 routed its
`RECALL_RELEVANCE_UNAVAILABLE` drops into the discard report and stopped there, because FRE-1476's
status vocabulary had not landed. It has since. So a broad turn whose reranker was silent still
composed `NOTHING_RELEVANT` — the false-absence claim FRE-1479's own AC-4 named. One shared helper
now binds both paths.

**The unreranked branch: two questions, where FRE-1479 answered one.** When
`multipath_recall_enabled` is off, the legacy Cypher branch never reranks.

* *Does the bound apply?* No. It was measured on reranker scores and describes only a reranked set;
  applying it would reject every item on a number that never described them. FRE-1479's reasoning is
  right and is kept.
* *Has the path established relevance?* No — and this question was not asked before. D4's no-score
  rule is unconditional, and the only candidate fallback is the dense similarity FRE-1477 measured
  as admitting no calibrated bound.

So the items are admitted and the turn is `UNAVAILABLE`: shown, but not a record the model may treat
as complete. This is what lets AC-2 hold without qualification.

## A consequence worth naming before deploy

**With the reranker unavailable, this path now admits nothing and the turn reports `UNAVAILABLE`.**
`rerank()` never raises and never returns empty — it degrades to a passthrough whose scores are rank
order with no model id — so a Voyage outage takes this path from "admits five items on rank order"
to "admits none, and says so". That is the designed behaviour, it matches what FRE-1479 already
shipped for broad recall, and it is the FRE-1170 disease being made visible rather than silent. It
is still a live behaviour change under outage conditions and should be read as one.

## References

- ADR-0148 D2/D4/AC-10 — `docs/architecture_decisions/ADR-0148-absence-must-be-reachable-and-sayable.md`
- FRE-1479 — `docs/research/2026-09-10-fre-1479-reranker-relevance-calibration.md`, PR #1122
- FRE-1477 — `docs/research/2026-09-10-fre-1477-proactive-relevance-calibration.md`
- FRE-695 — reranker scales are arbitrary and not comparable across arms
- FRE-1170 — the reranker degrades to passthrough silently
- FRE-1476 — the four-state memory-context vocabulary this composes into
- Harnesses — `scripts/eval/fre1480_path_population/`, `scripts/eval/fre1479_reranker_calibration/`
