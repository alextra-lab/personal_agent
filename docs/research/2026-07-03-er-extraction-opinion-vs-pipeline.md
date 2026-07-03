# ER Extraction — Opinion vs. Pipeline, reconciled with the FRE-630 / ADR-0109 findings

**Date:** 2026-07-03
**Author posture:** review + reconciliation (no production change). This doc compares an external
opinion on entity–relation (ER) extraction against Seshat's live pipeline, then reconciles that
comparison against build1's two measured findings landed the same day:
[FRE-630 extraction-quality benchmark + SOTA survey](2026-07-03-fre-630-extraction-quality-sota.md)
and [ADR-0109 entity-taxonomy redesign](../architecture_decisions/ADR-0109-entity-taxonomy-redesign.md).
**Related:** ADR-0087 (measurement-first recall quality) · ADR-0097/0098 (knowledge-class taxonomy &
substrate) · ADR-0106 (System/User boundary) · the FRE-636 taxonomy spike.

> **Privacy note.** No verbatim transcripts, PII, or deployment identifiers. Failure modes are
> grounded in the live corpus but described generically.

---

## TL;DR

1. **The opinion's thesis — "better extraction is task design, not model size" — is now *proved*, not
   just plausible, on our own data.** FRE-766's controlled matrix (4 model families + a purpose-built
   encoder converging at a ~0.86–0.89 entity-type ceiling) is direct evidence: convergent failure across
   independent architectures is a *structural* ceiling, not a capacity one.
2. **The one place the study was too generous was the schema.** The study rated our schema "clear/strong
   ✅." That is true *mechanically* (a closed, enumerated, controlled vocabulary — real SOTA hygiene) but
   false *by design*: ADR-0109 shows the type **boundaries** were never derived and are ill-posed
   (`Concept`/`Technology`/`Topic` are unanswerable). Presence ≠ good design. Both statements hold; the
   study measured the first and missed the second.
3. **The taxonomy fix (ADR-0109 V2) re-sequences every surviving lever.** Ontology enforcement (FRE-760)
   survives but must enforce **V2**, not V1, or it hardens a schema we are replacing. Exemplar retrieval
   is low-value for *typing* until V2 exists (this predicts why FRE-759 failed). Decomposition is
   orthogonal (a precision lever) and survives as a measure-first candidate. Fine-tuning stays
   deprioritized — and ADR-0109 independently blocks it upstream on the gold set.

---

## Part 1 — The opinion (external, summarized)

The opinion answers "do we need a stronger model for ER extraction?" with: no — quality comes from the
work *around* the model. Its levers:

1. **Clear schema / ontology** — enumerate entity types, relation labels, and constraints; give them to
   the model explicitly.
2. **Structured prompting + negative examples** — strict JSON triplets, examples of what *not* to extract.
3. **Decomposition** — NER → entity-pair filter → relation-classify, rather than a one-shot triplet dump.
4. **Joint entity+relation learning** — when fine-tuning, couple the stages to cut error propagation.
5. **Domain fine-tuning / instruction tuning** — small supervised set on your schema; higher ROI than a
   bigger base model.
6. **Retrieval of in-context exemplars** — "recall, retrieve, reason"; nearest prior demonstrations.
7. **Refinement / post-processing** — a symbolic layer that dedups, enforces ontology constraints,
   discards invalid triples, reconciles conflicts.

Its verdict: larger ≠ better if the pipeline is weak; specialize and add structure before scaling capacity.

---

## Part 2 — The opinion vs. our pipeline (the original study)

**Stale premise, corrected.** The opinion assumes a local mid-size model on Apple Silicon. Reality:
extraction runs on **hosted `gpt-5.4-mini`** in prod (`config/models.cloud.yaml:49`), `gpt-5.4-nano`
local (`config/models.yaml:36`) — we moved off local Qwen after the FRE-365 cache diagnostic. So
"start with a 7–14B local model and fine-tune it" is advice we've half-taken: we kept the model small and
cheap and invested in the pipeline instead.

**Scorecard** (grounded in `second_brain/entity_extraction.py`, `memory/service.py`, `memory/dedup.py`,
`second_brain/quality_monitor.py`):

| # | Opinion's lever | Us | Evidence |
|---|---|---|---|
| 1 | Clear schema / ontology | ✅ present *(see Part 3.2 — presence ≠ well-posed)* | 7 entity + 6 relation types enumerated + knowledge classes — `entity_extraction.py:35–67` |
| 2 | Structured output + negative examples | ✅ have it | JSON-only, fence-stripping fallback, fail-open (`:570–599`); negatives at `:101–135` |
| 3 | Decomposition (NER → pair → classify) | ❌ gap | single generative call dumps entities+relations+stances+claims (`:463–466`) |
| 4 | Joint entity+relation *learning* | ➖ N/A | zero-shot prompting, no fine-tuning |
| 5 | Domain fine-tuning | ❌ not done | no supervised tuning on our schema |
| 6 | In-context exemplar retrieval | ❌ gap | static template, only user/assistant slots (`:32–204`) |
| 7 | Refinement / symbolic layer | ✅ ahead of it | vector dedup (`dedup.py:50–111`); bitemporal claim adjudication + stance supersession + confidence-gated living descriptions (`service.py:1250–1481`); standing quality monitor |

**The recall-vs-precision nuance.** The opinion's flagship lever (#3 decomposition) targets *precision* —
cutting spurious triples from unrelated pairs. But our historically measured failure (FRE-636 spike) was
*recall* of specific knowledge classes: the single-call extractor flattened Stance and dropped Personal
facts. We fixed that recall problem the way the opinion would prescribe — task design, not a bigger model
— by enriching the *same call's* schema with explicit `class`/`stances[]`/`claims[]` slots (FRE-637/711/725,
live). What we had *not* taken is decomposition, a precision play.

**Original ranked levers** (this is what the reconciliation in Part 3 re-sequences):

1. Ontology **constraint enforcement** as a post-step (called cheap/high-value).
2. In-context **exemplar retrieval** for the lossy classes.
3. **Decomposition** — scoped A/B, measure-first.
4. **Fine-tuning** — deprioritize.

---

## Part 3 — Reconciliation with build1's findings (FRE-630 + ADR-0109)

### 3.1 Thesis alignment — convergent failure is *direct proof*, not just support

The study argued the "task design, not model size" thesis from the FRE-636 spike (a single observed
failure) and from the strategic choice to keep the model small. FRE-766 turns that argument into a
**controlled experiment**, and the result is the strongest possible confirmation:

| cell | entity_type_accuracy | reasoning tokens |
|---|---:|---:|
| mini-none (prod) | 0.76 | 0 |
| mini-medium / high | 0.78 / 0.78 | 973 / 3175 |
| full-medium / high | 0.85 / 0.85 | 568 / 1443 |
| **sonnet5-adaptive** | **0.89** | 0 |

Swapping across `gpt-5.4-mini`, `gpt-5.4`, `claude-sonnet-5`, **and a purpose-built encoder (GLiNER)**
does not break a ~0.86–0.89 ceiling, and all four architectures **mis-type the same entities** (`trie`,
`retrieval-augmented generation`, `behavioral economics`, `game theory`). When capable, unrelated models
fail in the *same place*, the ceiling is structural, not capacity. Capability moved the number a little
(sonnet best, with *zero* reasoning tokens); reasoning depth moved it negatively. **This is the study's
thesis, proved: parameter count and reasoning budget are not the lever.** State it plainly — the opinion
was right, and we now have the benchmark to say so with evidence rather than analogy.

### 3.2 The schema tension — resolved: enumerated ≠ well-posed

The study rated schema "clear/strong ✅ (#1)." ADR-0109 calls the taxonomy **ill-posed** and names it the
root cause. Both are true; they describe different properties:

- **What the study scored (mechanically present):** a *closed, enumerated, controlled* vocabulary handed
  to the model in-prompt. This is real SOTA hygiene — closed-vocabulary extraction beats open generation,
  and it is exactly why our hallucination rate is 0.00 and forbidden-edge rate is 0.02. The *hygiene* is
  genuinely strong.
- **What ADR-0109 exposes (design quality absent):** the *values* of that enum have fuzzy boundaries that
  **nobody ever drew on purpose** — the entity-type block traces to the initial commit with no ADR ever
  deriving it. "Is a `trie` a `Technology` or a `Concept`?" is unanswerable, so four independent models
  disagree on it identically.

The reconciliation: the opinion's recommendation #1 has two sub-parts — *have a controlled schema* and
*have well-drawn type boundaries*. We nailed the first and missed the second, and the study's ✅ conflated
them. A closed vocabulary with ill-posed boundaries is a well-enforced bad schema: the enforcement works,
the thing being enforced is wrong. ADR-0109 V2 (8-type, GoLLIE-style inclusion **and exclusion**
definitions: `TechnicalArtifact / MethodOrConcept / DomainOrTopic / Phenomenon / …`) is the missing
second sub-part — the *first principled derivation* of the type vocabulary. The knowledge-**class** axis
(World/Personal/System/Stance) is the one part that *was* deliberately designed and is unchanged and
orthogonal.

### 3.3 Re-pointing the surviving levers to V2

The study's ranked levers survive, but the taxonomy fix re-sequences them — and one of them was already
tested-and-failed for exactly the reason ADR-0109 names.

| Study lever | Reconciled disposition | Why |
|---|---|---|
| **#1 Ontology enforcement** (→ **FRE-760** rel-type write gate, in flight) | **Survives — but must enforce V2, not V1.** Lower urgency than the study implied. | Enforcing a schema we're about to replace hardens the wrong vocabulary. FRE-630 also measures off-vocab edges at only 2% → defense-in-depth, not a live fire. Sequence behind (or forward-compatible with) ADR-0109 V2. |
| **#2 Exemplar retrieval** | **Low-value until V2 exists.** | **This predicts why FRE-759 failed.** Static type/claim exemplars regressed relationship-typing and moved entity-type 0.76→0.77 (nil). You cannot demonstrate the "correct" side of a boundary that does not exist. The claim-recall sliver (exemplars 0.25→0.42, short of 0.8) hands off to DSPy-compiled extraction, not hand-drafted retrieval. |
| **#3 Decomposition** | **Orthogonal (precision) — survives as a measure-first candidate; priority drops.** | It targets spurious/redundant triples, but the benchmark's precision-side traps are already green (hallucination 0.00, forbidden-edge 0.02, dedup 1.00). It attacks a problem we mostly do not have. Keep it measure-gated; do not build on assumption. |
| **#4 Fine-tuning** | **Stays deprioritized — reinforced.** | ADR-0109 independently blocks it upstream: the gold set (36 single-author, ambiguous cases) is too small to fine-tune and too ambiguous to certify a ceiling. Fix the schema first; the schema fix changes the labels a tuner would learn. |

### 3.4 What this leaves

The taxonomy is the root cause and the near-free, high-leverage fix; every prompt/model/retrieval lever
either failed (FRE-758/759/766) or is now sequenced *behind* V2. The correct ordering is: **land ADR-0109
V2 (re-labeled gold + 8-type GoLLIE prompt + powered A/B + KG migration) first**, then re-point ontology
enforcement (FRE-760) at V2, then re-evaluate exemplar/DSPy work against the re-baselined benchmark.
Decomposition remains a one-line, measure-gated candidate — not a ticket — until the precision traps show
a problem the current pipeline does not currently have.

---

## Levers after the taxonomy fix (sequenced)

1. **ADR-0109 V2** — the root-cause fix. Everything below sequences behind it.
2. **Ontology enforcement (FRE-760)** — re-target at V2 vocab before it merges; low urgency (2% off-vocab).
3. **DSPy-compiled extraction** — the principled successor to hand-drafted exemplars; compile against the
   FRE-630 `score_case` metric, holding the near-ideal metrics as constraints (the collateral-damage
   problem FRE-759 exhibited). Owns the claim-recall gap (0.33).
4. **Fine-tuning / purpose-built model** — deprioritized; gated on a larger, second-labeled gold set.
5. **Decomposition (NER → pair → classify)** — *measure-gated candidate only* (no ticket): revisit **iff**
   a post-V2 precision measurement (redundant-relationship-pair / orphan-entity rate) shows spurious-triple
   noise the current single-call path does not currently exhibit.

---

## References

- [FRE-630 — KG extraction quality → SOTA](2026-07-03-fre-630-extraction-quality-sota.md) (baseline,
  instrument, SOTA survey; FRE-758/759/766 lever A/Bs).
- [ADR-0109 — Entity & Relationship Taxonomy V1→V2](../architecture_decisions/ADR-0109-entity-taxonomy-redesign.md).
- [ADR-0097](../architecture_decisions/ADR-0097-ingested-knowledge-taxonomy.md) /
  [ADR-0098](../architecture_decisions/ADR-0098-memory-substrate-and-lifecycle-architecture.md) — knowledge-class taxonomy & substrate (orthogonal, unchanged).
- [FRE-636 taxonomy spike](2026-06-27-fre-636-taxonomy-validation.md) — the "extraction is the binding constraint" finding.
- Sainz et al. (2024), *GoLLIE*, ICLR — inclusion+exclusion definitions override model priors.
- Edge et al. (2024), *GraphRAG*, arXiv:2404.16130; Zaratiana et al. (2024), *GLiNER*, arXiv:2311.08526.
