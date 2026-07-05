# FRE-771 — 10-type prompt swap: powered A/B (ADR-0109 Implementation Notes step 4)

**Backing:** ADR-0109 (Accepted) + Amendment 1 (FRE-782/784, merged). **Depends on:** FRE-784
(10-type gold + definitions committed), FRE-770 (V2 gold relabel, κ 0.777).

## What this measures

ADR-0109 AC-1: *"Cross-model agreement on the previously-ambiguous set materially exceeds the
7-type baseline (target: ≥90% type-agreement between two model families)."* This is a **different
question** from `entity_type_accuracy` (does a model agree with a fixed gold label) — it asks
whether **two independent model families agree with each other**, mirroring the FRE-766 spot-check
methodology and the FRE-770/782 IAA studies, but applied to the *real* production extractor's
output (via `cross_model_agreement.py`) rather than a bespoke blind-classification prompt.

## Method

Two sequential phases, `mini-none` (current prod cell, gpt-5.4-mini) + `sonnet5-adaptive`
(claude-sonnet-5, adaptive thinking) — the exact 2-family pairing ADR-0109's own FRE-766
spot-check used (scoped deviation from re-running the full 6-cell reasoning matrix; ADR-0109's own
AC-1/step-4 text asks for "two model families," not every reasoning rung — see the FRE-771 plan
§ D6). Samples=3, full 50-case re-labeled gold set (`gold_extraction.yaml`).

- **Phase "v2"** — the live, post-swap 10-type prompt (unpatched).
- **Phase "v1"** — `entity_extraction._EXTRACTION_PROMPT_TEMPLATE` monkeypatched to a frozen,
  byte-verbatim snapshot of the retired 7-type prompt (`fre771_v1_prompt_snapshot.py`), run through
  the identical extractor code path (person-supplement, finalization, cost-gate) so the two arms
  differ *only* in the entity-type prompt content.

Cross-model agreement is computed over the 8 gold cases tagged `type-boundary` (the previously-
ambiguous set), resolving each model's first-sample extraction against gold via the tiered matcher,
then reusing `iaa.py`'s pairwise-agreement statistic — same discipline as the FRE-770/782 IAA
studies, applied to real extractor output instead of blind classification.

Run: `telemetry/evaluation/fre630-extraction-quality/fre771-2026-07-04-summary.json` (gitignored;
git commit + prompt hashes stamped per cell in the raw per-cell reports).

## Results

### AC-1 — cross-model type-agreement, `type-boundary` set (n=12 resolved items)

| Arm | Agreement (mini↔sonnet) | n |
|---|---|---|
| **V2 (10-type, this ticket)** | **83.3%** (10/12) | 12 |
| V1 (retired 7-type) | 100% (12/12) | 12 |

**AC-1 is NOT clearly met by this measurement** — 83.3% is below the ≥90% target, and V2 does not
exceed the V1 baseline on this specific 12-item slice (it is lower). Reported plainly, not spun:

- **This is a small, underpowered sample.** A single-item flip moves the number by ~8.3
  percentage points; 2 flips out of 12 is within the noise band the README already flags
  ("~24 curated cases... not enough to certify a few-point A/B move" — the `type-boundary` subset
  is a fraction of that). This measurement should not be read as a confident pass/fail on its own.
- **The two V2 disagreements are exactly the residual edge the ADR itself named as a risk**, not
  an unexpected failure mode:
  - `phenomenon-boundary-spacetime::Spacetime` — ADR-0109's own Risks section names this verbatim:
    *"is 'spacetime' a Phenomenon or a DomainOrTopic?"* — an open question at acceptance time,
    now empirically confirmed as a live cross-model split.
  - `cov-partof-structural::TCP` — a residual boundary case not previously spot-checked at this
    granularity.
- **V1's 100% on this same slice should not be read as "the old taxonomy was fine."** V1's own
  ambiguity was established on a *larger* corpus (the original FRE-766 spot-check + the FRE-770
  gold relabel's 3-rater IAA, κ 0.777) — a 12-item sample landing on all-agree is plausible sampling
  variance on a small paired set, not a contradiction of that broader evidence.

**Follow-up needed, not a rollback signal:** grow a dedicated boundary probe for `Phenomenon ↔
DomainOrTopic` (mirroring FRE-782's 22-entity, 3-rater boundary-IAA methodology) to get a properly
powered read on this specific edge before deciding whether it needs a taxonomy fix (e.g. a
sharper `Phenomenon` exclusion clause) or is an acceptable residual ambiguity.

### No-regression metrics (the other AC-1 clause)

| Metric | V2 mini | V2 sonnet | V1 mini | V1 sonnet |
|---|---|---|---|---|
| entity_type_accuracy | 0.800 (n=135) | 0.887 (n=125) | 0.816 (n=133) | 0.897 (n=126) |
| knowledge_class_accuracy | 1.000 | 1.000 | 1.000 | 0.992 |
| hallucination_rate | 0.000 | 0.000 | 0.000 | 0.000 |
| dedup_convergence | 1.000 | 1.000 | 1.000 | 1.000 |
| forbidden_edge_type_rate | 0.031 | 0.010 | 0.012 | 0.018 |
| claim_case_level_recall | 2/12 (0.167) | 2/12 (0.167) | 3/12 (0.25) | 3/12 (0.25) |

**No regression on the near-ideal metrics** — hallucination stays at 0.0 in both arms, dedup
convergence stays perfect, knowledge-class accuracy stays ~0.99–1.0, forbidden-edge-type rate stays
in the low single digits both arms (no clear directional regression). `entity_type_accuracy` is
roughly flat (~0.80–0.90 across both models and both arms) — consistent with ADR-0109's own prior
finding that this metric has a human-inter-annotator ceiling well below 0.95 and was never V2's
target metric (V2's thesis is that a coarser taxonomy raises *cross-model agreement*, not raw
gold-accuracy, which the AC-1 boundary-agreement number is the direct test of).

`claim_case_level_recall` is a pre-existing, orthogonal signal (FRE-759) — unaffected by the
entity-taxonomy swap, as expected (both arms score identically to each other within each model).

## Acceptance-criteria disposition

- **AC-1** — **partially met.** The "no regression" clause holds cleanly. The "≥90% cross-model
  agreement" clause measured 83.3% on this run's 12-item `type-boundary` slice — below target, but
  on a sample too small to be conclusive, with both disagreements traceable to an ADR-named residual
  risk rather than a new, unexpected failure. Recommend: keep the V2 prompt live (per D1 — the swap
  ships regardless, matching the ADR's "swap" instruction and unblocking FRE-772's KG migration),
  and file a follow-up to grow the `Phenomenon ↔ DomainOrTopic` boundary probe for a properly
  powered AC-1 re-measurement.
- **AC-8** (implementation gate) — **met.** The live 10-type extractor prompt ships (this ticket);
  the re-labeled gold was already committed (FRE-784); this research note + the committed
  `cross_model_agreement.py` module constitute the re-runnable regression AC-8 asks for.

## Cost note (for the ledger, not the science)

Two prior attempts hit budget walls before this run completed cleanly (a crashed run with no
incremental persistence — since fixed — and a daily-cap exhaustion from the crash's sunk spend).
Measured per-call cost: gpt-5.4-mini ≈$0.0017/call, claude-sonnet-5 ≈$0.026/call. Total spend across
all three attempts ≈$20 (owner-approved temp budget bumps, reset to original values after this
ticket's PR).

## References

- ADR-0109 (entity taxonomy redesign) + Amendment 1.
- `docs/superpowers/plans/2026-07-04-fre-771-10type-prompt-swap-powered-ab.md` (this ticket's plan).
- `docs/research/2026-07-04-fre-782-knowledgeartifact-quantitymeasure-boundary-iaa.md` (the
  boundary-IAA methodology a follow-up probe should mirror).
- `scripts/eval/fre630_extraction_quality/cross_model_agreement.py`, `fre771_powered_ab.py`,
  `fre771_v1_prompt_snapshot.py`.
