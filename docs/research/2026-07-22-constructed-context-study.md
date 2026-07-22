# Constructed context: procedural extraction + KG-sourced next-turn context (STUDY)

> **Status:** study proposal — undeveloped, gated. Not a build. Sibling to
> `docs/research/2026-07-22-session-summary-kg-opportunity.md` (Workstream A) and the compaction
> repair (Workstream B). **By:** cc-explore. **Date:** 2026-07-22.
> **Gate:** do NOT start until (A) the KG summarizer is fixed and (B) compaction is repaired, and until
> the agent is actually usable for long multiturn sessions. This studies a *convergence*, not a
> standalone feature.

## Thesis

Today "compress the conversation for the next turn" is one lossy prose step (the context compressor).
This study asks whether next-turn context is better **constructed** by resolving each turn into its
*layers* and routing each layer to the substrate whose **lifecycle** it matches — then assembling the
next turn's context from those substrates instead of re-summarizing raw text.

## Anatomy of a multiturn turn (the deconstruction)

| Layer | What it is | Lifecycle | Home |
|---|---|---|---|
| 1. Declarative | facts, entities, relationships | durable | **KG** (have it) |
| 2. Procedural / state | goal, plan, current step, decisions **and rejections**, open loops, focus | **volatile** (mutates each turn) | session-scoped working-state (**missing**) |
| 3. Interactional | reference frame / deixis, dynamics | session-scoped | working-state + learning model |
| 4. Provenance | how we know it (retrieved vs asserted, citations) | durable | KG (partial) |
| 5. Trajectory | the arc / how thinking evolved | derived | construction-trace |

## The three stores (falls out of lifecycle separation)

- **KG** — products of cognition (durable facts). *What is true.*
- **Working-state** — process, live (ephemeral, overwritten each turn). *Where we are.* **Not KG** —
  writing volatile state into a durable graph reproduces the LIVING-Claims / first-write-wins
  pathology (ADR-0097/0098).
- **Construction-trace** — retained *history of the process* (the deltas: rejected branches, step
  transitions, corrections). *How the knowing unfolded.* The substrate for meta-learning; paired with
  an external outcome label it is the teacher signal (see the verifier note).

## The proposed constructed context (three parts)

Next-turn context = **A. KG-declarative** (facts + relationships, already extracted) **+ B. associative
"sparks"** (correlated KG connections *not in the conversation* — entity-X-now relates to entity-Z-from-
a-past-session via relationship-R though never said this turn; KG-only, a compressor can never do it;
ADR-0114 associative-memory thread) **+ C. procedural working-state** (extracted live, session-scoped).

**Procedural extraction (the missing piece):** promote the compressor's prose blob into an explicit,
schema'd, session-scoped object updated each turn — roughly
`{goal, plan, current_step, decisions[], rejected[], open_loops[], focus}`. The compressor's
Decisions/Open-Items headings are already a primitive of this — so this is an *upgrade of a live
component*, not greenfield, and it does **not** land in the KG.

## The freshness-gradient insight (why it's plausible)

The compressor evicts the **middle** (old) and keeps the **tail** (recent) verbatim. By the time a turn
is old enough to be in the evicted middle, background consolidation has very likely already processed
it into the KG. So the eviction boundary and the consolidation-freshness boundary roughly coincide:
**KG-source the (already-consolidated) middle; keep the (maybe-unconsolidated) recent tail verbatim** —
with a freshness guard (fall back to prose-summary for any turn not yet consolidated). The verbatim
tail also carries the procedural fidelity (half-built code, exact error strings) that resists
schematization — so procedural extraction shrinks the middle; it never fully replaces the tail.

## Scope boundaries (hard)

- **Turn-boundary only.** This operates at turn return, NOT inside the inner thinking/tool loop. That
  loop's latency envelope cannot absorb a KG round-trip + extraction per iteration; do not try.
- **Latency is the dominant risk** even at turn boundaries — the study must budget it explicitly.
- **Gated on A + B healthy**, and on the agent being usable for long multiturn sessions (it is not yet).

## Open questions / what to measure before believing it

1. Are "reasoning moves" (hypothesis / evidence-seek / correction / rejection) cleanly extractable, or
   does the model confabulate structure over messy text? **The empirical crux.**
2. Procedural mis-extraction risk: a wrong "we're on step 4" misgrounds the next turn *worse* than raw
   text (which is at least accurate). Needs a correctness check.
3. Latency budget at turn boundary: KG query + associative inference + procedural extraction vs. the
   current single compressor call.
4. How much of continuation genuinely needs verbatim vs. schematizable structure (sizes the tail).

## Convergence

Converges with Workstream A (KG summary/embedding) and B (compaction) **after both are healthy**.
Until then it is a study, deliberately decoupled.
