# ADR-0071: Two-Source One-Gate Memory Model

**Status**: Proposed
**Date**: 2026-05-21
**Deciders**: Project owner
**Related**: ADR-0069 (R2-Backed Artifact Substrate), ADR-0070 (Output Channel Model), ADR-0064 (Inbound User Identity via Cloudflare Access), ADR-0052 (Seshat Owner Identity Primitive), ADR-0060 (Knowledge Graph Quality Stream), ADR-0067 (Reflection Surfacing), FRE-368 ✅, FRE-369 (user uploads), FRE-372 (proposed — Qwen3-VL primary swap)
**Implementation plans**: deferred — produced when downstream FREs are taken up

---

## Context

Seshat's memory graph today is shaped by a single pipeline:

```
turn → entity extraction (qwen3-8b background role) → straight into Neo4j as :Person / :Place / :Event / semantic facts
```

That worked at small volume, with one user, on conversational input only. Three forces now break the assumption that "everything we can extract should become memory":

1. **Document ingestion is imminent.** ADR-0069 shipped the artifact substrate; FRE-369 will land user uploads (photos, screenshots, PDFs, URLs). A 30-page medical record exploded into entity-relation triples destroys signal-to-noise in a graph designed to hold conversational gist.
2. **The existing pipeline has the same flaw.** A 40-turn debugging session about an unrelated tangent reshapes the agent's model of the user just as much as a clinical PDF would — because there is no curation gate today. We have been getting away with it at low volume; we should not extend the anti-pattern to documents.
3. **The graph holds the "soul" of the collaboration.** This framing came from the project owner on 2026-05-21. The core memory graph represents who the user is and what the agent–user relationship is about. Facts do not get to define that relationship just because they were extracted from text the agent processed. They get judged.

Two related industry signals frame the design space. **GraphRAG** (Microsoft, 2024) and **HippoRAG** (NeurIPS 2024/2025) demonstrated that hybrid graph + chunked-retrieval systems outperform pure vector RAG for multi-hop reasoning, but both build their entity graphs *separately* from any conversational memory the agent maintains. **MemGPT / Letta** and **Mem0** demonstrated that agents that *swap memory in and out of context* — explicitly choosing what to surface — outperform agents with monolithic memory. The convergent move across the SOTA: separation of substrates by purpose, with explicit promotion/swap rules between them.

The existing memory graph is not wrong. It is the agent's working soul. It is just one of two stores the agent needs, and it has been operating without the curation gate it deserves.

---

## Decision

Adopt a **two-source, one-gate memory model**: a logically separated document graph (`:Doc` scope) coexists with the existing core soul graph (`:Core` scope) in a single Neo4j Community database, and a **single promotion review function** (run by the brainstem) gates every entry into `:Core` regardless of source.

### D1 — Logical separation via labels, not databases

All Neo4j nodes carry a scope label:

- `:Core` — the soul graph. Entities and relationships about the user, their relationships, ongoing themes, the agent–user collaboration's history.
- `:Doc` — the document graph. Entities, claims, and chunks extracted from uploaded artifacts. Bookkeeping for what the agent has been *shown*, not what it *remembers*.

Reasoning for logical (not physical) separation:

- Neo4j Community Edition supports a single user database; multi-database is Enterprise-only and not justified at our scale.
- Labels are first-class in the storage engine — the planner uses them as the leading filter and label-scoped scans are nearly free.
- Logical separation is reversible: a future move to multi-database (Enterprise migration, or to two co-located instances) is a label-rename, not a data-model change.

`:Core` and `:Doc` are mutually exclusive — no node carries both. Cross-graph references go through edges with explicit semantics (see D6).

### D2 — Document graph ontology

`:Doc` uses an ontology deliberately distinct from `:Core`:

| Node | Role |
|---|---|
| `:Doc:Document` | One per artifact ingested. Properties: `artifact_id` (FK → `artifacts.id`), `title`, `ingested_at`, `source_type` (upload / capture / note). |
| `:Doc:Chunk` | A retrievable chunk of a document. Properties: `chunk_id`, `text`, `embedding_ref` (FK → `artifact_chunks.id`), `page` / `offset`. |
| `:Doc:DocEntity` | A noun-phrase extracted from a chunk. Properties: `entity_type` (person / medication / condition / place / org / date / amount / …), `surface_form`, `normalized_form`. |
| `:Doc:Claim` | A subject-predicate-object proposition the agent extracted from a chunk. Properties: `subject_ref`, `predicate`, `object_ref`, `confidence`. |

Edges within `:Doc`:

```
(:Doc:Document)-[:CONTAINS]->(:Doc:Chunk)
(:Doc:Chunk)-[:MENTIONS]->(:Doc:DocEntity)
(:Doc:Claim)-[:ABOUT {role: 'subject'|'object'}]->(:Doc:DocEntity)
(:Doc:Claim)-[:EVIDENCED_BY]->(:Doc:Chunk)
```

**Crucial property**: a `:Doc:DocEntity {text: "Dr. Chen"}` is not (yet) a person in the user's life. It is a noun-phrase that appeared on page 4 of a clinical note. The promotion gate decides whether Dr. Chen is also a `:Core:Person`.

Symmetric ontology across both graphs was considered and rejected (Alt E) because it conflates "mentioned in a source" with "exists in the agent's model of the user."

### D3 — Candidate queue in Postgres, not Neo4j

Extracted facts awaiting promotion review live in a Postgres `candidate_facts` table, not as `:Candidate` nodes in Neo4j. Reasoning: Neo4j should hold *authoritative state* (what the agent considers true at `:Core` scope, what the agent has cataloged at `:Doc` scope). Hypothetical or pending state is queue work, and queue work belongs in a relational store with explicit status columns.

Schema (executable detail will live in `docker/postgres/migrations/`):

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PRIMARY KEY` | |
| `user_id` | `UUID NOT NULL` | FK to `users.user_id` |
| `source_type` | `TEXT NOT NULL` | `'turn'` or `'document'` |
| `source_id` | `UUID NOT NULL` | `turn_id` or `artifact_id` |
| `source_chunk_id` | `UUID NULL` | For doc-sourced candidates, which chunk |
| `subject` | `JSONB NOT NULL` | Normalized entity reference |
| `predicate` | `TEXT NOT NULL` | |
| `object` | `JSONB NOT NULL` | Entity reference or literal |
| `confidence` | `REAL NOT NULL` | From extractor, 0..1 |
| `status` | `TEXT NOT NULL` | `'pending' \| 'promoted' \| 'rejected' \| 'awaiting_user'` |
| `decision_reason` | `TEXT NULL` | LLM rationale on review |
| `reviewed_at` | `TIMESTAMPTZ NULL` | |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |

Indexes: `(user_id, status, created_at)`, `(source_type, source_id)`.

### D4 — One promotion gate, two source types

Both pipelines converge on a single brainstem function:

```
review_candidates_for_promotion(user_id, batch_size) -> ReviewResult
```

It pulls `pending` candidates, calls an LLM-judged review (D5), writes promotions to `:Core` and rejections to the queue's `status` column. Source-symmetry is the point: a fact extracted from a turn and a fact extracted from a PDF face the same gate.

Phased rollout (D8) initially limits the gate to document-sourced candidates; the conversation-sourced pipeline keeps its current direct-write path until the gate is proven, then is retrofitted.

### D5 — Review is LLM-judged with a structured rubric

A new model role `promotion_review_role` is added to `config/models.cloud.yaml`, defaulting to **Sonnet** (judgment-heavy work; the cost is bounded by upload + consolidation cadence). The review prompt receives:

- The candidate fact (subject, predicate, object)
- Provenance (source artifact or turn snippet)
- A snapshot of the user's existing `:Core` subgraph touching the same entities (for novelty and overlap detection)
- The scoring rubric (below)

Returns a structured judgment: `PROMOTE` (with 1-sentence rationale), `REJECT` (with rationale), or `ASK_USER` (with a phrased question).

V1 rubric — the brainstem composes weights into a final call:

| Signal | Weight | Interpretation |
|---|---|---|
| **About the user personally** | high | Subject or object references the owner `:Person {is_owner: true}` |
| **Concerns a known relationship** | medium | Subject/object overlaps an existing `:Core` entity for this user |
| **Recurring across sources** | medium | Same claim or entity appears in ≥2 sources |
| **High-significance entity type** | low → medium | Medications, conditions, ongoing events vs. one-off mentions |
| **Explicit user signal** | maximal | User has marked the source or claim as worth remembering |

Weights are tunable. The rubric will evolve as we accumulate review traces; ADR-0067 reflection-surfacing already provides the telemetry stream we need to learn from past decisions.

### D6 — Provenance is preserved on every promotion

Every node and edge added to `:Core` through promotion carries a back-reference to its source:

```
(:Core:Person)-[:KNOWN_FROM]->(:Doc:Document)
(:Core:Person)-[:KNOWN_FROM]->(:Core:Turn)
(:Core:Fact)-[:DERIVED_FROM]->(:Doc:Claim)
```

This is the only allowed cross-scope edge type. Reasoning:

- The agent can always answer "where did I learn that?" by traversing `:KNOWN_FROM`.
- Demotion is straightforward: removing the promoted node leaves the source artifact and its `:Doc` graph intact.
- The user can audit the soul graph back to its sources at any time.

`:KNOWN_FROM` is the conceptual analog of W3C-PROV (used in ADR-0052's `:PARTICIPATED_IN` edge for conversational turns); the existing pattern extends cleanly to document sources.

### D7 — Promotion uses MERGE, not duplicate

When a `:Core:Person` already exists for the user (e.g., "Dr. Chen" promoted from a prior medical record), a new candidate referencing the same entity MERGEs, not inserts. The dedup discipline from FRE-342 (`memory/dedup.py`) extends directly: the `user_id IS NOT NULL` filter that prevents owner-anchor collisions also gates promotion-time merges.

A `:Core:Person` may carry multiple `:KNOWN_FROM` edges over time — one per source that contributed to the agent's knowledge of that person.

### D8 — Phased rollout: docs first, conversation retrofit second

The new pipeline ships in stages, with the doc-graph as a low-risk testbed:

| Phase | Scope | What ships | Risk |
|---|---|---|---|
| **P1** | Doc-graph schema + ingest | `document_memory/` module, `:Doc` ontology, `candidate_facts` table, `search_documents` tool | Low — no impact on existing memory |
| **P2** | Promotion review for doc-sourced | `review_candidates_for_promotion()`, `promotion_review_role`, narrate-and-confirm UX (D9) | Low — only doc candidates flow through gate |
| **P3** | Conversation pipeline retrofit | Existing entity-extraction pipeline writes to `candidate_facts` instead of directly to `:Core`; same gate applies | Medium — touches the production memory path |
| **P4** | Backfill review (optional) | Audit existing `:Core` entries via the gate retroactively; flag for user review where confidence is low | Medium — opt-in, not on by default |

P3 specifically runs in shadow mode first: candidates are queued and reviewed but `:Core` writes continue via the legacy path. We diff: did the gate make the same decision? Where it didn't, we examine. After a defined parity window, the legacy path is removed.

### D9 — Narrate-and-confirm UX (v1)

The agent surfaces its promotion decisions to the user explicitly, after a document is ingested:

> *I've read the 2024 cardiology report. Three things I'd like to remember as part of what I know about you:*
> - *You're taking metoprolol 25mg daily (since March 2024)*
> - *Dr. Chen is your cardiologist*
> - *Your ALT was borderline elevated in March; Dr. Chen wanted a follow-up in 6 months*
>
> *The rest stays in [the document](artifacts.frenchforet.com/...). Should I remember anything else, or none of these?*

Mechanism: a new `proposed_promotions` block in the artifact card (PWA, ADR-0070 Tier 3) lists candidate items with confirm / reject affordances. Confirm flips queue status to `'promoted'` and triggers the `:Core` write; reject flips to `'rejected'` with a user-supplied reason recorded for the rubric to learn from.

Fully-autonomous promotion (no confirmation) is rejected for v1 (Alt D). It can be enabled later for high-confidence items once the rubric has accumulated enough trace data to justify trust.

### D10 — Privilege & sharing posture unchanged

`:Doc` nodes inherit ADR-0064's per-user ownership semantics: a user's documents and their derived `:Doc` subgraph are not visible to other users; promotion only produces `:Core` entities scoped to the same `user_id`. The placeholder for cross-user sharing (FRE-345) applies to both scopes uniformly.

---

## Consequences

### Positive

- **The soul stays clean.** The agent's model of who the user is is curated, not accumulated by default. Promotion is an explicit act, not an extraction side-effect.
- **The fix applies symmetrically.** The same gate cleans up the existing conversation pipeline, not just the new document one. We were planning to add docs as a feature; we are instead fixing a load-bearing piece of the brain.
- **Provenance is built in.** Every `:Core` fact can be traced back to its source — a turn, a document, a chunk. The agent's "where did I learn that?" answer becomes structural, not heuristic.
- **Reversible to physical isolation.** If we ever outgrow logical separation, the move to multi-database is a label-rename and a connection-string change. The data model survives.
- **Aligned with SOTA direction.** GraphRAG-style structured + chunked retrieval over documents; MemGPT-style explicit memory swapping; HippoRAG-style anchored traversal. The architecture is in the same family as the current best ideas without copying any one of them.
- **The brain becomes intentional.** Promotion review is a small narrative the agent makes about itself ("I read this; here's what I think matters"). The user sees and shapes that narrative. This is the cognitive-architecture work the project is for.

### Negative / Risks

- **Promotion adds latency and cost.** Each upload triggers one Sonnet call per ~5–20 candidate facts. Cost is bounded by upload cadence (low) but is no longer zero. Mitigation: bounded batch sizes, the budget gate from ADR-0065 already applies.
- **Promotion can be wrong.** An over-cautious gate forgets things the user expected to be remembered; an over-eager gate clutters the soul. Mitigation: D9's narrate-and-confirm catches both classes early; rubric weights are tunable per the trace data ADR-0067 already collects.
- **Label discipline is enforced by convention + tests, not by the database.** A query that forgets to filter by scope leaks across graphs. Mitigation: a test scans all production Cypher for unscoped `MATCH` clauses; CI fails on violations.
- **Conversation pipeline retrofit (P3) touches production memory writes.** Risk of regression in semantic-fact accumulation. Mitigation: shadow-mode rollout with diff-against-legacy parity gate; only flip after the gate matches legacy decisions within a defined tolerance.
- **The candidate queue can grow unboundedly** if review falls behind upload. Mitigation: bounded retention (90 days default for unreviewed candidates), brainstem schedules review automatically on idle, ADR-0040 Linear-as-feedback can surface persistent backlog.
- **New ontology must be authored carefully.** `:DocEntity` and `:Claim` are not just `:Person` and `:Fact` with a different label — getting the shape wrong forecloses promotion patterns. Mitigation: this ADR commits to the schema; refinement happens via amendment, not on-the-fly.

### Neutral

- **Memory protocol types are unchanged.** `MemoryType` enum (`WORKING` / `EPISODIC` / `SEMANTIC` / `PROCEDURAL` / `PROFILE` / `DERIVED`) continues to describe the `:Core` graph. `:Doc` is a separate concern at a different layer of the system; it does not need a `MemoryType` variant.
- **Substrate is unchanged.** Bytes still live in R2 per ADR-0069. Chunks still live in `artifact_chunks` (Postgres + pgvector). The new module operates on metadata already produced by FRE-369's ingest pipeline.
- **`recall_personal_history` and `search_memory` continue to query `:Core` only.** No agent-visible change to existing tools. A new `search_documents` tool is added; the LLM picks between them.

---

## Alternatives Considered

### A. Continue as today — single graph, no gate

*Rejected.* This is the current state, and the fact that we're writing this ADR is acknowledgment that it's wrong. Continuing means the document pipeline inherits the same anti-pattern.

### B. Two physical Neo4j databases

*Rejected.* Neo4j Community supports one user database; two databases requires Enterprise pricing. The label-based separation gives us equivalent logical isolation with the same database. If scale ever demands physical separation, the migration path is preserved.

### C. Document content in pgvector only, no graph at all

*Rejected.* Pure chunk retrieval works for "what did the document say about X" but cannot answer multi-hop questions ("which of my doctors prescribed which medications, across all my medical records") that graph traversal handles natively. The `:Doc` graph gives us multi-hop over document content; pgvector gives us text fidelity. Both, not either.

### D. Fully autonomous promotion (no user confirmation)

*Rejected for v1.* Promotion criteria are unproven; user confirmation in the loop catches errors early and produces trace data that lets us tune the rubric. Once high-confidence patterns are stable, a future ADR amendment can enable autonomous promotion for above-threshold items.

### E. Symmetric ontology — `:Person`, `:Place`, `:Event` in both graphs distinguished only by `:Core` / `:Doc` label

*Rejected.* A `:Person` mentioned in a document is not the same kind of thing as a `:Person` in the user's life — it is a *mention*, a noun-phrase, possibly the same person, possibly a homonym, possibly a fictional reference. Distinct ontology (`:DocEntity`, `:Claim`) keeps that distinction honest and makes promotion an explicit type-converting operation, not a label flip.

### F. Promotion gate runs synchronously inside the ingest path

*Rejected.* Promotion review is a judgment-heavy LLM call; running it inline blocks upload completion on a multi-second LLM round-trip. Asynchronous brainstem review (ADR-0067 reflection-surfacing precedent) keeps the upload responsive and lets review batch across the multiple candidates a single document produces.

### G. Build a third graph for "external knowledge" (web, public sources)

*Rejected for now.* The two-graph distinction (soul vs. shown) is sufficient for current needs. A future "world knowledge" scope (encyclopedic facts the agent learned but not from the user or from user-supplied documents) can be added as `:World` if/when the need is concrete. Premature now.

---

## Implementation Pointers

Files and modules touched as this ADR is implemented across its phases:

**New (`document_memory/`)** — mirror of `memory/` for `:Doc` scope:
- `document_memory/schema.py` — `:Document` / `:Chunk` / `:DocEntity` / `:Claim` definitions
- `document_memory/ingest.py` — accepts an artifact, extracts entities/chunks/claims, writes `:Doc` subgraph
- `document_memory/queries.py` — `search_documents`, `get_claims_for_artifact`, `get_chunks_for_query`
- `document_memory/extractor.py` — per-modality extraction (text via `pymupdf`, image via vision call or VL native, URL via `trafilatura`)

**New (`brainstem/promotion.py`)**:
- `review_candidates_for_promotion(user_id, batch_size)`
- LLM-judged rubric application; queue → `:Core` writes
- Hooks into existing brainstem scheduler (ADR-0067 precedent)

**New (`tools/`)**:
- `search_documents` tool — agent-callable retrieval over `:Doc` + chunks
- `confirm_promotion` / `reject_promotion` tools — wired to PWA confirm/reject affordances

**Modified (`memory/`)**:
- `memory/queries.py` — all `MATCH` clauses gain `:Core` scope filter
- `memory/dedup.py` — promotion-time merge path
- `memory/protocol.py` — unchanged
- New test: AST scan ensures no production Cypher omits scope filter

**Modified (`config/models.cloud.yaml`)**:
- New `promotion_review_role` (default Sonnet)
- `entity_extraction_role` continues for both turn and document extraction; the difference is downstream (queue vs. direct write)

**Modified (`docker/postgres/migrations/`)**:
- New migration: `candidate_facts` table + indexes

**Modified (PWA `seshat-pwa/`)**:
- `ArtifactCard.tsx` — `proposed_promotions` block with confirm/reject UI
- `agui-client.ts` — confirm/reject endpoints

**Modified (`service/`)**:
- New endpoint `POST /api/v1/promotions/{candidate_id}/decide` (confirm | reject | edit)

Implementation plans for each phase will be written in `docs/superpowers/plans/YYYY-MM-DD-fre-XXX-*.md` when the corresponding FRE is taken up. ADR-0071 deliberately does not produce a single mega-plan; the phased rollout means four smaller plans, gated independently.

---

## Verification

Acceptance criteria for the ADR-0071 design as a whole, beyond per-FRE plan ACs:

| AC | What |
|---|---|
| **AC-1** | Every Cypher query in `memory/` filters by `:Core`. Enforced by `tests/memory/test_scope_discipline.py` — fails CI on unscoped `MATCH`. |
| **AC-2** | Every Cypher query in `document_memory/` filters by `:Doc`. Same test, complementary direction. |
| **AC-3** | Promotion of an entity that already exists in `:Core` MERGEs, does not duplicate. Regression test: `tests/brainstem/test_promotion_merge.py`. |
| **AC-4** | Every promoted `:Core` node has at least one `:KNOWN_FROM` edge. Constraint enforced at write site; regression test. |
| **AC-5** | Demotion of a promoted entity (removing `:Core` node) leaves the `:Doc` source intact. Round-trip test. |
| **AC-6** | A `candidate_facts` row's status moves through `pending → promoted/rejected/awaiting_user` exactly once. Status transition test. |
| **AC-7** | Narrate-and-confirm UX: user confirming a proposed promotion produces a `:Core` node within 5s on a warm system. |
| **AC-8** | Phase 3 (conversation retrofit) shadow-mode parity ≥ 95% with legacy direct-write path before legacy is removed. Measured over a defined trace window. |
| **AC-9** | Cost: end-to-end promotion review for a typical 10-page PDF (~15 candidate facts) ≤ $0.05 at Sonnet pricing. |

Each phase (P1–P4) will produce its own implementation plan with its own AC subset.

---

## Related

- **ADR-0064** — Inbound User Identity (auth + per-user ownership semantics that scope both graphs)
- **ADR-0067** — Reflection Surfacing in Context Assembly (precedent for brainstem-scheduled LLM judgment work; trace store for tuning rubric)
- **ADR-0069** — R2-Backed Artifact Substrate (physical layer for documents whose entities populate `:Doc`)
- **ADR-0070** — Output Channel Model (the artifact card surface used by D9's narrate-and-confirm UX)
- **ADR-0060** — Knowledge Graph Quality Stream (the existing quality-governance pattern that promotion review complements)
- **ADR-0052** — Seshat Owner Identity Primitive (`:KNOWN_FROM` is a sibling of `:PARTICIPATED_IN`; both are W3C-PROV-aligned)
- **FRE-368** ✅ — agent artifact tools (the producers of `:Doc` content)
- **FRE-369** — user uploads (the first new consumer of this ADR)
- **FRE-372** (proposed) — Qwen3-VL primary swap (parallel; affects how images become `:Doc` content but does not gate this ADR)
- **FRE-226** — auto-updating CLAUDE.md (future candidate for the same gate)
- **HippoRAG**, **GraphRAG**, **MemGPT/Letta**, **Mem0** — research influences cited in Context

---

*ADR-0071 is Proposed pending owner acceptance. On acceptance, FRE-369 may proceed against it; conversation-pipeline retrofit (P3) gets its own approval gate after P1+P2 ship and the gate's behavior is observed in production.*
