# Seshat Program Architecture — Layers, Substrate, and the Reconciliation Loop

> **Living document.** This spec evolves as the program learns. It governs *how the work is
> organized* — which capabilities are shared substrate, which are consumers, and what order they
> must be built in. The ADRs it spawns govern the individual architectural decisions; when this
> document and an ADR conflict, the ADR governs the decision and this document governs the
> portfolio shape around it.
>
> **Status:** Draft — 2026-06-06
> **Origin:** FRE-504 (architecture review — decomposition first-run, 7 threads → ADRs), `adr` session
> **Seed:** `docs/research/2026-06-06-decomposition-first-run-findings.md` ·
> `docs/superpowers/plans/write-this-all-up-dynamic-graham.md` (§ Architecture Review)
> **Spawns:** ADR-0088 (Execution Topology Observability Contract) · ADR-0089 (Artifact Execution
> Security Model)
> **Audience:** Anyone scoping a project, sequencing a wave, or placing a ticket. The canonical map
> for "where does this work belong and what must exist before it."

---

## 1. Why this document exists

The portfolio grew bottom-up — one project per incident, per ADR, per thread. That produced real
work but two structural problems:

1. **Cross-cutting capabilities got trapped inside single consumers.** The clearest case: the
   *measurement substrate* (result-type taxonomy, route-trace ledger, canonical eval set) lives
   inside the **Seshat Pedagogical Architecture** project as "M2: Mapping & Measurement" — but Turn
   Cost, Turn Reliability, Memory Recall, and Inference Architecture all need it just as much.
   Pedagogy is the most *demanding* consumer of measurement, not its *owner*.

2. **Capabilities outpaced their integration.** The 2026-06-06 decomposition first-run
   (trace `87cbd720`) shipped a new execution topology with backend cost-joinability but **no live
   surface, no memory grounding, and silent degradation** — because observability was bolted to one
   execution path (`orchestrator/executor.py`) and never treated as a shared concern. We could not
   *see* what the new topology did.

Both problems have the same root: **we never named the difference between substrate and consumer,
or the order they depend on each other.** This document does.

---

## 2. The organizing principle: substrate pillars vs. feature consumers

The portfolio already contains the answer — the **Memory Recall Quality** project opens with it:

> *"a substrate pillar with multiple consumers, not a feature of any one stream."*

Generalized:

- **Substrate pillar** — a cross-cutting capability that *many* streams depend on. Owned in its own
  right, with its own definition-of-done. Examples: observability, memory recall, inference
  plumbing, context/injection quality.
- **Feature consumer** — a stream that delivers user-facing or agent-facing behavior by *standing
  on* the substrate. Examples: the Socratic-tutor pedagogical layer, turn-cost optimization,
  reliability hardening.

The failure mode we are correcting is a substrate pillar **hidden inside** a consumer. When that
happens it is under-invested (it serves one master), it is invisible to its other consumers, and it
silently rots. Observability bolted to `executor.py` is exactly this.

**Rule:** if more than one consumer needs a capability *as infrastructure* — emission, storage,
retrieval, plumbing — it is substrate; lift it. The rule does **not** sweep up everything shared: a
shared *policy* (how to route, what to ground) is not infrastructure — it is L1 intent or an L3
consumer concern. Substrate is the machinery; policy is what decides how the machinery is used. Keep
them in different layers (this is exactly the Inference plumbing-vs-policy split in §4).

---

## 3. The layer model

```
┌────────────────────────────────────────────────────────────────────────────┐
│ L3  CONSUMERS  — features built on the substrate                             │
│     • Pedagogical Layer        (M3 rituals / spaced-rep / concept extraction │
│                                  / field notes / learning model;             │
│                                  M4 delegation policy, cross-thread corr.;    │
│                                  M5 pedagogical eval harness)                 │
│     • Turn Cost & Latency       (decomposition cost, artifact-build efficiency)│
│     • Turn Reliability          (loud degradation, misclassification traps)   │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                 │ build on
┌───────────────────────────────▼──────────────────────────────────────────────┐
│ L2  SUBSTRATE PILLARS  — cross-cutting capabilities, many consumers            │
│     • Memory Recall Quality     (write-completeness + retrieval; ADR-0087)     │
│     • Inference Architecture     (model tier / thinking / delegation PLUMBING + │
│                                   planner/decomposition mechanism; ADR-0082)   │
│     • Context & Memory Injection (ADR-0081 Extended — D4-trim / D5 / D6)       │
│     • Artifact Execution Security (sandbox-not-sanitize; ADR-0089)            │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                 │ are evaluated against
┌───────────────────────────────▼──────────────────────────────────────────────┐
│ L1  THE MATRIX  — intended traversal (the normative spec)                      │
│     For each use-class: intended topology, tools, AND knowledge access at both │
│     the orchestration and the pedagogical layer.                              │
│     ← FRE-453 canonical set + a knowledge-access column + the decomposed case  │
└───────────────────────────────┬──────────────────────────────────────────────┘
                       reconciled │↕ with  (§5 the loop)
┌───────────────────────────────▼──────────────────────────────────────────────┐
│ L0  OBSERVABILITY SUBSTRATE  — makes ACTUAL traversal observable (reconcile/ship gate) │
│     • Execution Topology Observability Contract  (ADR-0088): emit status,      │
│       cost, and degradation at a layer EVERY topology passes through           │
│     • Route-Trace Ledger        (FRE-452): actual traversal, per turn          │
│     • Result-Type Taxonomy      (FRE-451): the vocabulary L1/L0 are written in │
└────────────────────────────────────────────────────────────────────────────┘
```

The most important property of this stack is about **validation, not authoring.** L1 (the intended
matrix) can be — and should be — *written forward* without L0; declaring how a use-class *should*
traverse needs no telemetry. What needs L0 is **reconciliation**: you cannot measure, optimize, or
ground what you cannot observe, so you cannot tell whether actual matches intended until L0 exists.
The decomposition first-run failed precisely here — the new topology ran below the observability
floor, so its *actual* traversal was unmeasurable even though we could have stated its *intended*
one. So: author L1 in parallel with L0; do not ship L2/L3 *to default* ahead of the L0 that proves
them.

---

## 4. The layers in detail

### L0 — Observability substrate (functional, not technical)

We declare observability a **functional** capability with a definition-of-done, *deliberately*. In
most systems observability is a technical cross-cutting NFR — and that assumption is exactly what
bolted it to `executor.py` and let it die at the sub-agent boundary. Calling it functional forces
acceptance criteria and an owner; calling it technical lets it stay best-effort. We choose the
forcing function.

L0 contains these pieces:

- **Execution Topology Observability Contract (ADR-0088).** Today `turn_status` / `STATE_DELTA` and
  the cost/token meter emit from the single-agent loop in `orchestrator/executor.py`; the sub-agent
  path (`orchestrator/sub_agent.py`) and the expansion controller emit none. The backend trace
  ledger booked the correct cost ($0.9028) while the user-facing meter showed the primary-only
  tally ($0.57). The contract: **status, cost, and degradation are trace-scoped concerns emitted at
  a boundary every execution topology crosses — primary loop, sub-agent fan-out (HYBRID),
  decomposition (DECOMPOSE/DELEGATE), and the future planner-executor — not bolted to one loop.**
  This ADR also defines the **observable-first done bar**: a new orchestration capability is not
  shippable-to-default until it emits status, aggregates cost, and signals degradation *loudly*.
  - **ADR-0088's central deliverable is to *name the concrete common boundary*** — the actual point
    in the code that every topology provably passes through, where emission attaches. Until that
    boundary is identified the contract is aspiration, not implementable. Candidate seams to
    evaluate, not assume: the **trace-scoped cost ledger** (which already aggregates all 9 calls
    correctly — `api_cost_recorded` booked $0.9028 while the meter lied), the **Redis event bus**
    (`events/`), and the **AG-UI transport** (`transport/agui/`). ADR-0088 must pick one and prove
    the topologies converge on it.
  - *Shipped:* **FRE-501** (merged 2026-06-06, PR #171) is the tactical first brick — it rolls
    sub-agent **and planner** cost into the live turn meter and emits `turn_status` during expansion.
    Tellingly, it did so **per-path** (adding emission to `executor.py`, `expansion_controller.py`,
    and `sub_agent.py` separately) — i.e. the very per-loop pattern this contract exists to replace.
    ADR-0088's job is to generalize that into **one durable boundary** so the next topology inherits
    emission instead of re-implementing it; FRE-501 and the contract must not drift.
- **Route-Trace Ledger (FRE-452).** Per-turn capture of stimulus → gateway classification → model
  path → cognitive work actually done → result type. The *actual* half of the reconciliation loop.
  It is only as trustworthy as the contract above: until ADR-0088 lands, the ledger is blind on
  every decomposed turn.
- **Sub-agent auditability (FRE-505).** Each sub-agent's *input context*, *output*, and *injected
  digest* captured and inspectable — the engineering-visibility complement to the user-facing meter.
  Answers "what was each sub fed, what did it do, what did it return," which the first-run could only
  reconstruct by forensics.
- **Gate-decision telemetry (FRE-506).** Every deterministic gate logs its decision —
  `pass`/`reject`/`strip`/`bypassed` — starting with the sandbox gate that silently let ~33 KB of JS
  through. This is the L0 dependency ADR-0089 (§4 L2) needs to *validate* enforcement.
- **Result-Type Taxonomy (FRE-451).** Strictly the **vocabulary/schema** the ledger and matrix are
  written in — a definitional artifact co-located with L0, not observation itself. Its central move —
  separating **orchestration events** (what the harness did) from **pedagogical outcomes** (what the
  learner got) — is the lens that resolves the knowledge-gating question in §6.

### L1 — The matrix (intended traversal)

The matrix declares, for each known use-class, **how we intend the interaction to traverse the
harness**: which topology, which tools, and — the column we are adding here — **knowledge access at
both layers**:

- *Orchestration-required* grounding: what finishing the task needs. (`get CPU usage` → none.)
- *Pedagogically-desired* grounding: what the turn should opportunistically surface regardless.
  (`get CPU usage` → maybe "third spike this week; last time it was the leak in X" —
  `cross_connection_made`.)

The seed is **FRE-453** (canonical eval set, 7 turn types, each with an *expected model path* and
*expected result types* — already two columns of the matrix). It needs two additions to serve this
program:

1. **A knowledge-access column** (the two-layer split above), first-class rather than implicit.
2. **The use-class that actually broke** — a **build/teach-an-artifact-about-a-previously-discussed
   subject** case, seeded from trace `87cbd720`. The eval set exists to expose "where SINGLE-path
   routing silently strips the tutor"; today it omits the turn where the tutor *was* stripped.

### L2 — Substrate pillars

Each is a cross-cutting capability with multiple L3 consumers:

- **Memory Recall Quality** (ADR-0087, FRE-435) — write-completeness + retrieval quality,
  measurement-first. Consumes L0 (to measure retrieval) and L1 (the matrix declares knowledge
  access). **The §6 knowledge-gating decision is owned here**, derived from the matrix — not patched
  into `recall_controller.py` off one trace.
- **Inference Architecture** (ADR-0082) — the **plumbing and mechanism** of how inference is
  dispatched: the `model_tier` field on `GatewayOutput`, `_determine_initial_model_role()`, the
  delegation/decomposition machinery, and the **planner/decomposition mechanism** itself. This is
  clean substrate — it depends only on L0 (its gate is the route-trace ledger, FRE-452) and on L1's
  declared paths. **Critical distinction (per Codex review): the *plumbing* is L2; the *routing
  policy* that decides which path a use-class takes is not.** That policy is L1 intent (the matrix
  declares intended paths) realized by an L3 consumer (the pedagogical delegation policy, M4).
  ADR-0082 must stay neutral infrastructure and must not absorb pedagogical routing policy.
  - **Planner reliability lives here.** The first-run's root cause — planner schema-validation
    failure degrading to tool-less generic sub-agents (FRE-502) — is a *mechanism* defect, and the
    decomposition mechanism is this pillar. L0 makes that degradation *visible* (loud); this pillar
    makes the planner *robust* (schema-validation recovery + discovery-aware fallback). Visibility
    and robustness are different jobs in different layers; both are required.
- **Context & Memory Injection** (ADR-0081 Extended) — D4-trim / D5 cold-tier reinjection / D6 pin.
  Keeps injected context high-quality under the cache-aware frozen layout.
- **Artifact Execution Security** (ADR-0089) — move from *sanitize output* (FRE-496 strip-and-
  deliver, adversarial and lossy) to *sandbox execution* (FRE-397 tiers: SVG → sandboxed-JS iframe
  → JSX). FRE-500 flags the strip enforcement off as a temporary bridge.

### L3 — Feature consumers

- **Pedagogical Layer** — the Socratic-tutor features: M3 (rituals, spaced repetition, concept
  extraction, field notes, learning model), M4 (delegation policy, cross-thread correlation), M5
  (pedagogical eval harness). These *consume* memory recall, the matrix, observability, and
  inference plumbing. They are the most demanding consumer — which is why measurement must not live
  inside them.
- **Turn Cost & Latency** — consumes L0 (real decomposed-cost roll-up) and Inference (the
  decomposition machinery).
- **Turn Reliability** — consumes L0 (loud degradation) and Inference.

---

## 5. The reconciliation loop (L0 ↔ L1)

The matrix (intended) and the ledger (actual) form a **spec ↔ observation control loop**. The unit
of learning is neither alone — it is **the gap between them.**

```
   L1 matrix (intended)  ──declare──►  how we think each use SHOULD traverse
            ▲                                        │
            │ reconcile (loud)                       │ measure
            │                                        ▼
   L0 ledger (actual)    ◄──observe──   how each use DID traverse
```

Every gap resolves in **one of two directions, as an explicit, reasoned choice**:

- **Conform the harness to the matrix** — the ledger shows behavior the matrix says is wrong.
  (build/teach got shallow grounding; matrix says deep → fix the harness.)
- **Conform the matrix to the harness** — the ledger reveals our intent was wrong or the harness
  found something better. (we declared a use-class needs HYBRID; it does fine SINGLE → update the
  matrix.)

**A gap left silently unreconciled is silent degradation one level up** — the same failure we are
fixing in the harness, now in our own process. The loop must be *loud or it rots*.

This loop is the agent's own **episodic → semantic** consolidation, lifted to the program level: the
ledger is episodic (what happened), the matrix is semantic (consolidated belief about what should
happen), reconciliation is promotion. Therefore the matrix and ledger are **durable, versioned
artifacts** — the canonical record of how we currently believe Seshat should behave. They are also
the seam where, eventually, the harness reads its *own* ledger against the matrix and proposes
amendments. We **engineer the matrix forward**; we leave the ledger as the instrument through which
the harness later audits itself. ("Reverse-engineering current behavior" is the harness's future
job, not the basis for our decisions now.)

> **Status of the loop: principle, not yet a running control system.** This section defines *intent*,
> not an operating mechanism. Before the loop is something the program (or the harness) actually
> *runs*, it needs an owner, a cadence, a gap schema (how a matrix↔ledger divergence is recorded),
> reconciliation thresholds (what size/recurrence of gap forces a decision), and a review surface.
> Operationalizing the loop is **itself a deliverable** — an L0/L1 follow-up ticket, not an
> assumption downstream work may lean on today. Until then, treat "reconcile loudly" as a discipline
> humans apply by hand when reading the ledger, not an automated controller.

---

## 6. Worked example — the knowledge-gating decision

The decomposition run exposed that deep recall is gated to `task_type == CONVERSATIONAL`
(`request_gateway/recall_controller.py:173`). "Teach me about X" classifies `TOOL_USE`, so the
*exact* pedagogical use case is structurally denied deep grounding. This document does **not** decide
the fix — it routes it:

- The current gate is **eligibility gating by output-shape** — the wrong primitive. It fuses three
  separate decisions: *eligibility* (may this touch knowledge?), *triggering* (when / how hard do we
  pull?), and *budget* (how much survives into context — the 500-tok cap that trimmed 10→2).
- **One candidate hypothesis** (illustrative only — *not decided here*; it is owned by Memory Recall
  Quality / ADR-0087+FRE-435 and must be justified against that program's measurement before it
  ships): replace the categorical eligibility gate with a cheap relevance probe, so grounding becomes
  *emergent from relevance* rather than *imposed by category*, driven by knowledge-building intent
  rather than output shape, split across the two L1 layers. This document records the hypothesis to
  show *how* the routing works; it explicitly does **not** ratify it. Other resolutions (keep a gate
  but widen its axis; tier by budget; reclassify to a memory task) are equally open and decided
  there, not here.

This is the template for how the program works: a defect surfaces in a consumer (decomposition), the
*decision* lands in the right substrate pillar (Memory Recall), and the matrix↔ledger loop validates
it — engineered, not reverse-engineered. The architecture spec *routes* the decision; it does not
*make* it.

---

## 7. Sequencing

Sequencing follows **validation gates**, not a strict serial chain. The rough order is
**L0 → L1 → L2 → L3**, but with one refinement from the §3 correction: **L1 is authored in parallel
with L0**, because declaring intended traversal needs no telemetry; L0 only gates *reconciling* L1
against reality (and shipping any consumer to default). The architecture-level reason the FRE-504
"visibility-first" ordering is correct, not merely prudent: *you cannot run a spec ↔ observation loop
— or ship a consumer to default — on a topology the observation half cannot see.*

| Wave | Layer | What | Gates the next wave by… |
|------|-------|------|--------------------------|
| 0 | L0 | Topology observability contract (ADR-0088) + ledger (FRE-452) + sub-agent auditability (FRE-505) + gate-decision telemetry (FRE-506) + taxonomy/vocabulary (FRE-451) | making every topology measurable |
| 0‖ | L1 | Matrix (FRE-453 + knowledge-access column + decomposed case), written in the FRE-451 vocabulary | declaring intended traversal to reconcile against (authored *in parallel* with Wave 0) |
| 1 | L2 | Memory Recall · Inference plumbing + planner reliability · Injection quality · Artifact security | giving each pillar a measurement foundation; knowledge-gating decided here |
| 2 | L3 | Pedagogical features · Turn Cost · Turn Reliability | (delivered on solid, measurable substrate) |

Waves overlap where dependencies allow. **Caveat (per Codex review):** ADR-0089 artifact security can
have its *policy designed* in parallel, but **enforcing or validating** it depends on L0 gate-decision
telemetry (FRE-506) — without it, ADR-0089 ships blind. The hard constraint throughout: **no consumer
ships to default ahead of the observability that proves it works.**

---

## 8. Recommended portfolio moves (owner / master action)

This document recommends; it does not execute project surgery.

1. **Create an L0 project — "Observability Foundation" (functional).** Owns the topology
   observability contract, the route-trace ledger (FRE-452), sub-agent auditability (FRE-505),
   gate-decision telemetry (FRE-506), and the result-type taxonomy (FRE-451, the vocabulary). Consider
   whether to merge with **Agent Driven Gate Health Monitoring** (ADR-0053) — overlapping
   instrumentation surface, but different cadence (anomaly issues vs. foundational substrate);
   recommendation: keep separate, cross-reference.
2. **Lift the M2 measurement substrate out of Pedagogical "M2."** FRE-451 (taxonomy) and FRE-452
   (ledger) move into the L0 project. **FRE-453 spans two layers**: its *expected-path / result-type*
   content is the **L1 matrix seed** (normative), while *running* it as an eval depends on L0. Place
   FRE-453 with L1 (the matrix), gated on the L0 ledger — do not file it as wholly L0. Pedagogical
   keeps M1 (done) + M3/M4/M5 features and *consumes* the lifted substrate. This re-scopes Inference
   Architecture (its gate FRE-452 moves) and Memory Recall (it consumes the matrix), so the move
   should precede approving those pillars.
3. **Approve the three Needs-Approval pillars together, after this structure settles** — Memory
   Recall Quality, Inference Architecture, ADR-0081 Extended — with boundaries drawn per §4 so they
   need only one carve, not a carve-then-recarve.

### Execution status (2026-06-06, owner-authorized)

Most of §8 has been applied to Linear (owner authorized "approve on my behalf, restructure"):

- ✅ **Observability Foundation** project created (Approved) — `observability-foundation-8b305d1921e6`.
- ✅ **Lifted into it:** FRE-451 (taxonomy), FRE-452 (ledger), FRE-453 (matrix), FRE-505 (sub-agent
  auditability, from Turn Cost), FRE-506 (gate-decision telemetry). FRE-453 is filed *in* the L0
  project, which owns the L1 matrix (no separate L1 project) — a deliberate co-location of the
  reconciliation loop's two halves, not a separate project.
- ✅ **FRE-502** (planner reliability) moved to **Inference Architecture** per §4.
- ✅ **Pillars approved:** Memory Recall Quality, Inference Architecture, ADR-0081 Extended.
- ⏳ **Artifact Execution Security pillar (ADR-0089): deferred to the ADR-0089 authoring step** — the
  ADR defines the pillar's shape, so its project + the re-homing of FRE-497/498/499/500 (currently in
  Turn Cost) land *with* the ADR, not before it.
- 🧹 **Residual for master:** Pedagogical "M2: Mapping & Measurement" milestone is now empty;
  FRE-505/506 carry no priority.

Kept as recommendation (not executed): the Gate Health Monitoring merge question (#1) — left separate.

---

## 9. What this spawns

- **ADR-0088 — Execution Topology Observability Contract.** Keystone of L0 (threads 1, 4, 6, 7 of
  FRE-504). Cross-cutting emit/cost/degradation contract bound to a first-class topology abstraction;
  observable-first done bar. **Its central, non-skippable task is to identify the concrete common
  emission boundary** (see §4 L0) — without it the ADR is aspiration, not a contract.
- **ADR-0089 — Artifact Execution Security Model.** L2 pillar (thread 5). Sandbox-not-sanitize. Its
  *policy* can be designed in parallel, but its *enforcement/validation* depends on L0 gate-decision
  telemetry (FRE-506) — the ADR must state that dependency, not claim independence.
- **Deferred to Memory Recall Quality** (threads 2, 3): the knowledge-grounding decision, derived
  from the L1 matrix and validated by the reconciliation loop — not a same-altitude ADR written off
  one trace.

---

*Update this document when a capability moves between layers, a new substrate pillar is named, or the
reconciliation loop changes a matrix row by enough to alter the portfolio shape.*
