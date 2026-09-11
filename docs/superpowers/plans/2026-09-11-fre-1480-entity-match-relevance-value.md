# FRE-1480 — Give the entity-match path a relevance value (ADR-0148 D4)

**Ticket:** FRE-1480 · **Backing:** ADR-0148 D4 · **Blockers cleared:** FRE-1477 (`Awaiting Deploy`),
FRE-1479 (`Done`, PR #1122).
**Branch:** `fre-1480-entity-match-relevance-value` · **Diff class:** self-serve (no production
write path, no destructive code, no schema change, no cost or governance code).

---

## AC-1 first — the measured population, before the mechanism

ADR-0148 D4 leaves this ticket one decision: **which** relevance value the path acquires. AC-1
requires that decision to be made against a number. The number comes first.

**Instrument.** `agent-captains-captures-*`, field `recall_admission` (the ADR-0125 evidence
record). Each capture lists every candidate the recall layer produced, its kind, its score and
whether it was admitted. The producing path is read off the item shape, and every discriminator is
derived from the producing code rather than guessed:

| Path | Signature, and where it comes from |
|---|---|
| proactive | `suggestions.candidates` scores every admitted item through the sibling map (`context.py`), so no admitted item is unscored |
| entity match | `_multipath_query_memory` caps `conversations + entities` at `query.limit` (5), scores turns by fused rank `(total - position) / total`, and never scores entities |
| broad recall | `_format_broad_recall_context` emits `entity` and `session` items only, never `episode`, at `limit=20` |
| reached, admitted nothing | nothing admitted, but the proactive path reported drops — so proactive ran and returned no candidates, which is exactly the fall-through condition into the entity-match path |

**Window: 2026-08-13 to 2026-09-10. 258 captured turns carrying a `recall_admission` record.**

| Quantity | Value |
|---|---|
| Entity-match path **reached** (proactive ran, admitted nothing) | **24 turns (9.3%)** |
| ...of which **admitted** at least one item | **8 turns (3.1%)** — 33% of reached |
| Items admitted across the window | **38** |
| ...entities | **28, every one carrying no relevance value at all** |
| ...episodes | **10, every one carrying a fused-rank value, not a relevance value** |
| Items per admitting turn | mean 4.75, min 3, max 5 (the `limit=5` cap) |
| Turns classified broad recall | 1 |
| Turns classified proactive | 233 (90.3%) |

Two facts the table settles, and a limit on it:

1. **The path is small but it is not rare**, and it is reached in exactly the condition the rest of
   this chain exists to make honest — nearly one turn in ten. Every admitting turn sat at or near
   its own cap.
2. **The defect is wider than the ticket states.** The ticket names the entities, which carry no
   score. The 10 episodes carry `(total - position) / total` — rank order wearing the score field.
   That is the same defect FRE-1479 removed one path over, and it is present here too.
3. **Two limits, both measured rather than asserted.**
   * Captures exist only for completed turns, and ES loses events episodically (FRE-1051). The
     absolute turn counts are a lower bound. The ratios and the per-item findings are read off the
     item records themselves and do not depend on the corpus being complete.
   * The classifier attributes a turn to this path only when proactive left drops behind. Proactive
     can also fall through having produced no candidates **and** no discards, and such a turn is
     indistinguishable from one where the recall layer never ran (codex plan-review). That bucket is
     the ceiling on what `reached_turns` may undercount, the script reports it beside the figure it
     qualifies, and over this window it is **0 turns** — so the gap is real in principle and empty
     in fact here.

Reproduce: `scripts/eval/fre1480_path_population/measure.py` (committed with this change).

---

## The mechanism: route through the multipath core and use the reranker score

The ticket offers two candidates. The measurement, plus three facts read from the source, make this
one-sided.

**Candidate 1 is already the deployed route.** `query_memory` dispatches to
`_multipath_query_memory` whenever `query_text` is present and `multipath_recall_enabled` is on
(`service.py:4224`). The entity-match path always passes `query_text=user_message`, and the VPS sets
`AGENT_MULTIPATH_RECALL_ENABLED=true`. So the path already runs the fused + reranked core today.

**The core does not distinguish the two paths.** `_multipath_fused_recall`'s `path` argument is
documented as *"telemetry only"* (`service.py:5192`). `path="broad"` and `path="entity"` run the same
arms, the same RRF fusion, the same `reranker_input_cap`, and the same `rerank()` call. The score
space is not merely comparable to FRE-1479's — it is the same function of the same `query_text`.

**The score already exists and is already preserved; it is discarded one function below the
boundary.** FRE-1479 added `rerank_score` / `rerank_model` to `FusedResult` (`fusion.py:61-68`).
`_multipath_query_memory` walks those very items and replaces the value with
`(total - position) / total` (`service.py:5678`).

**Candidate 2 is not available.** Computing a fresh similarity over `query_text` needs its own bound
in embedding space. FRE-1477 measured the serving embedder and reported an **incompatibility**: no
bound rejects the median top-ranked non-match while admitting 90% of the labelled positives. D4's
named response to that is *"a reranker-side bound or a different arm"*. Candidate 2 would add a
second scorer and a second calibration in order to reach a score space already measured as having no
admissible bound, while a calibrated reranker score for the same items sits two lines away.

**Decision: candidate 1.** Recorded on the ticket with this reasoning.

### The bound is this path's own

D4 says one bound per path. The two paths here share a score space, so the *rule's* stated reason —
incomparable spaces — does not apply. The rule still does, and it is honoured literally:
a separate setting, a separate committed artifact, and a separate `config_guard` check. Reusing
`broad_recall_relevance_bound` directly would couple two paths whose cores are free to diverge, and
would leave AC-7 with no artifact naming this path.

The artifact is produced by **running** the harness, not by copying FRE-1479's number. The harness
already takes `--artifact`, so no harness change is needed. A genuinely independent re-run is also a
reproducibility check on FRE-1479's bound: close agreement strengthens both, and material divergence
is a finding the owner needs.

---

## Steps

Every step names its verification. TDD throughout: the failing test comes first and is confirmed
failing before the implementation.

### Step 1 — Commit the population measurement

Move the measurement script to `scripts/eval/fre1480_path_population/measure.py` with its
classifier documented. Read-only against ES. No substrate write.

*Verify:* `uv run python -m scripts.eval.fre1480_path_population.measure` reproduces the table above.

### Step 2 — Calibrate, and commit the artifact

```bash
make test-infra-up
uv run python -m scripts.eval.fre1479_reranker_calibration.calibrate \
  --run-id fre1480-$(date +%Y%m%d) \
  --artifact config/calibration/entity_match_relevance_bound.json
```

Test substrate only (`:7688` / `:9201` / `:5433`), pinned by the harness before any
`personal_agent` import. Roughly 54 paid rerank requests plus the seed's embedding calls.

*Verify:* the artifact exists, `component.role == "reranker"`, `component.model` equals the serving
reranker, `incompatible` is false, and `positive_admitted_share >= 0.90`.

**If the run reports an incompatibility**, no bound is configured, the gate ships inert, and that is
the result — the harness never picks a number. Surfaced to master rather than worked around.

### Step 3 — Register the artifact

`src/personal_agent/config/calibration.py`: add
`ENTITY_MATCH_RELEVANCE_BOUND_FILE = "entity_match_relevance_bound.json"`. The schema is
`RerankerRelevanceCalibration`, unchanged — same component role, same two document shapes.

*Verify:* `load_reranker_calibration(root, ENTITY_MATCH_RELEVANCE_BOUND_FILE)` parses the artifact.

### Step 4 — Settings

`src/personal_agent/config/settings.py`:

* `entity_match_relevance_bound: float | None`, default the measured value. `None` means no
  calibration is in force and the gate does not fire. It never defaults to zero.
* `entity_match_relevance_gate_enabled: bool = True`. Off restores pre-FRE-1480 admission exactly,
  which is what makes AC-4 able to fail.

Both carry the FRE-1479 comment block's discipline: the space is the serving reranker's own, and the
value is not comparable to `proactive_memory_relevance_bound`.

*Verify:* `make mypy`; `uv run python -m scripts.check_config`.

### Step 5 — `config_guard.check_entity_match_bound_calibration`

Mirrors `check_broad_recall_bound_calibration` over the new file and setting: the same five
reportable states, none of which changes the configured value. Registered in the aggregate.

*Verify (AC-7):* a test that sets the artifact's `component.model` to one not serving and asserts a
finding is raised while the configured bound stays in force.

### Step 6 — Plumb the score to the boundary

The score exists on `FusedResult`; three hops carry it out.

1. `memory/models.py` — `MemoryQueryResult` gains
   `relevance_values: dict[str, RelevanceValue]`, where `RelevanceValue` is a frozen model of
   `(score, model)`. Also `relevance_scored: bool`, False unless the reranking branch produced the
   set — FRE-1479's own field, for the same reason.

   **The key is namespaced by kind** (codex plan-review, confirmed in source). The two identifier
   spaces are not the same and are not disjoint: `EntityNode.entity_id` is populated from
   `node.name` (`service.py:447`), while `TurnNode.turn_id` is a turn identifier, and
   `FusedResult.item_id` is a Neo4j `elementId` for entity-kind items and a `turn_id` for turn-kind
   ones (`fusion.py:24-30`). An unnamespaced map lets one kind overwrite the other whenever an
   entity's name equals a turn id, and the item that loses gets another item's score and provenance.
   The key is therefore built from `memory_item_identity`'s own kind, the same namespacing
   discipline `turn_evidence.py` already applies to stance targets and for the same reason.
2. `memory/service.py::_multipath_query_memory` — in the existing walk over `recall.items`, where
   both the fused item and its resolved node are already in hand, record
   `relevance_values[node identity] = RelevanceValue(item.rerank_score, item.rerank_model)` when
   both are present. `relevance_scores` (the rank map) is left exactly as it is: it is the sort key
   the service contract already publishes, and this change adds a fact rather than reinterpreting
   one.
3. `memory/protocol_adapter.py::recall` — put `relevance_score` / `relevance_model` on each entity
   dict and each episode dict, read from `relevance_values`. `MemoryRecallResult` gains
   `relevance_scored: bool`. This mirrors FRE-1479's shape exactly, so the boundary reads the same
   two field names on both paths.

*Verify (AC-5):* a test driving the path end to end, asserting the value at the boundary equals the
score `_rerank_fused_items` computed for that item, and is not the RRF value and not the rank value.

### Step 7 — The gate at the admission boundary

`request_gateway/context.py`. `_broad_recall_relevance_verdict`'s three conditions are exactly this
path's conditions — a score exists, a model produced it, and that model is the one the bound was
calibrated against. **Generalise the existing function** to take its bound, its gate flag and its
calibrated model as arguments rather than writing a second copy: ADR-0148 D4 states one predicate
for every path, and a second copy becomes a second rule the moment either changes. This is the move
FRE-1479's own harness made with `choose_bound`, and a test asserts the function identity so a later
copy fails rather than drifting.

In the entity-match arm: build the verdict per entity and per episode, drop the rejected with their
reason, and admit the rest with their score in the sibling map. `_calibrated_reranker_model` gains
the artifact filename as an argument, cached per file.

*Verify:* AC-2 (every admitted item carries a value), AC-3 (a candidate at the calibrated negative
median whose name resolves and whose age is inside the 30-day window is rejected), AC-4 (the same
fixture with `entity_match_relevance_gate_enabled=False` is admitted).

### Step 8 — No relevance value means `UNAVAILABLE`, not `NOTHING_RELEVANT` (AC-6)

FRE-1476's vocabulary has landed, and `MemoryStatusReport.status` already composes `UNAVAILABLE`
from `recall.outcome is not COMPLETED`. What is missing is the report: a `RECALL_RELEVANCE_UNAVAILABLE`
drop never reaches the stage report, so a turn whose scorer was silent composes `NOTHING_RELEVANT`
today — the false-absence claim D4 forbids.

The rule:

> The path reports FAILED whenever **at least one** candidate was dropped as
> `RECALL_RELEVANCE_UNAVAILABLE`, whatever else the turn admitted.

**An earlier draft of this plan added "and it admitted nothing", and that was wrong.** Codex
plan-review caught it and ADR-0148 settles it in its own words (lines 266-277): the state ordering
is *"fixed and total"*, rule 1 is *"`UNAVAILABLE` outranks everything. If any arm failed to run to
completion, the turn did not establish what exists, **whatever else happened**"*, and the section
closes *"A partially degraded recall is therefore `UNAVAILABLE`, never `NOTHING_RELEVANT`."*

The case the weaker predicate got wrong is not hypothetical. `_rerank_fused_items` can score some
indices and omit others from one response (`service.py:5408-5424`), and FRE-1479's own shipped test
constructs exactly that mixed result (`test_broad_recall_relevance.py:323-331`). Under the weaker
rule such a turn admitted the scored item, reported COMPLETED, and composed `POPULATED` — a turn
presenting partial evidence as if the corpus had been searched to completion.

A turn whose every candidate **was** measured and fell below the bound still composes
`NOTHING_RELEVANT`, which stays correct: relevance was measured, and nothing cleared it.

Note the consequence, which is intended rather than tolerated: a turn can now be `UNAVAILABLE`
**while showing items**. Rule 1 outranks `POPULATED` by design — the items are shown, and the turn
may not claim the record is complete.

*Verify (AC-6):* three tests — the scorer disabled, the scorer raising, and the partial response
that scores one item and omits another — each asserting the composed status is `UNAVAILABLE`.

### Step 9 — Fold-in: the same composition for broad recall

The helper Step 8 adds is one call at the broad-recall return site. FRE-1479 shipped its
`RECALL_RELEVANCE_UNAVAILABLE` drops into the discard report and stopped there, so broad recall has
the identical false-absence defect its own AC-4 named. Folding it in is one line against leaving the
same defect alive in the neighbouring path while this ticket closes it here (skill step 5: fold in,
do not over-ticket). Flagged explicitly in the handoff.

Step 11's second half folds in here too, for the same reason: broad recall's
`relevance_scored == False` branch establishes no relevance value either, and leaving the two paths
answering D4's no-score rule differently would be worse than the divergence this closes.

*Verify:* broad-recall tests with the reranker disabled, and with `relevance_scored=False`, each
asserting `UNAVAILABLE`.

### Step 10 — AC-8: genuinely relevant entity recall survives, proved end to end and per probe

The artifact's admitted share is necessary and **not sufficient**, which codex plan-review is right
about: it measures the bound against a score list and proves nothing about the plumbing, the
resolution or the admission decision that sit between the reranker and `memory_context`. A
regression in any of those would leave the share untouched.

Two checks, both required:

1. **The bound retains the calibrated positives.** The committed artifact's
   `positive_admitted_share >= 0.90`, asserted for the entity half and the turn half **separately**,
   so neither shape can drag the other.
2. **Relevant recall survives the whole chain, per probe.** Drive a probe set of positives end to
   end — core, resolver, adapter, boundary — with the gate disabled to establish each probe's
   pre-change outcome, then again with it armed, and compare **per probe identifier**. Scores come
   from the committed artifact's own `positive_scores`, so the fixture population is the measured
   one rather than an invented one.

Per probe, not in aggregate. ADR-0148's own AC-6 states the reason: *"An aggregate count that holds
while individual probes swap outcomes is a failure, not a pass."*

*Verify:* both tests.

### Step 11 — The unreranked branch: the bound stays off it, and the turn is `UNAVAILABLE`

When `multipath_recall_enabled` is off, or `query_text` is empty, `query_memory` uses the legacy
Cypher branch, which never reranks.

**An earlier draft said only "the gate is inert here", copying FRE-1479's choice. Codex
plan-review showed that contradicts AC-2** — the plan would have claimed every admitted item carries
a relevance value while deliberately admitting unscored items on this branch. The contradiction is
real, and resolving it exposes that FRE-1479 answered one question where D4 asks two:

* **Does the bound apply?** No. The bound was measured on reranker scores and describes only a
  reranked result. Applying it to a set the reranker never saw would reject every item on the
  strength of a number that never described them. FRE-1479's reasoning is right and is kept.
* **Has the path established relevance?** No — and FRE-1479 never asked this. D4's no-score rule is
  unconditional: *"A path with a calibrated fallback value it can compute … uses that rather than
  reporting `UNAVAILABLE`. A path with neither reports `UNAVAILABLE`."* The only candidate fallback
  here is a dense similarity, and FRE-1477 measured the serving embedder as admitting **no**
  calibrated bound. So there is no calibrated fallback, and the rule applies.

**So: admit unchanged, and report `UNAVAILABLE`.** The items are shown, and the turn may not reason
from them as though the record were complete. AC-2 then holds without qualification for every item
admitted under an armed gate, and a branch that establishes no relevance value cannot license
unqualified reasoning.

Production runs multipath, so this is not the served case — but it is the case that makes the claim
honest.

*Verify:* a test that the unreranked branch admits unchanged, logs the warning naming the reason,
and composes `UNAVAILABLE`.

### Step 12 — Documentation

* `docs/research/2026-09-11-fre-1480-entity-match-relevance-value.md` — the AC-1 measurement, the
  mechanism decision and its evidence, the calibration result, and the two limits.
* `docs/reference/CONFIG_INVENTORY.md` — the two new settings.

---

## Acceptance criteria table

| AC | What it demands | How it is met | Where the proof is |
|---|---|---|---|
| AC-1 | Population measured before the mechanism is chosen | 258 turns, 2026-08-13..09-10: reached 24 (9.3%), undercount ceiling 0, admitted 8 (3.1%), 38 items, all 28 entities unscored | Step 1; the table above; ticket comment |
| AC-2 | Every admitted item carries a relevance value | Under an armed gate, unscored items are dropped, so admission implies a value. An unreranked branch arms no gate and reports `UNAVAILABLE` instead, so it licenses no unqualified reasoning either | Steps 7 and 11 tests |
| AC-3 | Gate rejects at the calibrated median non-match, in its own space | Fixture at `negative_median_rerank`, name resolving, age inside 30 days | Step 7 test |
| AC-4 | That test can fail | Same fixture, `entity_match_relevance_gate_enabled=False` | Step 7 test |
| AC-5 | The gated value is the path's own scorer output | Assert equality with `_rerank_fused_items`'s score; assert it is not the RRF or rank value | Step 6 test |
| AC-6 | No value means no admission, and `UNAVAILABLE` not `NOTHING_RELEVANT` | Step 8's rule: **any** unavailable drop reports FAILED, per ADR-0148's total ordering | Step 8 tests (disabled, raising, partial response) |
| AC-7 | Bound traces to an artifact naming the component | `entity_match_relevance_bound.json`, role `reranker`, plus the `config_guard` staleness check. ADR-0148 AC-10 names this artifact explicitly | Steps 2, 3, 5 |
| AC-8 | Genuinely relevant recall survives | Committed `positive_admitted_share >= 0.90` per document half, **and** a per-probe end-to-end comparison gate-off against gate-on | Step 10 tests |

---

## Halt conditions

* The calibration reports an incompatibility → no bound configured, gate inert, surface to master.
* `make mypy` shows more than 5 errors this change did not introduce → separate ticket.
* The plan would drop or quarantine historical rows → it does not; nothing here writes substrate.

## References

* ADR-0148 D4 — `docs/architecture_decisions/ADR-0148-absence-must-be-reachable-and-sayable.md`
* FRE-1479 — `docs/research/2026-09-10-fre-1479-reranker-relevance-calibration.md`, PR #1122
* FRE-1477 — `docs/research/2026-09-10-fre-1477-proactive-relevance-calibration.md` (the embedder
  incompatibility that closes candidate 2)
* FRE-695 — reranker scales are arbitrary and not comparable across arms
* FRE-1170 — the reranker degrades to passthrough silently
* `memory/service.py:4224`, `:5160-5200`, `:5336-5425`, `:5602-5730`; `memory/fusion.py:52-77`;
  `request_gateway/context.py:151-360`, `:693-773`; `request_gateway/memory_status.py`

---

## Codex plan-review — round 1

Five findings. Three were accepted as stated, two were accepted and then measured rather than
argued. Nothing was waved away.

| # | Finding | Disposition |
|---|---|---|
| 1 | Step 8's predicate reported a **partially degraded** rerank response as COMPLETED, so a turn admitting one scored item while dropping another composed `POPULATED` | **Accepted, rewritten.** ADR-0148's ordering is "fixed and total" and rule 1 says `UNAVAILABLE` outranks everything "whatever else happened". The predicate is now any unavailable drop, with no admitted-count qualifier |
| 2 | The `relevance_values` key was unnamespaced across two identifier spaces that are not disjoint — `EntityNode.entity_id` is the entity **name** (`service.py:447`), `TurnNode.turn_id` is a turn id | **Accepted.** Verified in source. The key is namespaced by kind, which costs nothing and removes the class |
| 3 | AC-2 claimed every admitted item carries a value while Step 11 deliberately admitted unscored legacy items | **Accepted, and it exposed more than the contradiction.** D4 asks two questions where FRE-1479 answered one. The bound still does not apply to an unreranked set; the turn now reports `UNAVAILABLE` |
| 4 | The AC-1 classifier misses a fall-through where proactive left no drops | **Accepted, then measured.** The script now publishes that bucket as an explicit undercount ceiling beside the figure it qualifies. Over this window it is **0 turns** |
| 5 | AC-8 substituted calibration retention for an end-to-end regression proof | **Accepted.** The artifact share is kept and a per-probe end-to-end comparison is added, per ADR-0148 AC-6's own rule against aggregates |

Two of codex's verdicts were confirmations rather than defects, and both are load-bearing: the
mechanism claim (`path` is telemetry only, so the two paths share one score space) was verified
against `service.py:5203-5275`, and the separate-artifact decision turns out to be named outright by
ADR-0148 AC-10 — *"the entity-match bound against whichever scorer that path acquires"*. Codex also
noted, correctly, that the rerun is provenance rather than a statistically distinct population; that
is recorded in the research document rather than claimed otherwise.
