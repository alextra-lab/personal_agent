# FRE-771 — swap the extraction prompt to the 10-type GoLLIE definitions + powered A/B

**Backing:** ADR-0109 (Accepted) + Amendment 1 (FRE-782/784, merged), Implementation Notes
steps 3–4. **Depends on:** FRE-784 (merged, PR #365) — the 10-type gold + definitions are
committed. **Gates:** FRE-772 (KG migration).

## Scope (from the ticket)

1. Replace the entity-type block in the extraction prompt with the **ten** GoLLIE-style
   inclusion/exclusion/example definitions (8 accepted 2026-07-03 + KnowledgeArtifact +
   QuantityMeasure). Keep the knowledge-class block and the stance/claim contract unchanged.
2. Run the existing FRE-766 matrix harness on the re-labeled 10-type gold (now 50 cases,
   `test_all_entities_have_v2_type` passes for all), ≥3 samples/cell, comparing V2 against the
   current (V1) prompt across model families. Fold in a fresh re-baseline (FRE-770's baseline
   predates the 10-type gold growth).
3. Acceptance (ADR AC-1): cross-model type-agreement on the previously-ambiguous set ≥90%
   between two model families, no regression on hallucination/dedup/forbidden-edge/
   knowledge-class.

## Key design decisions (revised after codex plan-review — see bottom of this section)

**D1 — "swap" means the live prompt becomes V2 unconditionally.** `entity_extraction.py`'s
production default carries only the 10-type block after this ticket — no settings flag, no
permanent dual-branch (unlike FRE-759's flag-dark few-shot pattern, which was explicitly
provisional). This matches the ADR's own word ("swap"; ADR-0109:62,188) and unblocks FRE-772 (KG
migration), which needs the live extractor already emitting V2 types. **Codex-confirmed.**

**D2 (revised) — the "current" (V1) comparison arm is an eval-script monkeypatch, not a new
production parameter.** `_build_extraction_prompt` and `prompt_material_for_hash` both read
`_EXTRACTION_PROMPT_TEMPLATE` as a **module global at call time** — no code change needed in
`entity_extraction.py` beyond the literal swap. The new eval driver keeps its own **frozen
verbatim snapshot** of today's (pre-swap) template — mirroring `relabel_v2_types.py`'s existing
`V2_TYPE_DEFINITIONS` convention of freezing a point-in-time copy for a research script rather
than threading a new parameter through production code (codex finding: relocate the frozen
block into the eval script, not `entity_extraction.py`). The driver does:
```python
original = entity_extraction._EXTRACTION_PROMPT_TEMPLATE
entity_extraction._EXTRACTION_PROMPT_TEMPLATE = _V1_PROMPT_TEMPLATE_SNAPSHOT
try:
    ...  # run the V1-arm calls
finally:
    entity_extraction._EXTRACTION_PROMPT_TEMPLATE = original
```
**Concurrency constraint (codex-flagged):** the V1 and V2 arms must never run concurrently against
the shared module global — the driver runs them as two **sequential phases** (V2 arm to
completion first — the module is already V2 by default, no patch needed — then patch, run the
V1 arm to completion, then restore). Each phase is internally concurrent across the 2 model
families (matches `bench.py`'s existing per-model `asyncio.gather` pattern); only the two phases
themselves are serialized. This keeps `extract_entities_and_relationships`'s signature **completely
unchanged** — no new eval-only parameter, addressing codex's point (b).

**D3 (revised) — scoring must key off the field the live prompt actually emits, with an explicit,
non-defaulted selector.** `scoring.score_case` gets a new **required** keyword-only param
`entity_type_field: Literal["v1", "v2"]` (**no default** — codex: a silent default change to
scoring semantics is exactly the kind of thing that should be forced explicit at every call site,
not hidden behind a function default). `"v2"` resolves each gold entity's `v2_type or entity_type`
(safe fallback — existing hand-built `GoldEntity` fixtures in `tests/evaluation/test_fre630_metrics.py`
/ `test_fre630_report.py` don't set `v2_type` and must keep passing unchanged, updated to pass
`entity_type_field="v2"` explicitly). `"v1"` resolves `entity_type` only, for the comparison arm.
**Every existing call site is updated to state its choice explicitly**: `harness.py` and `bench.py`
(both now run the live, post-swap V2 extractor) pass `"v2"`; the new driver passes `"v2"` for its
V2 phase and `"v1"` for its monkeypatched phase; the 4 existing unit-test call sites pass `"v2"`.

**D4 — `cells.py`'s `_VALID_TYPES` (used by `classify_smoke`) must move to the 10-type set**
(import `ALLOWED_ENTITY_TYPES_V2` from `gold.py` instead of hand-duplicating a 7-type frozenset).
Without this, every post-swap smoke check reports `schema_violation` (the model correctly emits
e.g. `MethodOrConcept`, which isn't in the current 7-type smoke allowlist) and `bench.py` would
refuse to run any cell. Add a unit test asserting `classify_smoke` treats a V2-typed entity as
`"ok"` (codex test-gap finding).

**D2-b (codex test-gap finding) — the flag-gated FRE-759 few-shot exemplar block
(`_EXTRACTION_FEWSHOT_EXEMPLARS`, default OFF) names V1-only types ("Concept vs Topic vs
Technology") in its disambiguation exemplars.** It is dead by default, but if ever re-enabled
post-swap it would inject stale, self-contradicting guidance against the new V2 header. Since this
ticket is already touching this exact prompt file, update the exemplar text to the V2 equivalent
(MethodOrConcept vs DomainOrTopic vs TechnicalArtifact) as a small correctness-only edit (not a
redesign — the flag's own logic/default is untouched), and add a test that the block never
contains a V1-only type token.

**D5 — cross-model type-agreement is a *new* pure metric, not `entity_type_accuracy`.** AC-1 asks
whether two model families agree with *each other*, not whether either agrees with a fixed gold
label (that's what the FRE-766/770/782 spot-checks already measured). `iaa.py`'s
`pairwise_agreement_by_pair` already does exactly this over `{item_id: [label_per_rater]}` — reuse
it directly instead of writing new statistics. The "previously-ambiguous set" is the 8 cases /
11 entities already tagged `type-boundary` in `gold_extraction.yaml` (no new fixture needed —
confirmed via `grep type-boundary`).

**D6 — live-run scope: 2 model families, not the full 6-cell reasoning matrix. Codex-confirmed
acceptable** against the ADR's own literal text (not just the ticket's looser paraphrase): step 4
asks for "across model families, samples≥3" (ADR-0109:188) and AC-1's bar is "≥90% type-agreement
between two model families" (ADR-0109:224) — neither requires re-running every reasoning-effort
rung. The FRE-766 5-cell-plus-baseline matrix instead varies *reasoning effort within* GPT-5.4 — an
orthogonal axis this ticket doesn't need to re-litigate. Running the full matrix × 2 taxonomy arms
× 3 samples × 50 cases = 1800 calls (vs. 600 for a 2-family subset) adds no further evidence toward
AC-1. **Scope:** `mini-none` (current prod cell) + `sonnet5-adaptive` (frontier, distinct family) —
the exact pairing the ADR's own FRE-766 spot-check used — × `{v1, v2}` taxonomy × 3 samples × 50
cases = 600 calls, run as two sequential phases (D2). Documented as a scoped deviation from the
ticket's "existing matrix harness" phrase, justified against the ADR's own acceptance wording, in
both the PR and the final ticket comment to master.

## Files touched

| File | Change |
|---|---|
| `src/personal_agent/second_brain/entity_extraction.py` | Swap `_EXTRACTION_PROMPT_TEMPLATE`'s ENTITY TYPES block + JSON-footer `type` enum to the 10 V2 keys; fix GOOD EXAMPLES using V1-only types (GraphRAG→MethodOrConcept, Qwen3.5/Postgres/Neo4j→TechnicalArtifact); update the flag-gated FRE-759 few-shot exemplar text (D2-b) to V2 type names. **No new parameters, no frozen V1 content — production module stays clean.** |
| `scripts/eval/fre630_extraction_quality/scoring.py` | Add **required** `entity_type_field: Literal["v1","v2"]` keyword-only param to `score_case`; resolve `gold_types` accordingly |
| `scripts/eval/fre630_extraction_quality/harness.py` | Update its one `score_case(...)` call site to pass `entity_type_field="v2"` |
| `scripts/eval/fre630_extraction_quality/bench.py` | Update its one `score_case(...)` call site to pass `entity_type_field="v2"` |
| `scripts/eval/fre630_extraction_quality/cells.py` | `_VALID_TYPES` → import `ALLOWED_ENTITY_TYPES_V2` from `gold.py` (10 types), drop the hand-duplicated 7-type frozenset |
| `scripts/eval/fre630_extraction_quality/cross_model_agreement.py` (new) | Pure: collect per-entity per-model type labels for `type-boundary` cases → `iaa.pairwise_agreement_by_pair` |
| `scripts/eval/fre630_extraction_quality/fre771_powered_ab.py` (new) | Driver: two sequential phases — V2 arm (production template, unpatched) then V1 arm (module-global monkeypatch of `entity_extraction._EXTRACTION_PROMPT_TEMPLATE` to a frozen pre-swap snapshot, restored in `finally`) — each phase running `{mini-none, sonnet5-adaptive}` concurrently, samples=3, full gold set; standard metrics per arm (`entity_type_field` matching the arm) + cross-model agreement over `type-boundary` cases; writes summary JSON+MD |
| `tests/evaluation/test_fre630_metrics.py` | Update 2 existing `score_case(...)` call sites to pass `entity_type_field="v2"` explicitly (behavior unchanged — fixtures don't set `v2_type`, so the fallback to `entity_type` preserves today's asserted values); add new coverage below |
| `tests/evaluation/test_fre630_report.py` | Update 2 existing `score_case(...)` call sites to pass `entity_type_field="v2"` explicitly |
| `tests/evaluation/test_entity_extraction_taxonomy.py` (new) | Unit tests for D1 (the swap itself) — no production-code test needed for D2 since there's no new parameter; the monkeypatch is exercised by the (unit-testable) driver logic instead |
| `docs/architecture_decisions/ADR-0109-entity-taxonomy-redesign.md` | Update AC-1/AC-8 status + Status Updates once the run completes |
| `docs/research/2026-07-04-fre-771-10type-prompt-swap-powered-ab.md` (new) | Research note recording the run |

## Steps (atomic, TDD)

1. **Failing test first — prompt swap.** Write `tests/evaluation/test_entity_extraction_taxonomy.py`:
   - `test_default_prompt_uses_v2_ten_types` — `_build_extraction_prompt(...)` contains all 10 V2
     type keys as the JSON-footer enum values (not just prose tokens) and all 10 GoLLIE
     definitions' distinguishing phrases (inclusion + exclusion), and does NOT contain the 3
     V1-only keys (`"Technology"`, `"Concept"`, `"Topic"`) anywhere in the ENTITY TYPES block or
     JSON footer.
   - `test_fewshot_block_has_no_v1_only_types` — `_EXTRACTION_FEWSHOT_EXEMPLARS` (rendered
     regardless of the flag) contains none of the 3 V1-only keys (D2-b).
   Run: `make test-k K=test_entity_extraction_taxonomy` → confirm it fails.
2. **Implement the prompt swap** in `entity_extraction.py` per D1: replace the ENTITY TYPES block
   + JSON-footer `type` enum with the 10 V2 GoLLIE definitions; fix the 4 GOOD-EXAMPLES lines using
   V1-only types (GraphRAG→MethodOrConcept, Qwen3.5/Postgres/Neo4j→TechnicalArtifact); update the
   few-shot exemplar text (D2-b). Re-run step-1 tests → green.
3. **Failing test — scoring field selection.** Extend `tests/evaluation/test_fre630_metrics.py`
   with `test_score_case_entity_type_field_v1_vs_v2` — a `GoldEntity` with both `entity_type="Concept"`
   and `v2_type="MethodOrConcept"` set; an extraction emitting `"type": "MethodOrConcept"` scores
   `entity_type_accuracy == 1.0` under `entity_type_field="v2"` and `== 0.0` under `"v1"`; the
   reverse for an extraction emitting `"type": "Concept"`. Confirm it fails (param doesn't exist).
4. **Implement `entity_type_field`** (required kwarg, D3) in `scoring.py`. Update the 2 call sites
   in `tests/evaluation/test_fre630_metrics.py`, the 2 in `test_fre630_report.py`, the 1 in
   `harness.py`, and the 1 in `bench.py` to pass `entity_type_field="v2"` explicitly. Re-run the
   full existing `test_score_case_end_to_end_*` suite → still green (fixtures don't set `v2_type`,
   so the `v2_type or entity_type` fallback preserves today's asserted values).
5. **Fix `cells.py`** (D4): `_VALID_TYPES` → import `ALLOWED_ENTITY_TYPES_V2` from `gold.py`,
   drop the hand-duplicated 7-type frozenset. Add/extend a `classify_smoke` unit test asserting a
   V2-typed entity (e.g. `"type": "MethodOrConcept"`) classifies `"ok"`, not `"schema_violation"`.
6. **New pure module `cross_model_agreement.py`** — write its unit tests FIRST (hand-computed
   3-item, 2-rater agreement example, mirroring `iaa.py`'s own test style — new
   `tests/evaluation/test_fre771_cross_model_agreement.py`), then implement (thin wrapper: collect
   `{entity_key: [label_per_model]}` for `type-boundary` cases from per-model case-run results →
   `iaa.pairwise_agreement_by_pair`).
7. **Driver `fre771_powered_ab.py`** — two sequential phases per D2/D6: Phase 1 (V2, unpatched)
   runs `{mini-none, sonnet5-adaptive}` concurrently (reusing `bench.py`'s `_run_cell_case`/
   `_register_cell_pricing` machinery), `entity_type_field="v2"`. Phase 2 monkeypatches
   `entity_extraction._EXTRACTION_PROMPT_TEMPLATE` to the frozen pre-swap snapshot (captured from
   git history before step 2's edit — paste verbatim into this file with a "frozen, FRE-771 A/B
   reference only" comment), runs the same 2 cells, `entity_type_field="v1"`, then restores the
   original template in a `finally`. Both phases feed their `type-boundary`-case results to the
   new cross-model-agreement module. No unit test for the CLI driver itself (I/O — matches the
   existing `harness.py`/`bench.py` precedent of "exercised by the run, not unit-tested"), but DO
   add a fast unit test that the monkeypatch/restore is exception-safe (patch, raise inside the
   `try`, assert the module global is restored).
8. **Quality gates**: `make test-k K=fre630`, `make test-k K=test_entity_extraction`, `make test`,
   `make mypy`, `make ruff-check`, `make ruff-format`.
9. **Run the live powered A/B** (test substrate — `make test-infra-up` first):
   ```
   uv run python -m scripts.eval.fre630_extraction_quality.fre771_powered_ab \
       --run-id fre771-2026-07-04 --samples 3
   ```
   Bounded to the D6 scope (2 families × 2 taxonomies × 3 samples × 50 cases = 600 calls).
10. **Research note + ADR update.** Write `docs/research/2026-07-04-fre-771-...md` with the
    per-arm table (entity_type_accuracy, hallucination/dedup/forbidden-edge/knowledge-class —
    "no regression" check) + the cross-model type-agreement number (v1 vs v2) on the
    `type-boundary` set. Update ADR-0109's AC-1 row + Status Updates section with the measured
    result (pass/fail against the ≥90% bar, stated plainly either way).
11. **PR.** Standard PR flow per the build skill.

## Acceptance-criteria mapping (for the master-gate proof)

- **AC-1** (≥90% cross-model type-agreement, previously-ambiguous set) — proven by step 9–10's
  `type-boundary` cross-model-agreement number.
- **"No regression"** on hallucination_rate / dedup_convergence / forbidden_edge_type_rate /
  knowledge_class_accuracy — proven by comparing the v2-arm aggregate against the v1-arm
  aggregate in the same run (step 9).
- **AC-8** (implementation gate) — the loader-test half was already satisfied by FRE-784; this
  ticket closes the remaining half ("the live 10-type extractor prompt ... reproduce this ADR").

## Codex plan-review outcome (2026-07-04)

All three open questions resolved (see D2/D3/D6 above, each marked "Codex-confirmed" or
"revised"): (1) the 2-family live-run scope is an acceptable reading of the ADR's own AC-1/step-4
text; (2) `entity_type_field` becomes a **required** kwarg, not a silently-defaulted one; (3) the
frozen V1 prompt content is relocated out of production code entirely, into the eval driver, via a
module-global monkeypatch rather than a new parameter — this also eliminated the need for any
change to `extract_entities_and_relationships`'s signature. Additional test-gap findings folded in
above: assert the full 10-definition text (not just tokens) renders; the flag-gated few-shot block
must not regress to V1-only types; explicit `entity_type_field` at every call site;
`classify_smoke` V2 coverage. Paid-call precedent (FRE-766/770/782 all ran real cloud calls at
comparable or larger scale in this same ADR chain) confirmed consistent with running this ticket's
600-call A/B for real.
