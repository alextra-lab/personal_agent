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
