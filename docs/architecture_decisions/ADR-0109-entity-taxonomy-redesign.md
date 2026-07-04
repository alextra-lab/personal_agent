# ADR-0109: Entity & Relationship Taxonomy — V1 (inherited) → V2 (first principled derivation)

**Status:** Accepted (2026-07-03, owner) · **Amendment 1** (2026-07-04, FRE-782, owner): entity taxonomy grows **8 → 10 types** (adds `KnowledgeArtifact` + `QuantityMeasure`, each validated to the entity bar) — see [§ Amendment 1](#amendment-1-2026-07-04-fre-782--knowledgeartifact--quantitymeasure-8--10-types)
**Date:** 2026-07-03 (amended 2026-07-04)
**Scope:** the entity-*type* and relationship-*type* vocabularies of the extractor (entity-first). The knowledge-*class* axis (World/Personal/System/Stance — ADR-0097/0098/0106) is **unchanged and orthogonal**.
**Gives an ADR to something that never had one:** the type vocabularies were **never** defined or justified by any prior ADR — see §Provenance. This is not a supersession; it is the **first** documented derivation.
**Backing evidence:** FRE-630 (extraction-quality benchmark), FRE-758/759/766 (temperature / exemplar / model×reasoning levers, all disproved or marginal), and the FRE-766 taxonomy spot-checks (this session).

> **Terminology (owner):** **V1** = the current *inherited* taxonomy — 7 entity types (`Person, Organization, Location, Technology, Concept, Event, Topic`) + 6 relationship types (`PART_OF, USES, RELATED_TO, SIMILAR_TO, CREATED_BY, LOCATED_IN`) — live in production. **V2** = the redesign here (a **10-type** entity vocab — 8 accepted 2026-07-03, +2 by Amendment 1 — plus a gated relationship vocab). *V1 is real and shipped; V2 is accepted and unbuilt.*

---

## Provenance — where V1 came from (and why "how did we decide" has no good answer)

**V1 was never designed.** git traces the entity-type and relationship-type blocks in the extraction
prompt to the **Initial commit** — they were baked in at project inception, an inherited/ad-hoc
"here are common KG types" choice from early prototyping, and **no ADR ever derived or justified
them.** Every ADR that names them (0098, 0025, 0026, …) merely *uses* the list.

The **only** taxonomy axis that *was* deliberately designed is the knowledge **class** — `World /
Personal / System / Stance` (ADR-0097, explicitly "a hypothesis… held loosely"; ADR-0098 its
implementation). That axis is *subject/ownership*, orthogonal to entity *type*.

**This is the root cause, stated plainly:** the `Concept ↔ Technology ↔ Topic` boundaries are fuzzy
because **nobody ever drew them on purpose.** Four independent models disagreeing on them (below) is
the symptom of an un-derived vocabulary, not model weakness. V2 is therefore the **first principled
derivation**, not a tweak of a considered design.

---

## Context

The extraction program set an entity-type-accuracy target of ≥0.95 on the FRE-630 benchmark. A full session of levers **failed to reach it and, more importantly, revealed *why*:**

- **FRE-758** — pinning temperature: no effect.
- **FRE-759** — few-shot exemplars: regressed relationship-typing, marginal on entity-type.
- **FRE-766** — model × reasoning depth (mini/full/sonnet × none/medium/high): entity-type flat at **0.76 → 0.89** (sonnet best), **nothing cleared 0.95**; reasoning traded entity-type gains for edge-type losses; prompt *format* (JSON/XML vs prose) did nothing.
- **Purpose-built encoder models** (GLiNER, GLiREL, CPU spot-check): made the **same** boundary errors as the LLMs.

**The decisive finding is convergent failure.** Four independent architectures (`gpt-5.4-mini`, `gpt-5.4`, `claude-sonnet-5`, and GLiNER's encoder) **trip on the exact same cases** — `trie`, `retrieval-augmented generation`, `behavioral economics`, `game theory` — flipping between `Concept`, `Technology`, and `Topic`. When capable, unrelated models all "fail" in the *same place*, the schema is wrong, not the models.

**Two root causes:**
1. **Ill-posed boundaries.** `Concept` ("an abstract idea, methodology, or domain principle") vs `Technology` ("a software tool, framework, language, model, or API") forces the unanswerable question *"is a `trie` a tool or an idea?"*. `Concept` vs `Topic` overlaps similarly. `Technology` was also **too narrow** (software-only — no bucket for hardware).
2. **The target is above the human ceiling.** Fine-grained entity typing has human inter-annotator agreement (typed-F1) of ~0.80–0.90; our gold is single-author. A 0.95 target against a single-annotator ambiguous gold asks a model to agree with one person *more than two people agree with each other* — measurement noise, not a reachable goal.

**The lever is the taxonomy itself.** Merging overlapping types beats sharpening the line between them: *you cannot mislabel across a boundary that does not exist.* This also aligns with the literature — LLMs perform **worse** on fine-grained typing; a coarser, cleaner set plays to their strengths — and with GoLLIE (ICLR 2024): definitions with explicit inclusion **and exclusion** criteria override the model's prior.

### Evidence (FRE-766 spot-checks, this session)

Minimal focused prompt, direct API, `gpt-5.4-mini` (temp 0) + `claude-sonnet-5` (adaptive). n=1 per cell — directional; the *signal* is cross-model **agreement**.

**7-type Option B** (Person/Organization/Location/TechnicalArtifact/MethodOrConcept/DomainOrTopic/Event) — mini↔sonnet agreed on **9 of 10** entities that previously flip-flopped: `trie`/`RAG` → MethodOrConcept, `behavioral economics`/`game theory` → DomainOrTopic (with `Nash equilibrium`/`Prisoner's Dilemma` → MethodOrConcept — a clean domain-vs-concept split), `FastAPI`/`Python`/`PostgreSQL` → TechnicalArtifact, `Big Bang` → Event. The **one miss** — `cosmic microwave background` (mini: MethodOrConcept, sonnet: TechnicalArtifact) — exposed a gap: no bucket for **natural phenomena**.

**8-type (adds `Phenomenon`)** — CMB → **Phenomenon** on both; the `MethodOrConcept ↔ Phenomenon` boundary was clean at **5/5** (gravity, photosynthesis, greenhouse effect, black hole, Maillard reaction — all Phenomenon on both models); **no regression** on the previously-resolved cases. Residual differences were extraction *breadth* (mini over-extracts spans), **not** type disagreement.

---

## Decision

### V2 — entity types (validated)

Replace the V1 7-type entity-*type* vocabulary with a **10-type** taxonomy (8 accepted 2026-07-03; `KnowledgeArtifact` and `QuantityMeasure` added by [Amendment 1](#amendment-1-2026-07-04-fre-782--knowledgeartifact--quantitymeasure-8--10-types) after their own 3-rater validation), each defined GoLLIE-style (inclusion + **exclusion** + example). The knowledge-*class* axis (World/Personal/System) is unchanged and applied orthogonally.

| key | definition (inclusion · **exclusion** · e.g.) |
|---|---|
| `Person` | a real, named individual human. **Not** "User"/"Assistant", generic roles, teams, orgs. |
| `Organization` | a named company, institution, agency, department, team, or standards body. **Not** software products or locations. |
| `Location` | a named geographic or physical place. **Not** organizations named after places, namespaces, repos. |
| `TechnicalArtifact` | a concrete, named engineered/built thing you install, run, call, deploy, configure, process, or physically use — **software or hardware**, and the **data assets a system runs on** (datasets, benchmarks, gold files, prompts). **Not** a human-authored work you read to understand (→ KnowledgeArtifact); **not** an abstract method/idea (→ MethodOrConcept). *e.g. Python, Neo4j, FastAPI, a GPU, an oscilloscope, a benchmark gold file, an extraction prompt.* *(scope widened by Amendment 1 to name the KnowledgeArtifact boundary)* |
| `KnowledgeArtifact` | a concrete, named **human-authored work whose purpose is to convey understanding to a reader** — a document, ADR, report, paper, article, chapter, specification, or plan. **Not** a thing you run/deploy/process (datasets, benchmarks, gold files, prompts, software, hardware → TechnicalArtifact); **not** the facts extracted from it into the KG (this names the **source document itself**, not its contents); **not** a broad field (→ DomainOrTopic). *e.g. ADR-0109, the GoLLIE paper, a design spec, a drafted book chapter, an incident post-mortem report.* **(Amendment 1)** |
| `MethodOrConcept` | a specific **human-invented** abstract idea, method, technique, algorithm, data structure, pattern, or principle. **Not** a built artifact; **not** a broad field; **not** a natural phenomenon. *e.g. GraphRAG, trie, Nash equilibrium, retrieval-augmented generation.* |
| `DomainOrTopic` | a broad field, domain, discipline, or subject area as a whole. **Not** a specific technique within it (→ MethodOrConcept). *e.g. behavioral economics, cosmology, cybersecurity, game theory.* |
| `Phenomenon` | a naturally-occurring physical/natural phenomenon, process, effect, force, limit, or observable that exists independently of human design. **Not** a human-invented method (→ MethodOrConcept); **not** the quantity used to measure it (→ QuantityMeasure). *e.g. cosmic microwave background, gravity, photosynthesis, the greenhouse effect, the Maillard reaction, the diffraction limit.* *(exclusion sharpened by Amendment 1)* |
| `QuantityMeasure` | a named **physical quantity, property, dimension, or unit of measure** — an axis along which things are measured. **Not** the naturally-occurring phenomenon/effect/limit that exhibits it (→ Phenomenon); **not** a human-invented method to compute it (→ MethodOrConcept); **not** a specific measured value. *e.g. wavelength, mass, temperature, frequency, luminosity.* **(Amendment 1)** |
| `Event` | a specific named occurrence, milestone, incident, release, or time-bound activity. *e.g. the Big Bang, ICLR 2024, a production outage.* |

**Rationale for `Phenomenon` as a distinct type** (rather than folding into `MethodOrConcept`): the "human-invented abstraction vs naturally-occurring phenomenon" line is *clean and teachable* (5/5 cross-model agreement measured), it is common in the owner's domains (physics, cosmology, cooking-chemistry, acoustics), and it is pedagogically meaningful — *methods to practice · phenomena to understand · domains to survey · artifacts to use*. The usual "more types hurt LLMs" risk did **not** materialize in the spot-check because the added boundary is unambiguous.

### V2 — relationship types (candidate; not yet validated to the entity bar)

The V1 relationship vocab (`PART_OF, USES, RELATED_TO, SIMILAR_TO, CREATED_BY, LOCATED_IN`) is
**equally inherited and un-derived**, and FRE-759 + the FRE-766 direct-call spot-checks already exposed
two design faults:
- **`RELATED_TO` is a mis-designed catch-all.** Defined as the "general" relationship, it *overlaps*
  every specific type, so models flip between it and `USES`. The RE literature (TACRED/SemEval/SciERC)
  treats the generic relation as a **gated None-of-the-Above last resort**, never a co-equal option.
- **`USES` overlaps it** and lacks a directional definition (`A uses B` vs `A is used for B`).

**V2 candidate** (the FRE-759 tightened definitions, owner-arbitrated): keep the 6-type set, but
(a) re-cast `RELATED_TO` as an explicit last-resort NoTA fallback ("only when no specific type fits;
never when a specific type applies"), (b) give every relation a **direction** and a functional
inclusion/exclusion definition, (c) add an explicit *emit-nothing-if-none-fits* rule.

**Honesty flag — this half is NOT yet validated.** Only the *entity* V2 was cross-model spot-checked;
the relationship V2 is a *proposal*, not a measured result. It needs its own agreement spot-check (and
the gold re-label, since the tightened `USES`/`RELATED_TO` will disagree with V1 gold labels — the
`trie → prefix-search` case flips `USES` → `RELATED_TO`). Do not ship the relationship half on the
entity half's evidence.

---

## Amendment 1 (2026-07-04, FRE-782) — KnowledgeArtifact + QuantityMeasure (8 → 10 types)

**Supersession note:** this amendment grows the entity vocabulary from 8 to **10 types**. Everywhere the sections below (Decision, Consequences, Implementation Notes, Levers, the original AC-5) say "8 types," read "**10 types**" — the two additions are validated to the same entity bar as the original eight and carry the same GoLLIE inclusion/exclusion/example contract. The knowledge-*class* axis and the relationship vocab are untouched.

### Why: two gaps the 8-type set could not home

The FRE-770 gold re-label (3 blind raters, overall Fleiss κ 0.777) surfaced two entities the accepted 8 types could not cleanly type — not model weakness, but *missing categories*, the same failure signature that motivated V2 in the first place:

1. **A human-authored work you read to understand.** `Neuroplasticity Chapter` (a Personal-class chapter draft) drew a genuine 3-way split (MethodOrConcept / TechnicalArtifact / Event) and was only *provisionally* ruled TechnicalArtifact (`v2_needs_owner_signoff`). Forcing authored works into `TechnicalArtifact` turns it into a junk drawer — the opposite of V2's "one clean boundary per type name" philosophy.
2. **A physical quantity you measure.** `Wavelength` was majority-ruled MethodOrConcept but flagged: measurable quantities (wavelength, mass, temperature) are neither human-invented methods nor occurring phenomena.

Owner decision (2026-07-04): add a type for each, **each gated on its own 3-rater validation** — a new type earns its place only if the boundary holds at high cross-model agreement, exactly as `Phenomenon` did in the original ADR.

### The two new types (definitions in the § Decision table above)

- **`KnowledgeArtifact`** — a human-authored work whose purpose is to convey understanding to a reader.
- **`QuantityMeasure`** — a named physical quantity, property, dimension, or unit of measure.

Three boundary rules encode the decisions in the type names (the V2 design philosophy):

1. **Read-for-knowledge vs run/process** (`KnowledgeArtifact` ↔ `TechnicalArtifact`). A KnowledgeArtifact is a thing you *read to understand*; a TechnicalArtifact is a thing you *run, deploy, or process*. Datasets, benchmarks, gold files, and prompts are **TechnicalArtifact** (system tooling), **not** KnowledgeArtifact — this widens TechnicalArtifact's scope to name the boundary explicitly.
2. **The artifact, not the knowledge in it** (`KnowledgeArtifact` ↔ the KG). A KnowledgeArtifact is the **source document itself**, distinct from the facts extracted out of it into the knowledge graph. This keeps `KnowledgeArtifact` from overloading the KG's own notion of "knowledge."
3. **Measurable property vs occurring effect vs invented method** (`QuantityMeasure` ↔ `Phenomenon` ↔ `MethodOrConcept`). The raw *quantity* (wavelength) is QuantityMeasure; the naturally-arising *phenomenon, effect, or limit* that exhibits it (Rayleigh scattering, the diffraction limit) is Phenomenon; the human-*invented* method that computes over it (the Fourier transform) is MethodOrConcept.

### Validation — 3-rater IAA, boundary probe (both types clear the bar)

Same instrument as FRE-770 (`iaa.build_iaa_report`; raters `gpt-5.4-mini`, `gpt-5.4`, `claude-sonnet-5`), run on a 22-entity probe concentrated on the two contested boundaries, definitions = the full 10-type set. Full method + per-entity table: [FRE-782 boundary-IAA research note](../research/2026-07-04-fre-782-knowledgeartifact-quantitymeasure-boundary-iaa.md).

- **Overall Fleiss κ = 0.900** (n=22), above FRE-770's 0.777 ceiling.
- **`KnowledgeArtifact` ↔ `TechnicalArtifact` is airtight — κ 1.000 both sides.** All 6 authored works → KnowledgeArtifact unanimous; all 6 tooling → TechnicalArtifact unanimous. The three "system tooling" cases (the FRE-630 gold set, the extraction prompt, `gold_extraction.yaml`) went **TechnicalArtifact unanimously**. Note the honest reading: the raters were not shown the numbered boundary rule, but the *substance* of it is embedded in the type definitions (which name datasets/benchmarks/gold-files/prompts as TechnicalArtifact) — so this validates that the **definitions carry the boundary**, i.e. the read-vs-run line is teachable from the written definition without extra instruction, not that raters rediscovered it unaided.
- **`QuantityMeasure` holds — κ 0.847.** Wavelength, Mass, Temperature, Frequency, Luminosity all unanimous.
- **Both FRE-770 flagged cases resolved:** `Neuroplasticity Chapter` → **KnowledgeArtifact (3/3)**; `Wavelength` → **QuantityMeasure (3/3)**.
- **Two residual boundary cases (ruled by disambiguation, not waived), carried into the gold re-label + FRE-771 prompt:** `Redshift` (2/3 — the measured quantity → QuantityMeasure vs the stretching process → Phenomenon) and `Diffraction Limit` (3-way — a naturally-arising *limit/constraint* → **Phenomenon**, consistent with FRE-770; QuantityMeasure is reserved for the raw property). These are the QuantityMeasure↔Phenomenon edge — a *real* residual ambiguity the new type introduces at "a physical effect expressed as a number" — and it is why QuantityMeasure earns κ 0.847, not 1.000. It is bounded to that edge: it drives the entire per-type κ dip on Phenomenon/MethodOrConcept and leaves the raw-property core unanimous.

**Falsification bar (externally anchored, met):** the bar is not a number picked to pass — it is the *already-accepted* V2 types' own FRE-770 agreement (overall κ 0.777; weakest-accepted per-type κ 0.645, `MethodOrConcept`), both of which predate this run. Reject `QuantityMeasure` unless its per-type κ clears those marks **and** all five raw-property anchors (`Wavelength`/`Mass`/`Temperature`/`Frequency`/`Luminosity`) are unanimous — else park `Wavelength` as MethodOrConcept and ship only `KnowledgeArtifact`. κ 0.847 (> 0.777) with all five anchors unanimous clears it. This validates the two **new boundaries** are teachable; it does **not** certify production-wide stability across all 10 types — that is AC-1's powered A/B on the full re-labeled gold (FRE-771).

### Case resolutions (for the gold + FRE-771 seam)

| entity (gold case) | FRE-770 provisional | Amendment 1 ruling | basis |
|---|---|---|---|
| Neuroplasticity Chapter (`personal-writing-project`) | TechnicalArtifact ⚠ needs sign-off | **KnowledgeArtifact** — clear `v2_needs_owner_signoff` | 3/3 unanimous; a drafted chapter is an authored work read to understand |
| Wavelength (`physics-scattering`) | MethodOrConcept (gap-flagged) | **QuantityMeasure** | 3/3 unanimous; a measurable physical property |

### Alternatives (for the amendment specifically)

1. **Park Wavelength as MethodOrConcept, add only KnowledgeArtifact.** The minimal fix for the one signed-off case. **Rejected** — leaves a named, recurring gap (physical quantities are common in the owner's physics/acoustics/cosmology domains) and the validation showed `QuantityMeasure` is a *clean* boundary, so the "more types hurt LLMs" risk did not materialize. Kept as the falsification fallback, not needed.
2. **Special-case Personal-class entities to a bespoke type instead of a general KnowledgeArtifact.** **Rejected** — entity *type* is orthogonal to knowledge *class* by ADR-0109's own design; `KnowledgeArtifact` types an ADR (System/World) and a personal chapter (Personal) alike, without coupling the two axes.
3. **Fold both into `TechnicalArtifact` / `Phenomenon` (do nothing).** **Rejected** — this is exactly the junk-drawer failure V2 exists to prevent, and it re-creates the convergent 3-way split FRE-770 measured.

---

## Alternatives Considered

> These are the alternatives for the original 7→8-type decision. The alternatives for the 8→10-type Amendment 1 decision (park Wavelength / special-case Personal-class / fold into existing types) are in [§ Amendment 1 → Alternatives](#alternatives-for-the-amendment-specifically).

1. **Keep 7 types, tighten definitions only (the FRE-759 direction).** Sharper Concept/Technology lines. **Rejected as insufficient** — humans still disagree on that boundary, so sharpening cannot exceed the IAA ceiling; the spot-check showed the *merge* (not the sharpening) is what produced cross-model agreement.
2. **Single `Subject` type (owner's Option A) covering concepts/methods/domains/topics.** Cleaner but keeps `Technology` separate, so the `trie`/`RAG` (artifact-vs-idea) confusion partially survives. **Rejected** — Option B resolves it, A does not.
3. **Fine-tune / adopt a purpose-built model (GLiNER/GLiREL).** The dominant IE lever, but **blocked upstream** by the gold set (36 single-author ambiguous cases — too small to fine-tune, too ambiguous to certify a ceiling), and it re-hosts extraction on a separate model. Orthogonal to and gated behind this decision.
4. **Do nothing — accept ~0.86.** Viable *if* entity-type accuracy doesn't matter downstream (see Implementation Notes: the recall-impact check). **Deferred to that check**, but the taxonomy fix is near-free and pedagogically better regardless.

---

## Consequences

### Positive
- **Collapses the measured ambiguity** — cross-model agreement on the flip-flopping cases went from near-zero to ~near-total; raises the effective annotation ceiling.
- **`Technology` breadth fixed** — hardware and software both live in `TechnicalArtifact`.
- **More pedagogically expressive** — the method/phenomenon/domain distinctions map onto the tutor North Star.
- **LLM-friendly** — coarser, cleaner boundaries; and the definitions are enforceable across model families (encoder or generative).

### Negative
- **Schema migration blast radius** — the entity-`type` value appears in the extraction prompt, **existing Neo4j nodes**, the FRE-630 gold set, and any downstream consumer that keys on type. Not a clean 1:1 remap (`Concept` → one of MethodOrConcept / DomainOrTopic / Phenomenon depending on the node).
- **A near-one-way door** for the persisted graph — re-typing historical nodes requires an LLM re-classification pass, not a rename.

### Risks and Mitigations
- **Natural-phenomena edge cases beyond the 5 tested** (e.g. is "spacetime" a Phenomenon or a DomainOrTopic?) — mitigate with a larger phenomenon probe set before rollout.
- **Residual `MethodOrConcept ↔ DomainOrTopic` fuzz** ("is X a method or a field?") — fewer boundaries, but the survivor still needs crisp GoLLIE definitions + a probe.
- **Downstream consumers keyed on old types** — audit before migration; the recall-impact check gates whether the distinction even matters.

---

## Implementation Notes

**Sequence (each gated):**
1. **Downstream-impact check (do first, cheap).** Does memory recall / the pedagogical layer actually key on entity *type*? If recall keys on the entity node, not its type-subtype, coarsening is near-free and this ADR is low-risk. If a consumer needs the old grain, weigh it here.
2. **Gold re-label** — re-type the FRE-630 gold to the **10** types (resolves the ambiguous labels the metric was penalizing) + grow the phenomenon/boundary coverage. *(FRE-770 relabeled the 8; the Amendment 1 build follow-up extends `KnowledgeArtifact`/`QuantityMeasure` and applies the two case resolutions.)*
3. **Prompt update** — swap the entity-type block for the **10-type** GoLLIE definitions (FRE-771); keep the knowledge-class block and stances/claims contract.
4. **Powered A/B** — tightened-taxonomy vs current on the re-labeled gold, across model families, samples≥3 (beat the n=1 spot-check noise).
5. **KG migration** — an idempotent re-classification pass mapping/​re-typing existing nodes (`Technology`→`TechnicalArtifact`; `Topic`→`DomainOrTopic`; `Event`→`Event`; `Concept`→ LLM-classified into MethodOrConcept / DomainOrTopic / Phenomenon). Snapshot Neo4j first; ADR-0074 identity threading on the migration writes.

**Orthogonality:** the knowledge class (World/Personal/System) and the stance/claim emission contract are unchanged; only the entity `type` enum changes.

---

## Levers after the taxonomy fix (this ADR sequences the others)

The convergent-failure diagnosis makes V2 the **root-cause fix**, which re-orders every other
extraction lever behind it. The full reconciliation lives in
[the ER-extraction opinion-vs-pipeline study](../research/2026-07-03-er-extraction-opinion-vs-pipeline.md);
the sequencing consequence for *this* ADR:

1. **V2 (this ADR)** lands first — re-labeled gold → 10-type GoLLIE prompt → powered A/B → KG migration.
2. **Ontology enforcement (FRE-760, rel-type write gate)** must enforce **V2, not V1** — a write-gate
   built on the inherited entity-type enum would harden the exact schema this ADR replaces. It is also
   *low urgency* (FRE-630 measured off-vocab edges at only ~2% → defense-in-depth, not a live fire), so
   it sequences behind, or is made forward-compatible with, V2.
3. **Prompt exemplars / DSPy-compiled extraction** re-baseline against the V2-relabeled gold. Static
   few-shot exemplars already failed (FRE-759) partly *because* they cannot demonstrate the correct side
   of a boundary that does not exist — coarsening the boundary (V2) is the prerequisite, not a parallel
   track.
4. **Fine-tuning / purpose-built model** stays deprioritized (Alternatives #3) — blocked upstream on the
   gold set until V2 relabels it.
5. **Extraction decomposition (NER → pair → classify)** is orthogonal (a *precision* lever) and remains a
   *measure-gated candidate only* — the FRE-630 precision traps are already green, so it is not a ticket
   until a post-V2 measurement shows spurious-triple noise.

---

## Verification / Acceptance Criteria

| # | Criterion | Proof |
|---|-----------|-------|
| AC-1 | Cross-model agreement on the previously-ambiguous set materially exceeds the 7-type baseline (target: ≥90% type-agreement between two model families) | powered A/B (step 4) |
| AC-2 | `MethodOrConcept ↔ Phenomenon` and `MethodOrConcept ↔ DomainOrTopic` boundaries hold on an expanded probe set (no new convergent-failure cases) | probe-set spot-check |
| AC-3 | No downstream memory-recall regression from coarsening | recall-impact check (step 1) + recall eval |
| AC-4 | KG migration re-types every existing entity node (no `Concept`/`Technology`/`Topic` remnants; 0 orphans) | migration report + joinability probe (ADR-0074) |
| AC-5 | Gold re-labeled to the **10** types + benchmark re-baselined | curated table (FRE-770 relabeled the 8; the amendment's build follow-up extends the two new types) |

**Amendment 1 criteria come in two tiers.** AC-1–AC-5 already *mix* decision-time evidence (the spot-checks that justified accepting V2) with build-proven outcomes (the powered A/B, the migration report); Amendment 1 makes that split **explicit** for its own criteria. AC-6/AC-7 are **decision-validation gates — MET by this amendment** (they gate whether adding each type is a sound decision; the rejection rule + measured result are below, reproducible from the [research note's probe appendix](../research/2026-07-04-fre-782-knowledgeartifact-quantitymeasure-boundary-iaa.md#appendix--probe-fixture-22-entities)). AC-8 is an **implementation gate — PENDING**, proven by FRE-771 + the build follow-up (same status as AC-1/AC-4). Acceptance of the amendment ≠ implementation of it.

**On the thresholds:** the probe was run first, then these rules written — so the bar is anchored to an **external, pre-existing** number rather than a value chosen to pass. The anchor is the *already-shipped* V2 types' own agreement in FRE-770: overall κ **0.777**, and a weakest-accepted per-type κ of **0.645** (`MethodOrConcept`). A new type earns its place only if it is **at least as agreed-upon as the types already accepted** — it must clear both those FRE-770 marks. (0.777 and 0.645 predate this run and were not tuned here.)

| # | Criterion | Rejection rule (anchored to the FRE-770 shipped-type floor, not tuned to pass) | Proof / status |
|---|-----------|-----------|----------------|
| AC-6 (Amendment 1 — **decision gate, MET**) | The `KnowledgeArtifact ↔ TechnicalArtifact` boundary is teachable from the definitions alone: on the boundary probe, the authored-works side classifies `KnowledgeArtifact` and the system-tooling side (datasets/benchmarks/gold-files/prompts) classifies `TechnicalArtifact` | **Reject unless** both types' per-type κ ≥ the FRE-770 shipped-type marks (> 0.777 overall / ≥ 0.645 weakest-accepted) **and** all three "system tooling" probes (FRE-630 gold set, extraction prompt, `gold_extraction.yaml`) are unanimous `TechnicalArtifact` **and** every authored-work probe is unanimous `KnowledgeArtifact` | **MET** — FRE-782 3-rater probe: both κ **1.000**, all six tooling + all six authored-works unanimous (research note). |
| AC-7 (Amendment 1 — **decision gate, MET**) | `QuantityMeasure` names a clean raw-property region distinct from `Phenomenon`/`MethodOrConcept`, and `Wavelength` is no longer gap-flagged | **Reject unless** QuantityMeasure per-type κ ≥ the FRE-770 shipped-type marks (> 0.777 / ≥ 0.645) **and** all five raw-property anchors {`Wavelength`, `Mass`, `Temperature`, `Frequency`, `Luminosity`} are unanimous. The two *boundary* cases (`Redshift`, `Diffraction Limit`) are governed by the § Amendment 1 disambiguation rules and are **not** part of the raw-property core, so their split does not excuse a core failure — it is ruled, not waived. | **MET** — QuantityMeasure κ **0.847** (> 0.777 ceiling, > every accepted type's per-type κ); all five raw-property anchors unanimous; `Wavelength` → QuantityMeasure 3/3 (research note). |
| AC-8 (Amendment 1 — **implementation gate, PENDING**; owned by FRE-771 + the build follow-up) | The live 10-type extractor prompt + the re-labeled gold + a committed boundary regression reproduce this ADR: `Neuroplasticity Chapter` types `KnowledgeArtifact`, `Wavelength` types `QuantityMeasure`, `v2_needs_owner_signoff` is cleared, no gold entity carries an off-vocab `v2_type`, and the KA/TA + QM boundaries re-hold when the committed 10-type instrument is re-run on the probe fixture | **PENDING** — gold diff + loader test (`ALLOWED_ENTITY_TYPES_V2` in `scripts/eval/fre630_extraction_quality/gold.py` = the 10 types) + a committed boundary regression: check the 22-entity fixture into `scripts/eval/fre630_extraction_quality/` (extend `relabel_v2_types.py`'s `V2_TYPE_DEFINITIONS` to 10 types) and assert the KA/TA + QM boundaries hold, in a test beside the existing `test_phenomenon_coverage_*` / `test_all_entities_have_v2_type` cases. A stale 8-type prompt, an un-retyped case, or a bled boundary fails the assertion. Scope-note: production-wide 10-type stability across *all* types is AC-1's powered A/B, not this probe. |

---

## References

- **FRE-630** extraction-quality benchmark; **FRE-758/759/766** (temperature / exemplar / model×reasoning levers — disproved/marginal).
- Sainz et al. (2024), *GoLLIE: Annotation Guidelines improve Zero-shot Information Extraction*, ICLR — definitions need inclusion **and exclusion**; models ignore terse labels and fall back on priors.
- Boylan et al. (2025), *GLiREL: Generalist Zero-Shot Relation Extraction*, NAACL; GLiNER family — encoder IE; on our cases made the **same** boundary errors (convergent-failure evidence).
- Fine-grained-typing + IAA literature — LLMs lag on fine-grained typing; inter-annotator agreement (~0.80–0.90 typed-F1) is the practical ceiling.
- **ADR-0097 / ADR-0098** (knowledge taxonomy + substrate — entity-*type* half superseded here); **ADR-0106** (System/User boundary — class axis, unchanged).
- **FRE-770** — V2 gold re-label + 3-rater IAA (κ 0.777); surfaced the two gaps Amendment 1 closes. Research note: `docs/research/2026-07-04-fre-770-gold-relabel-iaa.md`.
- **FRE-782** (Amendment 1) — `KnowledgeArtifact` + `QuantityMeasure` boundary validation (3-rater IAA, κ 0.900). Research note: `docs/research/2026-07-04-fre-782-knowledgeartifact-quantitymeasure-boundary-iaa.md`.

---

## Status Updates

### 2026-07-03 — Proposed → Accepted
Drafted from the FRE-766 exploration (convergent-failure diagnosis + 7-type→8-type spot-checks); accepted by the owner the same day for the 8-type entity vocabulary. Implementation sequenced through the build stream (FRE-769 downstream-impact check ✅, FRE-770 gold re-label + IAA ✅, FRE-771 prompt swap pending).

### 2026-07-04 — Amendment 1: Accepted (8 → 10 types)
Adds `KnowledgeArtifact` (authored works) and `QuantityMeasure` (physical quantities), each validated to the entity bar by a 3-rater boundary IAA (overall κ 0.900; KnowledgeArtifact↔TechnicalArtifact κ 1.000; QuantityMeasure κ 0.847 — see [§ Amendment 1](#amendment-1-2026-07-04-fre-782--knowledgeartifact--quantitymeasure-8--10-types)). Closes the two FRE-770 gaps (`Neuroplasticity Chapter` → KnowledgeArtifact, `Wavelength` → QuantityMeasure). **Gates FRE-771** — the prompt swap must encode all 10 types. Build follow-up promotes the 10-type set into the committed instrument + re-labels the gold.
