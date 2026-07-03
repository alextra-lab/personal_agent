# FRE-630 — KG extraction quality → SOTA: survey, instrument, and baseline

**Date:** 2026-07-03
**Ticket:** [FRE-630](https://linear.app/frenchforest/issue/FRE-630) (Memory Recall Quality, parent FRE-435) · Tier-1:Opus
**Backing methodology:** [ADR-0087](../architecture_decisions/ADR-0087-memory-recall-quality-measurement.md) (measurement-first) ·
precedent: [FRE-636 taxonomy spike](2026-06-27-fre-636-taxonomy-validation.md), the FRE-435/489/670 recall harness
**Method posture:** measure-don't-assert (FRE-433/434). This Phase-1 PR changes **no production behaviour** —
it adds an eval instrument, this survey, and a baseline. Improvements are approved follow-up tickets.

> **Privacy note.** The gold set and this note are generalized. The failure modes are grounded in Seshat's
> own live corpus (FRE-630 evidence + the FRE-636 spike) but all cases are curated/paraphrased — no verbatim
> transcripts, no PII, no owner name / home / vehicle / deployment identifiers.

---

## TL;DR

1. **The ticket's failure catalog is largely pre-`gpt-5.4` and must be re-measured, not assumed.** The
   extractor was substantially rebuilt after FRE-630 was filed (2026-06-27): it now carries the 3-class
   knowledge taxonomy (World/Personal/System, FRE-637), emits **structured** stances + claims instead of
   flattening them into descriptions (FRE-638/711/725), uses a 6-type controlled relationship vocabulary,
   applies embedding-based dedup + a confidence-gated description-correction path (FRE-711/725), and runs on
   **gpt-5.4-mini** (cloud) / gpt-5.4-nano (local) — **not** the qwen3-8b the ticket's bugs came from.
2. **The decisive gap was a missing instrument.** Nothing scored whether extraction was *correct*; the
   FRE-435 harness measures *retrieval*, assuming extraction is right. Phase 1 builds the missing
   **pre-write extraction-quality benchmark** (`scripts/eval/fre630_extraction_quality/`).
3. **Baseline (gpt-5.4-mini, 81 extractions): the ticket's failures do not reproduce; two new weak spots do.**
   Hallucination 0.00, forbidden-edge 0.02, dedup 1.00, knowledge-class 1.00, stance-emission 1.00 — the
   qwen-era catalog is gone. The evidence-backed targets are instead **entity_type_accuracy 0.80** and
   **claim_emission_recall 0.33** (details in Part 4).
4. **The field's playbook for typed-edge correctness + canonicalization + hallucination control** (GraphRAG
   gleanings, HippoRAG/iText2KG entity resolution, schema-constrained relation extraction, extract-then-verify)
   maps cleanly onto concrete changes in our pipeline — each filed as its own flag→verify A/B follow-up.

---

## Part 1 — The current extractor (what already exists)

`second_brain/entity_extraction.py::extract_entities_and_relationships(user, assistant) -> dict` returns
`entities` / `relationships` / `stances` / `claims` / `summary`. The contract (system + user prompt, ~1.8k
lines of rules) already encodes much of the SOTA hygiene the ticket asked for:

| Surface | Current state |
|---|---|
| Entity types | 7 controlled: Person, Organization, Location, Technology, Concept, Event, Topic |
| Knowledge class | 3 controlled: World / Personal / System (FRE-637; the FRE-636 operational bucket) |
| Relationship vocab | 6 controlled: PART_OF, USES, RELATED_TO, SIMILAR_TO, CREATED_BY, LOCATED_IN |
| Stance / claim | Emitted as **structured** items (not flattened) — the FRE-636 fix |
| Canonicalization | Prompt rules ("Python" not "python") + embedding dedup at write (`memory/dedup.py`) |
| Description gate | Confidence-gated correction/enrichment (FRE-711/725) |
| Model | gpt-5.4-mini (cloud) / gpt-5.4-nano (local) via LiteLLM |

**Known structural gap (confirmed by code read):** the extracted `relationship_type` is written to Neo4j
**without validation** — an off-vocabulary edge type (the ticket's `LIVES_IN`) would pass straight through.
There is no write-time relationship-type gate. This is the single clearest candidate change.

---

## Part 2 — The instrument (Phase-1 deliverable)

A **pre-write** benchmark: it scores the extractor's *output dict* against a curated gold set — no Neo4j
write, so it isolates extraction quality from persistence. Pure, unit-tested core (matcher/metrics/scoring/
report) + a gold set + an I/O harness that runs the real extractor. Full design: the FRE-630 plan and the
package `README.md`.

**Metrics.** Entity P/R/F1 · entity-type accuracy · knowledge-class accuracy · relationship P/R ·
**relationship-type correctness** (right-type-given-right-endpoints) · hallucination rate ·
forbidden-edge-type rate · extraction-empty-fallback rate · dedup convergence · description-integrity proxy ·
stance/claim emission recall.

**The matcher matters (codex plan-review P0.2).** LLM extraction is non-deterministic and gold names won't
string-match; a naive exact match punishes valid paraphrases and cascades one name miss into false
relationship misses (edges are scored over endpoints). Each extracted name resolves to at most one gold
entity via **exact → alias → narrow-fuzzy** tiers; relationships score over the *resolved* endpoints. This
mirrors standard practice for open-schema KG-extraction evaluation, where canonicalization/equivalence — not
surface identity — is the unit of comparison.

**Interpretation caveat — sparse-gold precision dilution (measured, not hypothetical).** On the very first
live case the extractor emitted 5 valid entities where the gold labeled 1 (it also caught the basilica, the
mosaics, the city). Because the gold set labels *salient* entities rather than *every* entity, **raw entity
precision/F1 is diluted on sparsely-labeled cases and is advisory only.** The trustworthy signals are
**recall** (did it get the entities we care about?) and the **trap-based** metrics (hallucination rate,
forbidden-edge-type rate, relationship-type correctness) which are precision-side but do not punish
correct-but-unlabeled extractions. This is the standard incomplete-gold problem in open information
extraction; a closed-world / exhaustively-labeled subset is the follow-up if a hard precision number is
needed.

---

## Part 3 — SOTA survey

What the field does for the three things the ticket cares about — **typed-edge correctness**,
**canonicalization/entity-resolution**, and **hallucination control** — in LLM-driven KG construction.

### 3.1 LLM graph construction pipelines
- **GraphRAG** (Edge et al., Microsoft, 2024, arXiv:2404.16130). LLM extracts *element instances* (entities,
  relationships, and **claims**) under a typed prompt, then does community detection (Leiden) and
  hierarchical summarization. Two transferable ideas: (a) **"gleanings"** — multiple extraction rounds where
  the model is asked "what did you miss?", trading tokens for **recall**; (b) treating **claims** as
  first-class extraction targets (Seshat already does, via `claims`).
- **HippoRAG / HippoRAG 2** (Gutiérrez et al., 2024/2025, arXiv:2405.14831, arXiv:2502.14802). OpenIE triple
  extraction + Personalized PageRank over the KG, with **synonymy edges** added between near-duplicate nodes
  as the entity-resolution mechanism. Transferable: alias/synonym linking as an explicit graph operation
  rather than a one-shot dedup.
- **iText2KG** (Lairgi et al., 2024, arXiv:2409.03284). **Incremental** KG construction with distinct
  entity/relation modules that resolve each new item against the existing graph by embedding similarity
  before insertion — precisely the dedup problem, done at construction time and zero-shot.

### 3.2 Typed relation extraction (edge-type correctness)
- **Schema-constrained / closed-vocabulary extraction.** Constraining the model to a fixed relation set is
  the standard defense against invented edge types. **REBEL** (Huguet Cabot & Navigli, EMNLP 2021) casts
  relation extraction as constrained seq2seq over a known relation inventory. **GLiNER** (Zaratiana et al.,
  2024, arXiv:2311.08526) does constrained, label-conditioned extraction. Seshat already prompts a 6-type
  vocab; the missing half is **enforcement** — a validation gate that rejects/normalizes off-vocabulary
  edges at the write path (today absent).
- **Ontology grounding.** **Text2KGBench** (Mihindukulasooriya et al., ISWC 2023) evaluates ontology-driven
  LLM KG generation and shows type/relation adherence improves with in-context ontology exemplars — argues
  for a few-shot type/edge exemplar block in the prompt.

### 3.3 Entity resolution / canonicalization
- **CESI** (Vashishth et al., WWW 2018) — canonicalizing open KBs by learning entity/relation embeddings and
  clustering. Establishes the embedding-cluster approach Seshat's `dedup.py` already approximates.
- **Blocking + embedding match** is the standard scalable ER recipe; the transferable refinement is
  **model-proposed aliases** (ask the extractor for accepted surface forms) feeding the dedup key — which is
  also exactly what makes the *benchmark's* matcher fair, and could feed the *write-path* dedup.

### 3.4 Hallucination control
- **Extract-then-verify.** A second pass that checks each extracted triple against the source text
  (FActScore-style atomic verification; Min et al., EMNLP 2023, arXiv:2305.14251) filters unsupported facts.
  Candidate: a cheap verification/self-consistency vote for low-confidence entities/edges before write.
- **Self-consistency** (Wang et al., 2022, arXiv:2203.11171) — sample N extractions, keep items that recur —
  is the determinism/robustness lever this benchmark's `--samples N` is built to measure.
- **LLM-as-judge** for description quality (Zheng et al., 2023, arXiv:2306.05685) — replaces the current
  deterministic description-integrity proxy with a rubric-scored judge (a named follow-up).

### 3.5 How reference benchmarks are sized (why 24 is a seed set)
DocRED (Yao et al., 2019) ≈ 5k docs; SciERC, CrossRE, WebNLG, Text2KGBench all label hundreds–thousands of
examples. A 24-case curated set is a **high-signal regression/calibration probe**, not a statistically
powered benchmark — it detects large regressions and the named failure modes, but a few-point A/B needs
paired per-case deltas and, eventually, a larger set (codex plan-review P1.3).

---

## Part 4 — Baseline (current extractor: gpt-5.4-mini)

_Run: `baseline-20260703`, 27 cases × 3 samples (81 extractions, ≈ $0.43), model `gpt-5.4-mini`,
prompt_hash `fade5e71`, matcher 1.0, gold_schema 1.0. Curated summary — raw run is gitignored._

**Aggregate (mean±std over all sampled cases):**

| metric | value | reading |
|---|---:|---|
| entity_recall | **0.90**±0.23 | strong — gets the entities we label |
| entity_precision | 0.66±0.29 | *advisory* — diluted by sparse gold (extractor emits valid unlabeled entities) |
| entity_f1 | 0.75±0.21 | dominated by the precision dilution above |
| **entity_type_accuracy** | **0.80**±0.35 | ⚠️ weak spot — ~1 in 5 matched entities gets the wrong type |
| **knowledge_class_accuracy** | **1.00**±0.00 | ✅ World/Personal/System nailed — the FRE-636 mis-class concern does **not** reproduce |
| relationship_type_correctness | **0.89**±0.30 | strong — right endpoints → right edge type |
| relationship_precision / recall | 0.33 / 0.56 | noisy (sparse rel gold; different cases excluded per metric — not directly comparable) |
| **hallucination_rate** | **0.00**±0.00 | ✅ no `DISCUSES` / role-label / tool-name garbage |
| **forbidden_edge_type_rate** | **0.02**±0.08 | ✅ residence-vs-visit essentially does **not** reproduce |
| **dedup_convergence** | **1.00**±0.00 | ✅ case-variants always collapse |
| description_integrity | 0.99±0.07 | ✅ clean single sentences, no stance-flatten |
| **stance_emission_recall** | **1.00**±0.00 | ✅ stances emitted structurally — the FRE-636 flattening fix holds |
| **claim_emission_recall** | **0.33**±0.47 | ⚠️ weak spot — personal situational claims under-emitted |
| empty_fallback_rate | 0.00±0.00 | ✅ never fell back to the empty result |

**Headline: the ticket's qwen-era failure catalog largely does not reproduce on gpt-5.4-mini.** Residence
edges, hallucinated entities, flattened stances, and case-variant duplicates are all at or near their ideal
values. This is the measure-don't-assert payoff — proposing a `VISITED`/`TRAVELED_TO` vocabulary split (the
ticket's suggestion) would fix a bug the current extractor **does not have**.

**Two evidence-backed weak spots** (these, not the ticket's list, are what the improvement tickets target):
1. **entity_type_accuracy 0.80** — the extractor picks the wrong one of the 7 types ~20% of the time
   (Concept↔Topic and Concept↔Technology boundaries, from the per-case diffs). A prompt few-shot type
   exemplar block or an extract-then-verify type check is the candidate (SOTA §3.2/§3.4).
2. **claim_emission_recall 0.33** — structured personal *claims* are under-emitted (the FRE-636 "Personal
   dropped" finding partially persists for claims, even though *stances* are now perfect). A claim-focused
   prompt exemplar or a dedicated claim pass is the candidate.

**Per-tag caveat (sparse-gold, as predicted).** Low per-tag entity_f1 — csirt/security 0.31,
travel/residence-vs-visit 0.46, system/agent-arch ~0.58 — is **not** extraction failure: those cases label
1–2 salient entities while the extractor legitimately emits more (recall stays high, `hallucination_rate`
is 0 across every tag). Read these as "gold under-labeled," not "extractor wrong." The clean precision-side
signals are the trap metrics, which are green everywhere. Strong-gold tags (tech-stack, game-theory,
cosmology) sit at entity_f1 1.00.

> **Reading note.** `relationship_precision/recall/f1` each exclude a *different* set of cases (a metric is
> `None` on a vacuous denominator), so they are not directly comparable to each other; use
> `relationship_type_correctness` (0.89) as the clean edge-quality signal.

### 4.1 FRE-758 A/B — pinning temperature to 0.0 (2026-07-03)

_Run: `temp-pin-20260703`, 24 cases × 3 samples (72 extractions, ≈$0.09), model `gpt-5.4-mini`,
prompt_hash `fade5e717889` (unchanged), matcher 1.0, gold_schema 1.0. Same gold set, same prompt,
same matcher as the baseline above — **temperature is the only variable changed** (provider
default ~1.0 → pinned 0.0 via `config/models.cloud.yaml` + `entity_extraction.py`). Curated
summary — raw run is gitignored._

> **Correction to Part 4's header:** the baseline run stamp says "27 cases"; the gold set
> (`gold_extraction.yaml`) has always had 24 cases (confirmed by direct count, unchanged since the
> single commit that added it) — the "27" was a transcription error in the original write-up, not
> a real difference in what was benchmarked. Both runs cover the same 24-case set.

**Aggregate (mean±std), baseline (temp≈1.0, uncontrolled) vs. temp-pinned (0.0):**

| metric | baseline | temp-pin-0.0 | read |
|---|---:|---:|---|
| entity_recall | 0.90±0.23 | 0.91±0.20 | flat |
| entity_precision | 0.66±0.29 | 0.60±0.28 | flat (still precision-diluted, per §Part 4 caveat) |
| entity_f1 | 0.75±0.21 | 0.70±0.23 | flat |
| **entity_type_accuracy** | **0.80±0.35** | **0.78±0.36** | **flat — std unchanged, mean did not move toward 0.95** |
| knowledge_class_accuracy | 1.00±0.00 | 1.00±0.00 | unchanged |
| relationship_type_correctness | 0.89±0.30 | 0.91±0.28 | flat |
| hallucination_rate | 0.00±0.00 | 0.00±0.00 | unchanged |
| forbidden_edge_type_rate | 0.02±0.08 | 0.01±0.05 | flat |
| dedup_convergence | 1.00±0.00 | 1.00±0.00 | unchanged |
| description_integrity | 0.99±0.07 | 0.99±0.06 | unchanged |
| stance_emission_recall | 1.00±0.00 | 1.00±0.00 | unchanged |
| **claim_emission_recall** | **0.33±0.47** | **0.50±0.50** | **mean up, std also up (noisy — very few `claim`-tagged cases)** |
| empty_fallback_rate | 0.00±0.00 | 0.00±0.00 | unchanged |

**Result: FRE-758's acceptance criteria were NOT met.** Per-metric std bands did not collapse
(`entity_type_accuracy` std is unchanged at 0.35→0.36; `claim_emission_recall` std *increased*
0.47→0.50) and `entity_type_accuracy` did not move toward the ≥0.95 target (0.80→0.78, within
noise of flat). This is not a code defect — the fix is mechanically verified correct (unit test
asserts the cloud call forwards `temperature=0.0`; a config-loader test asserts both deployed
config files' `entity_extraction_role` resolves `temperature=0.0`; a live smoke test confirmed
`gpt-5.4-mini` accepts the override without error) — pinning temperature to 0.0 for this model
measurably does **not** reduce sample-to-sample extraction variance.

**Working hypothesis (not proven here, flagged for a follow-up):** `gpt-5.4-mini` is a
reasoning-tier model; the `temperature` parameter exposed via the Chat Completions API most likely
governs only final-answer token sampling, not the model's internal reasoning trace — if the
extraction non-determinism originates in reasoning-path variance rather than output-token
sampling, no exposed `temperature` value would collapse it. Reasoning-tier determinism (if OpenAI
exposes a `seed` parameter or similar for this model) is a candidate for a dedicated follow-up
ticket rather than folding into FRE-759 (prompt/DSPy — a different lever, unaffected by this
finding).

**Disposition:** the temperature pin ships regardless — it is correct, harmless (no regression on
any metric), and removes one axis of uncontrolled variance even though it wasn't the dominant one.
The non-determinism problem itself is now handed off with a ruled-out lever: FRE-759 (type/claim
prompt/DSPy A/B) proceeds as queued; a `seed`-parameter investigation is a candidate new ticket if
the owner wants to keep pulling this thread.

### 4.2 FRE-759 A/B — few-shot type/claim exemplars (2026-07-03)

_Two runs on the **FRE-759-expanded 36-case gold set** (12 keyed-claim cases, +2 entity-type-boundary
cases — the historical 24-case baseline no longer applies, per the codex P1.2 fresh-baseline rule).
Same model (`gpt-5.4-mini`), same gold, same matcher; **the flag-gated few-shot exemplar block is the
only variable** (`entity_extraction_fewshot_exemplars_enabled`). Flag-OFF `prompt_hash 8a1bdd119ca3`,
flag-ON `7951f9e9ef7e`. `--samples 3`. Claim recall read **case-level** (distinct cases, not
sample-flattened — codex P1.1). Curated summary — raw runs gitignored._

**Aggregate, flag-OFF baseline vs flag-ON candidate:**

| metric | flag-OFF | flag-ON | target | verdict |
|---|---:|---:|---|---|
| **entity_type_accuracy** | **0.76±0.39** | **0.77±0.37** | ≥0.95 | ✗ flat — **AC-1 not met** |
| **claim_case_level_recall** (distinct cases) | **3/12 (0.25)** | **5/12 (0.42)** | ≥0.8 | ✗ +2 cases, far short — **AC-2 not met** |
| claim_emission_recall (sample aggregate) | 0.28±0.45 | 0.36±0.48 | — | directional only |
| **relationship_type_correctness** | **0.99±0.08** | **0.77±0.40** | ≥0.89 (pin) | ✗ **regressed — AC-3 violated** |
| forbidden_edge_type_rate | 0.01±0.10 | 0.03±0.14 | low | ✗ slightly worse |
| knowledge_class_accuracy | 1.00 | 1.00 | 1.00 | ✓ held |
| hallucination_rate | 0.00 | 0.00 | 0.00 | ✓ held |
| dedup_convergence | 1.00 | 1.00 | 1.00 | ✓ held |
| stance_emission_recall | 1.00 | 1.00 | 1.00 | ✓ held |
| empty_fallback_rate | 0.00 | 0.00 | 0.00 | ✓ held |

**Paired per-case (the diagnostic signal):**
- **Type exemplars work when precisely aimed but do net damage.** The Topic exemplar fixed its exact
  target (`type-topic-subject-area` 0.0→1.0) and two others improved (`history-bronze-age` 0.78→1.0,
  `security-incident-response` 0.83→1.0), but the block caused **collateral type regressions**
  (`game-theory-prisoners-dilemma` 1.0→0.5, `cs-data-structure` 0.83→0.5) → **net flat**.
- **Claim exemplars give a modest real gain** (3→5 of 12 distinct cases) but nowhere near ≥0.8.
- **The block broadly regresses edge-typing** (`hallucination-misspelled-reltype` 1.0→0.0,
  `game-theory` 1.0→0.5, `cs-data-structure` 1.0→0.33) — the added type/claim guidance appears to
  distract the model from relationship-type discipline. This is a real, ~4–5-case systemic effect,
  not single-case noise.

**Result: FRE-759's acceptance criteria were NOT met by the hand-drafted exemplars.** entity-type did
not move (0.76→0.77), claim recall improved but fell far short (0.25→0.42 case-level), and
relationship-type correctness regressed below its pin. This is the measure-don't-assert payoff: the
lever is decisively characterized rather than assumed.

**Disposition:** the flag ships **default-OFF** — zero live impact (default behaviour is the flag-OFF
baseline, edge-typing intact). What ships and stays is the **permanent instrument + mechanism**: the
powered 12-case claim gold + case-level `claim_case_level_recall` metric (both needed by *every* future
extraction A/B, including DSPy), the flag-aware `prompt_hash`, and the reusable flag-gated exemplar
seam (dormant). **The lever hands off to DSPy-compiled extraction (FRE-759's owner-preferred option):**
compiling few-shot demos + instructions against this benchmark's `score_case` targets the exact metrics
*and* holds the near-ideal ones as constraints — precisely the collateral-damage problem hand-tuning
just exhibited. A narrower, edge-type-preserving exemplar retry is a secondary candidate. Filed as
follow-ups.

### 4.3 FRE-766 — model × reasoning benchmark, and the taxonomy root-cause (2026-07-03)

_Owner-designed matrix. 5 cells + a reused mini@none baseline (the FRE-759 flag-OFF run), 36-case
gold, 3 samples/cell, by-model concurrent, direct-metered cost/latency. `mini-xhigh` cut mid-run as
**measured non-viable** (32 000 reasoning tokens = the whole budget, 0 JSON emitted, ~210 s/call).
Curated summary — raw runs gitignored._

**Aggregate (mini@none = current prod baseline):**

| cell | entity_type | claim (case) | rel-type | class·halluc | lat p50 | reasoning tok | cost |
|---|---:|---:|---:|---|---:|---:|---:|
| **mini-none** (baseline) | 0.76 | 3/12 | ~0.99 | 1.00·0.00 | — | 0 | — |
| mini-medium | 0.78 | 2/12 | 0.76 | 1.00·0.00 | 8.4 s | 973 | $0.91 |
| mini-high | 0.78 | 0/12 | 0.67 | 1.00·0.00 | 21 s | 3175 | $1.97 |
| full-medium | 0.85 | 2/12 | 0.72 | 1.00·0.00 | 7.5 s | 568 | $2.32 |
| full-high | 0.85 | 2/12 | 0.77 | 1.00·0.00 | 14 s | 1443 | $3.72 |
| **sonnet5-adaptive** | **0.89** | 2/12 | **0.96** | 1.00·0.00 | 5.2 s | 0 | $2.29 |

**Findings:**
1. **No cell clears the bars** (entity_type ≥0.95, claim ≥0.8). Best entity-type ≈ **0.89 (sonnet)** — and sonnet reached it with **zero reasoning tokens** and the lowest latency. Model *capability*, not reasoning depth, is what moved entity-type.
2. **Reasoning is a poor trade for extraction.** On mini it didn't beat @none on entity-type, tanked claims (mini-high 0/12), and was slowest+costly; on the full model it barely moved. `xhigh` is a runaway (cut).
3. **The rel-type/claim "regressions" are largely measurement artifacts** (harness-removed direct-call spot-checks): the reasoning cells emit `RELATED_TO` where the single-author gold says `USES` (defensible — a trie is *used for* prefix search, not dependent on it), and emit the *same* claim under a different but-valid `facet` key. Both "worst" metrics are substantially gold-label artifacts, not real quality loss.
4. **The decisive finding — convergent failure = the taxonomy is the root cause.** `gpt-5.4-mini`, `gpt-5.4`, `claude-sonnet-5`, **and a purpose-built encoder (GLiNER, CPU spot-check)** all mis-type the **same** entities — `trie`, `retrieval-augmented generation`, `behavioral economics`, `game theory` — flipping between `Concept`/`Technology`/`Topic`. Four independent architectures agreeing on *where* they fail means the **schema is ill-posed**, not the models. And the ~0.86 ceiling is at/above single-annotator inter-annotator agreement for fine-grained typing — 0.95 against a single-author ambiguous gold is chasing noise above the human ceiling.

**Levers ruled out or shown marginal this program:** temperature (FRE-758, nil) · few-shot exemplars (FRE-759, regressed) · model × reasoning (FRE-766, flat-to-negative) · prompt *format* JSON/XML (nil) · tighter *definitions* (modest, weak-models-only) · purpose-built encoder models GLiNER/GLiREL (same boundary errors; low-confidence on relations). **The lever that worked** — collapsing the ambiguous `Concept`/`Technology`/`Topic` boundaries via an **8-type taxonomy** (`…, TechnicalArtifact, MethodOrConcept, DomainOrTopic, Phenomenon, …`): a spot-check took mini↔sonnet agreement on the flip-flopping cases from near-zero to **9–10/10**, with a clean `Phenomenon` boundary (5/5) and no regression. Captured as **[ADR-0109](../architecture_decisions/ADR-0109-entity-taxonomy-redesign.md)** (Proposed) — the recommended next step, gated behind a downstream-recall-impact check.

**Disposition:** FRE-766's benchmark is complete. **No production config change** — model/reasoning swaps don't clear the bar and reasoning isn't worth it; the mini@none (current prod, zero reasoning) config stands. The real fix is upstream (taxonomy, ADR-0109). FRE-764 (DSPy) is not moot but is lower-priority than the schema fix. The mechanism (config-driven `reasoning_effort`, the eval DI seam) ships as reusable infrastructure.

---

## Part 5 — Recommended improvements (→ follow-up tickets, each its own A/B)

**Reprioritized by the baseline** (the ticket's suggested fixes are *not* at the top — the extractor no
longer has those bugs). Ranked by measured impact:

1. **Entity-type accuracy (0.80 → target ≥0.95).** Few-shot type exemplars in the prompt for the confused
   boundaries (Concept↔Topic, Concept↔Technology) and/or an extract-then-verify type check. *(SOTA §3.2/§3.4.)*
2. **Claim emission (0.33 → target ≥0.8).** Personal situational claims are under-emitted; add a
   claim-focused prompt exemplar or a dedicated claim pass. *(FRE-636 finding, partially persists.)*
3. **DSPy-compiled extraction** *(owner-raised; strong fit — DSPy is already in the stack for
   `captains_log/reflection_dspy.py`, but extraction is a hand-written template today).* Define a DSPy
   `Signature` for extraction and **use this benchmark's `score_case` as the DSPy metric** to *compile* the
   prompt (few-shot demos + instructions) rather than hand-tuning it. This subsumes #1/#2 as an optimization
   target and is the principled version of "adjust the prompt." *(SOTA §3.2; self-consistency §3.4.)*
4. **Relationship-type validation gate** at the write path (reject/normalize off-vocabulary edge types).
   A real structural gap (no enforcement today) — but *low urgency*: the baseline shows off-vocab edges at
   only 2%, so this is defense-in-depth, not a live fire. *(SOTA §3.2.)*
5. **Description-integrity LLM-judge** to replace the deterministic proxy. *(SOTA §3.4.)*
6. **Phase-2 post-write graph-state benchmark** — score the persisted graph (embedding dedup, correction
   gate, write-time validation), which this pre-write instrument by design does not observe.
7. **Closed-world / exhaustively-labeled gold subset** so entity precision becomes a hard number, plus a
   blind second-labeler pass (cf. FRE-636 inter-rater).

**Explicitly NOT recommended (measure-don't-assert):** a `VISITED`/`TRAVELED_TO` vocabulary split. The
ticket proposed it, but the baseline shows the extractor does **not** mis-assert residence for visits
(`forbidden_edge_rate` 0.02, and 0.00 on the visit cases). Adding vocabulary to fix a non-existent bug
would be speculative complexity.

---

## Part 6 — Method limitations (stated plainly)
- **Small set (24 cases)** — calibration/regression, not statistically powered.
- **Sparse gold** — entity precision/F1 is diluted on non-exhaustively-labeled cases; lead with recall + traps.
- **Non-determinism** — a stochastic LLM; the baseline runs `--samples 3` with mean±std, but the bands are
  wide at n=3. Prompt/model/matcher revisions are stamped so runs are never silently compared.
- **Author-as-labeler** — the gold set was authored by this session; boundary calls (type vs class) are one
  labeler's. A blind second-labeler pass is the obvious hardening (cf. FRE-636's inter-rater check).

---

## References
- Edge et al. (2024), *From Local to Global: A Graph RAG Approach to Query-Focused Summarization*, arXiv:2404.16130.
- Gutiérrez et al. (2024), *HippoRAG*, arXiv:2405.14831; (2025) *HippoRAG 2*, arXiv:2502.14802.
- Lairgi et al. (2024), *iText2KG: Incremental Knowledge Graphs Construction Using LLMs*, arXiv:2409.03284.
- Huguet Cabot & Navigli (2021), *REBEL: Relation Extraction By End-to-end Language generation*, EMNLP Findings.
- Zaratiana et al. (2024), *GLiNER: Generalist Model for NER using Bidirectional Transformer*, arXiv:2311.08526.
- Mihindukulasooriya et al. (2023), *Text2KGBench: A Benchmark for Ontology-Driven KG Generation from Text*, ISWC.
- Vashishth et al. (2018), *CESI: Canonicalizing Open Knowledge Bases*, WWW.
- Min et al. (2023), *FActScore*, EMNLP, arXiv:2305.14251.
- Wang et al. (2022), *Self-Consistency Improves Chain of Thought Reasoning*, arXiv:2203.11171.
- Zheng et al. (2023), *Judging LLM-as-a-Judge (MT-Bench)*, arXiv:2306.05685.
- Yao et al. (2019), *DocRED: A Large-Scale Document-Level Relation Extraction Dataset*, ACL.
