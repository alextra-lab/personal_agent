# FRE-773 — ADR-0109 V2 relationship half: validate the tightened relationship vocabulary before any write-path adoption

**Ticket:** FRE-773 (Approved, stream:build1, Tier-1:Opus, parent FRE-630, project Memory Recall Quality)
**Backing ADR:** ADR-0109 (Accepted 2026-07-03), § "V2 — relationship types (candidate; not yet validated to the entity bar)" (lines 77–96), and the honesty flag: *"Do not ship the relationship half on the entity half's evidence."*
**Template:** FRE-770 (entity-half gold re-label + measured IAA, merged #358) — this ticket is its relationship-axis mirror and reuses its machinery (`iaa.py` verbatim; `relabel_v2_types.py` as the driver pattern; the dual-field gold-schema discipline).

## What this ticket is (and is not)

FRE-773 is **the missing measurement, not a build-and-ship** (ticket's own words). Two deliverables:

1. **Author the V2 relationship-type definitions** — recast the 6 inherited V1 rel types into GoLLIE-style definitions (inclusion + **exclusion** + direction + example), with `RELATED_TO` recast as a gated last-resort None-of-the-Above (NoTA) fallback and an explicit *emit-nothing-if-none-fits* rule (ADR § V2 relationship types, (a)/(b)/(c)).
2. **Measure cross-model agreement + re-label the relationship gold** — run the tightened definitions through the same blind 3-model-rater IAA pipeline FRE-770 used, over the gold's relationship triples; adjudicate disagreements; dual-field the gold with a `v2_rel_type` (V1 `rel_type` stays scored).

**NOT in scope:**
- **Not** a prompt swap. The live extractor (`entity_extraction.py:74–80`) keeps emitting V1 rel types. (The relationship prompt swap is a future ticket, the rel-axis analogue of FRE-771; not created here.)
- **Not** the write-path gate — FRE-760 (relationship-type validation gate) *consumes* this measurement; this ticket's output is the evidence FRE-760 needs before it can adopt V2. FRE-773 **blocks** FRE-760.
- **Not** a KG migration of existing edges.

## The V1 → V2 relationship definitions (the authoring deliverable)

**V1 (inherited, terse, un-derived — `entity_extraction.py:74–80`):**
`PART_OF` "component or subset of another" · `USES` "uses or depends on another" · `RELATED_TO` "general semantic relationship" · `SIMILAR_TO` "comparable or equivalent" · `CREATED_BY` "created or authored by another" · `LOCATED_IN` "geographically within another".

**V2 (this ticket — same 6 keys, GoLLIE-style, directional; `RELATED_TO` gated):** the exact text is authored in the driver's `V2_REL_DEFINITIONS` dict and copied into the research doc. Draft (to be codex-reviewed and owner-approved before coding):

| key | direction | definition (inclusion · **exclusion** · e.g.) |
|---|---|---|
| `PART_OF` | source **is part of** target | source is a structural component, member, subfield, stage, or constituent *of the whole* target — the source is literally a piece of target. **Not** a functional dependency (→ USES); **not** "source is a concept/method *studied within* the field target" — topical containment of an idea inside a subject area is **not** structural membership and is either `RELATED_TO` or no edge (so `Nash Equilibrium`/`Game Theory`, `Redshift`/`Cosmology`, `Penetration Testing`/`Cybersecurity` are *topical*, not `PART_OF`). *e.g. Containment PART_OF Incident Response (a phase of the process); Interval Recognition PART_OF Ear Training.* |
| `USES` | source **depends on / invokes** target | source functionally depends on, invokes, consumes, or is built on target to operate. Ask "does source *require* target to work?" **Not** similarity (→ SIMILAR_TO); **not** part/whole (→ PART_OF); **not** a mere "is used for" association in the reverse direction (→ RELATED_TO). *e.g. FastAPI USES PostgreSQL.* |
| `CREATED_BY` | source **was created by** target | source (an artifact/work) was authored, invented, produced, or originated by target (a person or organization). **Not** use or membership. *e.g. Linux CREATED_BY Linus Torvalds.* |
| `LOCATED_IN` | source **is located in** target | source is geographically or physically situated within place target. **Not** organizational membership (→ PART_OF). *e.g. Alhambra LOCATED_IN Granada.* |
| `SIMILAR_TO` | **symmetric** (direction not meaningful) | source and target are comparable, analogous, or near-equivalent alternatives at the same level of abstraction. **Not** one depending on the other (→ USES); **not** part/whole (→ PART_OF). *e.g. PostgreSQL SIMILAR_TO MySQL.* |
| `RELATED_TO` | last-resort NoTA | **Gated fallback.** Emit ONLY when the two entities are clearly associated but **no specific type above applies** — never when a specific type fits. If the association is weak/topical and no directional type holds, use `RELATED_TO`. *e.g. Cosmic Microwave Background RELATED_TO Big Bang (evidence-of association, not part/use/creation).* |

**Emit-nothing-if-none-fits rule:** if no relationship type (including `RELATED_TO`'s weak-association bar) genuinely holds between the pair, emit **no** edge.

**Expected V1-gold disagreements the tightened defs create** (the measurement's whole point): the ADR names `Trie —USES→ Prefix Search` (gold line 412) flipping to `RELATED_TO` — under directional `USES`, a trie does not *depend on* prefix search (if anything the reverse), so the honest label is the NoTA `RELATED_TO`. Cases like this are what the rater pass surfaces and adjudicates.

## Design decision: dual-field the gold, don't replace (mirrors FRE-770)

Add optional `v2_rel_type` (+ adjudication metadata) to `GoldRelationship`, keep V1 `rel_type` untouched and scored. This keeps `make test` and the live V1 harness green throughout, exactly as FRE-770 did for entities.

- `gold.py`:
  - New `ALLOWED_REL_TYPES_V2 = frozenset({PART_OF, USES, RELATED_TO, SIMILAR_TO, CREATED_BY, LOCATED_IN})` (same 6 keys — V2 keeps the set; only the definitions/gating change). A named `REL_TYPE_NOTA = "RELATED_TO"` constant documents the gated-fallback role.
  - **A distinct `REL_V2_NO_EDGE = "NONE"` sentinel** (codex-required, Q2/Q5): a machine-checkable marker meaning *the V2 vocab says no edge should exist between this pair* — deliberately **not** a member of `ALLOWED_REL_TYPES_V2`, so it can never be silently read as a real relationship type. It is the honest label for a gold triple the raters converge on `NONE`, and it always co-carries `v2_needs_owner_signoff=True` (the V1 `rel_type` is retained; pruning the gold edge is out of scope). `v2_rel_type` is valid iff it is in `ALLOWED_REL_TYPES_V2` **or** equals `REL_V2_NO_EDGE`.
  - `GoldRelationship` gains: `v2_rel_type: str = ""`, `v2_adjudicated: bool = False`, `v2_adjudication_rationale: str = ""`, `v2_needs_owner_signoff: bool = False` (parallels `GoldEntity`'s FRE-770 fields exactly).
  - `_parse_relationship` validates `v2_rel_type` against `ALLOWED_REL_TYPES_V2 ∪ {REL_V2_NO_EDGE}` **only when non-empty** (tolerant during incremental authoring; the committed final gold has 100% coverage, enforced by a new test).
  - Bump `GOLD_SCHEMA_VERSION` `"1.1"` → `"1.2"` (shape changed, per the module's own bump discipline).
  - Extend `all_authored_strings` to include `v2_adjudication_rationale` for the PII scan (it now contains authored text).

## Method — blind 3-rater relationship classification + measured IAA

New driver `scripts/eval/fre630_extraction_quality/relabel_v2_rels.py` — the relationship mirror of `relabel_v2_types.py`:

- **Item** = one gold relationship triple: `(case_id, source, v1_rel_type_hidden, target, context)`. The rater sees the ordered pair **source → target**, the owning case's `source_user`+`source_assistant` context, and the 6 V2 rel definitions verbatim. **Blind:** no V1 label, no other rater's answer, no rater identity.
- **Raters:** the same 3 as FRE-770 — `gpt-5.4-mini` (temp 0), `gpt-5.4` (temp 0), `claude-sonnet-5` (adaptive) — two provider families. Reuse the `Rater`/`RATERS` shape. (Same honesty caveat as FRE-770: "3 model raters across 2 provider families," not "3 independent annotators"; the per-rater-pair agreement table keeps OpenAI-internal vs cross-family agreement legible.)
- **Prompt** asks for exactly one of the 6 keys + a one-line rationale, JSON. Because the ADR adds an emit-nothing rule, the rater is also allowed to answer `"NONE"` (no edge should exist) — captured as a **distinct outcome, never coerced into a type**. `NONE` is not in `ALLOWED_REL_TYPES_V2`; it is a first-class rater outcome the report counts separately, and it participates in the IAA category set as its own category (so raters splitting between a type and `NONE` registers as a genuine disagreement, not a hidden coercion).
- **IAA:** reuse `iaa.py` verbatim (`build_iaa_report`), categories = the 6 V2 rel keys + `NONE`. Overall Fleiss' kappa + per-type one-vs-rest + rater-pair table + disagreements. Directionality is baked into the item (source→target is fixed), so a rater disagreeing on *direction* shows up as a type disagreement, which is what we want to measure.
- **Honest read of statistical power (codex-required, Q4):** at ~21 + ≥4 triples the **overall** cross-model kappa is the headline figure the ticket's "measured" AC turns on; the **per-type** one-vs-rest kappas are **sparse/diagnostic, not robust** — several types (esp. `SIMILAR_TO`, `CREATED_BY`, `LOCATED_IN`) have only 1–3 positives, so `iaa.py` will honestly report `status="undefined_zero_variance"` / low `n_positive` rather than a fake number. The research doc frames per-type rows as directional and calls out which types are too sparse to certify — it does **not** overclaim per-type agreement. This is the relationship-axis analogue of the ADR's own "n=1 directional spot-check" honesty.
- **Direct litellm, cost-gate-bypassed** — identical deliberate, called-out exception as FRE-770 (offline research classification, no production extraction, no KG write, keys via `settings.*_api_key` never `os.getenv`). Raw per-item/per-rater records → gitignored `telemetry/evaluation/fre630-extraction-quality/v2-rel-relabel-<run-id>.json`. `--dry-run` (stub raters), `--limit N`, `--run-id`. `prompt_hash()` pins the definition revision used.
- **Cost:** ~21 existing + ~6–8 new triples × 3 raters × one short call ≈ 80–90 calls of a few hundred tokens — well under $0.50. **Unbudgeted, cost-gate-invisible spend → explicit owner approval required before the real (non-dry-run) run**, same gate FRE-770 honored.

## Adjudication (mirrors FRE-770)

- **Unanimous on a type (3/3):** `v2_rel_type` = agreed label, `v2_adjudicated=False`.
- **Majority on a type (2/3):** builder rules against the ADR's inclusion/exclusion text for the specific pair+context; `v2_adjudicated=True` + one-line rationale; `v2_needs_owner_signoff=False`.
- **3-way type split:** builder assigns a concrete `v2_rel_type` (never empty), `v2_adjudicated=True` + rationale, **and** `v2_needs_owner_signoff=True`, named individually in the final ticket comment.
- **Raters converge on / majority `NONE` (codex-required — no silent coercion, Q2/Q5):** `v2_rel_type = REL_V2_NO_EDGE` (**not** a concrete type), `v2_adjudicated=True` + rationale "V2 vocab says no edge; V1 gold asserts one", `v2_needs_owner_signoff=True`. This surfaces "V2 says this edge shouldn't exist" as a **prominent, machine-checkable validation finding requiring an owner decision** — never a clean-looking but possibly-false relationship label. The V1 `rel_type` is retained (pruning the gold edge is out of scope — this ticket measures, it does not prune). A `NONE`-vs-type split with no majority is treated as a 3-way split *and* flagged `NONE`-adjacent in the ticket comment.

## Coverage growth (modest — parallel to FRE-770, targeted at the two named faults)

The ADR names exactly two design faults: (1) `RELATED_TO` catch-all overlap, (2) `USES` non-directionality. The gold has 21 triples but **zero `SIMILAR_TO`** examples and few directional-`USES`/NoTA boundary pairs. Add a small set of new cases (best-effort V1 `rel_type` so they load/score under the live harness), each through the same rater+adjudication pass:

1. A **`USES` direction** pair where reversing it is wrong (e.g. an app `USES` a datastore) — anchors the directional definition.
2. A **`SIMILAR_TO`** pair (currently 0 in gold) — e.g. two comparable databases or two comparable algorithms.
3. Two **`USES` ↔ `RELATED_TO`** boundary pairs beyond `Trie/Prefix Search` — a "used-for" association that should land NoTA, and a genuine dependency that should stay `USES`.
4. One **`PART_OF` ↔ `RELATED_TO`** boundary pair.

Exact new cases finalized during implementation; ≥4 new relationship triples total, all in owner domains already present in the gold (physics/CS/cooking/security), no new PII.

## Re-baseline

Adding cases changes the gold. **Outcome: deferred** — the V1 `rel_type` scoring semantics are
unchanged, `make test` does not run the extraction harness, and a full re-baseline needs a live
extraction LLM + test substrate and spend beyond the owner-authorized rater run. Recorded as a fast
follow / master deploy-time step in the research doc; it does not gate FRE-773's measurement AC.

## Tests (TDD — written first)

- `tests/evaluation/test_fre630_gold_set.py` (extend):
  - `test_all_relationships_have_v2_rel_type` — every gold relationship has `v2_rel_type` set and in `ALLOWED_REL_TYPES_V2 ∪ {REL_V2_NO_EDGE}`.
  - `test_rel_no_unresolved_signoff_without_rationale` — every `v2_needs_owner_signoff=True` relationship carries a non-empty `v2_adjudication_rationale`.
  - `test_no_edge_marker_requires_signoff` — every relationship with `v2_rel_type == REL_V2_NO_EDGE` also has `v2_needs_owner_signoff=True` and a rationale (the codex "no silent coercion" contract, made machine-checkable).
  - `test_similar_to_coverage` — ≥1 relationship with `v2_rel_type == "SIMILAR_TO"` (was 0 in V1 gold).
  - bump `MIN_CASES` if the new case count requires it.
- `tests/evaluation/test_relabel_v2_rels.py` (new, small): `_parse_rater_response` tolerant-parse (valid JSON, off-vocab type → error, `NONE` outcome captured), `collect_rel_items` flattens every gold triple, `_dry_run_response` schema-valid, `prompt_hash` stable. No real API calls.
- `gold.py` loader: a unit test asserting `v2_rel_type` off-vocab raises `GoldSetError` only when non-empty (tolerant-while-authoring contract).
- `iaa.py` is reused unchanged — its FRE-770 tests already cover it; if I touch it at all, extend `test_iaa.py`.

## Deliverable: research doc

`docs/research/2026-07-04-fre-773-relationship-v2-validation.md` — the V1→V2 definitions table (verbatim from the driver), the per-type IAA/kappa table + rater-pair table, the adjudication table (every majority/split case + rationale + the `Trie/Prefix Search` flip), the `NONE` outcomes flagged for owner, the new cases added, the re-baseline numbers, residual open questions.

## Acceptance-criteria proof plan

| AC (FRE-773 ticket) | Proof |
|---|---|
| Tightened V2 relationship definitions authored (RELATED_TO as gated NoTA; every type directional w/ inclusion+exclusion; emit-nothing rule) | `V2_REL_DEFINITIONS` in the driver + the definitions table in the research doc; codex-reviewed |
| Relationship-V2 cross-model agreement **measured** | `iaa.py` per-type `KappaResult` + rater-pair table over the gold triples, in the research doc (real 3-rater run) |
| Relationship gold **re-labeled** (before FRE-760 adopts the vocab) | `v2_rel_type` populated on every gold relationship via the rater+adjudication pipeline; `test_all_relationships_have_v2_rel_type` |
| Disagreements adjudicated | every majority/split gets `v2_adjudicated=True`+rationale; 3-way/`NONE` flagged `v2_needs_owner_signoff` + named in ticket comment |
| Coverage of the two named faults grown | ≥4 new triples incl. a `SIMILAR_TO` and `USES↔RELATED_TO`/direction boundaries; `test_similar_to_coverage` |
| V1 harness untouched / green | V1 `rel_type` unchanged, `GOLD_SCHEMA_VERSION` bumped, re-baseline recorded |

## Owner-decision gates (surfaced, not assumed)

1. **Plan approval before coding** (build skill Step 3, Standard/Complex → codex-reviewed first).
2. **Real API-spend approval** before the non-dry-run rater run (~80–90 direct, cost-gate-invisible calls; < $0.50) — build & test everything on `--dry-run` first, then STOP and confirm, exactly as FRE-770 did.

## Sequence (each step verified)

1. Author V2 defs + `gold.py` schema changes → verify: `make test-file FILE=tests/evaluation/test_fre630_gold_set.py` (new tests fail first, then pass after gold populated — TDD).
2. Write `relabel_v2_rels.py` + its unit test → verify: `--dry-run` smoke prints an IAA table; `test_relabel_v2_rels.py` passes.
3. **STOP — owner approval for real spend.**
4. Real 3-rater run → adjudicate → populate `v2_rel_type` in gold → verify: `test_all_relationships_have_v2_rel_type` passes.
5. Coverage cases added + rated + adjudicated → verify: `test_similar_to_coverage` passes.
6. Re-baseline harness → curated numbers into research doc.
7. Research doc written → quality gates (`make test`, `make mypy`, `make ruff-check/format`, `pre-commit`) → PR.
