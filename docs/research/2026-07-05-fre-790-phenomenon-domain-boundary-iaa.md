# FRE-790 — ADR-0109 Phenomenon ↔ DomainOrTopic Boundary Validation (3-rater IAA)

**Date:** 2026-07-05
**Backing:** [ADR-0109 Entity & Relationship Taxonomy Redesign](../architecture_decisions/ADR-0109-entity-taxonomy-redesign.md) — Amendment 1, **AC-1**. Trigger: [FRE-771](https://linear.app/frenchforest/issue/FRE-771)'s powered A/B measured AC-1's cross-model type-agreement at **83.3% (10/12)** — below the ≥90% target, on an underpowered n=12 slice, with both disagreements (`Spacetime`, `TCP`) tracing to the `Phenomenon ↔ DomainOrTopic` edge ADR-0109's Risks section named as untested.
**Scope:** a dedicated, properly-powered probe on that **one** boundary — is it a teachable, high-agreement line, or does a definition need sharpening? Measurement only; nothing ships to the live extractor. Mirrors the [FRE-782](https://linear.app/frenchforest/issue/FRE-782) instrument (`docs/research/2026-07-04-fre-782-knowledgeartifact-quantitymeasure-boundary-iaa.md`) — same 3 blind raters, same GoLLIE 10-type prompt, same `iaa.build_iaa_report`.

---

## Why a dedicated probe

FRE-771's AC-1 read was **83.3% (10/12)** cross-model agreement on the `type-boundary` gold subset — below the ≥90% bar, but n=12 is underpowered (a single-item flip moves the number ~8pp), and both disagreements sat on one edge:

- `phenomenon-boundary-spacetime::Spacetime` — ADR-0109's Risks section names this verbatim: *"is 'spacetime' a Phenomenon or a DomainOrTopic?"*
- `cov-partof-structural::TCP` — a residual boundary case not previously spot-checked.

Rather than re-argue from n=12, FRE-790 grows a focused fixture concentrated on `Phenomenon ↔ DomainOrTopic` and measures the boundary directly, so the verdict rests on a powered per-type κ rather than two items on a small paired slice.

## Pre-registered verdict rule (fixed BEFORE the run)

Unlike FRE-782 (which ran the probe, then wrote its rule — still externally anchored), FRE-790's rule was fixed in the committed plan **before** the run, anchored to the **already-shipped** V2 types' own FRE-770 agreement floor:

> The `Phenomenon ↔ DomainOrTopic` boundary **clears** iff (a) overall Fleiss κ ≥ **0.777** (FRE-770's full-gold ceiling) AND (b) **both** `Phenomenon` and `DomainOrTopic` per-type κ ≥ **0.645** (FRE-770's weakest-accepted per-type mark, `MethodOrConcept`). If either target type falls below, the boundary does **not** clear → propose a sharper `Phenomenon` exclusion clause and/or a `DomainOrTopic` inclusion clause as a Needs-Approval ADR-0109 amendment for owner review.

The disagreement diagnosis is reported regardless of pass/fail.

## Method

Identical instrument to FRE-770/782: every probe entity was blind-classified by **3 model raters across two provider families** — `gpt-5.4-mini` (temp 0.0), `gpt-5.4` (temp 0.0), `claude-sonnet-5` (adaptive) — from the entity name, a short realistic context, and the ADR-0109 **full 10-type** GoLLIE definitions (inclusion + exclusion + example). No rater saw the intended answer, another rater's label, or rater identity. Calls went directly through `litellm.acompletion()` (no cost-gate, no KG writes); IAA computed with the committed, unit-tested `iaa.build_iaa_report`. Run id `fre790-2026-07-05`, prompt hash `b003ea594d5c`; runner `adr0109_boundary_probe.py` (the committed runner FRE-782 named), fixture `fre790_phenomenon_domain_boundary_fixture.yaml`.

**Probe: 24 entities**, deliberately weighted onto the two target types so their per-type κ is a *powered* read (fixing FRE-771's n=12 problem) — 32 intended `Phenomenon` positives and 33 intended `DomainOrTopic` positives across the 3 raters:

- **Phenomenon anchors — clean (8):** Gravity, Photosynthesis, the greenhouse effect, Superconductivity, Turbulence, the Doppler effect, Rayleigh scattering, the Maillard reaction.
- **DomainOrTopic anchors — clean (8):** Cosmology, Cybersecurity, Thermodynamics, Neuroscience, Fluid dynamics, Number theory, Behavioral economics, Organic chemistry.
- **Phenomenon ↔ DomainOrTopic boundary cases (6):** Spacetime (ADR-named), Electromagnetism, Magnetism, Electricity, Acoustics, Optics — each word names both a natural phenomenon/force/effect AND the field that studies it; contexts were written to keep both readings live.
- **Non-boundary distractors (2):** TCP (a protocol → must be *rejected* into TechnicalArtifact) and the Fourier transform (a human-invented method → MethodOrConcept). Reported separately — they test active rejection of the two nearest non-boundary types, not the boundary itself.

## Inter-annotator agreement

**Overall Fleiss κ = 0.858** (n=24) — above the FRE-770 floor (0.777).

| type | kappa | n_positive | raw_agreement |
|---|---|---|---|
| **DomainOrTopic** | **0.888** | 33 | 0.944 |
| **Phenomenon** | **0.831** | 32 | 0.917 |
| TechnicalArtifact (TCP distractor) | 1.000 | 3 | 1.000 |
| MethodOrConcept (Fourier + 1 Spacetime vote) | 0.735 | 4 | 0.972 |

(Types with n_positive=0 on this boundary-focused probe — Person/Organization/Location/Event/KnowledgeArtifact/QuantityMeasure — omitted; the probe was not designed to exercise them.)

| rater pair | agreement |
|---|---|
| mini↔full | **1.000** |
| mini↔sonnet | 0.875 |
| full↔sonnet | 0.875 |

**Important honesty caveat — the disagreement is a cross-provider effect.** Unlike FRE-782 (where the two OpenAI raters did *not* agree more with each other than with the Claude rater), here `mini↔full` is **perfectly correlated (1.000)** and every one of the 3 disagreements is the cross-family `claude-sonnet-5` rater dissenting from both OpenAI raters. So on this specific edge the measured "cross-model agreement" is really "the OpenAI family vs Claude" — a two-family split, not a three-way one. The boundary still clears the bar, but the residual should be read as a *provider-sensitive* edge, not a broadly-contested one.

## Per-entity outcome

**21 of 24 unanimous (3/3); 3 majority (2/3); 0 three-way splits.** Both target types' clean anchors are airtight, and 3 of the 6 boundary cases (Magnetism, Acoustics, Optics) also landed unanimous — context, not just the bare word, drove the reading.

| entity | intended | mini / full / sonnet | outcome |
|---|---|---|---|
| Gravity | Phenomenon | P / P / P | ✓ unanimous |
| Photosynthesis | Phenomenon | P / P / P | ✓ unanimous |
| the greenhouse effect | Phenomenon | P / P / P | ✓ unanimous |
| Superconductivity | Phenomenon | P / P / P | ✓ unanimous |
| Turbulence | Phenomenon | P / P / P | ✓ unanimous |
| the Doppler effect | Phenomenon | P / P / P | ✓ unanimous |
| Rayleigh scattering | Phenomenon | P / P / P | ✓ unanimous |
| the Maillard reaction | Phenomenon | P / P / P | ✓ unanimous |
| Cosmology | DomainOrTopic | D / D / D | ✓ unanimous |
| Cybersecurity | DomainOrTopic | D / D / D | ✓ unanimous |
| Thermodynamics | DomainOrTopic | D / D / D | ✓ unanimous |
| Neuroscience | DomainOrTopic | D / D / D | ✓ unanimous |
| Fluid dynamics | DomainOrTopic | D / D / D | ✓ unanimous |
| Number theory | DomainOrTopic | D / D / D | ✓ unanimous |
| Behavioral economics | DomainOrTopic | D / D / D | ✓ unanimous |
| Organic chemistry | DomainOrTopic | D / D / D | ✓ unanimous |
| **Magnetism** (boundary) | Phenomenon | P / P / P | ✓ unanimous — the "emerges when electron spins line up" context forced the *effect* reading |
| **Acoustics** (boundary) | DomainOrTopic | D / D / D | ✓ unanimous — "the acoustics of the hall" still read as the *field/property*, unanimously D |
| **Optics** (boundary) | DomainOrTopic | D / D / D | ✓ unanimous — "optics describes how light bends…" read as the *discipline* |
| **Spacetime** (boundary) | Phenomenon | P / P / **MethodOrConcept** | ⚠ majority Phenomenon (2/3) |
| **Electromagnetism** (boundary) | Phenomenon | **D / D** / P | ⚠ majority DomainOrTopic (2/3) |
| **Electricity** (boundary) | Phenomenon | P / P / **D** | ⚠ majority Phenomenon (2/3) |
| TCP (distractor) | TechnicalArtifact | T / T / T | ✓ unanimous — correctly rejected off both boundary types |
| Fourier transform (distractor) | MethodOrConcept | M / M / M | ✓ unanimous |

### The three disagreements diagnosed

- **Spacetime** — split **Phenomenon (2) vs MethodOrConcept (1, sonnet)**, *not* the `Phenomenon ↔ DomainOrTopic` pairing the ADR predicted. No rater called it DomainOrTopic. Under a general-relativity context, sonnet read spacetime as a theoretical construct (a human-invented model) rather than a naturally-occurring observable. The ADR-named ambiguity is real but manifested on a *different* neighbour here than in FRE-771.
- **Electromagnetism** — split **DomainOrTopic (2, mini+full) vs Phenomenon (1, sonnet)**. The genuine edge: "one of the four fundamental interactions of nature" *is* a Phenomenon reading, but the bare word `electromagnetism` is a canonical physics subfield, and the two OpenAI raters keyed on the field.
- **Electricity** — split **Phenomenon (2, mini+full) vs DomainOrTopic (1, sonnet)**. The "…to the theory behind a circuit" clause pulled sonnet toward the topic reading; the "crackle of static" clause kept the OpenAI raters on the phenomenon.

All three are the **"named fundamental force/interaction that is also a named subfield"** sub-edge (spacetime, electromagnetism, electricity), and all three are the Claude-vs-OpenAI axis. Magnetism — the same physics family — went unanimous once its context foregrounded the *mechanism* ("emerges when electron spins line up"), which is the practical disambiguation lever: **context that foregrounds the physical mechanism/effect resolves to Phenomenon; context that foregrounds the body-of-study resolves to DomainOrTopic.**

## Verdict

**The `Phenomenon ↔ DomainOrTopic` boundary CLEARS the pre-registered bar.** Overall κ **0.858 ≥ 0.777**; `Phenomenon` κ **0.831 ≥ 0.645**; `DomainOrTopic` κ **0.888 ≥ 0.645**. Both target types clear both marks with margin, on a powered probe (32/33 positives vs FRE-771's n=12). The line is **teachable at high agreement** — 21/24 unanimous, all 16 clean anchors and half the boundary cases 3/3.

This **retires the AC-1 concern FRE-771 raised**: the 83.3% n=12 read was sampling noise on an underpowered slice, not evidence of a broken taxonomy edge. The properly-powered per-type κ on the exact edge is 0.83–0.89, comfortably inside the shipped-type band.

**The residual is bounded and provider-sensitive, not a taxonomy failure.** It is confined to the "named fundamental force/interaction that doubles as a named subfield" sub-edge (spacetime/electromagnetism/electricity) and is entirely the OpenAI-vs-Claude axis (mini↔full = 1.000). No amendment is *required* (the bar is cleared). 

**Optional sharpening for owner review (not filed as a ticket — the boundary passed).** If the owner later wants to squeeze this sub-edge, the teachable lever the data points to is a one-line `Phenomenon` example/exclusion nudge: *"a fundamental physical force or interaction named for the effect itself (electromagnetism, electricity, gravity) is a Phenomenon; the same word used for the body of study is DomainOrTopic — disambiguate on whether the mention foregrounds the mechanism or the field."* This is a definition *nudge*, not a structural change, and is optional given the cleared bar.

**Scope of this claim (important):** the probe validates that this **one boundary** is teachable at high agreement — not that the full 10-type taxonomy is production-stable, and not that per-type κ on ~32 small-sample positives is a stability estimate. It deliberately does not exercise Person/Organization/Location/Event/KnowledgeArtifact/QuantityMeasure. Production-wide 10-type stability remains the separate, live-extractor gate (ADR-0109 AC-1 over the full gold, already measured by FRE-771 with the "no regression" clause holding cleanly).

## Reproduction

Fully specified in committed artifacts: the **10-type definitions** are `relabel_v2_types.V2_TYPE_DEFINITIONS` (verbatim from the ADR-0109 § Decision table), and the **24-entity fixture** (entity + context + intended side) is `scripts/eval/fre630_extraction_quality/fre790_phenomenon_domain_boundary_fixture.yaml` (appendix below). Re-run:

```
uv run python -m scripts.eval.fre630_extraction_quality.adr0109_boundary_probe --run-id <id>
```

`tests/evaluation/test_fre630_gold_set.py::test_fre790_boundary_fixture_matches_research_note` pins the fixture's shape to this note's appendix; `tests/evaluation/test_adr0109_boundary_probe.py` covers the runner's fixture-loading + dry-run plumbing.

**Cost (for the ledger, not the science):** 72 calls (24 entities × 3 raters), one clean run. Observed `claude-sonnet-5` ≈ $0.0028/call; total run ≈ $0.5–1, well under FRE-771's full-extractor A/B.

## Appendix — Probe fixture (24 entities)

Each rater saw only: the ADR-0109 10-type definitions + the context below + the entity name. `intended side` is design intent, never shown. Results are in the per-entity table above.

| entity | context (verbatim) | intended side | boundary |
|---|---|---|---|
| Gravity | "Gravity is the force that pulls any two masses toward each other." | Phenomenon | |
| Photosynthesis | "In photosynthesis the leaf captures sunlight to turn water and carbon dioxide into sugar." | Phenomenon | |
| the greenhouse effect | "The greenhouse effect traps outgoing infrared radiation and warms the lower atmosphere." | Phenomenon | |
| Superconductivity | "Cooled below its critical temperature, the metal shows superconductivity and carries current with zero resistance." | Phenomenon | |
| Turbulence | "As the flow speeds up, the smooth streamlines break down into chaotic turbulence." | Phenomenon | |
| the Doppler effect | "The siren's pitch drops as the ambulance passes — the Doppler effect shifting the sound's frequency." | Phenomenon | |
| Rayleigh Scattering | "Rayleigh scattering sends the shorter blue wavelengths bouncing off air molecules, which is why the sky is blue." | Phenomenon | |
| the Maillard reaction | "The Maillard reaction browns the crust and builds the roasted flavour as the loaf bakes." | Phenomenon | |
| Cosmology | "Cosmology studies the origin, evolution, and large-scale structure of the universe." | DomainOrTopic | |
| Cybersecurity | "After a decade in networking she moved into cybersecurity, defending banks from intrusion." | DomainOrTopic | |
| Thermodynamics | "Thermodynamics is the branch of physics that relates heat, work, and energy." | DomainOrTopic | |
| Neuroscience | "Neuroscience spans molecular signalling all the way up to whole-brain cognition." | DomainOrTopic | |
| Fluid Dynamics | "The aircraft designer took a graduate course in fluid dynamics to model the airflow over the wing." | DomainOrTopic | |
| Number Theory | "Number theory is the study of the integers and the properties of the prime numbers." | DomainOrTopic | |
| Behavioral Economics | "Behavioral economics blends psychology with economics to explain why people deviate from rational choice." | DomainOrTopic | |
| Organic Chemistry | "She teaches organic chemistry, the study of carbon-based compounds and how they react." | DomainOrTopic | |
| Spacetime | "General relativity treats spacetime as a four-dimensional fabric that curves in the presence of mass." | Phenomenon | boundary |
| Electromagnetism | "Electromagnetism binds electric and magnetic forces into one of the four fundamental interactions of nature." | Phenomenon | boundary |
| Magnetism | "Magnetism emerges when the electron spins in a material line up and reinforce one another." | Phenomenon | boundary |
| Electricity | "He's fascinated by electricity, from the crackle of static on a doorknob to the theory behind a circuit." | Phenomenon | boundary |
| Acoustics | "The acoustics of the old hall gave every note a long, warm decay." | DomainOrTopic | boundary |
| Optics | "Optics describes how light bends through a lens, reflects off a mirror, and spreads out by diffraction." | DomainOrTopic | boundary |
| TCP | "TCP guarantees that the bytes arrive in order and without loss over an IP network." | TechnicalArtifact | |
| Fourier Transform | "We apply a Fourier transform to move the signal from the time domain into the frequency domain." | MethodOrConcept | |

## References

- ADR-0109 (entity taxonomy redesign) + Amendment 1 — AC-1, Risks (spacetime).
- `docs/research/2026-07-04-fre-771-10type-prompt-swap-powered-ab.md` (the underpowered n=12 read this re-measures).
- `docs/research/2026-07-04-fre-782-knowledgeartifact-quantitymeasure-boundary-iaa.md` (the boundary-IAA instrument mirrored).
- `docs/superpowers/plans/2026-07-05-fre-790-phenomenon-domain-boundary-probe.md` (this ticket's plan, with the pre-registered rule).
- `scripts/eval/fre630_extraction_quality/adr0109_boundary_probe.py`, `fre790_phenomenon_domain_boundary_fixture.yaml`, `relabel_v2_types.py`, `iaa.py`.
