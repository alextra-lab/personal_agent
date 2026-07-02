# FRE-727 — Breaking open the "~46% System noise" bucket (live KG, 2026-07-02)

**Date:** 2026-07-02
**Ticket:** [FRE-727](https://linear.app/frenchforest/issue/FRE-727) (Memory Recall Quality)
**Backing ADR:** [ADR-0106](../architecture_decisions/ADR-0106-system-user-knowledge-boundary-dispatch-observe-ground.md) (the System/User knowledge boundary)
**Extends:** [FRE-636 taxonomy-validation spike](2026-06-27-fre-636-taxonomy-validation.md) (the ~46% figure this note decomposes)
**Method posture:** measurement-first, read-only against the live prod Neo4j (no writes; cf. FRE-375 / ADR-0087). Credentials read from `settings`, never guessed (Neo4j lock-out risk on a wrong password).

> **Privacy note.** Generalized. System/infra terms are shown verbatim (not PII); genuine user
> topics are withheld. The owner's name appearing as a plain `Person` entity is generalized to
> "the owner node."

---

## Why this exists

FRE-636 found ~46% of extracted entities are "Operational / non-user-knowledge" and recommended a
System class or pre-taxonomy gate. ADR-0106 needed to know **whether that one bucket is homogeneous**
— because the boundary design hinges on it. It is not: it holds at least three structurally different
kinds, only one of which is genuine noise.

---

## Corpus state (2026-07-02)

- **7,581** `:Entity` (all `memory_type=semantic`) · **2,172** `:Turn` — grown since the FRE-636 spike
  (7,366 / 2,133); nothing gates System material, so it keeps accreting.
- **`class` property does not exist** — every entity returns `class=None`. FRE-637 (class emission) and
  FRE-639 (System gate) are **not deployed**. The System-vs-User machinery lives only in the extraction
  *prompt*, nowhere in storage or query. **The boundary is being designed before it is built.**
- **~23%** (1,718 / 7,581) have NULL/empty descriptions — extraction junk, orthogonal to the System
  question.
- Entity types: Concept 3,791 · Technology 1,571 · Topic 1,139 · Event 402 · Organization 276 ·
  Location 215 · Person 187.

## The bucket, broken open

Sampling operational-looking entities (by description containing agent/harness/healthcheck/telemetry,
ordered by mention count), the single "Operational" bucket splits into:

| Kind | Representative live entities (mention count) | ADR-0106 route |
|---|---|---|
| **(a) Ephemeral machine state** | `Elasticsearch` "status yellow" (719), `Health Check` (20), `Self-telemetry` (15), `System RAM` "usage %" (8), `Backend Healthcheck` (4), `approval_ui_disabled_proceeding` (28), `unauthenticated_request` (7), `web_search_connect_failed` (6) | `ephemeral` → observe + drop (ES only) |
| **(b) The harness as a studied subject** | `ToolLoopGate` "a gate mechanism… dedupes tool calls…" (42), `MCP Gateway` "must be started so tools appear" (17), `Tool Execution` phase (4), `prompts.py`, `Persistent Memory` (10) | `knowledge` (World + Stance) → User KG |
| **(c) Generic tech, ops-framed** | `DNS-based service discovery` (12), `PgBouncer` (14), `TCP` (26), `UTC` (12), `uvicorn` (50) | `knowledge` (World) → User KG |

The discriminating tell is in the description: **(a)** is *state at a moment* ("status yellow",
"usage %", "error occurred") — worthless tomorrow; **(b)** is *a durable fact about how a thing works*
— structurally identical to any World topic the owner studies (ADR-0098 D5's "medical textbook the
owner is studying" example, with "the harness" substituted). The current extraction prompt would stamp
(b) `class=System` and discard it — throwing away pedagogically central knowledge.

The owner's own identity also leaks as a plain `Person` entity (mention 110, "developer of the agent
harness") — a separate ADR-0052 owner-node concern, noted not addressed here.

## Reproducing this

Read-only Cypher against the prod Neo4j (`bolt://…:7687`, `READ` access mode):

```cypher
// totals
MATCH (e:Entity) RETURN count(e);
MATCH (t:Turn)   RETURN count(t);
// class axis present?
MATCH (e:Entity) RETURN e.class AS class, count(*) ORDER BY count(*) DESC;
// empty-description share
MATCH (e:Entity)
RETURN sum(CASE WHEN e.description IS NULL OR e.description='' THEN 1 ELSE 0 END) AS empty, count(*);
// operational-looking sample, by mention count
MATCH (e:Entity)
WHERE toLower(coalesce(e.description,'')) CONTAINS 'agent'
   OR toLower(coalesce(e.description,'')) CONTAINS 'harness'
   OR toLower(coalesce(e.description,'')) CONTAINS 'healthcheck'
   OR toLower(coalesce(e.description,'')) CONTAINS 'telemetry'
RETURN e.name, e.entity_type, e.mention_count, left(e.description,140)
ORDER BY e.mention_count DESC LIMIT 120;
```

Then hand-bucket the sample into (a)/(b)/(c) by the state-vs-fact tell above.

## Limitations

- Single-labeler hand-bucketing of a **120-item** operational-looking sample — directional, not a
  measured rate (same posture as FRE-636). The (a)/(b)/(c) split establishes *heterogeneity*, which is
  all ADR-0106's boundary needs; it does not claim precise proportions.
- The description-contains filter is a coarse frame; a rigorous per-class survival audit over a random
  draw is the follow-up if a hard proportion is ever needed.
