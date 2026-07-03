# ADR-0109: Entity & Relationship Taxonomy — V1 (inherited) → V2 (first principled derivation)

**Status:** Proposed
**Date:** 2026-07-03
**Scope:** the entity-*type* and relationship-*type* vocabularies of the extractor (entity-first). The knowledge-*class* axis (World/Personal/System/Stance — ADR-0097/0098/0106) is **unchanged and orthogonal**.
**Gives an ADR to something that never had one:** the type vocabularies were **never** defined or justified by any prior ADR — see §Provenance. This is not a supersession; it is the **first** documented derivation.
**Backing evidence:** FRE-630 (extraction-quality benchmark), FRE-758/759/766 (temperature / exemplar / model×reasoning levers, all disproved or marginal), and the FRE-766 taxonomy spot-checks (this session).

> **Terminology (owner):** **V1** = the current *inherited* taxonomy — 7 entity types (`Person, Organization, Location, Technology, Concept, Event, Topic`) + 6 relationship types (`PART_OF, USES, RELATED_TO, SIMILAR_TO, CREATED_BY, LOCATED_IN`) — live in production. **V2** = the redesign proposed here (8-type entity + a gated relationship vocab). *V1 is real and shipped; V2 is proposed and unbuilt.*

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

Replace the V1 7-type entity-*type* vocabulary with an **8-type** taxonomy, each defined GoLLIE-style (inclusion + **exclusion** + example). The knowledge-*class* axis (World/Personal/System) is unchanged and applied orthogonally.

| key | definition (inclusion · **exclusion** · e.g.) |
|---|---|
| `Person` | a real, named individual human. **Not** "User"/"Assistant", generic roles, teams, orgs. |
| `Organization` | a named company, institution, agency, department, team, or standards body. **Not** software products or locations. |
| `Location` | a named geographic or physical place. **Not** organizations named after places, namespaces, repos. |
| `TechnicalArtifact` | a concrete, named engineered/built thing you can install, run, call, configure, or physically use — **software or hardware**. **Not** an abstract method/idea (→ MethodOrConcept). *e.g. Python, Neo4j, FastAPI, a GPU, an oscilloscope.* |
| `MethodOrConcept` | a specific **human-invented** abstract idea, method, technique, algorithm, data structure, pattern, or principle. **Not** a built artifact; **not** a broad field; **not** a natural phenomenon. *e.g. GraphRAG, trie, Nash equilibrium, retrieval-augmented generation.* |
| `DomainOrTopic` | a broad field, domain, discipline, or subject area as a whole. **Not** a specific technique within it (→ MethodOrConcept). *e.g. behavioral economics, cosmology, cybersecurity, game theory.* |
| `Phenomenon` | a naturally-occurring physical/natural phenomenon, process, effect, force, or observable that exists independently of human design. **Not** a human-invented method (→ MethodOrConcept). *e.g. cosmic microwave background, gravity, photosynthesis, the greenhouse effect, the Maillard reaction.* |
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

## Alternatives Considered

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
2. **Gold re-label** — re-type the FRE-630 gold to the 8 types (resolves the ambiguous labels the metric was penalizing) + grow the phenomenon/boundary coverage.
3. **Prompt update** — swap the entity-type block for the 8-type GoLLIE definitions; keep the knowledge-class block and stances/claims contract.
4. **Powered A/B** — tightened-taxonomy vs current on the re-labeled gold, across model families, samples≥3 (beat the n=1 spot-check noise).
5. **KG migration** — an idempotent re-classification pass mapping/​re-typing existing nodes (`Technology`→`TechnicalArtifact`; `Topic`→`DomainOrTopic`; `Event`→`Event`; `Concept`→ LLM-classified into MethodOrConcept / DomainOrTopic / Phenomenon). Snapshot Neo4j first; ADR-0074 identity threading on the migration writes.

**Orthogonality:** the knowledge class (World/Personal/System) and the stance/claim emission contract are unchanged; only the entity `type` enum changes.

---

## Verification / Acceptance Criteria

| # | Criterion | Proof |
|---|-----------|-------|
| AC-1 | Cross-model agreement on the previously-ambiguous set materially exceeds the 7-type baseline (target: ≥90% type-agreement between two model families) | powered A/B (step 4) |
| AC-2 | `MethodOrConcept ↔ Phenomenon` and `MethodOrConcept ↔ DomainOrTopic` boundaries hold on an expanded probe set (no new convergent-failure cases) | probe-set spot-check |
| AC-3 | No downstream memory-recall regression from coarsening | recall-impact check (step 1) + recall eval |
| AC-4 | KG migration re-types every existing entity node (no `Concept`/`Technology`/`Topic` remnants; 0 orphans) | migration report + joinability probe (ADR-0074) |
| AC-5 | Gold re-labeled to the 8 types + benchmark re-baselined | curated table |

---

## References

- **FRE-630** extraction-quality benchmark; **FRE-758/759/766** (temperature / exemplar / model×reasoning levers — disproved/marginal).
- Sainz et al. (2024), *GoLLIE: Annotation Guidelines improve Zero-shot Information Extraction*, ICLR — definitions need inclusion **and exclusion**; models ignore terse labels and fall back on priors.
- Boylan et al. (2025), *GLiREL: Generalist Zero-Shot Relation Extraction*, NAACL; GLiNER family — encoder IE; on our cases made the **same** boundary errors (convergent-failure evidence).
- Fine-grained-typing + IAA literature — LLMs lag on fine-grained typing; inter-annotator agreement (~0.80–0.90 typed-F1) is the practical ceiling.
- **ADR-0097 / ADR-0098** (knowledge taxonomy + substrate — entity-*type* half superseded here); **ADR-0106** (System/User boundary — class axis, unchanged).

---

## Status Updates

### 2026-07-03 — Proposed
Drafted from the FRE-766 exploration (convergent-failure diagnosis + 7-type→8-type spot-checks). **Not yet approved.** Finalization (codex review, sequenced implementation tickets, migration plan) to run through the `/adr` session; not bundled into the FRE-766 mechanism PR.
