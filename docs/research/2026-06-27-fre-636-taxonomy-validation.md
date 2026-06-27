# FRE-636 — Taxonomy-validation spike: ADR-0097 Personal/World/Stance against real data

**Date:** 2026-06-27
**Ticket:** [FRE-636](https://linear.app/frenchforest/issue/FRE-636) (Memory Recall Quality) ·
**Backing ADR:** [ADR-0097 Ingested-Knowledge Taxonomy](../architecture_decisions/ADR-0097-ingested-knowledge-taxonomy.md) (Proposed, held loosely)
**Feeds:** FRE-635 (author of ADR-0098, Memory Substrate & Lifecycle Architecture)
**Method posture:** measurement-first (cf. FRE-433/434, ADR-0087). Read-only sampling, no writes to prod substrate (FRE-375).

> **Privacy note.** This corpus is the owner's personal knowledge graph. This public note is
> generalized — verbatim user text, home location, owned vehicles, travel plans, and the owner's name
> are withheld. The specific, fully-cited findings live in the private FRE-635 thread.

---

## TL;DR — Verdict: **KEEP all three, but EXTEND with an explicit non-user-knowledge bucket; the real gap is extraction, not taxonomy.**

1. **KEEP** Personal / World / Stance. On genuine user-knowledge they are mutually distinct and
   collectively sufficient — a blind second labeler agreed with the primary labels on **39/40 items
   (97.5%)**, and a single genuine conversation turn cleanly exhibits all three. Do **not** simplify
   away Stance because it is rare; its rarity is an extraction artifact (below), and it is the
   pedagogical crown jewel.
2. **EXTEND** the model with an explicit **Operational / non-user-knowledge** class *or* a
   pre-taxonomy quality gate. ADR-0097 claims every ingested item is one of the three classes; on real
   data **~46% of all extracted entities are operational noise** (the agent's own code/infra/telemetry)
   that fits none of them. The "covers everything" claim is false unless this material is gated out
   before classification.
3. **The decisive finding is about extraction, not the taxonomy.** Personal and Stance are nearly
   absent at the *entity* level (~1% and ~3% in genuine sessions) but **present and explicit at the
   *source-conversation* level**. The pipeline flattens Stance into World-entity description clauses
   and drops Personal situational facts. ADR-0098 must not read "few Stance entities" as "Stance is
   unused / too complex."

---

## What was sampled

Prod KG (read-only): 7,366 `:Entity` nodes (all `memory_type=semantic`), 2,133 `:Turn`, 75 `:Session`.
Entity types: Concept 3699, Technology 1546, Topic 1120, Event 376, Organization 258, Location 200,
Person 167.

**Most of the corpus is test/dev/agent-operational noise**; genuine user-knowledge lives in real-life
topic threads. Two samples + two controls (codex methodology review, 2026-06-27):

- **Sample R — unbiased random (N=150)** drawn from all 7,366 entities. Gives the operational-noise
  fraction and an unconfounded taxonomy-fit estimate. *(Guards against the seeded sample confirming
  the taxonomy by construction.)*
- **Sample G — genuine-session depth (N=150)** drawn from 32 sessions whose `dominant_entities` are
  real-world topics (cooking, optics/physics, philosophy of mind, game theory, music theory, security/
  CSIRT, travel, vehicle-leasing, French literature, CS data structures). Depth on the subset the
  taxonomy is *for*.
- **Blind second labeler (N=40 stratified)** — an independent agent open-coded the items and force-
  classified them with *neutral* definitions (no ADR, no expected outcome). Inter-rater check.
- **Extraction-loss turn probe** — genuine `:Turn` source text vs. what survived into entities.

Rubric was **pre-registered before labeling** (hard rules for flattened Stance, self-referential
operational items, NULL descriptions; negative controls). Primary labeler: Claude (this session).

---

## Counts

### Sample R — unbiased random, N=150 (the corpus as a whole)

| Class | Count | Share | Notes |
|---|---:|---:|---|
| **World** | ~77 | ~51% | reusable impersonal know-how across many domains |
| **None / Operational** | ~69 | ~46% | agent codebase / infra / telemetry / test-scaffold / control-state |
| **Personal** | ~1 | ~1% | the user's own ongoing artifact (e.g. a recipe collection) |
| **Stance** | ~0 | ~0% | none surfaced in the random draw |
| **Ambiguous** | ~3 | ~2% | World-vs-Operational generic-tech-in-ops-context |

> ~5% of items sit on a World/Operational border (generic tech named inside an ops context, e.g. `df`,
> `ps aux`, "Observability"); the true operational share is therefore ~44–49% (95% CI for p≈0.46,
> n=150 is ±8%). Either way: **roughly half of everything extracted is not user-knowledge at all.**

### Sample G — genuine sessions, N=150 (the tutor corpus)

| Class | Count | Share | Representative example *forms* |
|---|---:|---:|---|
| **World** | ~133 | ~89% | a physics scattering law; a game-theory game; a cooking technique with a temperature; a leasing concept (residual value); an incident-response framework; a CS data structure |
| **None / Operational** | ~12 | ~8% | agent tools (`web_search`, `run_python`), self-telemetry, a memory-graph reset event (tool/infra leakage into genuine sessions) |
| **Stance** | ~4 | ~3% | "a subject area *retained about the user*"; "a *practical goal* for ear training"; "a *preferred* dish style"; "a *performance goal*" |
| **Personal** | ~1 | ~1% | "a place where *the user said they were at home*" |
| **Ambiguous** | 0 | 0% | — |

**Forced-fit / flattening rate:** within World, ~10% carried a *latent stance* clause that was rounded
into a World description — e.g. a drivetrain concept described as "*central to the user's preference
for…*", or a product described as one "*the user likes strongly*". This is the flattening signature
(finding #3).

---

## Inter-rater check (blind second labeler, N=40)

- **Forced classification agreement: 39/40 = 97.5%.** The lone disagreement — a "drivetrain concept…
  central to the user's preference" item (primary: World+latent-stance; second: Stance) — is *exactly*
  the flattened-Stance boundary, i.e. the disagreement is itself evidence of finding #3, not rubric
  instability.
- **Open coding (unprompted):** the second labeler's 7 natural categories collapsed to **World +
  Operational + a small goals/interests (Stance/Personal) cluster**. It did **not** spontaneously
  produce a clean three-way Personal/World/Stance split; instead **Operational emerged as a first-class
  kind** — independent corroboration of finding #2.

The three classes are therefore *reproducible* under independent labeling, but a non-user-knowledge
("Operational") kind is reproducibly *missing* from the taxonomy.

---

## Extraction-loss probe — taxonomy fit vs. pipeline survival (two denominators)

Codex flagged the risk of conflating "the taxonomy doesn't fit" with "the extractor never produces
this class." Reading genuine `:Turn` source text settles it.

A single genuine source turn (a vehicle-purchase deliberation) contained, in the user's own words, all
three classes at once — paraphrased:

- **Personal** — a situational fact about the user (an expiring lease; that they are actively shopping;
  an action they took that day).
- **Stance** — explicit first-person affect toward two specific products ("I like X a lot"; "I love Y").
- **World** — drivetrain categories and specs being compared.

What survived into the graph:

| Class | Present at source? | Survived into entities? |
|---|---|---|
| **World** | yes | **yes** — densely (every product, category, and spec became an entity) |
| **Stance** | yes, explicit, first-person | **flattened** — became a clause inside a World entity's description ("a product the user likes strongly"); no structured Stance node/edge |
| **Personal** | yes, explicit | **dropped** — the user's situational facts were not extracted as entities |

**Conclusion:** all three classes are present and distinct *at the source*. The near-absence of
Personal/Stance in the graph is an **extraction-quality** failure (the extractor's 7-type schema —
Person/Org/Location/Tech/Concept/Event/Topic — has no slot for a user-stance or a user-situational
fact), **not** evidence the taxonomy is wrong or too complex. The taxonomy is being tested through a
lossy pipeline; do not mistake the pipeline's silence for the taxonomy's failure.

---

## Answers to the three questions ADR-0098 asked

**Holds?** — Yes, on genuine user-knowledge. The three classes are mutually distinct (97.5% inter-rater
agreement) and jointly cover genuine items once operational noise is removed. World carries the corpus;
Personal and Stance are real and distinct where they appear.

**Too complex?** — No — but Stance and Personal look rare *only because of extraction loss*, so the
"maybe World+Stance are one thing / maybe drop a class" worry is not supported. At the source level
Stance is a clearly separable, first-person, pedagogically central signal. **Do not simplify to two
classes.**

**Insufficient?** — Yes, in one concrete way: it lacks a home for **non-user-knowledge / operational**
material, which is ~46% of what is actually extracted. ADR-0097's "every ingested item is one of three
classes" is false on real data. No evidence yet of a needed *fourth user-knowledge* class — "goals"
and "intentions" classify cleanly as Stance or Personal, as ADR-0097 predicted. The missing piece is a
**System/Operational pre-class or a quality gate**, not a fourth pedagogical class.

---

## Recommendations for ADR-0098

1. **Keep Personal / World / Stance.** Validated as distinct and sufficient for tutor-scoped
   user-knowledge. Treat Stance as first-class despite low current counts.
2. **Add an explicit pre-taxonomy gate (or a `System/Operational` class).** Decide deliberately where
   the ~46% operational material lives: filtered before the three-class triage, or assigned to an
   out-of-scope bucket. Do not let it be silently mislabeled World (it inflates World and pollutes the
   tutor corpus).
3. **The binding constraint is extraction, not storage.** The current extractor (7 entity types, no
   stance/relation/owner-fact emission) cannot represent Stance as a relation or Personal as a
   situational fact. Any substrate ADR-0098 designs will be starved of Personal/Stance until the
   extraction step is changed to emit them. **Sequence extraction redesign before — or with —
   substrate.**
4. **Re-validate on clean data.** These ratios are confounded by heavy test/dev traffic. Once an
   operational gate exists, re-run this classification on gated genuine data to get true class
   proportions.

---

## Method limitations (stated plainly)

- **Single primary labeler** (Claude, this session) who knew ADR-0097 — confirmation-bias risk,
  mitigated but not eliminated by the pre-registered rubric, negative controls, and the blind
  second-labeler (39/40 agreement). Not a substitute for owner adjudication of boundary cases.
- **Seeded genuine-session frame** over-represents the owner's named threads; the unbiased Sample R is
  the antidote and tells the same story (World-dominant, Operational-heavy, Personal/Stance sparse).
- **Counts are approximate** (±~5% from World/Operational border cases) and reflect one random draw.
- Classification was over `:Entity` description text; the turn probe is small — directional, not a
  measured survival rate. A larger source-vs-entity survival audit is the obvious follow-up if ADR-0098
  wants a quantified extraction-loss number.
