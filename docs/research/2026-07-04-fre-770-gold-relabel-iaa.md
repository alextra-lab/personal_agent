# FRE-770 — Gold Re-label to the ADR-0109 V2 Taxonomy: Method, IAA, and Adjudication

**Date:** 2026-07-04
**Backing:** [ADR-0109 Entity & Relationship Taxonomy Redesign](../architecture_decisions/ADR-0109-entity-taxonomy-redesign.md), Implementation Notes step 2 (gated on step 1, FRE-769, Done). Implementation plan: `docs/superpowers/plans/2026-07-04-fre-770-gold-relabel-iaa.md` (codex-reviewed before coding).
**Scope:** re-labels the FRE-630 gold set to the V2 8-type taxonomy for *measurement purposes only* — the live extractor/harness still score against the V1 7-type vocabulary until FRE-771 swaps the extraction prompt. See "Scope boundary" in the implementation plan.

---

## Method

Every gold entity was blind-classified by **3 model raters across two provider families** — `gpt-5.4-mini` (temp 0.0), `gpt-5.4` (full, temp 0.0), `claude-sonnet-5` (adaptive) — using a single-turn classification prompt: the entity's canonical name, its owning case's source text (context), and the ADR-0109 V2 GoLLIE definitions (inclusion + exclusion + example), copied verbatim. No rater saw the current V1 label, another rater's answer, or rater identity.

Calls went **directly** through `litellm.acompletion()` (`scripts/eval/fre630_extraction_quality/relabel_v2_types.py`), bypassing the app's cost-gated `LiteLLMClient` — a deliberate, called-out exception (this is offline classification, not the production extraction path; no `entity_extraction.py` DI seam applies). Every run stamps a `prompt_hash` and raw per-entity/per-rater records to gitignored telemetry (`telemetry/evaluation/fre630-extraction-quality/v2-relabel-<run-id>.json`) so a relabel run is never misread against a different prompt/definition revision.

Run: `fre770-2026-07-04` — **65 entities**, all 3 raters responded with a valid, in-vocabulary label for every one (0 parse errors, 0 provider errors).

## Inter-annotator agreement

Fleiss' kappa (chance-corrected) per type, one-vs-rest, plus raw pairwise agreement by rater pair (`scripts/eval/fre630_extraction_quality/iaa.py`):

| type | kappa | status | n_positive | raw_agreement |
|---|---|---|---|---|
| DomainOrTopic | 0.744 | ok | 39 | 0.918 |
| Event | 0.852 | ok | 7 | 0.990 |
| Location | 1.000 | ok | 21 | 1.000 |
| MethodOrConcept | 0.645 | ok | 55 | 0.856 |
| Organization | -0.005 | ok | 1 | 0.990 |
| Person | 1.000 | ok | 3 | 1.000 |
| Phenomenon | 0.867 | ok | 37 | 0.959 |
| TechnicalArtifact | 0.738 | ok | 32 | 0.928 |

**Overall kappa: 0.777** (n=65 items) — this is our *measured* IAA ceiling, replacing the literature proxy the ADR previously cited (~0.80–0.90 typed-F1 in general fine-grained-typing IAA literature). 0.777 sits just below that range, which is expected: the V2 boundaries were only spot-checked at n=1 per cell before this ticket, not validated at scale.

`Organization` at kappa≈-0.005 with `n_positive=1` is a **near-zero-prevalence artifact**, not a signal that Organization is a bad type — only one entity (`Minoan Civilisation`, itself one of the 17 disagreements) was ever labeled Organization by any rater across the whole 65-item set, so the one-vs-rest statistic has almost no variance to measure. Read this row as "insufficient data," not "this type disagrees."

Rater-pair agreement (raw, not chance-corrected) — the honest reading of "how independent are these 3 raters":

| rater pair | agreement |
|---|---|
| full↔sonnet | 0.862 |
| mini↔full | 0.800 |
| mini↔sonnet | 0.800 |

The two OpenAI-family raters (mini, full) do **not** agree with each other more than either agrees with the cross-family Claude rater — full↔sonnet is actually the *highest* agreeing pair. This is reassuring: it means the 3-rater signal is not an artifact of two same-provider models converging on shared provider-specific biases while disagreeing with the outside model.

## Disagreements and adjudication

17 of 65 entities (26%) had at least one rater in the minority. All 17 were adjudicated by the builder against the ADR-0109 GoLLIE inclusion/exclusion/example text; **1** required a genuine 3-way-split ruling and is flagged for owner sign-off (`v2_needs_owner_signoff: true` in the gold YAML).

| entity (case) | rater labels (mini / full / sonnet) | ruling | basis |
|---|---|---|---|
| Diffraction Grating (`hallucination-misspelled-reltype`) | MethodOrConcept / TechnicalArtifact / TechnicalArtifact | **TechnicalArtifact** | Confirmed majority — a physical optical component, matches "software or hardware... physically use." |
| Relation Extraction (`hallucination-tool-names`) | MethodOrConcept / DomainOrTopic / DomainOrTopic | **DomainOrTopic** | Confirmed majority — names an NLP subfield/task category encompassing many techniques, not one bounded method. |
| Wavelength (`physics-scattering`) | MethodOrConcept / Phenomenon / MethodOrConcept | **MethodOrConcept** | Confirmed majority, but flagged: exposes a genuine taxonomy gap — physical *quantities/measures* (wavelength, mass, temperature) have no clean home in the 8-type vocabulary (not human-invented, but also not an occurring phenomenon/event itself). |
| Diffraction Limit (`optics-diffraction-limit`) | MethodOrConcept / Phenomenon / Phenomenon | **Phenomenon** | Confirmed majority — a naturally-arising physical *effect/constraint*, distinct from Wavelength (a raw property) above. |
| Hummus (`cooking-technique`) | MethodOrConcept / TechnicalArtifact / TechnicalArtifact | **MethodOrConcept** (overrides majority) | TechnicalArtifact's definition is explicitly scoped to "software or hardware" (e.g. Python, a GPU) — a food dish doesn't fit despite being physically concrete. Kept consistent with the Mapo Tofu ruling below. |
| Minoan Civilisation (`history-bronze-age`) | DomainOrTopic / DomainOrTopic / Organization | **DomainOrTopic** | Confirmed majority — a civilisation is a historical/cultural subject of study, not a structured institution/company (Organization's definition). |
| Incident Response (`security-incident-response`) | MethodOrConcept / DomainOrTopic / MethodOrConcept | **MethodOrConcept** | Confirmed majority — a specific structured methodology/lifecycle (named phases), not a whole field. |
| Manual Transmission (`stance-and-claim-vehicle`) | MethodOrConcept / TechnicalArtifact / TechnicalArtifact | **TechnicalArtifact** | Confirmed majority — a concrete, physically-installable mechanical hardware component; genuinely engineered hardware, unlike the food-dish cases. |
| Ear Training (`stance-ear-training-goal`) | MethodOrConcept / DomainOrTopic / MethodOrConcept | **MethodOrConcept** | Confirmed majority — a specific practice/technique (graded drills), not a broad field. |
| Mapo Tofu (`stance-preferred-cuisine`) | MethodOrConcept / MethodOrConcept / TechnicalArtifact | **MethodOrConcept** | Confirmed majority, consistent with Hummus above. |
| Neuroplasticity (`personal-writing-project`) | Phenomenon / DomainOrTopic / Phenomenon | **Phenomenon** | Confirmed majority — a naturally-occurring biological process (like photosynthesis, the ADR's own Phenomenon example), not a field of study. |
| Learning (`personal-writing-project`) | MethodOrConcept / DomainOrTopic / DomainOrTopic | **DomainOrTopic** | Confirmed majority — used generically ("the substrate of learning"), invokes the broad subject, not one named process. |
| **Neuroplasticity Chapter** (`personal-writing-project`) | MethodOrConcept / TechnicalArtifact / Event | **TechnicalArtifact** ⚠️ **needs owner sign-off** | **3-way split, no clean fit.** A personal creative-writing artifact (a chapter draft) doesn't map onto any V2 type designed for world-knowledge entities. Provisionally ruled TechnicalArtifact (closest: a concrete, named authored artifact) but this exposes a genuine taxonomy gap for Personal-class creative works — may warrant a 9th type or Personal-specific handling before FRE-771 treats V2 as settled. |
| Cost Gate (`system-self-telemetry`) | MethodOrConcept / MethodOrConcept / TechnicalArtifact | **TechnicalArtifact** (overrides majority) | The Cost Gate is a concrete, deployed software component (ADR-0065's Postgres-backed reservation system, the `cost_gate/` module) — not an abstract policy. "Software or hardware" explicitly covers backend services like this. |
| Compact SUV (`claim-car-shopping`) | DomainOrTopic / TechnicalArtifact / TechnicalArtifact | **TechnicalArtifact** | Confirmed majority — names a vehicle category, but still a concrete, physically-driveable hardware class (consistent with Manual Transmission). |
| Plant-Based Diet (`claim-diet-change`) | DomainOrTopic / DomainOrTopic / MethodOrConcept | **DomainOrTopic** | Confirmed majority — a broad dietary category/subject area, not one bounded technique. |
| Spacetime (`phenomenon-boundary-spacetime`) | MethodOrConcept / Phenomenon / Phenomenon | **Phenomenon** | Confirmed majority — **resolves ADR-0109's own named open question** ("is spacetime a Phenomenon or a DomainOrTopic?"): the naturally-existing structure of the universe, independent of human design. |

**Only one 3-way split** (Neuroplasticity Chapter) — consistent with the ADR's own high cross-model agreement measurements (9/10, 5/5) on comparable cases, and with the plan's expectation that 3-way splits would be rare.

## Coverage growth

Beyond the ADR's own 5 spot-checked Phenomenon examples, this ticket added:

- **Regression anchors** (reproduce the ADR's own finding): `phenomenon-gravity`, `phenomenon-photosynthesis`, `phenomenon-greenhouse-effect`, `phenomenon-black-hole`, `phenomenon-maillard-reaction` — all landed unanimous `Phenomenon` (no disagreement), confirming the pipeline reproduces the ADR's 5/5 result.
- **New coverage, not in the ADR spot-check**: `phenomenon-boundary-spacetime` (the ADR's own named unresolved risk case — now resolved, see table above), `phenomenon-acoustics-resonance` (an acoustics/music example, an owner domain the ADR names but never tested — unanimous Phenomenon), plus 3 new MethodOrConcept↔DomainOrTopic boundary pairs: `boundary-cybersecurity-pentest`, `boundary-buffer-overflow`, `boundary-cosmology-redshift`.
- **Relabeled pre-existing entities** that already looked like Phenomenon under V2: `Cosmic Microwave Background` (unanimous Phenomenon), `Rayleigh Scattering` (unanimous Phenomenon) — plus `Interference` and `Diffraction Limit` surfaced as Phenomenon-typed through the general rater pass (not pre-selected, discovered by the pipeline).

**Total Phenomenon coverage: 13 entities** (well beyond the ADR's original 5), spanning physics, cosmology, biology, climate, cooking-chemistry, and acoustics.

## Gold set growth

10 new cases (12 new entities) added to `gold_extraction.yaml`, growing the set from 36 → 46 cases (53 → 65 entities). New cases carry a best-effort V1 `type` so they load/score under the still-live V1 harness (`test_gold_set_loads_and_hits_size` and friends all still pass), plus the full `v2_type` (+ adjudication metadata where applicable) via this ticket's pipeline.

## Re-baseline

`gold_schema_version` bumped 1.0 → 1.1 (additive `v2_type`/`v2_adjudicated`/`v2_adjudication_rationale`/`v2_needs_owner_signoff` fields; `type`/`ALLOWED_ENTITY_TYPES` — the scored V1 vocab — unchanged). Because the case count changed, a fresh harness run is required per the gold file's own convention ("a gold change invalidates the historical baseline").

Run `fre770-rebaseline-2026-07-04` (46 cases × 3 samples, `gpt-5.4-mini`, the prod extractor, against the test cost substrate):

| metric | value |
|---|---|
| entity_precision | 0.47±0.31 |
| entity_recall | 0.96±0.12 |
| entity_f1 | 0.63±0.23 |
| entity_type_accuracy (V1) | 0.83±0.33 |
| knowledge_class_accuracy | 1.00±0.00 |
| relationship_f1 | 0.71±0.22 |
| hallucination_rate | 0.00±0.00 |
| claim_emission_recall | 0.19±0.40 |

Full per-tag breakdown and per-case diffs: `telemetry/evaluation/fre630-extraction-quality/fre770-rebaseline-2026-07-04.md` (gitignored raw run; this curated table is the committed record). This is the **new reference baseline** for FRE-771's powered A/B — it is not comparable to the pre-FRE-770 24/36-case baselines (different `gold_schema_version` and case count).

## Open item for the owner

**`Neuroplasticity Chapter` (personal-writing-project case) needs a sign-off decision** — see the adjudication table above. The 3-way rater split exposes a real gap: the V2 8-type taxonomy has no clean category for a Personal-class creative-writing artifact (a chapter someone is drafting). Options for FRE-771 or a follow-up: (a) accept the provisional `TechnicalArtifact` ruling, (b) special-case Personal-class entities to a different type, or (c) add a 9th type for authored/creative artifacts. Flagged in `gold_extraction.yaml` via `v2_needs_owner_signoff: true` on that entity so it isn't silently treated as settled.

## Acceptance-criteria proof (FRE-770)

| AC | Proof |
|---|---|
| Gold re-labeled to 8 types by ≥2 independent annotators | 65/65 entities carry `v2_type` from the 3-rater pipeline; `test_all_entities_have_v2_type` |
| Reported IAA per type | Kappa table above; overall kappa 0.777 |
| Disagreements adjudicated | 17/17 disagreements ruled with rationale (table above); 1 flagged `v2_needs_owner_signoff` |
| Boundary coverage grown beyond the 5 spot-checked | 13 Phenomenon entities (5 anchors + 8 new/relabeled); `test_phenomenon_coverage_grown_beyond_adr_spotcheck` |
| Benchmark re-baselined | `fre770-rebaseline-2026-07-04` run (46 cases × 3 samples) recorded above |
