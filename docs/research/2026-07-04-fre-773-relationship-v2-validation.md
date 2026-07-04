# FRE-773 — ADR-0109 V2 relationship half: cross-model agreement + gold re-label

**Date:** 2026-07-04 · **Ticket:** FRE-773 · **Backing:** ADR-0109 § "V2 — relationship types" (the
explicitly-unvalidated half) · **Template:** FRE-770 (entity-half re-label, merged #358).

> ADR-0109 honesty flag: *"Only the entity V2 was cross-model spot-checked; the relationship V2 is a
> proposal, not a measured result… Do not ship the relationship half on the entity half's evidence."*
> This document supplies the missing measurement.

## What was measured

The V1 relationship vocabulary (`PART_OF, USES, RELATED_TO, SIMILAR_TO, CREATED_BY, LOCATED_IN`) was
inherited and never derived. ADR-0109 names two design faults: `RELATED_TO` is a catch-all that
overlaps every specific type, and `USES` lacks a direction. This ticket authored a tightened **V2**
vocabulary (same 6 keys; directional GoLLIE definitions; `RELATED_TO` recast as a gated
None-of-the-Above last resort; an explicit emit-nothing-if-none-fits rule), then ran it through the
same blind 3-model-rater agreement pipeline FRE-770 used, over the FRE-630 gold's relationship
triples, and re-labeled the gold with a dual `v2_rel_type` field (V1 `rel_type` untouched and still
scored).

## The V1 → V2 relationship definitions

V1 (inherited, terse — `entity_extraction.py:74–80`): `PART_OF` "component or subset of another" ·
`USES` "uses or depends on another" · `RELATED_TO` "general semantic relationship" · `SIMILAR_TO`
"comparable or equivalent" · `CREATED_BY` "created or authored by another" · `LOCATED_IN`
"geographically within another".

V2 (authored here; verbatim in `relabel_v2_rels.py:V2_REL_DEFINITIONS`, `prompt_hash` pins the
revision the run used):

| key | direction | inclusion · **exclusion** · e.g. |
|---|---|---|
| `PART_OF` | source **is part of** target | source is a **structural** component/member/stage/constituent of the whole. **Not** a functional dependency (→ USES); **not** "a concept/method merely *studied within* the field target" — topical containment is **not** structural membership (→ RELATED_TO or NONE). *e.g. Containment PART_OF Incident Response.* |
| `USES` | source **depends on** target | source functionally depends on / invokes / is built on target to operate — source *requires* target. **Directional:** if target is merely "used for" source, or the dependency runs the other way, or it is only a loose association → **not** USES (→ RELATED_TO). *e.g. FastAPI USES PostgreSQL.* |
| `CREATED_BY` | source **created by** target | an artifact/work authored/invented/produced by a person or org. **Not** use or membership. *e.g. Linux CREATED_BY Linus Torvalds.* |
| `LOCATED_IN` | source **located in** target | geographic/physical containment within a place. **Not** organizational membership (→ PART_OF); **not** a datastore. *e.g. Alhambra LOCATED_IN Granada.* |
| `SIMILAR_TO` | **symmetric** | comparable/analogous/near-equivalent alternatives at the same level. **Not** one depending on the other (→ USES); **not** part/whole. *e.g. PostgreSQL SIMILAR_TO MySQL.* |
| `RELATED_TO` | **gated NoTA last resort** | ONLY when clearly associated but **no specific type applies** — never when one fits. If the association is weak/topical and no directional type holds → RELATED_TO; if nothing meaningful connects them → **NONE** (emit nothing). *e.g. Cosmic Microwave Background RELATED_TO Big Bang.* |

## Method (mirrors FRE-770)

- **Raters:** 3 models across 2 provider families — `gpt-5.4-mini` (temp 0), `gpt-5.4` (temp 0),
  `claude-sonnet-5` (adaptive). Framed honestly as "3 raters across 2 provider families," not "3
  independent annotators" (two are OpenAI siblings — the rater-pair table below keeps that legible).
- **Blind:** each rater sees only the ordered `source → target` pair, the case's source text, and the
  6 V2 definitions. No V1 label, no other rater's answer, no rater identity. `NONE` (no edge) is an
  allowed, first-class outcome — never coerced into a type.
- **Direct litellm, cost-gate-bypassed** — a deliberate, called-out exception (offline classification,
  no production extraction, no KG write), owner-authorized. Raw per-item/per-rater records are
  gitignored telemetry. Overall spend for both runs was under $0.50.
- **Metric:** Fleiss' kappa (chance-corrected) overall + per-type one-vs-rest, plus raw pairwise
  agreement per rater pair (`iaa.py`, reused verbatim from FRE-770).

## Results — cross-model agreement (final run, n=25 relationships)

**Overall Fleiss' kappa = 0.680** across 25 relationship items (substantial agreement; comparable to
the entity half's 0.777).

| rel type | kappa | status | n (rater votes) | raw agreement |
|---|---|---|---|---|
| CREATED_BY | 1.000 | ok | 3 | 1.000 |
| LOCATED_IN | 0.842 | ok | 7 | 0.973 |
| PART_OF | 0.750 | ok | 15 | 0.920 |
| USES | 0.614 | ok | 22 | 0.840 |
| RELATED_TO | 0.520 | ok | 25 | 0.787 |
| SIMILAR_TO | 1.000 | ok | 3 | 1.000 |
| NONE | — | undefined_zero_variance | 0 | 1.000 |

| rater pair | agreement |
|---|---|
| mini↔full (OpenAI-internal) | 0.840 |
| mini↔sonnet (cross-family) | 0.720 |
| full↔sonnet (cross-family) | 0.720 |

**Honest read of statistical power (per plan / codex Q4):** the **overall** kappa is the headline the
"measured" AC turns on. The **per-type** rows are **sparse/diagnostic, not robust** — `CREATED_BY`,
`SIMILAR_TO` rest on 3 rater-votes each and `NONE` has zero positives (no rater ever judged an
existing gold edge to be a non-edge). The lowest-agreement type is `RELATED_TO` (κ=0.520) — expected,
since it is precisely the boundary the tightening targets: raters still occasionally reach for a
specific type where the gated NoTA is correct (and vice-versa). No `NONE` outcomes and **no 3-way
splits** occurred; 9 of 25 items split 2/1.

## Adjudication (all 9 splits; 16 unanimous accepted as-is)

Every split was ruled against the V2 GoLLIE text for the specific pair + context. Two are **builder
overrides of the model majority** — both are the ADR's named `USES`-direction fault, where the
majority picked `USES` but the tightened directional definition makes `RELATED_TO` correct.

| pair | votes | V2 label | note |
|---|---|---|---|
| Nash Equilibrium → Game Theory | RELATED_TO 2 / PART_OF 1 | **RELATED_TO** | topical containment ≠ structural PART_OF (V1 was PART_OF → flip) |
| Prisoner's Dilemma → Game Theory | RELATED_TO 2 / PART_OF 1 | **RELATED_TO** | same (V1 PART_OF → flip) |
| Hard Problem of Consciousness → Philosophy of Mind | RELATED_TO 2 / PART_OF 1 | **RELATED_TO** | same (V1 PART_OF → flip) |
| Redshift → Cosmology | RELATED_TO 2 / USES 1 | **RELATED_TO** | tool used *by* the field (reverse) / observational assoc. (V1 PART_OF → flip) |
| Rayleigh Scattering → Wavelength | RELATED_TO 2 / USES 1 | **RELATED_TO** | weak dependence on a variable ≠ functional USES |
| Diffraction Grating → Interference | USES 2 / RELATED_TO 1 | **USES** | grating's operation functionally depends on interference |
| Knowledge Graph → Neo4j | USES 2 / LOCATED_IN 1 | **USES** | depends on Neo4j to persist/query; LOCATED_IN is for places, not datastores |
| **Trie → Prefix Search** | USES 2 / RELATED_TO 1 | **RELATED_TO** *(override)* | **the ADR's named flip.** USES is directional; a trie does not depend on prefix search (the reverse holds). `sonnet`'s own rationale concluded RELATED_TO despite labeling USES. |
| **Regular Expression → Input Validation** | USES 2 / RELATED_TO 1 | **RELATED_TO** *(override)* | designed direction-fault case: a regex does not depend on validation; validation uses the regex → gated RELATED_TO. |

**The two overrides are the validation payoff:** even under the tightened directional definition, the
`USES` catch reversal is subtle enough that 2/3 frontier raters still mislabel it — which is *precisely*
the evidence that a downstream write-path gate (FRE-760) enforcing the V2 direction rule has real work
to do, not a theoretical concern. The gold now encodes the correct labels; both overrides are recorded
in-schema (`v2_adjudicated=true` + rationale) and named here for owner visibility.

## Gold re-label outcome

All 25 gold relationships now carry `v2_rel_type` (V1 `rel_type` unchanged). V2 distribution:
`RELATED_TO` 11 · `USES` 6 · `PART_OF` 4 · `LOCATED_IN` 2 · `SIMILAR_TO` 1 · `CREATED_BY` 1. `RELATED_TO`
grew from V1's 5 to 11 — the tightened gating correctly reassigns the over-broad topical-`PART_OF` and
reverse-`USES` edges to the NoTA fallback, which is the ADR's whole point.

**Coverage growth (the two ADR-named faults):** 4 new relationship cases were added — `pandas → NumPy`
(clean directional USES, unanimous), `MySQL → PostgreSQL` (SIMILAR_TO — V1 gold had zero, unanimous),
`Regular Expression → Input Validation` (USES↔RELATED_TO direction boundary — split, overridden), and
`TCP → Internet Protocol Suite` (a *structural* PART_OF, unanimous — the clean contrast to the topical
PART_OF flips). The 8 new coverage entities carry builder-assigned `v2_type` (unambiguous
artifacts/methods); FRE-773 measures the relationship label, and entity re-labeling was FRE-770's scope.

## Scope boundaries & residual open questions

- **No prompt swap, no write-gate, no KG migration.** The live extractor still emits V1. FRE-760 (the
  write-path gate) is the consumer of this measurement and must enforce V2, not V1 (ADR-0109 §Levers 2).
- **V1 harness re-baseline deferred.** The gold grew 46→50 cases, but the V1 `rel_type` scoring
  semantics are unchanged and `make test` does not run the extraction harness. A full re-baseline needs
  a live extraction LLM + test substrate and spend beyond the authorized rater run, so it is left as a
  fast follow (or a master deploy-time step) rather than run here — it does not gate FRE-773's
  measurement AC.
- **`RELATED_TO` at κ=0.520** is the residual soft spot; if the write-gate proves the NoTA boundary
  matters in production, a larger targeted probe (more USES↔RELATED_TO direction pairs) would sharpen
  the per-type estimate beyond the diagnostic level reported here.
- **No `NONE` cases** arose — every existing gold edge carries at least a weak association. The
  emit-nothing path is implemented and machine-checkable (`REL_V2_NO_EDGE` + mandatory owner-signoff)
  but was not exercised by the current gold; it will matter for extractor-emitted (not gold) edges.

## References

- ADR-0109 § V2 relationship types (the approach these definitions implement).
- FRE-770 research doc (`docs/research/2026-07-04-fre-770-gold-relabel-iaa.md`) — the entity-half
  template and IAA method.
- `scripts/eval/fre630_extraction_quality/relabel_v2_rels.py` (driver), `iaa.py` (statistics, reused).
