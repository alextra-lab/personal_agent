# ADR-0084 — Pedagogical Architecture: Socratic Tutor Layer, Result Type Taxonomy, and Delegation Policy

**Status:** Accepted — 2026-06-03
**Related:** ADR-0082 (superseded for the pedagogical routing question — see D6), ADR-0033 (multi-provider model taxonomy — defines `primary` / `sub_agent` tiers), ADR-0074 (identity / joinability — the emit-site discipline the M2 instrument inherits), FRE-447, Seshat Pedagogical Architecture project, `docs/research/2026-06-03-pedagogical-architecture-origins.md`, `docs/specs/PEDAGOGICAL_NORTH_STAR.md`
**Amendments:** ADR-0091 (2026-06-14) — adds the **turn completion-status layer** to the §D4 taxonomy (third, orthogonal layer; see §D4 "Completion status").

> **ADR numbering note:** FRE-447, FRE-432, and the project description reference this ADR as "ADR-0083".
> ADR-0083 was assigned to the adaptive-limits / SLM-health observability work (FRE-399, 2026-06-02).
> This ADR is correctly numbered **0084**. Linear doc-drift to be reconciled by the master session.

---

## Context

### Origin: FRE-432 and the 83% problem

The gateway's `_determine_initial_model_role()` (`executor.py:974`) unconditionally returns
`ModelRole.PRIMARY` — every `SINGLE` turn runs on the thinking model regardless of how trivial it
is. Measured across 2,614 turns (ES `agent-logs-*`, 30 days):

- `CONVERSATIONAL` — 66.3 % of all turns
- `MEMORY_RECALL` — 6.1 %
- `TOOL_USE` — 16.4 %

**~83 % of traffic pays a 32,768-token thinking budget, a 600 s timeout, and the single GPU
inference slot on turns that may not need any of it.** ADR-0082 (FRE-432) proposed routing
`CONVERSATIONAL` and `MEMORY_RECALL` SINGLE turns to the non-thinking `sub_agent` tier as the
primary optimization.

That framing was invalidated. The route to invalidation is documented in detail in
`docs/research/2026-06-03-pedagogical-architecture-origins.md`.

### Why the original framing was wrong

A thorough architectural debate — including two rounds of Codex analysis (see Codex debate
transcripts, Linear doc attached to FRE-448) — surfaced four reasons:

**1. Primary is the pedagogical continuity layer.**
Seshat's adjusted North Star (personal Socratic tutor, see `docs/specs/PEDAGOGICAL_NORTH_STAR.md`)
means `CONVERSATIONAL` and `MEMORY_RECALL` turns are *not* trivially cheap. A `CONVERSATIONAL`
turn is often the Socratic dialogue itself — framing, tone calibration, challenge level, emotional
resonance, deciding what to ask *next* given the learner's current state. A `MEMORY_RECALL` turn
is often active-recall retrieval with pedagogical framing, not a plain lookup. Routing these to a
stripped, non-Socratic, user-facing `sub_agent` **removes the tutor from the tutoring turn**.

**2. The `sub_agent` tier lacks the user-facing contract.**
The current `sub_agent` has no operator stanza, no Socratic personality, a 2000-character summary
cap, and a prompt that says "respond with the result only." Making it the terminal endpoint for a
full user turn is a category change, not a model swap. A SINGLE-path delegate that can serve a
full learner turn would have to be a *second primary-class tutor profile* — a different architecture
entirely, requiring its own ADR and evaluation.

**3. The local cost premise is weak.**
`primary` and `sub_agent` are the same model weights (Qwen3.6-35B-A3B MoE). The MoE
sparse-activation pattern may already suppress heavy computation on trivial turns at the model
level. The benefit of routing to `sub_agent` — if any — must be *measured*, not asserted. This is
a data-gated open decision, not a default assumption (see Open decisions).

**4. The right question is delegation, not replacement.**
"When does `primary` delegate *bounded cognition* to sub-agents?" is the right framing — not "how
do we replace `primary` for cheap turns?" Bounded cognition maps to the `HYBRID`/`DECOMPOSE`
path (primary plans, sub-agents execute discrete retrievals/extractions, primary synthesizes), not
SINGLE-path replacement.

### The pedagogical North Star

The core reconception: Seshat is a **personal Socratic tutor** — a thought partner that tracks
not just what was discussed but what was *understood*, what needs revisiting, and what threads
connect across domains. The difference between a knowledgeable person and a wise one is the
density of connections between what they know. Seshat's job is to build those structures over time.

This changes what "optimal results" means. The measurement question for any routing, model, or
delegation change is no longer "did the turn finish cheaply?" It is: did this turn preserve
pedagogical continuity, ask the right kind of question, strengthen recall, extract useful
structure, and connect knowledge over time?

---

## Decision

### D1 — Primary is the pedagogical continuity layer

`primary` is not merely the "thinking model" or the "default tier." It is the **pedagogical
continuity layer**: the model that carries the Socratic stance, the evolving learner model,
the emotional and conceptual thread across turns, and the responsibility for deciding what a turn
*means* in the long arc of learning.

This is the load-bearing architectural assumption of the Seshat pedagogical layer. Every
downstream routing, delegation, and model-tier decision must be compatible with it.

### D2 — Delegation is for bounded cognition only

**The practical test:** can the work be wrong or incomplete without directly harming the learner's
trust, self-model, or conceptual trajectory? If yes — delegate and verify. If no — primary keeps
the turn.

**Delegate to sub-agents (HYBRID/DECOMPOSE):**
- Retrieving prior session notes or raw memory candidates
- Scanning knowledge graph for candidate concepts
- Extracting structured concept/principle data from a source
- Drafting recall-card prompts for primary review
- Parallel cross-domain search for examples or counterexamples
- Consistency/contradiction checks against the knowledge graph
- Summarising a source text for primary synthesis

**Primary keeps:**
- Framing and tone of the response
- Challenge calibration (too hard → learned helplessness; too easy → disengagement)
- Learner uncertainty and emotional resonance
- Conceptual synthesis and identifying cross-domain connections
- The opening ritual and closing ritual
- Deciding what question to ask next
- Any output that shapes the learner's self-model or conceptual trajectory

**The boundary principle:** pedagogical degradation often appears as a *subtle* failure to
preserve stance, not as a task failure. A turn that finishes correctly but with the wrong tone,
at the wrong challenge level, or without preserving the open thread — is a pedagogical failure
even if it is an execution success.

### D3 — `sub_agent` is a surgical HYBRID tool, not a user-facing replacement

The current `sub_agent` tier is architected for HYBRID sub-tasks: bounded, verifiable, non-user-
facing retrieval and extraction work. It is the *right* tool for that. It is the *wrong* tool for
serving a full user turn directly.

A `sub_agent` that can correctly serve a full learner turn in the Socratic context would require:
- The operator stanza and personality definition
- Full Socratic framing instructions
- The learner model state and session history
- Challenge-level awareness and ritual awareness
- A quality contract equivalent to `primary`

That is a second primary-class model profile, not a tier swap. It requires independent design,
evaluation, and an ADR. Until that work is done, `sub_agent` stays on the HYBRID path only.

### D4 — Result type taxonomy (canonical, multi-layer separation)

> **Layer count (amended by ADR-0091, 2026-06-14).** As originally accepted this section defined a
> **two-layer** taxonomy (orchestration events + pedagogical outcomes). ADR-0091 adds a third,
> orthogonal **completion-status** layer (below). Where older text in this ADR or in
> `RESULT_TYPE_TAXONOMY_SPEC.md` still says "two-layer," read it as superseded by this amendment.

The measurement framework proposed in ADR-0082 conflated orchestration facts with learner-facing
outcomes. A turn can both execute delegation *and* produce a recall check — conflating these makes
measurement muddy and hides the pedagogically important signal.

**Canonical taxonomy (orchestration events + pedagogical outcomes + completion status):**

**Orchestration events** (what the harness did):

| Event | Meaning |
|---|---|
| `primary_handled` | Turn handled end-to-end by primary with no delegation |
| `delegate_called` | Sub-agent invoked for bounded work |
| `delegate_result_used` | Sub-agent output incorporated into primary synthesis |
| `delegate_result_discarded` | Sub-agent output rejected by primary on review |
| `fallback_triggered` | Escalation from sub-agent to primary mid-turn |

**Pedagogical outcomes** (what the learner got):

| Outcome | Meaning |
|---|---|
| `recall_practiced` | Active recall was exercised during the turn |
| `concept_extracted` | A concept was identified and tagged for the learning model |
| `principle_identified` | An underlying principle was named and anchored |
| `counterintuitive_finding_marked` | A result marked for reinforcement (surprises stick) |
| `open_thread_preserved` | An unresolved question was explicitly held open |
| `cross_connection_made` | A principle from one domain linked to another |
| `field_note_emitted` | A between-session observation was captured |
| `learner_state_updated` | The learner model was updated with new engagement signal |
| `synthesis_performed` | Multiple prior concepts connected into a new structure |
| `misalignment_detected` | A divergence between the learner's model and the actual concept was identified |

**Completion status** (did the exchange finish, and if not, why?) — *added by ADR-0091 (2026-06-14);
a third, orthogonal layer.* A labelled turn/conversation carries exactly one:

| Status | Meaning |
|---|---|
| `natural_end` | The conversation reached a natural conclusion |
| `clarification_requested` | The turn paused, **blocked on the user**, for information it cannot proceed without (a continuation signal, never a quality verdict; distinct from `open_thread_preserved`, where the *tutor* defers a thread while the turn still concludes) |
| `incomplete` | The conversation did not conclude within bounds (turn-cap exhausted or errored) |

The three layers are assigned **independently**. Pedagogical outcomes are scored **only** on
`natural_end` conversations; a `clarification_requested` turn is a correct pause carried forward by
the eval driver, never a pedagogical-outcome miss. See **ADR-0091** for the layer's definition,
the eval conversation driver, and the detection contract.

**The measurement question is not** "did the turn finish?" **It is:** did the turn preserve
continuity, ask the right kind of question, strengthen recall, extract useful structure, and
connect knowledge over time?

This taxonomy is the canonical reference for all subsequent FRE tickets in the Seshat Pedagogical
Architecture project. Any instrument, eval harness, or routing change is measured against
pedagogical outcomes, not only against orchestration events. See also
`docs/specs/PEDAGOGICAL_NORTH_STAR.md` §Result Type Taxonomy.

### D5 — Five-layer pedagogical architecture

The pedagogical layer is structured in five functional layers. This ADR records the architectural
decision to organize around these layers; the implementation specification and substrate mapping
live in `docs/specs/PEDAGOGICAL_NORTH_STAR.md`.

**Layer 1 — Knowledge Extraction.**
After each conversation, extract: concepts introduced, depth of engagement, curiosity signals
(topics the learner pursued beyond the immediate question), and open questions (threads not yet
resolved). This is post-conversation processing triggered by the closing ritual.

**Layer 2 — Learning Model.**
A pedagogical filter applied to extracted concepts: foundational principles are reinforced,
counterintuitive findings are reinforced (surprises stick), extrapolation zones are flagged for
careful handling, open threads are queued for retrieval, emotionally resonant moments are seeded
as anchors. This is the personalization layer — it governs what Seshat offers *this* learner, not
just what it knows.

**Layer 3 — Spaced Repetition Engine.**
Ebbinghaus forgetting curve + active recall: concepts are tagged with a `next_review` date. The
opening ritual surfaces the highest-priority recall prompts. Seshat asks — the learner retrieves.
The quality of retrieval updates the review schedule.

**Layer 4 — Thread Pulling.**
The knowledge graph exposes concept roots and branches. Seshat tracks readiness signals (depth of
engagement, mastery of prerequisites) and decides when a learner is ready to go deeper on a
thread they opened earlier. The opening ritual may pull a thread the learner has not thought about
since the session it appeared in.

**Layer 5 — Cross-Thread Correlation.**
Surfaces when a principle from domain A is the same structural principle as principle Y from
domain B. This is the highest-leverage layer — it is how a tutor builds the density of connections
that distinguishes knowledge from wisdom. Requires the knowledge graph and multi-session context.

### D6 — Relationship to ADR-0082

ADR-0082 (FRE-432, tier-aware model selection for SINGLE-strategy tasks) is **superseded for the
pedagogical routing question** by this ADR.

The pedagogical routing question — "when should `primary` be replaced by or share a turn with
another model?" — is now governed by D1–D3 of this ADR (the pedagogical continuity principle,
the bounded-cognition delegation test, and the `sub_agent`-as-surgical-tool constraint), not by
ADR-0082's proposed `TaskType × Complexity` mapping.

**What ADR-0082 D1 plumbing may still ship:**
The infrastructure decision in ADR-0082 D1 — adding a `model_tier` field to `GatewayOutput` and
making `_determine_initial_model_role()` read it rather than hard-returning `PRIMARY` — is neutral
infrastructure. It does not prescribe a mapping; it enables a future delegation policy to specify
one. The D1 plumbing may ship as part of M4 (Delegation Policy) once the M2 instrument confirms
the thinking-token hypothesis and the delegation policy is designed. It does not ship as a
cost/latency optimization; it ships as a foundation for a pedagogically-grounded routing decision.

ADR-0082 D2–D5 (the proposed tier mapping, escalation path, concurrency dividend, and joinability
requirements) are superseded. They are not deleted — the analysis there (especially the local vs
cloud distinction and the measurement requirements) remains valid reference material. But the
conclusion (route 83% of turns to `sub_agent`) is wrong given D1 of this ADR.

---

## Open decisions (M2 data-gated)

These questions must be resolved by the M2 mapping instrument (route trace ledger) before M3
implementation begins. **Do not design M3 around assumptions on these; measure first.**

**1. The thinking-token hypothesis.**
Does `primary` actually use significant thinking tokens on `CONVERSATIONAL` and `MEMORY_RECALL`
turns? (Hypothesis: yes, because `_determine_initial_model_role` always returns `PRIMARY` with
the full 32,768-token thinking budget.) If the answer is no — if MoE sparse activation already
suppresses thinking on trivial turns — the latency/cost argument for any routing change is weaker
than assumed. Measure `timings.cache_n` and thinking-token usage on a labeled turn sample before
any routing change. (This was the foundational unmeasured assumption of ADR-0082.)

**2. The deterministic-shell vs stochastic-core boundary.**
The route trace ledger must record not just the expected model path (from the gateway matrix) but
what *kind of cognitive work* was actually done. The key field: does the gateway label `MEMORY_RECALL
SINGLE` but the primary actually perform synthesis, challenge calibration, or emotional
interpretation? If yes — the label is lying. Fake-safe SINGLE routing that silently strips out the
tutor is a pedagogical failure that is invisible in the orchestration-event layer.

**3. The canonical ~7-turn eval set.**
Before M2 ships, define a representative labeled set covering: trivial conversational turns,
memory recall turns, opening ritual turns, closing ritual turns, cross-thread synthesis turns,
emotionally loaded learning turns, tool-heavy research turns. This set is the M2 gate criterion:
the instrument must correctly label every turn type with both an orchestration event and a
pedagogical outcome.

**4. Delegation-safe SINGLE vs delegation-risky SINGLE.**
The M2 instrument must distinguish: (a) SINGLE turns that are genuinely safe to delegate or
de-think (pure lookup, single-step factual), (b) SINGLE turns that are *apparently* safe but
actually carry pedagogical continuity (a casual follow-up that is actually a deferred open thread),
and (c) SINGLE turns that are explicitly pedagogical. Class (b) is the danger: these will look
like cost-optimization targets but are not.

---

## Consequences

### Positive

- Seshat's objective is now **measurable**: not "did the turn finish?" but whether pedagogical
  outcomes were produced. Every routing, model, and delegation decision has a principled
  evaluation criterion.
- The `sub_agent` tier is used for what it was built for (bounded HYBRID sub-tasks) — not misused
  as a user-facing replacement that degrades quality without a visible quality signal.
- The five-layer architecture gives M3 and M4 a clear implementation target: layers 1–3 are
  concrete engineering work against an existing substrate (Neo4j, Captain's Log, PostgreSQL).
- The delegation boundary is *testable*: the practical test ("can this be wrong without harming
  the learner's trust, self-model, or conceptual trajectory?") is a question any reviewer can
  apply to a proposed delegation.

### Negative / tradeoffs

- **The M2 instrument is a prerequisite for M3.** The five-layer architecture requires knowing
  which turns are pedagogically loaded before the extraction pipeline can be tuned. M3 cannot
  safely ship without the M2 eval set and route-trace labeling confirming where the pedagogical
  boundary sits.
- **The thinking-token efficiency question is deferred.** ADR-0082's cost/latency problem (83%
  of turns on the thinking tier) is real and unresolved. This ADR defers it correctly — it should
  not be solved by stripping the tutor — but the latency problem remains. M4 (Delegation Policy)
  must address it via a thinking-policy instrument (when to suppress/reduce the thinking budget on
  primary) rather than tier replacement.
- **Risk of mislabeled cognitive work.** The gateway matrix labels turns by `TaskType` and
  `Complexity`, but the actual cognitive work done may differ. Until the M2 instrument closes this
  gap, there is a risk that any routing change is based on a label rather than reality.
- **Increasing complexity of the session model.** The five-layer architecture adds significant
  post-conversation processing, state tracking, and ritual behavior. This is the right complexity
  — it matches the objective — but it must be costed honestly in M3 design.

---

## Verification

### M1 gate (this ADR)

- This ADR is committed to `docs/architecture_decisions/ADR-0084-…` and accepted.
- `docs/specs/PEDAGOGICAL_NORTH_STAR.md` is committed to the repo.
- `docs/research/2026-06-03-pedagogical-architecture-origins.md` is committed to the repo.
- FRE-432 scope has been revised in Linear (original scope superseded, reconceived scope recorded).

### M2 gate (prerequisite for M3)

- The route trace ledger instrument can label any turn with an orchestration event **and** a
  pedagogical outcome from the D4 taxonomy.
- The canonical ~7-turn eval set is defined and labeled.
- The thinking-token hypothesis is measured (not asserted): mean thinking-token usage per
  `TaskType` on the labeled eval set is known.
- The deterministic-shell boundary is documented: for each turn type, the instrument records
  whether the gateway label matches the actual cognitive work performed.
