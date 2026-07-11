# FRE-841 — ADR-0114 pre-registered eval artifacts (AC-2 hard negatives + AC-4 abstract-cue gold)

**Ticket:** FRE-841 (Approved) · **ADR:** ADR-0114 (D5/D7/D8, AC-2 + AC-4) · **Depends on:** FRE-838 (Done, frozen corpus live), FRE-839 (Done, evidence-layer schema)

## Scope

Build the two **frozen, pre-registered** eval artifacts AC-2 and AC-4 will be scored against
(scoring/pass-rules are FRE-843's job, not this ticket's). Both artifacts are committed JSON,
timestamped and content-hashed, generated from the real frozen study-sandbox corpus
(`bolt://localhost:7691`, 7925 `Entity` nodes / 10,290+34,301 total per FRE-838's manifest) —
**not** synthetic fixtures wholesale, so the eval reflects the real corpus ADR-0114 studies.

1. **AC-2 hard negatives**: V⁺ (case/near-variant surface pairs that SHOULD resolve to one hub)
   + V⁻ (homonym/polyseme pairs that must NOT merge).
2. **AC-4 abstract-cue gold**: ≥30 abstract cues spanning ≥4 real snapshot domains, each with a
   frozen gold neighborhood (+ distractors), annotated blind to any recall system's output.

## Investigation findings (grounding — already queried against the live sandbox)

- The study sandbox (`STUDY_NEO4J_PASSWORD` from primary `/opt/seshat/.env`; this worktree has no
  `.env`) is up and populated: 7925 `Entity` nodes (10 ADR-0109 kinds), 6178 carry a 1024-dim
  `embedding`. FRE-839's partial ingest run (`--limit`) has also run: 356 `Concept`/625
  `Category`/18 `Episode` — too sparse to ground AC-4 on; AC-4 gold is grounded on the full,
  frozen `Entity` corpus instead (neutral common ground for both arm A and arm C — FRE-843's
  scoring concern is mapping each system's output back to `Entity` identity, not this ticket's).
- **V⁺ is real and large**: a `toLower(trim(name))` grouping over `Entity` finds **542 real
  case-variant groups** already in the corpus — including the ADR's own named bug
  (`Arterial calcification` / `Arterial Calcification`, tagged `Phenomenon`/`MethodOrConcept`
  respectively — literally the type-scatter ADR-0114 exists to fix). This becomes V⁺ directly
  (pairwise combinations within each group), not a hand-built list.
- **V⁻ cannot be mined from the corpus** — checked known homonym-prone surface forms
  (`python`, `apple`, `mercury`, `java`, `turkey`, `amazon`, `mars`, ...) and every one maps to
  exactly **one** sense/kind in the real data (e.g. `Turkey`→`Location`, `Amazon`→`Organization`,
  `Mercury`→`DomainOrTopic`). The corpus has zero naturally-occurring homonym collisions at this
  scale — matches `writer.py`'s documented gap note almost verbatim ("two byte-identical strings
  referring to genuinely different things... left to FRE-841/843's fuller hard-negative test").
  V⁻ must therefore be a **seeded/injected adversarial set** — same posture as the ADR's own
  named examples ("Python the language vs python the animal" — illustrative, not
  corpus-attested) — each pair grounded in a real corpus-attested sense on one side where
  possible (`Python`/`Turkey`/`Amazon`/`Mars` are real Entities today) paired with a synthetic
  injected second sense, clearly labeled by provenance so scoring never confuses the two.
- Real snapshot domains present (sampled `DomainOrTopic`/`Entity` names): **health**
  (`Health Status Reporting`, `Respiratory infection`, `General practitioner`), **software /
  infra engineering** (`FastAPI`, `Knowledge Graph Writes`, `Approval Token via Redis Event
  Bus`), **history & archaeology** (`Mycenaean Greeks`, `Early Minoan`, `Yamnaya Culture`),
  **cybersecurity** (`CEH`, `Cryptography`), **cooking** (`Red Beans`, `Onion`, `Frutti di
  mare`), **music** (`Baroque polyphony`), **travel** (`Tokyo`, `San Giorgio Maggiore Basilica`,
  `Archaeological Museum of Chania`) — 7 domains, well above the ≥4 bar.
- `personal_agent.memory.embeddings.generate_embedding` + `cosine_similarity` already exist and
  are the right reuse point for cue embedding — no new embedder call site needed.

## Design (revised post-codex-review — see "Codex plan-review findings" below)

### AC-2 — `scripts/study/eval_artifacts/ac2_pairs.py`

- `mine_case_variant_groups(driver) -> list[CaseVariantGroup]`: the `toLower(trim(name))`
  grouping Cypher query above; returns `(normalized_name, [(name, entity_type), ...])` for every
  group with >1 distinct raw name, `provenance="corpus_case_variant"`. Real, deterministic, no LLM.
- `mine_near_variant_groups(driver) -> list[CaseVariantGroup]`: a **second**, looser grouping —
  strip all non-alphanumeric characters (hyphens, punctuation, parens) in addition to
  case-folding before grouping (e.g. `"95th Percentile (P95)"` / `"95th percentile (P95)"` /
  a hyphen/space variant) — `provenance="corpus_near_variant"`. Catches ADR AC-2's "near-variant"
  language, which pure case-folding does not (codex finding #3).
- `expand_to_pairs(groups) -> list[PositivePair]`: every group → all pairwise combinations
  (`itertools.combinations`), each pair carries the two raw names + their (possibly differing)
  `entity_type`s + each entity's stable `entity_id` (`_export_source_element_id`) + `provenance`.
- `SEEDED_HARD_NEGATIVE_PAIRS: tuple[NegativePair, ...]`: a fixed, hand-authored constant — the
  ADR's 2 named pairs (`Python`/`python`, `Apple`/`apple`; `Mercury` gets a planet/software pair
  since Mercury already exists as `DomainOrTopic` in the corpus) plus ~10 more spanning the
  corpus's real domains (`Turkey` country/bird, `Amazon` company/river, `Mars` planet/candy,
  `Java` island/language, `Bass` fish/music-instrument, `Crane` bird/machine, `Saturn`
  planet/car-brand, `Venus` planet/goddess) — each entry now carries: `surface_a, kind_a,
  sense_a` (short gloss, e.g. "the planet"), `entity_id_a` (the real corpus entity id when
  `provenance="corpus_attested_one_side"`, else `null`), and the mirrored `_b` fields, plus
  `provenance` (`"corpus_attested_one_side"` | `"fully_synthetic"`) and a `scoring_note`
  string telling FRE-843 how to instantiate the synthetic side (e.g. "create a fresh Concept
  with kind=Phenomenon, canonical_name='python', no corpus entity_id — synthetic fixture only")
  — codex finding #4: synthetic pairs need enough identity/provenance for FRE-843 to score
  without ambiguity.
- `build_ac2_artifact(driver) -> dict`: assembles `{positive_pairs, negative_pairs,
  generated_at, source_manifest_hash}` (the last field cross-references
  `scripts/study/snapshots/snapshot_manifest.json`'s `content_hash`, so the artifact is
  traceable to the exact frozen corpus it was mined from).
- CLI: `uv run python -m scripts.study.eval_artifacts.ac2_pairs [--execute]` — dry run prints
  counts only (mirrors `export_snapshot.py`'s safety convention); `--execute` writes
  `scripts/study/eval_artifacts/frozen/ac2_hard_negative_pairs.json` (committed, not gitignored
  — this IS the frozen artifact, unlike the raw-content-free snapshot manifest).

### AC-4 — `scripts/study/eval_artifacts/ac4_cues.py`

- `ABSTRACT_CUES: tuple[AbstractCue, ...]` — a fixed, hand-authored constant: ≥30 cues, each
  `(cue_text, domain)`, spanning the 7 confirmed domains (health, software/infra, history &
  archaeology, cybersecurity, cooking, music, travel) — abstract phrasing only ("health
  issues", "database performance problems", "ancient Mediterranean civilizations"), never a
  precise-fact query (AC-6's honesty guard: this ticket must not smuggle precision-cue phrasing
  into what's pre-registered as the abstract-cue set).
- `fetch_embedded_entities(driver) -> list[EmbeddedEntity]`: one Cypher pull of every `Entity`
  with a non-null `embedding` (`_export_source_element_id`, `name`, `entity_type`, `embedding`)
  — the frozen candidate universe. Also pulls every `Entity` regardless of embedding (name +
  `entity_type` + `entity_id` only) for the keyword-match source below.
- **Candidate pool is multi-source, not pure cosine-kNN** (codex finding #1 — the single most
  important fix from plan-review: a pool built *only* from embedding similarity to the cue text
  would systematically exclude exactly the category-relevant-but-lexically/embedding-distant
  items arm C's categorical entry exists to surface, per ADR D7/D8's "abstract-query recall
  miss" framing — biasing the frozen gold set toward what embedding similarity already finds
  and pre-deciding the study's own falsifiable question before it's asked). Two independent,
  differently-biased sources are merged and deduped by `entity_id`, each candidate tagged with
  which source(s) surfaced it:
  - **Source A — embedding cosine top-K** (`build_embedding_candidates(cue_embedding, entities,
    top_k=25)`): brute-force cosine similarity (`personal_agent.memory.embeddings.
    cosine_similarity`) in Python, no Neo4j vector index needed for a one-time offline build.
  - **Source B — per-cue keyword/domain match** (`build_keyword_candidates(cue, entities,
    max_candidates=20)`): a short, hand-authored keyword list per cue (e.g. cue "health issues"
    → `["health", "medical", "clinical", "disease", "infection", "diagnos", "calcification",
    "hypertension", "physician"]`), substring-matched (case-insensitive) against every `Entity`
    name in the full corpus — independent of embedding distance, so it can surface plausible
    category members an embedding-only pass would miss.
  - Merged pool per cue: typically 30-45 candidates (some overlap between sources) — each
    annotated with `pool_source: "embedding" | "keyword" | "both"` in the frozen artifact for
    auditability.
- Two-pass blind annotation, via the `Agent` tool (Claude Code subagent dispatch — **not**
  `personal_agent`'s LLM client/cost-gate; this never touches the deployed gateway or its
  budget, so it is a genuine methodology choice, not a cost loophole — documented as such in
  the frozen artifact and README per codex finding #5, not dismissed as "free"):
  - **Annotator 1**: one Agent dispatch given ALL `(cue, candidate_name, candidate_kind,
    candidate_entity_id)` tuples (candidates shuffled — no similarity/keyword-rank or
    `pool_source` signal shown), asked to label each `gold` / `distractor` with a one-line
    rationale. Blind to any recall system's output by construction (candidates are pre-computed
    by the two neutral sources above, never run through arm A or C).
  - **Annotator 2**: a second, independent Agent dispatch, same cues/candidates (re-shuffled),
    no visibility into annotator 1's labels.
  - **Adjudication**: this build session inspects every disagreement directly (real corpus
    entity names/kinds are inspectable, cheap) and records the final label + a one-line
    rationale — the "second adjudicating disagreements" role the AC names.
  - **Auditability (codex finding #5)**: the frozen artifact records, per cue: the exact
    keyword list used, both annotators' raw per-candidate labels, the list of disagreements,
    and the adjudication rationale for each — not just the final gold/distractor split.
- `build_ac4_artifact(cues_with_gold) -> dict`: `{cues: [{cue_text, domain, keywords,
  candidate_pool: [{entity_id, name, kind, pool_source}], annotator_1_labels, annotator_2_labels,
  disagreements, gold_neighborhood: [entity_id, ...], distractors: [entity_id, ...]}],
  generated_at, source_manifest_hash, annotation_method, scoring_note}` — `scoring_note`
  documents for FRE-843 that gold/distractor entries are `Entity._export_source_element_id`
  values, and that scoring arm C's `Concept`-shaped output requires mapping each returned
  `Concept` back to its backing `Entity` id(s) via `Surface`/`ALIAS_OF`/original ingest linkage
  before comparing against this gold set (codex finding #2).
- CLI: `uv run python -m scripts.study.eval_artifacts.ac4_cues [--execute]` — dry run computes
  candidate pools and prints counts only (no Agent dispatch, no write); the real annotation run
  (the Agent dispatches + adjudication) happens once, interactively, in this build session — the
  frozen output is committed directly as a static JSON file (not regenerated by re-running the
  script, since the annotation pass is a one-time human/agent judgment call, not a deterministic
  function of the corpus). The script's `--execute` path documents/reproduces the *candidate
  pool* generation for auditability; the committed JSON also embeds the raw candidate pool per
  cue (gold + distractor + rejected-by-both), so a reviewer can see exactly what was judged.

### Shared — `scripts/study/eval_artifacts/freeze.py`

- `freeze_json_artifact(payload: dict, path: Path, *, generated_at: datetime) -> dict`: stamps
  `generated_at` (ISO, UTC) + `content_hash` (sha256 over the canonicalized payload, excluding
  the hash field itself) onto *payload*, writes it pretty-printed+sorted-keys to *path*, creating
  parent dirs. Mirrors `export_snapshot.py`'s `build_manifest`/`compute_content_hash` shape so
  the two frozen artifacts and the corpus manifest are hashed the same way.

## Files

- `scripts/study/eval_artifacts/__init__.py` (new)
- `scripts/study/eval_artifacts/freeze.py` (new)
- `scripts/study/eval_artifacts/ac2_pairs.py` (new)
- `scripts/study/eval_artifacts/ac4_cues.py` (new)
- `scripts/study/eval_artifacts/frozen/ac2_hard_negative_pairs.json` (new, committed — real data)
- `scripts/study/eval_artifacts/frozen/ac4_abstract_cue_gold.json` (new, committed — real data)
- `tests/scripts/study/eval_artifacts/__init__.py` (new)
- `tests/scripts/study/eval_artifacts/test_freeze.py` (new)
- `tests/scripts/study/eval_artifacts/test_ac2_pairs.py` (new)
- `tests/scripts/study/eval_artifacts/test_ac4_cues.py` (new)
- `scripts/study/README.md` (update — document the new artifacts + how they were built)

## Steps (atomic)

1. `freeze.py` + `test_freeze.py` — hash/timestamp helper. Verify:
   `uv run python -m pytest tests/scripts/study/eval_artifacts/test_freeze.py -q`
2. `ac2_pairs.py` (mining + seeded negatives + artifact builder) + `test_ac2_pairs.py` (fake
   driver, mirrors `test_writer.py`'s `_ScriptedSession`/`_FakeResult` pattern). Verify:
   `uv run python -m pytest tests/scripts/study/eval_artifacts/test_ac2_pairs.py -q`
3. Run `ac2_pairs.py --execute` for real against the live sandbox (read-only Cypher, no
   sandbox writes, no LLM cost) → commit
   `scripts/study/eval_artifacts/frozen/ac2_hard_negative_pairs.json`.
4. `ac4_cues.py` (cue constants + per-cue keyword lists + two-source candidate pool + artifact
   builder, annotation-pass wiring left as a pluggable callback so unit tests never invoke the
   real `Agent` tool) + `test_ac4_cues.py` (fake driver + fake embeddings + fake keyword source +
   fake annotation callback — covers both sources contributing distinct candidates and the dedup
   logic). Verify: `uv run python -m pytest tests/scripts/study/eval_artifacts/test_ac4_cues.py -q`
5. Run the real candidate-pool build against the live sandbox (one real embedding call per cue
   via `generate_embedding` — cheap, existing infra, not a new cost line — plus the keyword-match
   pass, both read-only) + the two Agent annotation dispatches + adjudicate disagreements
   in-session → commit `scripts/study/eval_artifacts/frozen/ac4_abstract_cue_gold.json`.
6. Update `scripts/study/README.md` with a new "Pre-registered eval artifacts (FRE-841)" section
   documenting both artifacts, their provenance, and the annotation methodology.
7. Quality gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`.

## Acceptance-criteria proof this ticket carries

Per the ticket description, this ticket proves the **construction** half only (scoring/pass
rules are FRE-843):
- AC-2's artifact requirement: V⁺∪V⁻ built, frozen, committed *before* any resolver scoring
  exists in this repo (FRE-843 hasn't been built yet — trivially satisfied, but also true in
  spirit: this ticket doesn't run `resolve_concept_hub` against V⁺/V⁻ at all).
- AC-4's artifact requirement: ≥30 cues, ≥4 domains, gold neighborhoods + distractors, frozen and
  committed before any AC-4 scoring exists.

## Codex plan-review findings (applied)

Ranked findings from `codex:rescue` plan review, and how each was resolved in the design above:

1. **Candidate-pool neutrality (most important)** — pure embedding-cosine top-K would
   systematically exclude category-relevant, embedding-distant items arm C is meant to recover,
   pre-biasing the gold set toward what production's embedding-style recall already finds.
   **Fixed**: candidate pool is now two independent sources (embedding cosine + per-cue keyword
   match), merged and tagged by `pool_source`.
2. **Entity/Concept mapping contract** — AC-4 gold grounded in raw `Entity` identity is correct,
   but FRE-843 needs an explicit contract for mapping arm C's `Concept`-shaped output back to
   `Entity` ids. **Fixed**: every candidate/gold/distractor entry carries `entity_id`
   (`_export_source_element_id`); the frozen artifact's `scoring_note` documents the required
   `Concept`→`Entity` mapping path via `Surface`/`ALIAS_OF`.
3. **AC-2 "near-variant" not just case-fold** — original design only case-folded.
   **Fixed**: added `mine_near_variant_groups` (punctuation/whitespace-normalized grouping) as a
   second V⁺ source alongside `mine_case_variant_groups`.
4. **Seeded V⁻ pairs need stable identity/instantiation info for scoring**. **Fixed**: every
   seeded pair now carries `sense_a`/`sense_b` glosses, `entity_id_a`/`entity_id_b` (null for the
   synthetic side), and a `scoring_note` telling FRE-843 how to instantiate the synthetic sense.
5. **Annotation auditability** — the blind two-annotator + adjudication process should be
   inspectable, not just its final output. **Fixed**: the frozen AC-4 artifact now records the
   keyword list, both annotators' raw labels, the disagreement list, and adjudication rationale
   per cue, not only the final gold/distractor split. Also: the Agent-tool annotation choice is
   documented as a methodology decision (blind, independent, no personal_agent budget/cost-gate
   involvement), not waved off as merely "free."

## Risk tier

**Standard** — new evaluation methodology grounded in real corpus data, schema-adjacent
(re-reads the FRE-838/839 schema), no `src/` product-path change. Codex plan-review required
before implementation per the build skill (touches memory/eval substrate, multi-file).
