# FRE-782 — ADR-0109 Amendment: KnowledgeArtifact + QuantityMeasure Boundary Validation (3-rater IAA)

**Date:** 2026-07-04
**Backing:** [ADR-0109 Entity & Relationship Taxonomy Redesign](../architecture_decisions/ADR-0109-entity-taxonomy-redesign.md) — Amendment 1. Trigger: [FRE-770](https://linear.app/frenchforest/issue/FRE-770) surfaced two genuine gaps in the accepted 8-type V2 vocabulary.
**Scope:** validates whether adding two entity types — `KnowledgeArtifact` (human-authored works) and `QuantityMeasure` (physical quantities) — yields *teachable* boundaries, before they are promoted into the extractor (FRE-771). Measurement only; the live extractor still speaks V1 until FRE-771.

---

## Why an amendment

FRE-770 re-labeled the FRE-630 gold with a blind 3-rater IAA (overall Fleiss κ 0.777). Two entities fell out as real gaps the 8-type vocabulary could not home:

1. **Neuroplasticity Chapter** — a Personal-class creative-writing artifact — drew a genuine 3-way rater split (MethodOrConcept / TechnicalArtifact / Event), was only *provisionally* ruled TechnicalArtifact, and was flagged `v2_needs_owner_signoff`. There was no type for a work you *read to understand*.
2. **Wavelength** — a physical quantity — was majority-ruled MethodOrConcept but flagged as exposing a gap: physical *quantities/measures* (wavelength, mass, temperature) are neither human-invented methods nor occurring phenomena.

Owner decision (2026-07-04): add **`KnowledgeArtifact`** for authored works (rather than let TechnicalArtifact become a junk drawer) and **`QuantityMeasure`** for physical quantities — each **gated on the same validation bar** ADR-0109 held its 8 types to: the boundary must hold at high cross-model agreement, or the type is not shipped.

## Method

Identical to FRE-770's instrument: every probe entity was blind-classified by **3 model raters across two provider families** — `gpt-5.4-mini` (temp 0.0), `gpt-5.4` (temp 0.0), `claude-sonnet-5` (adaptive) — from the entity name, a short realistic context, and the GoLLIE-style type definitions (inclusion + exclusion + example). No rater saw the intended answer, another rater's label, or rater identity. Calls went directly through `litellm.acompletion()`; IAA computed with the committed, unit-tested `scripts/eval/fre630_extraction_quality/iaa.py` (`build_iaa_report`). This is an ADR-validation probe, not a change to the committed eval vocab — promoting the 10-type set into `gold.py` / `relabel_v2_types.py` is FRE-782's build follow-up.

The definitions under test were the **full 10-type set** (the 8 accepted V2 types verbatim + the 2 draft types), so raters had to choose the *new* type over its neighbours on merit — the boundary is only validated if they pick it when it fits and reject it when it does not.

**Probe: 22 boundary entities** (`fre782-probe-2026-07-04`), 3 raters each, 0 parse/provider errors — deliberately concentrated on the two contested boundaries plus anchors:

- **KnowledgeArtifact side (6):** ADR-0109, the GoLLIE paper, an architecture spec, the Neuroplasticity Chapter, the master plan, an outage post-mortem report.
- **TechnicalArtifact side (6):** the FRE-630 gold set, the extraction prompt, `gold_extraction.yaml`, Neo4j, a GPU, FastAPI — the first three deliberately probe the owner's "system tooling is TechnicalArtifact, not KnowledgeArtifact" rule.
- **QuantityMeasure side (6):** Wavelength, Mass, Temperature, Frequency, Luminosity, Redshift.
- **Phenomenon anchors (3):** Gravity, Rayleigh Scattering, Diffraction Limit.
- **MethodOrConcept anchor (1):** the Fourier Transform (the *method*, contrasted with Frequency the *quantity*).

## Inter-annotator agreement

**Overall Fleiss κ = 0.900** (n=22) — materially above FRE-770's measured 0.777 ceiling on the harder full-gold set.

| type | kappa | n_positive | raw_agreement |
|---|---|---|---|
| KnowledgeArtifact | **1.000** | 18 | 1.000 |
| TechnicalArtifact | **1.000** | 18 | 1.000 |
| QuantityMeasure | **0.847** | 18 | 0.939 |
| Phenomenon | 0.716 | 8 | 0.939 |
| MethodOrConcept | 0.734 | 4 | 0.970 |

(Types with n_positive=0 on this boundary-focused probe — Person/Organization/Location/DomainOrTopic/Event — omitted; the probe was not designed to exercise them.)

| rater pair | agreement |
|---|---|
| mini↔sonnet | 0.955 |
| mini↔full | 0.909 |
| full↔sonnet | 0.909 |

The two OpenAI-family raters do not agree more with each other than with the cross-family Claude rater — the signal is not a same-provider artifact.

## Per-entity outcome

**20 of 22 unanimous (3/3); 1 majority (2/3); 1 three-way split.** The two new boundaries land cleanly:

- **KnowledgeArtifact ↔ TechnicalArtifact is airtight (κ 1.000 both sides).** All six authored works → KnowledgeArtifact unanimously; all six tooling entities → TechnicalArtifact unanimously. Critically, the three "system tooling" cases the owner's boundary rule 1 assigns to TechnicalArtifact — the **FRE-630 gold set, the extraction prompt, and `gold_extraction.yaml`** — went TechnicalArtifact **unanimously**. Honest reading: the raters were not shown the *numbered* boundary rule, but its substance is embedded in the type definitions (which name datasets/benchmarks/gold-files/prompts as TechnicalArtifact). So this shows the **definitions carry the boundary** — the read-vs-run line is teachable from the written definition without extra instruction — not that raters rediscovered it unaided.
- **QuantityMeasure holds (κ 0.847).** Wavelength, Mass, Temperature, Frequency, Luminosity all unanimous → QuantityMeasure. **Wavelength — FRE-770's flagged gap — is now unanimous.**
- **Both FRE-770 flagged cases resolved:** Neuroplasticity Chapter → **KnowledgeArtifact (3/3)**; Wavelength → **QuantityMeasure (3/3)**.

### The two disagreements (documented, not blockers)

| entity | mini / full / sonnet | reading |
|---|---|---|
| **Redshift** | QuantityMeasure / Phenomenon / QuantityMeasure | Genuinely dual-natured — the measured quantity *z* vs the observed cosmological stretching. Majority QuantityMeasure under a "measure the redshift" context. **Disambiguation rule for the gold/prompt:** when the entity names the *measured quantity/value* → QuantityMeasure; when it names the *stretching process itself* → Phenomenon. |
| **Diffraction Limit** | QuantityMeasure / Phenomenon / MethodOrConcept | A naturally-arising *limit/constraint* (~λ/2) that reads as a bound, an effect, or a quantity. FRE-770 ruled it Phenomenon; adding QuantityMeasure fuzzed it. **Disambiguation rule:** a naturally-arising physical *limit/constraint/effect* → Phenomenon; QuantityMeasure is reserved for the raw measurable property, not the constraint it implies. |

Neither case touches KnowledgeArtifact. They are a **real, bounded** residual: adding QuantityMeasure genuinely introduces ambiguity at the "physical effect/limit expressed as a number" edge (Redshift, Diffraction Limit) — this is not waved away, it is *ruled* by the disambiguation rules and it is why QuantityMeasure earns κ 0.847, not 1.000. What the probe shows is that the ambiguity is **confined to that edge**: the raw-property core (Wavelength/Mass/Temperature/Frequency/Luminosity) is unanimous, and the entire per-type κ dip on Phenomenon (0.716) / MethodOrConcept (0.734) is driven by these two anchor entities, not by the new types' core regions.

## Verdict

**Rejection rule (externally anchored, not tuned to pass).** The probe was run first, then this rule written — so the threshold is anchored to a *pre-existing* number, not one chosen after seeing the result: the **already-accepted** V2 types' own FRE-770 agreement (overall κ 0.777; weakest-accepted per-type κ 0.645, `MethodOrConcept`). A new type earns its place only if it is at least as agreed-upon as the types already shipped. Reject `QuantityMeasure` unless its per-type κ clears those FRE-770 marks **and** all five raw-property anchors {Wavelength, Mass, Temperature, Frequency, Luminosity} are unanimous; reject `KnowledgeArtifact` unless it and TechnicalArtifact clear the same marks and every authored-work / system-tooling probe is unanimous. Result: KnowledgeArtifact/TechnicalArtifact κ 1.000 (all unanimous); QuantityMeasure κ 0.847 (> 0.777) with all five anchors unanimous. **Both clear the bar → ship both**, at 10 types total: Person, Organization, Location, Event, TechnicalArtifact, **KnowledgeArtifact**, **QuantityMeasure**, MethodOrConcept, DomainOrTopic, Phenomenon.

**Scope of this claim (important):** the probe validates that the **two new boundaries** are teachable — not that the full 10-type taxonomy is production-stable. It deliberately concentrates on the contested boundaries and does not exercise Person/Organization/Location/DomainOrTopic/Event. Production-wide 10-type stability is a *separate* gate: the powered A/B over the full re-labeled gold (ADR-0109 AC-1, owned by FRE-771). Carry the two disambiguation rules into the gold re-label and the FRE-771 prompt so the edge cases label consistently.

## Reproduction

The probe is fully specified in committed docs — the **10-type definitions** are the ADR-0109 § Decision table (verbatim), and the **22-entity fixture** (entity + context + intended side) is the [appendix below](#appendix--probe-fixture-22-entities). Run id `fre782-probe-2026-07-04`; the runner used was `adr0109_boundary_probe.py` (reuses the committed, unit-tested `iaa.build_iaa_report`). Anyone can re-run by feeding the appendix contexts + the ADR definitions to the three raters.

FRE-782's build follow-up (ADR-0109 AC-8) promotes the validated 10-type definitions into the committed instrument (`relabel_v2_types.py` `V2_TYPE_DEFINITIONS` + `gold.py` `ALLOWED_ENTITY_TYPES_V2`), **checks in this fixture as a boundary regression test**, and re-runs against the full gold set — so the AC-6/AC-7 boundary result becomes a re-runnable assertion, not a one-off.

## Appendix — Probe fixture (22 entities)

The exact classification inputs (each rater saw only: the ADR-0109 10-type definitions + the context below + the entity name; `intended side` is design intent, never shown). Results are in the per-entity table above.

| entity | context (verbatim) | intended side |
|---|---|---|
| ADR-0109 | "ADR-0109 is the architecture decision record that redesigns the entity taxonomy; I'm reading it to understand the rationale for the type boundaries." | KnowledgeArtifact |
| GoLLIE paper | "The GoLLIE paper (Sainz et al., ICLR 2024) argues that annotation guidelines with explicit inclusion and exclusion criteria improve zero-shot information extraction." | KnowledgeArtifact |
| architecture redesign spec | "The architecture redesign spec documents the seven-stage pre-LLM gateway pipeline and how each stage hands off to the next." | KnowledgeArtifact |
| Neuroplasticity Chapter | "Back to the chapter I'm drafting on neuroplasticity — I want to tie in learning. The chapter is meant to explain the ideas to a general reader." | KnowledgeArtifact |
| master plan | "The master plan lays out the current priorities and the sequencing across the whole portfolio so we know what to build next." | KnowledgeArtifact |
| outage post-mortem report | "The outage post-mortem report walks through the incident timeline, the root cause, and the follow-up action items." | KnowledgeArtifact |
| FRE-630 gold set | "The FRE-630 gold set is the benchmark we score the extractor against; each harness run loads it and reports precision and recall." | TechnicalArtifact |
| extraction prompt | "We swap the extraction prompt for the new GoLLIE definitions and re-run the powered A/B against the gold." | TechnicalArtifact |
| gold_extraction.yaml | "gold_extraction.yaml holds the labeled cases the harness loads and scores each entity against." | TechnicalArtifact |
| Neo4j | "We store the knowledge graph in Neo4j and query it over the Bolt protocol." | TechnicalArtifact |
| GPU | "The embedder runs on a GPU to keep latency low under load." | TechnicalArtifact |
| FastAPI | "The service is a FastAPI app that exposes the chat endpoint on port 9000." | TechnicalArtifact |
| Wavelength | "A diffraction grating splits light into its component wavelengths by the angle at which each is diffracted." | QuantityMeasure |
| Mass | "The mass of the object determines how strongly it is pulled by the gravitational field." | QuantityMeasure |
| Temperature | "As the temperature rises, the reaction rate increases roughly exponentially." | QuantityMeasure |
| Frequency | "The resonant frequency of the string sets the pitch you hear when it is plucked." | QuantityMeasure |
| Luminosity | "The star's luminosity is the total electromagnetic energy it radiates per second." | QuantityMeasure |
| Redshift | "Astronomers measure the redshift — how far the light has stretched to longer wavelengths — to infer recession speed." | QuantityMeasure (boundary) |
| Gravity | "Gravity is the force that pulls any two masses toward each other." | Phenomenon |
| Rayleigh Scattering | "Rayleigh scattering: shorter blue wavelengths scatter more strongly off air molecules, which is why the sky is blue." | Phenomenon |
| Diffraction Limit | "The diffraction limit — roughly half the wavelength of light — bounds how finely an optical system can resolve detail." | Phenomenon (boundary) |
| Fourier Transform | "We apply a Fourier transform to move the signal from the time domain into the frequency domain." | MethodOrConcept |
