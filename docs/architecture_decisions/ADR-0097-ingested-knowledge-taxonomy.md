# ADR-0097: Ingested-Knowledge Taxonomy (a hypothesis)

**Status**: Proposed — *hypothesis, held loosely* · **class vocabulary refined by ADR-0115 (2026-07-11) to Personal / World / Stance; `System` moved off the class axis onto the `output_kind` axis.**
**Date**: 2026-06-27
**Deciders**: Project owner
**Supersedes**: ADR-0071 (in part — the taxonomy it never made explicit)
**Paired with**: ADR-0098 (Memory Substrate & Lifecycle Architecture — *how* we implement this)
**Related**: ADR-0052 (Seshat Owner Identity Primitive), ADR-0067 (Reflection Surfacing), ADR-0069 (R2 Artifact Substrate), ADR-0070 (Output Channel Model); the pedagogical north star (Socratic tutor)

> This ADR is a **hypothesis about how to think**, not a design. It commits to a *vocabulary* — a way to differentiate what we ingest — so ADR-0098 can argue about storage without re-litigating meaning. It may be **wrong**, **too complex**, or **insufficient**. When in doubt, prefer fewer classes. Revise on evidence, not taste.
>
> It is also **tutor-scoped**, not a universal ontology: "World" means *reusable know-how the tutor can teach*, not all impersonal knowledge that could ever exist. Read it as a vocabulary for building the Socratic tutor, not a theory of everything.

---

## Context

ADR-0071 ("Two-Source One-Gate Memory Model") set out to handle "memories plus documents" but made two mistakes. It jumped straight to architecture (two graphs, one promotion gate), and it assumed every document is a source of *personal facts*. A recipe, a textbook, and a medical record are not the same kind of thing. The Socratic-tutor north star needs reusable know-how *and* a model of the user's relationship to it — neither of which "personal facts" can express.

Before we can decide where things live, we need to agree what *kinds* of things there are. This ADR establishes that vocabulary and nothing more. The architecture that consumes it is ADR-0098.

---

## Decision — the hypothesis

Two layers.

### Layer 0 — Source

Where knowledge *came from*. A **role** (provenance), **not** a storage mandate. Being a source does not imply we retain the raw bytes.

- **type**: `document | conversation | observation` (extensible)
- **properties**:
  - `co-authored?` — was the agent a *participant* (conversation) or a *reader* (document)? Affects how much downstream curation should trust a derived claim.
  - `streaming?` — ingested whole and once (document) vs. accruing turn-by-turn (conversation).
  - `retention` — verbatim (re-readable) vs. provenance-only (pointer + extracted knowledge, transcript discarded). **Resolved in ADR-0098.**

### Layer 1 — Knowledge classes

*What kind of knowledge* a source yields. Three:

| Class | It captures | Example |
|---|---|---|
| **Personal** | who the user is — their life, relationships, events | "Dr. Chen is my cardiologist" |
| **World** | reusable, impersonal know-how | how a four-stroke engine works |
| **Stance** | the user's *relation to* a piece of World knowledge | "I enjoy this dish"; "Seshat taught me X, mastery 0.6, review next week" |

### Invariants

1. A source is **not** a knowledge class. One source yields many Layer-1 items, of different classes. (A medical textbook the user is studying → *World* content + a *Stance* edge "learning it" + possibly *Personal* facts if annotated with their case.)
2. **Stance** is first-class even though it is structurally a link between a Personal subject and a World object. It is the **pedagogical layer** — preference, mastery, spaced-repetition state. It is the crown jewel, not a footnote.
3. This axis is **orthogonal to `MemoryType`** (`WORKING / EPISODIC / SEMANTIC / PROCEDURAL / PROFILE / DERIVED`). That axis is *lifecycle/derivation*; this one is *subject/ownership*. They compose ("I take metoprolol" = `Personal × SEMANTIC`; "I enjoy coq au vin" = `Stance × PROFILE`). This taxonomy **adds a dimension; it does not replace `MemoryType`.**

---

## Why three (the simplicity claim)

Three *appears* to be the minimum that separates "a fact about me," "a fact about the world," and "my relationship to a piece of the world." Drop to two and pedagogy collapses into either personal trivia or impersonal facts. Go to four and we would be inventing need we do not have yet. On this hypothesis, three is the smallest taxonomy that lets the tutor exist — a claim to be tested by ADR-0098's implementation, not assumed.

---

## What this is deliberately NOT

- **Not an architecture** — no graphs, stores, or databases here. (ADR-0098.)
- **Not a lifecycle** — aging, importance decay, retention policy. (ADR-0098.)
- **Not a replacement for `MemoryType`** — a second axis, not a substitute.

---

## Consequences

- **Ingest must classify, not assume.** The pipeline gains a triage step; "all documents → personal facts" is gone.
- **World and Stance become first-class** — which is what unlocks the pedagogical north star (knowledge-as-learnable-units + the user's mastery of them).
- **Risk, owned.** The hypothesis may *over-cut* (could World and Stance ever be one thing?) or *under-cut* (is there a fourth class hiding in "goals" or "skills"?). It is held loosely; ADR-0098's implementation is the first real pressure test, and evidence — not aesthetics — revises it.

---

## Alternatives Considered

- **One undifferentiated "memory" (ADR-0071's implicit model).** Rejected — cannot express impersonal know-how or the user's relationship to it, so the tutor cannot be built on it.
- **Classify by document type (medical / recipe / receipt / …).** Rejected — document type is a Layer-0 *hint*, not a knowledge class. The same document type can yield different knowledge classes.
- **Four or more classes (goals, skills, intentions, …).** Deferred — no earned need. Add a class when evidence demands it, not in anticipation. For now, a goal like "I want to learn calculus" is read as a *Personal* intention that typically also creates a *Stance* (a not-yet-mastered relation to a World concept) — not a class of its own.

---

## Open Questions → ADR-0098

- One graph vs. many; native cross-class joins vs. physical isolation.
- Tier-3 community/topic summarization for the World/document graph (**genuinely open — never decided**).
- Aging and shifting importance (memory decays; salience changes).
- Transcript retention policy for the `conversation` source type.
- Conversation co-authorship → how much the promotion/curation step trusts agent-derived claims.
- Scale at one year — does the chosen substrate hold.

---

*ADR-0097 is a Proposed hypothesis pending owner acceptance. It defines vocabulary only; ADR-0098 implements it and is where the architecture is decided.*
