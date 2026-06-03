# From Tier Routing to Pedagogical Architecture — The Origin Thread

**Status:** Complete — 2026-06-03
**Subject:** How a routine tier-routing optimization thread (FRE-432) evolved through architectural
debate into a reconception of the agent's core objective — and why the journey itself is the artifact.
**Provenance:** FRE-432 (original scope) → FRE-447 (ADR-0084) · FRE-448 (this doc) · FRE-449
(North Star spec) · FRE-450 (FRE-432 revision) · Codex debate transcripts (Linear doc attached
to FRE-448) · Seshat Pedagogical Architecture project
**Instruments:** ADR-0082 (superseded for pedagogical routing), ADR-0084
**Audience:** Architecture/research reference. The argument is the story — this reads as a
narrative because the path matters as much as the destination.

---

## 1. Abstract

On 2026-06-03 a routine implementation ticket was opened: FRE-432, "ADR-0082: tier-aware model
selection for SINGLE-strategy tasks — route non-thinking work to `sub_agent`." The goal was a
well-motivated cost and latency optimization: 83% of turns run on a thinking model with a 32,768-
token budget, yet most of those turns are conversational or simple lookups. Route them to the
non-thinking instruct tier. Obvious win.

The ticket was never implemented. Instead, two rounds of adversarial Codex analysis and a sustained
architectural debate surfaced that the optimization assumed the wrong objective. The agent is not a
smart assistant; it is a personal Socratic tutor. Conversational turns are *not* trivially cheap —
they are often the Socratic dialogue itself. Routing them to a stripped, non-Socratic `sub_agent`
removes the tutor from the tutoring turn.

The result: a new Linear project (Seshat Pedagogical Architecture), a new ADR (ADR-0084), a living
spec (PEDAGOGICAL_NORTH_STAR.md), and a reframed FRE-432 — all M1 Foundation work, no code yet.
The thread became a reconception.

This document records the intellectual journey: what we were building, what broke the assumption,
how the reconception emerged, what changed architecturally, and what M2 must answer before
anything is built.

The methodology here is the same as the cache-aware prompt layout work (FRE-433/434): **measure
the assumption before routing; test the hypothesis before building**. The difference is that the
assumption being measured was not a token count but an architectural objective.

---

## 2. What We Were Building

### 2.1 The original scope (FRE-432)

ADR-0082 documented a real and precisely measured problem. The gateway's
`_determine_initial_model_role()` (`executor.py:974`) unconditionally returns `ModelRole.PRIMARY`.
Every `SINGLE` turn runs on the thinking model. Measured traffic (ES `agent-logs-*`, 30 days,
n = 2,614):

| Strategy | Share | Reason | Share |
|---|---|---|---|
| **SINGLE** | 95.0 % | `conversational_always_single` | 66.3 % |
| delegate | 2.3 % | `tool_use_single` | 16.4 % |
| hybrid | 1.9 % | `memory_recall_always_single` | 6.1 % |
| decompose | 0.8 % | `analysis_simple` / `planning_simple` | ~5.9 % |

**~83% of all turns paid a 32,768-token thinking budget and the single GPU inference slot regardless
of how trivial they were.** Two model tiers exist and are deliberately differentiated (ADR-0033):

| Tier | Model | Thinking | Concurrency | Built for |
|---|---|---|---|---|
| `primary` | Qwen3.6-35B-A3B | **on**, 32,768-tok budget | 1 (GPU-bound) | deep reasoning, planning |
| `sub_agent` | Qwen3.6-35B-A3B-subagent | **off** (`disable_thinking: true`) | 3 | focused single-task |

The `sub_agent` tier was reachable only via HYBRID/DECOMPOSE expansion — never on the SINGLE path.
ADR-0082 proposed routing `CONVERSATIONAL` and `MEMORY_RECALL` SINGLE turns to `sub_agent`. The
conservative cut: ~72% of traffic (66% conversational + 6% recall) would move off the thinking tier.

The proposal was precise, measured, and wrong.

---

## 3. The Architectural Debate

### 3.1 SINGLE vs HYBRID path distinction

The first Codex round (see Codex debate transcripts) examined the architecture in detail. The key
clarification on the gateway decomposition matrix:

| Case | "Primary always plans" is correct? |
|---|---|
| HYBRID enforced mode (current default) | **Wrong.** Expansion controller deterministically spawns sub-agents — no primary planning call. |
| HYBRID autonomous mode | **Correct.** Primary decomposes → sub-agents execute → primary synthesizes. |
| SINGLE mode (all cases) | **Wrong.** One model end-to-end, no delegation. |

"Primary always plans" correctly describes only HYBRID autonomous mode. This cleared up a recurring
confusion: the HYBRID path and the SINGLE path are not the same decomposition — they have
fundamentally different topologies.

### 3.2 Local vs cloud asymmetry

The Codex analysis identified a critical split that the ADR-0082 proposal had not fully accounted
for:

**Local:** `primary` and `sub_agent` are the same model weights on the same llama.cpp backend.
The only real local benefit of tier routing is **thinking-token elimination** — removing the
32,768-token thinking budget from the turn. But this depends entirely on whether `primary` actually
*uses* significant thinking tokens on trivial turns. If MoE sparse activation already suppresses
heavy computation on easy turns (as the Qwen3 MoE architecture may do), the local benefit largely
vanishes. **This must be measured before routing.**

**Cloud:** Sonnet and Haiku are genuinely separate providers with separate throughput. The cloud
case for tier routing is stronger — real throughput headroom, real model-tier specialization.
Enable more aggressively on cloud once measured; flag-gated on local until the hypothesis is confirmed.

This local/cloud asymmetry is the same kind of finding that drove the cache layout work
(FRE-433/434): a fix that works on one backend is not automatically a fix on the other. Measure
against the real backend's ground truth, not a theoretical model.

### 3.3 Claude Code / Codex comparison

The Codex analysis included a comparison with how Claude Code and Codex themselves handle the
heavy/lightweight model split on the dominant traffic path:

> Neither has a deterministic lightweight/heavyweight split on the dominant traffic path. Claude
> Code uses one Sonnet model. Codex is task-isolated, single model per task. What our two-tier
> architecture buys: the ability to skip primary thinking on 66%+ of turns (conversational) and
> 6% of turns (memory_recall) without changing the model family. Where the complexity is overhead:
> any routing that adds a primary call before sub_agent, or any TOOL_USE routing before a
> tool-depth gate exists.

This framing was useful. The two-tier architecture is not an established pattern from reference
implementations — it is a novel choice that requires its own justification and its own measurement.
The reference systems that don't do this are not making a mistake; they simply don't have a
two-tier local architecture to exploit.

### 3.4 The risks list (before the North Star surfaced)

At this point in the debate, the remaining risks were framed as implementation and measurement
risks, not objective risks:

- Measure actual primary thinking-token usage on CONVERSATIONAL/MEMORY_RECALL turns
- Latency p50/p95: primary vs sub_agent on each task type
- D3 escalation rate (instruct→thinking) by task type — if MEMORY_RECALL escalates frequently, it should stay on primary
- Quality regression risk: MEMORY_RECALL is higher-risk than CONVERSATIONAL (reasoning may matter for accurate recall)

The analysis was pointing toward a cautious, measured rollout — not toward abandoning the idea.
Then the second Codex round happened.

---

## 4. The Plot Twist: Pedagogical North Star

The second round of Codex analysis began with a different framing. The original question ("how do
we get sub_agent to replace primary on 83% of turns?") was reframed as: "Is that actually what we
want? What is primary *for*?"

The answer changed everything:

> The right mental model is HYBRID/DECOMPOSE: primary plans, sub-agents execute bounded work,
> primary synthesizes. For Seshat, primary is not just a coordinator — it is the **pedagogical
> continuity layer**. It carries the Socratic stance, the learner model, the emotional and
> conceptual thread, and the responsibility for deciding what the turn *means* in the long arc
> of learning. Replacing that with a stripped sub_agent is not delegation. It is removing the
> tutor from the tutoring turn.

This was not a new implementation choice. It was a statement about what the agent *is*.

Seshat is not a general smart assistant that should be optimized for throughput on easy turns. It
is a **personal Socratic tutor** — a thought partner that tracks not just what was discussed but
what was *understood*, what needs revisiting, and what threads connect across domains.

Once this was stated explicitly, the implications were immediate:

**`CONVERSATIONAL` turns are not trivially cheap.** A conversational turn is often the Socratic
dialogue itself — the opening question, the challenge calibration, the framing that makes the
learner retrieve rather than receive. Routing it to a stripped instruct model with "respond with
the result only" removes the Socratic stance from the turn. This is not a quality risk. It is an
objective failure.

**`MEMORY_RECALL` turns are not plain lookups.** Active recall — asking the learner to retrieve
before explaining — is a pedagogical move that primary owns. A `sub_agent` that returns raw
memory candidates for primary to frame is bounded cognition (appropriate to delegate). A
`sub_agent` that *responds* to the user with a memory recall turn is removing the pedagogical
framing that makes active recall effective.

The 83% of traffic that looks like a cost optimization target is mostly the Socratic dialogue.
Optimizing it by routing it to a tier that lacks the Socratic contract is not an optimization.
It is a degradation that would be nearly invisible in execution-success metrics.

---

## 5. Codex Debate Round 2: Delegation Model Reconceived

### 5.1 The practical test

With the pedagogical objective established, the Codex analysis produced a clean decision rule for
delegation:

> **The practical test:** can the work be wrong or incomplete without directly harming the
> learner's trust, self-model, or conceptual trajectory? If yes — delegate and verify. If no —
> primary keeps the turn.

This test distinguishes bounded cognition (retrieving memory candidates — can be wrong and primary
corrects it) from pedagogical continuity (framing a recall prompt — being wrong here means the
learner receives incorrect scaffolding or no scaffolding at all).

**Delegate:** retrieving prior notes, scanning memory, extracting candidate concepts, checking
contradictions, summarising a source, finding examples across domains, generating recall prompts
for primary review.

**Primary keeps:** framing, tone, challenge calibration, learner uncertainty, emotional resonance,
conceptual synthesis, closing ritual, deciding what question to ask next.

### 5.2 The result type taxonomy

The second Codex round identified that the proposed taxonomy in ADR-0082 conflated two layers —
orchestration facts and learner-facing outcomes. A turn can both execute delegation *and* produce a
recall check. The conflation makes measurement muddy and hides the pedagogically important signal.

**Cleaner split:**

*Orchestration events* — what the harness did:
`primary_handled` | `delegate_called` | `delegate_result_used` | `delegate_result_discarded` | `fallback_triggered`

*Pedagogical outcomes* — what the learner got:
`recall_practiced` | `concept_extracted` | `principle_identified` | `counterintuitive_finding_marked` |
`open_thread_preserved` | `cross_connection_made` | `field_note_emitted` | `learner_state_updated` |
`synthesis_performed` | `misalignment_detected`

The Codex analysis flagged three outcomes that were missing from earlier taxonomy proposals:
`field_note_emitted` (between-session observations), `synthesis_performed` (connecting multiple
prior concepts), and `misalignment_detected` — because pedagogical degradation often appears as a
subtle failure to preserve stance, not a task failure. A turn that finishes correctly but without
the active-recall framing, at the wrong challenge level, or without preserving the open thread —
is a pedagogical failure in the outcome layer that is invisible in the orchestration layer.

### 5.3 The route trace ledger: minimum viable M2 instrument

The Codex analysis identified the minimum viable M2 instrument as a **route trace ledger**: a
per-turn record that captures the full path from stimulus to outcome. The most important field:

> The boundary between deterministic shell and stochastic core. If the router says "MEMORY_RECALL
> SINGLE" but primary actually performs synthesis, challenge calibration, or emotional
> interpretation — the label is lying. The instrument must record not just the expected model path
> but what *kind of cognitive work* was actually done.

Canonical ~7-turn eval set for M2 (to be finalized):
- Trivial conversational turn
- Memory recall turn (simple lookup)
- Opening ritual turn
- Closing ritual turn
- Cross-thread synthesis turn
- Emotionally loaded learning turn
- Tool-heavy research turn

These seven types expose where SINGLE-path routing is safe, fake-safe (carries pedagogical
continuity despite the label), and explicitly pedagogical.

### 5.4 FRE-432 reconceived

> The original FRE-432 is wrong because it treats the current sub_agent as a cheaper primary. It
> is not — it lacks the user-facing contract, Socratic personality, operator stanza, and full
> pedagogical frame. On the local path the cost premise is also weak: same model weights, primary
> may already suppress heavy thinking on easy turns.
>
> The right optimization target: primary remains the terminal pedagogical authority while using
> cheaper/faster modes for *bounded cognition* — not "replace primary on 83% of turns."

The reconceived scope: a **delegation policy plus primary thinking policy**. Easy conversational
turns stay on primary with thinking suppressed or minimal. Memory recall uses retrieval/delegate
workers to gather candidates — primary selects and phrases the recall. Complex synthesis uses
HYBRID/DECOMPOSE. If a future "small tutor" tier is introduced, it must be evaluated as a
pedagogical actor, not assumed equivalent because it is cheaper.

---

## 6. What Changed

Three architectural changes emerged from the reconception. None are implementation changes — they
are changes in what the agent *is* and what "good" means.

### 6.1 The result type taxonomy

The measurement framework shifted from execution-success metrics to a two-layer taxonomy. This is
not a minor addition — it changes what gets measured in every quality gate going forward. The M5
eval harness cannot be a regression-on-task-completion suite; it must include tests that distinguish
whether pedagogical outcomes were produced.

### 6.2 The delegation boundary

Delegation is no longer gated on `TaskType` and `Complexity` (as ADR-0082 proposed). It is gated
on the **practical test**: can the work be wrong without harming the learner's trust, self-model,
or conceptual trajectory? This test is applicable by any reviewer to any proposed delegation — it
is a principled boundary, not a lookup table.

### 6.3 The role of `sub_agent`

`sub_agent` was reframed from a cheaper-primary replacement to a surgical HYBRID tool. Its
appropriate domain is bounded cognition on the HYBRID/DECOMPOSE path. It is *not* appropriate as
a terminal endpoint for full user turns in a pedagogical context — until a "small tutor" profile
is designed, evaluated, and accepted by a separate ADR.

The D1 plumbing from ADR-0082 (`model_tier` on `GatewayOutput`, `_determine_initial_model_role()`
reading it) may still ship as neutral infrastructure in M4 — it enables a future delegation policy
without prescribing the mapping. The mapping itself is now gated on M2 measurement.

---

## 7. Key Findings

1. **The cost assumption was unmeasured.** ADR-0082 asserted that 83% of turns pay unnecessary
   thinking overhead. This may be true. But the claim that `primary` uses significant thinking
   tokens on trivial turns — the foundational premise — was never measured. The MoE sparse-
   activation pattern may already suppress it. Measure before routing. (See ADR-0084 §Open
   decisions §1.)

2. **The "primary always plans" assumption was wrong for the dominant path.** 95% of traffic is
   SINGLE-strategy — one model, end-to-end, no planning call. "Primary always plans" correctly
   describes only HYBRID autonomous mode. The gateway matrix and the SINGLE/HYBRID topology are
   fundamentally different structures.

3. **Local and cloud have different cost models.** The local case for tier routing depends on
   whether thinking is actually heavy on trivial turns (same model weights, MoE architecture).
   The cloud case is stronger (genuinely separate providers). Design for the backend you have,
   not the backend you imagine.

4. **Pedagogical degradation is invisible in execution-success metrics.** A turn that responds
   correctly but without Socratic framing, at the wrong challenge level, or without preserving
   the open thread — registers as a success in latency, cost, and task-completion metrics. It
   is only visible in the pedagogical-outcome layer. The taxonomy (§3) exists precisely to make
   this visible.

5. **The optimization target was wrong.** "Route 83% of turns to a cheaper tier" was the right
   framing for a generic assistant. For a Socratic tutor, the right optimization target is:
   *primary remains the pedagogical authority; bounded cognition is delegated, not the primary
   turn itself.*

---

## 8. The Debate / Exploration Process

### 8.1 Three-session architecture as adversarial reviewer

The debate ran in the **adr session** (this worktree, `worktree-adrs`), which owns the ADR and
architectural documentation. Codex was used as an **adversarial reviewer** — given the proposed
architecture and asked to identify failure modes, hidden assumptions, and better alternatives.

Two rounds:
- **Round 1:** Evaluate the ADR-0082 proposal before the pedagogical North Star had emerged.
  Conclusion: the proposal is technically sound but has unmeasured assumptions and the local
  benefit is weaker than claimed. The analysis pointed toward a more cautious rollout.
- **Round 2:** After the pedagogical North Star surfaced. The question was reframed. Conclusion:
  the proposal is wrong about the optimization target, not just the measurement confidence.

The shift from Round 1 to Round 2 is the architectural reconception. Round 1 still assumed the
objective was throughput. Round 2 questioned the objective.

### 8.2 How the North Star surfaced

The North Star emerged from a single question: "Is replacing `primary` on 83% of turns actually
what we want?" The answer required stating explicitly what the agent is — not in an architectural
sense, but in an objective sense. Once stated, the implications were immediate and cascading.

This is the same pattern as the cache-aware layout work: the reframe was the insight, not the
algorithm. "The prompt is not a string we rebuild each turn, it is a write-once append-only log
with a volatility gradient" changed what the implementation looked like. "Seshat is not a smart
assistant, it is a Socratic tutor" changed what optimization looks like.

The reframes cannot be gotten to by debugging the existing implementation. They require stepping
back and asking what the thing is.

### 8.3 The decision to spin a new Linear project

Once the pedagogical North Star was established, the FRE-432 scope was clearly invalidated —
not just changed, but invalidated. The original implementation could not be salvaged by adding
requirements. A new project (Seshat Pedagogical Architecture) was created to give the
reconception a proper home with its own milestone sequence, gate criteria, and issue tracking.
FRE-432 was reframed with the reconceived scope and moved back to Needs Approval.

This was the right call. An invalidated scope re-approved without changing its Linear record
would have left architecture drift in the issue history. The new project makes the
reconception visible.

### 8.4 Measure-don't-assert, applied to architecture

The cache-aware layout work established the methodology: measure the assumption, don't assert the
benefit. That methodology was applied here to an architectural assumption — and found it wanting.

The lesson is not specific to tier routing. It generalizes: **before optimizing any component of
the system, verify that the component is doing what you think it is, at the level you think it is,
for the reason you think it is.** The thinking-token usage on trivial turns is still unmeasured.
The M2 instrument exists to measure it.

---

## 9. Open Questions M2 Must Answer

Before M3 implementation begins, the route trace ledger must resolve:

**1. The thinking-token hypothesis.**
Does `primary` actually use significant thinking tokens on `CONVERSATIONAL` and `MEMORY_RECALL`
turns? Measure mean thinking-token usage per `TaskType` on a labeled sample. If the answer is
"no, MoE sparse activation suppresses it" — the latency/cost argument for any SINGLE-path routing
change is weaker than assumed.

**2. The deterministic-shell boundary.**
For each turn type in the canonical eval set, does the gateway label match the actual cognitive
work performed? If the router labels a turn `MEMORY_RECALL SINGLE` but `primary` actually performs
synthesis and challenge calibration — the label is lying and any routing based on it is fake-safe.

**3. Fake-safe SINGLE detection.**
Which SINGLE turns are genuinely safe for lighter treatment (pure factual lookup), which *appear*
safe but carry pedagogical continuity (a casual follow-up that is actually a deferred open thread),
and which are explicitly pedagogical (opening ritual, closing ritual, cross-thread synthesis)?
Class (b) is the danger: it looks like a cost-optimization target but is not.

**4. Canonical eval set definition (~7 types).**
Before the M2 instrument ships, define the representative labeled set. The seven turn types listed
in §5.3 are the candidate list; finalize and label before M2 gate.

**5. Route trace ledger field set.**
The M2 instrument must record (at minimum): stimulus → TaskType → routing decisions → model tier
selected → thinking enabled? → delegation occurred? → delegate prompt class → delegate output shape
→ primary synthesis behavior → final result type → latency → token cost → fallback path. The most
important field: the kind of cognitive work actually done (not just the gateway label).

---

## 10. References

- **ADR-0084** — `docs/architecture_decisions/ADR-0084-pedagogical-architecture-socratic-tutor-layer.md`
- **North Star spec** — `docs/specs/PEDAGOGICAL_NORTH_STAR.md`
- **ADR-0082** — `docs/architecture_decisions/ADR-0082-tier-aware-model-selection-for-single-tasks.md` (superseded for pedagogical routing)
- **Codex debate transcripts** — Linear doc `bb691c96-db86-400b-82e6-fede5e118450`, attached to FRE-448
- **Tickets** — FRE-432 (original + reconceived scope), FRE-447 (ADR-0084), FRE-448 (this doc), FRE-449 (North Star spec), FRE-450 (FRE-432 revision)
- **Seshat Pedagogical Architecture project** — Linear, M1–M5
- **Adjacent research** — `docs/research/2026-06-02-cache-aware-prompt-layout-and-compaction.md` (same measure-don't-assert methodology applied to cache assumptions)
