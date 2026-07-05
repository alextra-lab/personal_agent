# FRE-797 — ADR-0109 Phenomenon ↔ DomainOrTopic definition sharpening: decision (no change)

**Date:** 2026-07-05
**Backing:** [ADR-0109 Entity & Relationship Taxonomy Redesign](../architecture_decisions/ADR-0109-entity-taxonomy-redesign.md) — Amendment 1, AC-1.
**Predecessor:** [FRE-790 boundary IAA note](2026-07-05-fre-790-phenomenon-domain-boundary-iaa.md) (the cleared probe this optionally refines).
**Outcome:** **Do not sharpen.** No live-extractor change ships. Recorded here per the ticket's Phase-1 fail-closed path (a valid, complete outcome — no busywork change).

---

## The question

FRE-790 measured the ADR-0109 `Phenomenon ↔ DomainOrTopic` boundary with a blind 3-rater IAA probe
(gpt-5.4-mini, gpt-5.4, claude-sonnet-5) over a 24-entity fixture. The boundary **cleared** its
pre-registered bar (overall Fleiss κ **0.858**; Phenomenon **0.831**; DomainOrTopic **0.888** — all
above the FRE-770 floors), and the note concluded *"no amendment required."* It flagged one bounded
residual and offered — *for owner review, explicitly not filed as a ticket* — a one-line definition
"sharpening" clause. FRE-797 is the owner electing to evaluate that clause: does it close the
residual, tested via an A/B on the same fixture, fail-closing to no change?

Candidate clause (from the FRE-790 note):

> *"A fundamental physical force or interaction named for the effect itself (electromagnetism,
> electricity, gravity) is a Phenomenon; the same word used for the body of study that investigates
> it is DomainOrTopic — disambiguate on whether the mention foregrounds the mechanism or the field."*

## Phase 1 — review (the decision gate)

The ticket's Phase 1 is a genuine gate: *"If the review concludes the sharpening is not worth the
regression risk, stop here and record that verdict."* The review read the FRE-790 note, the A/B
instrument (`relabel_v2_types.py`, `adr0109_boundary_probe.py`,
`fre790_phenomenon_domain_boundary_fixture.yaml`), the live extractor definitions
(`entity_extraction.py:91-99`, verbatim-equal to `relabel_v2_types.V2_TYPE_DEFINITIONS`),
`taxonomy.py`, and the drift-guard `tests/evaluation/test_entity_extraction_taxonomy.py`.

Four findings drove the decision:

1. **The boundary already clears, with margin.** Per-type κ on the exact edge is 0.83–0.89 on a
   *powered* probe (32/33 positives), comfortably inside the shipped-type band. There is no defect
   to fix — FRE-790 retired the AC-1 concern that prompted it.

2. **The residual is a provable cross-provider artifact, not a taxonomy weakness.** Every one of the
   3 disagreements (spacetime, electromagnetism, electricity) is the lone Claude rater dissenting
   from the two OpenAI raters, whose mutual agreement is **perfect (mini↔full = 1.000)**. A
   definition edit is the wrong instrument for this: it cannot "fix" a boundary that isn't broken;
   at most it shifts *where* one provider's reading lands. The measured quantity — inter-rater
   κ — can only move by nudging that one model on 3 known cases.

3. **The measurement would be underpowered.** With 3 disputed cases across 3 raters, a single label
   flip dominates the κ delta. Re-running the same 24-item fixture with the two definitions swapped
   is a clean prompt-delta test but is hostage to those three items — it measures whether one model
   changes its reading of three ambiguous words under a slightly longer definition, not a stable
   taxonomy-clarity effect. A "win" at this n would be indistinguishable from noise.

4. **The naive clause carries an asymmetric regression vector, into a sensitive surface.** Naming
   *electromagnetism* and *electricity* as canonical **Phenomenon examples** in the live prompt
   risks pulling a genuine field-of-study mention ("a graduate course in electromagnetism") into
   Phenomenon — the exact failure the ticket warns of. Phase 3 would touch the live extraction
   prompt, a master-gated coordinated-deploy surface (the same one FRE-771 swapped). Trading a
   bounded, already-passing, provider-sensitive edge for a new regression vector on a hot surface is
   high blast-radius for low reward.

### Codex second opinion (approach review)

An independent Codex pass on the plan concurred and sharpened point 2–3: *"Arm B can only improve by
moving the Claude rater or by perturbing OpenAI; that is a provider-behavior test, not a general IAA
validity test… n=24 / 3 raters… is not powered to detect a small sharpening effect; one or two label
flips will dominate the conclusion… worth running only if the owner wants a cheap sanity check, not
because the existing evidence demands it."* It agreed a *symmetric* clause (mechanism→Phenomenon,
field→DomainOrTopic, without hardcoding electromagnetism/electricity as Phenomenon exemplars) is the
safer wording if one were to proceed, but noted symmetry may itself wash out any measurable signal.

## Decision

**Do not sharpen. Ship no change to the live extractor.** The boundary clears; the residual is a
provable cross-provider artifact that a definition edit cannot structurally resolve; the confirming
A/B would be underpowered on 3 known cases; and the candidate clause's live-prompt regression risk
outweighs a bounded, optional refinement of an already-passing edge. The FRE-790 disambiguation
lever — *context that foregrounds the physical mechanism resolves to Phenomenon; context that
foregrounds the body of study resolves to DomainOrTopic* — already lives in the note as guidance for
any future owner-initiated revisit, and is preserved here. Should the residual ever be revisited, the
right instrument is a broader multi-provider rater panel or a larger sub-edge fixture (to move the
measurement out of noise), not a one-line clause validated on n=3 disputed items.

**One-paragraph "why not" (ticket AC):** The Phenomenon↔DomainOrTopic boundary already clears its
agreement bar with margin, and FRE-790 showed the only residual disagreement is entirely the
OpenAI-vs-Claude provider axis on three dual-natured words — not a contested taxonomy line. A
definition tweak cannot fix a provider-specific reading tendency; the A/B that would test it is
underpowered on three cases (a single flip dominates the result); and the candidate clause would add
a genuine field→Phenomenon regression vector to the master-gated live extraction prompt. The
sharpening is not worth the regression risk on an already-passing edge, so nothing ships.

## What this does not change

- The FRE-790 fixture, instrument, and cleared verdict stand unchanged.
- The live extraction prompt and `taxonomy.py` are untouched; the ADR-0109 10-type drift-guard
  remains green.
- No follow-up ticket is filed — filing "revisit later" work would contradict the decision and the
  boundary passes as-is.

## References

- [FRE-790 boundary IAA note](2026-07-05-fre-790-phenomenon-domain-boundary-iaa.md) — the cleared
  probe (κ 0.858) and the optional clause this evaluates.
- [ADR-0109](../architecture_decisions/ADR-0109-entity-taxonomy-redesign.md) — Amendment 1, AC-1, Risks (spacetime).
- Instrument: `scripts/eval/fre630_extraction_quality/{relabel_v2_types.py, adr0109_boundary_probe.py, fre790_phenomenon_domain_boundary_fixture.yaml}`.
- Live surface (unchanged): `src/personal_agent/second_brain/entity_extraction.py`, `taxonomy.py`; drift-guard `tests/evaluation/test_entity_extraction_taxonomy.py`.
- Plan: `docs/superpowers/plans/2026-07-05-fre-797-phenomenon-domain-sharpening.md`.
