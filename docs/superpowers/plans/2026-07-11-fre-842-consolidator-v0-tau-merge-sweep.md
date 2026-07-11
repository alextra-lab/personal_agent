# FRE-842 — ADR-0114 offline consolidator v0 + τ_merge sweep

**Backing ADR:** `docs/architecture_decisions/ADR-0114-heterarchical-associative-memory-study.md`, decision D5.
**Depends on:** FRE-839 (evidence-layer schema, categorizer, accretion writer — merged), FRE-840 (baseline harness — merged).
**Feeds:** FRE-843 (v0 synthesis/seam — owns the real end-to-end run at a single τ_merge\*, AC-3/AC-4/AC-7 verdicts).

## Scope decision (mirrors FRE-840's precedent)

FRE-840 built the baseline-harness *mechanism* + AC-4 scoring rig but explicitly did not run
the real ≥30-cue AC-4 verdict — that's FRE-843's job once FRE-841's frozen cue set exists. This
ticket follows the same split for AC-3, worded to match FRE-840's own phrasing exactly (codex
plan-review finding: "proves AC-3" overclaims — this ticket builds the mechanism and reports real
computed numbers for the objectively-computable sub-parts, it does not deliver an AC-3 pass/fail
verdict):

- **This ticket builds and live-verifies the consolidator mechanism**: two-stage canonicalization
  (GDS Node Similarity candidate generation, combined with category-name-embedding cosine — see
  below, not Jaccard alone — plus a pure-Python fallback for both signals, then a rule-based typed
  decision), the τ_merge sweep driver (multi-seed, multi-ordering, checkpointed category-count
  curve), and the **objectively-computable** AC-3 sub-checks — (a) plateau, (d) distinctness, (e)
  non-collapse floor, (f) stochastic stability — plus the overlap-pair histogram and the top-20/tail
  **tables** (unrated). Reports real numbers from the live sandbox, not just fixtures.
- **Out of scope, deferred to FRE-843** (owns "AC-3 ... as quality gates" per its own ticket text):
  AC-3(b)/(c) head/tail **legibility**, which the ADR specifies needs "2 independent judges" — a
  human/LLM rating step, not a mechanism this ticket can honestly claim to prove alone. FRE-843
  selects τ_merge\* and runs the rating pass at that one operating point.
- Also out of scope: the real N-seed, full-102-session `run_ingest` sweep (N× the $5/day study
  budget cap) — that's a cost-incurring run needing an explicit owner go-ahead, same posture as
  `run_ingest --execute-full`. This ticket's sweep CLI is runnable today against whatever seeded
  ledgers already exist in the sandbox (currently: 46 episodes ingested at seed 0, 6 at seed 1 —
  confirmed live) and documents the runbook for a fuller run later.

**Codex plan-review (2026-07-11) — findings folded in below:** (1) name-embedding cosine must be
wired into the default candidate path, not left optional — ADR D5 names it as part of Stage 1, and
Jaccard-alone misses near-synonym category names with low current member overlap; (2) the
containment "size-ratio test" needs a precise definition + a minimum-size guard so noisy singleton
categories don't trigger spurious `SUBSUMED_BY` decisions; (3) AC-3 wording tightened above; (4)
confirmed sound as planned; (5) `apply_canonicalization_to_graph` must recompute `MEMBER_OF` from
assertions grouped by *canonical* category identity (never copy/merge derived-edge properties
directly), so a concept already belonging to both merge sides is aggregated once, not double-counted
— assertions stay untouched/immutable per D2, only reachable via a new `CANONICALIZED_AS` edge
+ a canonical-aware recompute; union-find representative selection must be a deterministic pass over
each final component, independent of union order; GDS candidate pairs must be normalized (unordered,
deduped, self-pairs excluded) before Stage 2 sees them; (6) the sweep CLI needs an explicit
study-target preflight (refuse to run unless the resolved Neo4j URI matches the study substrate),
mirroring `export_snapshot.py`/`baseline_harness.py`.

## Live-verified building blocks (this session, against the running `seshat-neo4j-study` container)

- `gds.graph.project('name', ['Concept','Category'], {MEMBER_OF: {orientation: 'REVERSE'}})` +
  `gds.nodeSimilarity.stream` returns real Category-Category Jaccard pairs from the live sandbox
  (e.g. `operational telemetry` / `agent mode evaluation` similarity 1.0) — confirms the GDS Stage-1
  design is correct, not just plausible.
- Real category scatter exists today: 818 Concepts, **1341 Categories**, 1667 `MEMBER_OF` edges from
  only 46 ingested episodes — the exact snowflake ADR-0114 predicts, a concrete before-picture for
  the consolidator to demonstrably reduce.

## Files

### New: `scripts/study/consolidator.py`

ADR-0114 D5 op (1) canonicalize + op (3′) decay/prune.

- `CategoryMembers` (normalized_name, display_name, concept_ids: frozenset[str])
- `CandidatePair` (category_a, category_b, jaccard, name_cosine, combined_score) — `category_a`/`category_b`
  always stored as an ordered pair (`min`/`max` by normalized_name) so dedup/lookup is trivial
- `TypedDecision` enum: `ALIAS | SUBSUMED_BY | RELATED | DISTINCT | UNCERTAIN`
- `CandidateDecision` (pair, decision, rationale)
- `CanonicalizationResult` (canonical_of: dict[str,str], decisions: list[CandidateDecision], canonical_category_count: int)
- `fetch_category_membership_snapshot(session) -> dict[str, CategoryMembers]` — live `MEMBER_OF` read
- `embed_category_names(names, *, embedder=generate_embeddings_batch) -> dict[str, list[float]]` —
  thin wrapper over `personal_agent.memory.embeddings.generate_embeddings_batch` (mode="document"),
  local-deferred import per house style; called **once per checkpoint snapshot** (category set doesn't
  depend on τ_merge) and the resulting embeddings are reused across the whole τ_merge grid for that
  checkpoint — not recomputed per config.
- `generate_candidates_pairwise(memberships, *, top_k, min_jaccard, name_embeddings=None, jaccard_weight=0.6) -> list[CandidatePair]` —
  pure-Python O(n²) Jaccard + optional name-cosine blend (`combined_score = jaccard_weight * jaccard +
  (1 - jaccard_weight) * name_cosine` when embeddings supplied, else `combined_score = jaccard`,
  documented fallback matching the ADR's explicit v0 sandbox-scale allowance); the v0 default path used
  by the sweep (fast, deterministic, no per-config Neo4j/embedder round trip once embeddings are cached
  per checkpoint)
- `generate_candidates_gds(driver, *, graph_name, top_k, similarity_cutoff, name_embeddings=None, jaccard_weight=0.6) -> list[CandidatePair]` —
  the ADR's "designated mechanism": project the bipartite graph (`orientation: REVERSE`), run
  `gds.nodeSimilarity.stream`, drop the projection in `finally`; normalizes pairs (unordered, self-pairs
  excluded, `(a,b)`/`(b,a)` deduped keeping max similarity) before returning, then blends in name-cosine
  the same way as the pairwise path. Live-verified above.
- `decide_candidate_type(pair, memberships, *, tau_merge, subsumption_containment_floor=0.8, subsumption_size_ratio_floor=2.0, min_category_size_for_subsumption=2, related_floor=0.3, uncertain_margin=0.15) -> CandidateDecision` —
  Stage 2 rule-based decision. Containment check FIRST, but only when **both** categories have
  ≥`min_category_size_for_subsumption` members (guards against a noisy 1-member category forcing a
  spurious hierarchy decision): `size_ratio = max(|A|,|B|) / min(|A|,|B|)`; if `size_ratio >=
  subsumption_size_ratio_floor` AND `max(containment_a_in_b, containment_b_in_a) >=
  subsumption_containment_floor` ⇒ `SUBSUMED_BY` (narrower subsumed by broader), never `ALIAS`,
  regardless of τ_merge — the "don't merge a broader parent into a narrower one" correctness guard the
  ADR names explicitly. Below the size/min-size gates, falls through to: τ_merge-gated `ALIAS`, an
  `UNCERTAIN` band just below τ_merge, a `RELATED` band below that, else `DISTINCT`.
- `canonicalize(memberships, candidates, *, tau_merge, ...) -> CanonicalizationResult` — union-find
  over `ALIAS`-only decisions (never `SUBSUMED_BY`/`RELATED`) used only to GROUP categories into
  components (internal root choice during union is irrelevant/order-dependent by design); canonical
  representative is picked in a **separate, deterministic pass over each final component** (member-count
  desc, then normalized_name asc) — tested by shuffling candidate input order and asserting identical
  canonical assignment.
- `apply_canonicalization_to_graph(session, result) -> None` — the real single-τ_merge\* write-back
  primitive for FRE-843's later use. Never rewrites or moves `MembershipAssertion`s (immutable per D2).
  For each merge group: (1) `MERGE (absorbed:Category)-[:CANONICALIZED_AS {tau_merge, decided_at}]->(canonical:Category)`
  for audit; (2) recompute `MEMBER_OF` **from assertions, grouped by canonical category identity**
  (walk `MembershipAssertion-[:PROPOSES]->Category` through zero-or-more `CANONICALIZED_AS` hops to
  the root, mirroring `writer.recompute_member_of_batch`'s `avg(confidence)`/`count(DISTINCT episode)`/
  `max(when)` aggregation but keyed by the canonical category, not the original) for every concept
  touched by the merge; (3) delete the now-superseded `MEMBER_OF` edges to absorbed categories. This
  is what correctly handles a concept that already belongs to both merge sides — it is aggregated once
  from the union of its backing assertions, never double-counted by copying two derived edges' stored
  properties together. **Not invoked against the live shared sandbox by this ticket** — write-only,
  unit-tested with a fake session (including the both-sides-already-member case); a real invocation is
  a consequential/shared-state action for FRE-843's owner-gated run.
- `decay_and_prune(session, *, reference_time, decay_factor, floor, stale_after) -> DecayPruneResult` —
  op (3′): `SET m.membership_confidence = m.membership_confidence * decay_factor` for edges whose
  `last_supported_at < reference_time - stale_after`, then delete (suppress) edges now below `floor`.
  Only the derived `MEMBER_OF` edge is touched — assertions untouched (evidence retained, D4's
  "forgetting is deliberate and reversible"). Dry-run by default (returns the would-be-affected counts
  without mutating) with an explicit `apply=True` to actually write, mirroring `export_snapshot.py`'s
  `--execute` posture.

New schema element (additive, no migration for existing data — mirrors how `SUBSUMES` was declared
schema-only-in-v0 by FRE-839): document `(:Category)-[:CANONICALIZED_AS {tau_merge, decided_at}]->(:Category)`
in `scripts/study/schema.py`'s module docstring list of schema-only-in-v0 elements (no new constraint
needed — it's an edge type, not a uniquely-constrained node).

### New: `scripts/study/sweep.py`

The τ_merge sweep driver + AC-3(a,d,e,f) computed checks.

- `AssertionRecord` (concept_id, category_normalized_name, category_display_name, proposed_confidence, episode_id, when, seed)
- `fetch_seeded_ledger(driver, seed) -> list[AssertionRecord]` — real `MembershipAssertion` read, one seed
- `chronological_episode_order(ledger) -> list[str]`
- `permuted_orders(episode_ids, *, n_permutations, seed) -> list[list[str]]` — deterministic
  `random.Random(derived_seed).shuffle`, ≥2 permutations per ADR AC-3
- `build_snapshot(ledger, episodes_included) -> dict[str, CategoryMembers]`
- `category_count_curve(ledger, order, *, tau_merge, checkpoint_every, candidate_fn) -> list[CurvePoint]` —
  `CurvePoint(conversations_processed, raw_category_count, canonical_category_count)`; `raw_category_count`
  at each checkpoint is the free no-consolidator control curve, computed from the SAME data with zero
  extra cost
- `plateau_check(curve, *, first_tertile_frac=1/3, final_tertile_rate_ceiling=0.25) -> PlateauResult` — AC-3(a)
- `distinctness_check(memberships, canonicalization, *, overlap_ceiling) -> DistinctnessResult` — AC-3(d)
  + overlap-pair histogram data
- `non_collapse_check(curve, *, floor) -> bool` — AC-3(e)
- `stochastic_stability_check(curves_by_seed, *, variance_bound) -> StabilityResult` — AC-3(f)
- `top20_and_tail_tables(memberships, canonicalization, *, tail_sample_size=20, seed) -> dict` — sorted
  tables, ready for a human/LLM rating pass — the rating itself is FRE-843's job
- `run_sweep(driver, *, seeds, tau_merge_grid, checkpoint_every, n_permutations, permutation_seed) -> SweepReport` —
  orchestrates the above across every (seed × ordering × τ_merge), returns a JSON-able report
- CLI (`__main__`): mirrors `run_baseline.py`'s `study_substrate_env` pinning; adds an explicit
  preflight (refuse to run unless the resolved Neo4j URI matches `StudySettings().neo4j_uri` /
  `localhost:7691`, mirroring `export_snapshot.py`/`baseline_harness.py`'s "never point this at prod"
  guard) before any read; writes `scripts/study/snapshots/consolidator-sweep-<run-id>.json`
  (gitignored) + prints a summary table

### Edited: `scripts/study/schema.py`

Add `CANONICALIZED_AS` to the module docstring's schema-only-in-v0 list (documentation only, no new
constraint statement — see above).

### Edited: `scripts/study/README.md`

New "Offline consolidator v0 + τ_merge sweep (FRE-842)" section: usage for `consolidator.py`'s
CLI-free primitives (imported, not a standalone script) and `sweep.py`'s CLI, the scope-decision note
above, and the runbook for a real full N-seed sweep later (cost/owner-gate call-out).

### New tests

- `tests/scripts/study/test_consolidator.py` — Jaccard candidate gen (pairwise + a fake-GDS-session
  variant asserting the projection/stream/drop Cypher calls happen in order, self-pair exclusion, and
  `(a,b)`/`(b,a)` dedup keeping max similarity), name-cosine blending (with/without embeddings supplied),
  Stage 2 decision-rule boundaries (alias / subsumed_by / related / distinct / uncertain, including: the
  containment-guard regression — a broad parent + narrow child pair must decide `SUBSUMED_BY` even when
  jaccard clears τ_merge; and the min-size guard — a 1-member category fully contained in a larger one
  must NOT be forced to `SUBSUMED_BY`, falls through to the normal ladder instead), union-find
  canonicalization (transitive merges; representative selection tested by shuffling candidate order and
  asserting an identical canonical assignment), decay/prune (dry-run reports without mutating; `apply=True`
  mutates only `MEMBER_OF`, never touches `MembershipAssertion`), and `apply_canonicalization_to_graph`'s
  both-sides-already-member case (a concept with existing `MEMBER_OF` edges to BOTH merge-side categories
  ends up with exactly one edge to the canonical category, aggregated once from the union of backing
  assertions — not double-counted).
- `tests/scripts/study/test_sweep.py` — permutation determinism (same seed ⇒ same order), curve
  checkpointing, plateau/distinctness/non-collapse/stability check functions against hand-built
  fixtures with known answers (a monotonically-growing no-merge fixture must fail non-collapse... no,
  must fail plateau; a fixture engineered to plateau must pass).
- `tests/scripts/study/test_consolidator_integration.py` (marked `integration`, skip-if-unreachable
  like `test_run_ingest_integration.py`) — live GDS candidate generation against the real running
  `seshat-neo4j-study` sandbox, proving the projected-graph Cypher this session hand-verified stays
  correct under the actual module code.

## Test plan

```
make test-file FILE=tests/scripts/study/test_consolidator.py
make test-file FILE=tests/scripts/study/test_sweep.py
make test-file FILE=tests/scripts/study/test_consolidator_integration.py   # needs study-infra-up (already running)
make test    # full suite
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Acceptance-criteria support this ticket delivers (mechanism + partial AC-3 — not an AC-3 pass verdict)

Per Step 2 of the build skill — the ticket names AC-3 as the slice it proves. Mirroring FRE-840's own
framing ("this ticket builds the reusable mechanism... not the verdict itself"), this ticket builds the
consolidator + sweep mechanism and reports real computed numbers, against the live sandbox, for AC-3's
**objectively-computable** sub-parts:

- AC-3(a) plateau — `plateau_check` run against the live 46-episode/seed-0 ledger, chronological order.
- AC-3(d) distinctness — `distinctness_check` + overlap histogram, same run.
- AC-3(e) non-collapse floor — `non_collapse_check`, same run.
- AC-3(f) stochastic stability — computed across whatever seeds exist (seed 0 vs seed 1; small-N caveat
  noted honestly — 6 episodes at seed 1 is too small for a real variance verdict, reported as such, not
  hidden).
- AC-3(b)/(c) legibility — **not proven here**; the mechanism produces the top-20/tail tables, rating is
  FRE-843's job at the chosen τ_merge\*.

The ticket comment to master will report the real numbers from this run (not fixture-only), state the
seed-1 small-N caveat plainly, and name FRE-843 as the owner of the AC-3(b)/(c) + AC-4 assembled verdict.
