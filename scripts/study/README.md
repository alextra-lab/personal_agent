# ADR-0114 study substrate (FRE-838)

Isolated Neo4j+GDS sandbox for the decoupled heterarchical associative-memory
research study (`docs/architecture_decisions/ADR-0114-heterarchical-associative-memory-study.md`,
decision D1). Never wired to prod; see `docker-compose.study.yml` and
`scripts/study/verify_isolation.py`.

## Stand up the substrate

```
make study-infra-up      # Neo4j+GDS on bolt://localhost:7691 (http :7478)
make study-infra-ps       # status
make study-infra-down     # stop
make study-infra-reset    # stop + wipe volume (full reset)
```

Requires `STUDY_NEO4J_PASSWORD` in `.env` — distinct from `NEO4J_PASSWORD`
(prod) by design.

**Resources (fix-forward, 2026-07-10):** master's first live corpus-load
attempt OOM-killed `neo4j-study` at the original 1.5g mem_limit / 1g heap —
the real prod corpus (10,290 nodes / 34,301 relationships) plus the GDS
plugin needs real headroom. Bumped to `mem_limit: 4g` / heap `2g` /
pagecache `512m` in `docker-compose.study.yml`; re-verified end-to-end
against a prod-scale synthetic corpus (see `tests/scripts/study/test_export_snapshot_scale.py`).

## Verify isolation (AC-5(1))

```
uv run python scripts/study/verify_isolation.py
```

Confirms: the compose file has no network leak paths (`network_mode`,
`extra_hosts`, `cloud-sim` membership), and a container on
`seshat-study-net` cannot resolve or reach prod's Neo4j/Postgres/
Elasticsearch (DNS + TCP probes). Also exercised as
`tests/scripts/study/test_verify_isolation.py` (static checks run in
`make test`; the runtime DNS/TCP probes are marked `integration` and need
`make study-infra-up` first).

## Export the frozen corpus (AC-5(2) + the corpus itself)

```
uv run python scripts/study/export_snapshot.py                # dry run — counts only
uv run python scripts/study/export_snapshot.py --execute       # real prod read + sandbox write
```

Both the direct-path form above and `uv run python -m scripts.study.export_snapshot [--execute]`
work (fix-forward, 2026-07-10: the direct-path form originally crashed with
`ModuleNotFoundError` on the deferred `scripts.study.config` import — direct
execution doesn't put the repo root on `sys.path` the way `-m` invocation
does; the script now bootstraps `sys.path` itself when run directly). Run
from the repo root either way.

Reads prod Neo4j (`Entity`/`Turn`/`Session`/`Person`/`Agent`/`Claim`/
`Location`/`EntityDescriptionVersion` + their relationships) and prod
Postgres `sessions.messages` (conversation traces) **read-only**, writes a
1:1 copy into the study sandbox, and writes `scripts/study/snapshots/snapshot_manifest.json`
(snapshot date, content hash over the full corpus including traces, node/
relationship/session counts — gitignored, no raw content).

**Safety:** refuses to write unless `--execute` is passed, and refuses to
run (even with `--execute`) unless `STUDY_NEO4J_URI` positively resolves to
the study substrate (`localhost:7691`) — never prod. A relationship whose
endpoint node wasn't resolvable (e.g. prod wasn't perfectly quiesced between
the node-read and relationship-read passes) is skipped, logged, and counted
in the manifest's `skipped_relationships` field, rather than aborting the
whole run — check that field is `0` after a real run; a non-zero count means
the frozen corpus is missing some edges and the run should be investigated
(likely re-run during a quieter window).

**AC-5(2) proof (zero prod deltas):** capture prod node/relationship totals
immediately before and after the `--execute` run (`count_nodes_and_relationships`
in `export_snapshot.py`, or `MATCH (n) RETURN count(n)` / `MATCH ()-[r]->()
RETURN count(r)` in `cypher-shell`) and confirm they're identical — the read
performs no writes, so a quiet window (no concurrent live turns) makes the
before/after counts a clean zero-delta proof.

**Status (2026-07-10): the live export has run.** `snapshot_manifest.json`
records 10,290 nodes / 34,301 relationships / 102 sessions,
`skipped_relationships: 0` — the complete corpus, including the
entity-to-entity associative edges (`RELATED_TO`/`USES`/`PART_OF`/
`SIMILAR_TO`) an earlier hardcoded-allowlist bug had silently dropped
(fixed in the dynamic label/rel-type discovery above). FRE-839+ are
unblocked.

## Evidence-layer schema, ingest categorizer, accretion writer (FRE-839)

ADR-0114 D2/D3/D4 — the learn-at-ingest, accrete-not-overwrite half, built
on the frozen corpus above.

```
uv run python -m scripts.study.schema           # apply/verify the evidence-layer schema (idempotent)
uv run python -m scripts.study.run_ingest --limit 5      # small, cheap sample
uv run python -m scripts.study.run_ingest --execute-full # ALL sessions — real LLM cost, see below
uv run python -m scripts.study.ac_proof          # AC-1 + mechanism-AC-2 report
```

**Schema** (`scripts/study/schema.py`): `Concept` hub (preserving the
ADR-0109 entity `kind` as a control property), `Surface`-`ALIAS_OF`-`Concept`
alias resolution, the evidence layer (`Episode`/`Mention`/
`MembershipAssertion`, append-only), the derived layer (`MEMBER_OF`,
`MENTIONED_IN`). `SUBSUMES` and `RelationAssertion` are schema-only in v0
(documented in the module, not populated) — v1 (FRE-855) and the relation
arm (FRE-840+) write to them with no migration needed.

**Ingest categorizer** (`scripts/study/categorizer.py`): an LLM reads the
full conversation and proposes 1-3 associative categories per concept
already known to be discussed in it (read off the frozen corpus's
`Session-[:DISCUSSES]->Entity` edge, not rediscovered). Provenance
(`model`/`prompt_version`/`seed`) is Python-stamped, never trusted from the
model. Cost routes through the isolated `study` cost-gate role
(`config/governance/budget.yaml`, $5/day · $7/week) — separate from
`entity_extraction`'s cap so a corpus run can never contend with live
production extraction.

**Accretion writer** (`scripts/study/writer.py`): appends `Mention`s and
`MembershipAssertion`s (never overwrites); `MEMBER_OF` is recomputed from
the full backing-assertion set after each conversation, batched (one
Cypher round-trip per episode, not per concept/membership — the FRE-838
N+1 lesson applied here too). Alias resolution mirrors
`memory/dedup.py`'s established algorithm, with one deliberate asymmetry:
exact case-insensitive matches merge regardless of `kind` (first-write-wins
on the shared hub — needed to collapse the ADR's own named case-variant
bug, where prod tagged variants with *different* kinds), while the
embedding-similarity fallback stays kind-gated (the real homonym-risk
path). See `writer.py`'s module docstring for the full rationale and the
one known, deliberately-deferred gap (byte-identical same-case homonyms —
FRE-841/843's job).

**Runbook — the real `--execute-full` corpus run:** this makes ~102 real,
paid LLM calls against the real conversation corpus. Preconditions:
1. `make study-infra-up` running against the real corpus (see above).
2. The `study` budget role/cap in `config/governance/budget.yaml` reflects
   an owner-confirmed value (not silently bumped).
3. An explicit owner go-ahead for the run itself (separate from the code
   being merged) — per this project's "confirm before consequential/
   cost-incurring actions" norm.

After it completes, `uv run python -m scripts.study.ac_proof` reads the
AC-1 (population-scale multi-parent accretion — median `MEMBER_OF` degree
≥2 **and** ≥60% of the eligible set with ≥2 provenance-distinct
memberships, the two conditions computed independently, never one
inferred from the other) and mechanism-AC-2 (alias resolution — the ADR's
own named case-variant pairs resolving to one `Concept` hub) numbers for
the final ticket comment. A clean null (the corpus doesn't clear the bar)
is a valid, budgeted ADR-0114 outcome, reported honestly — not reframed.

## Baseline harness + scoring rig (FRE-840)

ADR-0114 D7/D8 — arm A of the D9 ladder. Reproduces PRODUCTION multipath
recall (ADR-0104 rank-fusion, as enabled in the owner's live config) against
the frozen sandbox, and scores it (Recall@20/nDCG@20) via a paired-comparison
statistical rig FRE-843 will later point at arm C.

```
make study-infra-up
docker start cloud-sim-embeddings   # stop again when done -- the live
                                     # default profile is the managed OVH
                                     # embedder (README convention above)
uv run python -m scripts.study.run_baseline
docker stop cloud-sim-embeddings
```

**Baseline harness** (`scripts/study/baseline_harness.py`): connects a
`MemoryService` to the study sandbox (`bolt://localhost:7691`), enables the
flags ADR-0114 names as live (`multipath_recall_enabled`/
`lexical_arm_enabled`/`multiquery_arm_enabled`, floor 0.60), pins
`relevance_bounded_recall_enabled` off (the ADR does not claim ADR-0100 is
also live), and ensures the `entity_embedding`/`turn_entity_fulltext`
indexes exist (the study schema only builds the `Concept` vector index).
Env-pinning (`scripts.study.config.study_substrate_env`) is the CLI
entrypoint's job, not the harness module's — so the harness is safely
importable in unit tests without mutating the shared test process's env.

**Fix-forward (2026-07-10, discovered running this harness for real):** the
frozen corpus's entities all carry FRE-229 `visibility='group'`, which the
visibility filter only admits for **authenticated** requests. An
unauthenticated recall query silently sees zero entities on this corpus — a
false floor that would make any baseline-vs-study comparison meaningless.
`run_baseline_recall` sets `authenticated=True` to match how every real
production conversation actually reaches recall.

**Scoring rig** (`scripts/study/scoring_rig.py`): reuses
`recall_at_k`/`ndcg_at_k` from `scripts/eval/fre435_memory_recall/metrics.py`
(no reimplementation) and adds the AC-4 paired-comparison layer: a
percentile bootstrap CI (`paired_bootstrap_ci`) as the effect-size + 95% CI
primitive — chosen over Wilcoxon signed-rank because the ADR pre-registers
both as acceptable and `scipy` is not a dependency anywhere in this repo —
`paired_significance`/`non_inferiority_test`, and `evaluate_ac4`, which
combines AC-4(i) relative lift ≥1.10×, (ii) absolute floor, (iii)
significance, and the nDCG non-inferiority check into one verdict. Empty-gold
cue pairs are excluded from paired diffs (never silently coerced to 0) and
the excluded count is always reported.

**Scope note.** This ticket builds the reusable mechanism, not the AC-4
verdict itself: `scripts/study/baseline_cues_smoke.yaml` is a 5-cue
**smoke** fixture (drawn from the ADR's own named forensic examples) that
proves the harness+rig run end-to-end and produce a scored table — it is
**not** the AC-4 pre-registered ≥30-cue/≥4-domain frozen set. That set is
FRE-841's deliverable (a separate, concurrently-tracked ticket); FRE-843 (v0
synthesis) is the seam owner that runs `evaluate_ac4` for real once FRE-841's
frozen set and arm C (FRE-842) both exist.

```
uv run python -m scripts.study.run_baseline --cues <path-to-frozen-set.yaml> --run-id <id>
```

writes `scripts/study/snapshots/baseline-<run-id>.json` (gitignored) and
prints the scored table to stdout.

## Pre-registered eval artifacts (FRE-841)

`scripts/study/eval_artifacts/` builds the **frozen, pre-registered**
artifacts AC-2 and AC-4 will later be scored against (the pass rules and
numeric margins are FRE-843's job — this ticket only builds the ground
truth, before any scoring code exists). Both are committed JSON
(`scripts/study/eval_artifacts/frozen/`), timestamped and content-hashed
(`scripts/study/eval_artifacts/freeze.py`, mirroring `export_snapshot.py`'s
manifest pattern), and cross-reference the frozen corpus's own
`snapshot_manifest.json` content hash for traceability.

### AC-2 hard-negative pairs (`ac2_pairs.py` → `frozen/ac2_hard_negative_pairs.json`)

```
uv run python -m scripts.study.eval_artifacts.ac2_pairs            # dry run — counts only
uv run python -m scripts.study.eval_artifacts.ac2_pairs --execute  # writes the frozen artifact
```

**V⁺** (873 real pairs) is mined directly from the frozen `Entity` corpus,
not hand-built: a case-fold grouping (`corpus_case_variant`, 621 pairs)
plus a looser, punctuation-normalized grouping additive to it
(`corpus_near_variant`, 252 pairs — catches ADR AC-2's "near-variant"
language beyond plain case-folding). The near-variant normalizer is
deliberately conservative — it strips only low-information cosmetic
punctuation (hyphens, underscores, apostrophes, parens, colons, periods,
pipes) and never `+`/`*`/`/`/`&`, because a first live run found those
characters are load-bearing in this corpus's naming conventions
(`Security` vs `Security+` is a topic vs. a certification, not a
formatting variant; `Agent` vs `agent-*` is a concept vs. an index-glob
pattern) — stripping them produced false-positive "should merge" pairs
that would have corrupted AC-2's own ground truth.

**V⁻** (12 seeded pairs) cannot be mined from the corpus: a live check of
known homonym-prone surface forms (`python`, `apple`, `mercury`, `turkey`,
`amazon`, `mars`, ...) found every one maps to exactly one sense in the
real data today — the corpus has zero naturally-occurring homonym
collisions at this scale (matches `writer.py`'s documented gap almost
verbatim). V⁻ is therefore a hand-authored adversarial set — the ADR's own
2 named pairs plus 10 more spanning the corpus's real domains — each
resolved against the live corpus so a side that happens to be
corpus-attested carries its real `entity_id`/`kind` (`provenance`:
`corpus_attested_one_side` | `fully_synthetic`). Byte-identical same-case
pairs (e.g. `Mercury`/`Mercury` as planet vs. software — the hardest
documented gap) get their own `corpus_attested_same_surface_ambiguous`
provenance rather than "both sides attested": a name lookup necessarily
returns the *same* single node for both sides when the surface string is
identical, so FRE-843 must not score these by comparing `entity_id_a` to
`entity_id_b` (trivially equal by construction) — each pair's
`scoring_note` says so explicitly and points to the alternative fixture
FRE-843 needs (two separate ingest episodes independently asserting each
sense).

### AC-4 abstract-cue gold (`ac4_cues.py` → `frozen/ac4_abstract_cue_gold.json`)

```
uv run python -m scripts.study.eval_artifacts.ac4_cues            # dry run — counts only
uv run python -m scripts.study.eval_artifacts.ac4_cues --execute  # writes the intermediate
                                                                    # candidate-pool dump
```

35 abstract cues (module constant `ABSTRACT_CUES`) span 7 domains
confirmed present in the live snapshot (health, software/infra
engineering, history & archaeology, cybersecurity, cooking, music,
travel) — well above AC-4's ≥30 cues / ≥4 domains bar. Cues are abstract
topic labels only ("health issues", "cryptography and encryption"), never
a precise-fact query (AC-6's honesty guard).

**Candidate-pool generation is two independent sources, not pure
embedding-cosine kNN.** A pool built only from embedding similarity to the
cue text would systematically exclude exactly the category-relevant,
embedding-distant items the study's categorical-entry recall exists to
surface — pre-biasing the frozen gold set toward what production's
embedding-style recall already finds, and pre-deciding the study's own
falsifiable question (AC-4/D8) before it is asked (a plan-review finding
from `codex:rescue`, applied here). Source A is embedding cosine top-25
(`build_embedding_candidates`, reusing
`personal_agent.memory.embeddings.generate_embedding`/`cosine_similarity`
against the live managed embedder); Source B is a per-cue keyword list
substring-matched against every `Entity` name, independent of embedding
distance (`build_keyword_candidates`). The merged pool (`ac4_cues.py
--execute` → `frozen/ac4_candidate_pools.json`, an intermediate,
pre-annotation dump — not itself the frozen artifact) tags every candidate
`pool_source: embedding | keyword | both` for auditability. A live run
surfaced real cross-domain ambiguity worth calling out: the "health
issues" cue's pool is heavily populated by DevOps "system health
check"/"agent health" entities sharing the word "health" with the medical
sense — exactly the discrimination the annotation pass exists to make.

**Annotation is two independent Claude-Code `Agent`-tool dispatches — not
a call this script makes.** Neither the candidate-pool code nor
`build_ac4_artifact` can invoke the `Agent` tool (it's a build-session
tool, not a Python API); the build session dispatches one Agent per
domain per annotator pass (14 dispatches total: 7 domains × 2 annotators),
each given only the cue text, domain, and a *shuffled* candidate
name/kind list (no `pool_source`, no similarity/keyword rank) — blind to
each other's labels and to any recall system's output by construction
(the candidate pool is pre-computed by the two neutral sources above,
never run through production-multipath or the study's categorizer).
Disagreements between the two passes are adjudicated by the build session
with a recorded rationale (the "second adjudicating disagreements" role
ADR AC-4 names). `build_ac4_artifact` (pure Python, no LLM) then assembles
the frozen artifact from the fully-annotated results, keeping the full
audit trail per cue: the keyword list, both annotators' raw per-candidate
labels, the disagreement list, and every adjudication rationale — not just
the final gold/distractor split.

**Scoring contract for FRE-843**: `gold_neighborhood`/`distractors` are
`Entity._export_source_element_id` values. Scoring the production-multipath
baseline (arm A) against this gold set is a direct id comparison; scoring
the study's categorical-entry recall (arm C, which returns `Concept`
nodes) requires first mapping each returned `Concept` back to its backing
`Entity` id(s) via the `Surface`/`ALIAS_OF` chain established at ingest —
comparing `Concept.id` directly against these entity_ids would silently
fail to credit arm C for correct recalls. Both artifacts' `scoring_note`
field states this explicitly.

**Status (2026-07-11): the live annotation run has completed.**
`frozen/ac4_abstract_cue_gold.json` records 35 cues / 618 gold entries /
642 distractor entries across the 7 domains (1260 total judgments). The
two annotator passes agreed on 1187/1260 items (94.2%); the 73
disagreements were adjudicated individually and are recorded per-cue with
a rationale (`adjudications`). A few adjudication patterns worth noting:
a bare word can carry a different sense depending on which corpus cluster
it's embedded in (`"Baroque"` clustered with Venice-architecture entities
→ distractor for the music-style cue, even though the string alone reads
musical; a `kind=Organization` entity named `"Renaissance"` → distractor
for the same reason) — the entity's neighboring context and `kind` field,
not just its surface name, drove several rulings. One systematic pattern:
14 of the 30 "regional cuisines" disagreements were annotator 2 accepting
specific dishes (`"Bouillabaisse"`, `"Couscous"`, ...) as evidence of a
cuisine, where annotator 1 held a stricter "must be a cuisine label, not
a dish" line — adjudicated toward annotator 1's reading for all 14, since
specific dishes are already the concern of the separate "cooking
techniques and recipes" / "seafood dishes" cues.
