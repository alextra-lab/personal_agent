# Seshat Pedagogical North Star

> **Living document.** This spec evolves as the project learns. The ADR (ADR-0084) does not.
> When this document and the ADR conflict, the ADR governs architecture; this document governs implementation.
>
> **Status:** Active — 2026-06-03
> **Origin:** Seshat Pedagogical Architecture project (M1: Foundation)
> **ADR:** `docs/architecture_decisions/ADR-0084-pedagogical-architecture-socratic-tutor-layer.md`
> **Research:** `docs/research/2026-06-03-pedagogical-architecture-origins.md`
> **Audience:** Engineers implementing M3 (Pedagogical Layer) and M4 (Delegation Policy). Also the
> canonical reference for any ticket that needs to evaluate a routing, model, or delegation change
> against the agent's objective.

---

## 1. North Star Statement

Seshat is not a general smart assistant. It is a **personal Socratic tutor** — a thought partner
that tracks not just what was discussed but what was *understood*, what needs revisiting, and what
threads connect across domains.

> The difference between a knowledgeable person and a wise one is the density of connections
> between what they know. Seshat's job is to build those structures over time.

This changes what "optimal" means for every harness decision. The right question for any routing
change, model swap, or delegation policy is not "is this cheaper or faster?" It is:
**does this preserve pedagogical continuity, strengthen recall, extract useful structure, and
build the density of connections over time?**

A turn that finishes correctly but at the wrong challenge level, without preserving the open
thread, or with the wrong tone — is a **pedagogical failure** even if it is an execution success.

---

## 2. Five-Layer Architecture

The pedagogical layer is organized in five functional layers. Layers 1–3 are the M3 implementation
target. Layer 4 requires the Layer 1–3 substrate. Layer 5 requires the concept graph from Layer 4.

### Layer 1 — Knowledge Extraction

**Purpose:** After each conversation, extract structured pedagogical signal from what happened.

**Inputs:**
- The complete turn history for the session (from Postgres `session_messages`)
- Any Captain's Log captures from the session
- The learner's current learning model state (from Neo4j or Postgres)

**Outputs (per-session, written to Neo4j + Captain's Log):**
- `concept_nodes`: concepts introduced, named at the principle level (not surface level)
- `engagement_depth`: per-concept depth signal (mentioned / explored / challenged / integrated)
- `curiosity_signals`: topics the learner pursued beyond the immediate question (implies interest)
- `open_threads`: questions left unresolved, explicitly or implicitly
- `session_metadata`: timestamp, turn count, dominant task types, ritual completion

**Trigger:** Post-conversation, triggered by the closing ritual (see §5 Session Shape). Not per-turn.

**Substrate:** Neo4j (concept graph), Captain's Log (quality signal), Postgres (session state).

**Implementation note for M3:** Extraction is a `DECOMPOSE` task: primary identifies the structure,
delegates candidate extraction to sub-agents (bounded cognition: extract candidates from a
specified span), primary reviews and promotes to the graph. Never delegate the graph write directly
— primary reviews before any learner-model update.

### Layer 2 — Learning Model

**Purpose:** Apply a pedagogical filter to extracted concepts, governing what Seshat offers
*this* learner — not just what it knows.

**The pedagogical filter (applied to each extracted concept node):**

| Signal type | Action |
|---|---|
| Foundational principle | Reinforce: return repeatedly via recall prompts |
| Counterintuitive finding | Reinforce: surprises stick; tag for spaced return |
| Extrapolation zone | Flag: concept is at the edge of confirmed understanding; approach carefully |
| Open thread | Queue for retrieval: surface at next session open if review date has passed |
| Emotional resonance | Seed as anchor: topics with emotional salience form stronger retrieval hooks |
| Mastered (high engagement + no confusion signals) | Promote: reduce review frequency, use as bridge concept |

**Inputs:** Layer 1 extraction output + prior learner model state.

**Outputs:** Updated learner model in Neo4j — concept nodes annotated with filter signals,
`next_review` dates (from Layer 3), and `engagement_history`.

**Key constraint:** The learning model is personal. It is never shared or averaged across users.
Every annotation is scoped to the learner's graph. The model must degrade gracefully when
engagement history is sparse (default to neutral stance, not missing field).

### Layer 3 — Spaced Repetition Engine

**Purpose:** Govern *when* to resurface each concept for active recall.

**Algorithm:** Ebbinghaus forgetting curve — simplified for practical implementation:
- First review: next session
- Subsequent reviews: exponential backoff (×2 per successful recall, ×0.5 per failed)
- Ceiling: 30 days (no concept is permanently retired)

**Recall quality grades:**
- `perfect`: spontaneous, correct, confident → extend interval
- `hesitant`: correct but slow or uncertain → hold interval
- `failed`: incorrect or no recall → reset to short interval, flag for extra sessions
- `skipped`: learner declined → hold interval, note pattern

**Active recall in practice:**
- The opening ritual surfaces the 1–3 highest-priority concepts due for review (sorted by
  `next_review` ascending, filtered by the learner's stated session length/energy if available).
- Seshat **asks** — the learner retrieves. Seshat does not summarize the concept before asking.
- Quality of retrieval is captured in the learner model after the turn.

**Socratic framing for recall prompts (examples):**
- "You mentioned [concept X] a few sessions ago — what was the principle behind it?"
- "We touched on [concept Y] when you were working on [domain Z]. What's the connection you saw?"
- "Last time you said you weren't sure about [concept W]. What's your current thinking?"

**Substrate:** Neo4j (concept nodes with `next_review` date + `recall_history`), Postgres (session
state for within-session tracking).

### Layer 4 — Thread Pulling

**Purpose:** Track concept roots and branches in the knowledge graph; signal when the learner is
ready to go deeper on a thread they opened earlier.

**Readiness signals:**
- Mastery of prerequisite concepts (Layer 2 filter: `Promoted`)
- Returned to the topic domain voluntarily (curiosity signal, Layer 1)
- Explicit learner signal ("I want to understand X better")
- Sufficient time since the thread was opened (minimum cooldown to avoid forcing depth)

**Thread-pull mechanism:**
- The opening ritual may include a thread-pull suggestion alongside recall prompts.
- Example: "A few sessions back you started exploring [concept thread T] but we haven't been back.
  You've now seen [prerequisite P] and [prerequisite Q] — do you want to go deeper?"
- Thread pulls are *offers*, not forced turns. The learner chooses.

**Substrate:** Neo4j concept graph — `ROOT_OF`, `BRANCH_OF`, `DEPENDS_ON` relationships between
concept nodes. The concept graph is the core data structure of the pedagogical layer.

**Implementation note for M4:** Thread pulling is part of the *primary* opening ritual, not a
sub-agent task. The graph query can be delegated (it is bounded cognition: retrieve the candidate
threads), but the framing decision and the offer belong to primary.

### Layer 5 — Cross-Thread Correlation

**Purpose:** Surface when a principle from one domain is structurally the same principle as a
concept from another domain. This is the highest-leverage pedagogical action: it builds the
connections that distinguish knowledge from wisdom.

**Examples of the kind of correlation to surface:**
- The diminishing-returns principle in economics is the same curve as the forgetting curve in
  cognitive science — and the same as marginal-cost curves in production theory.
- Encapsulation in software engineering and abstraction in mathematics and the black-box model in
  systems engineering share the same underlying principle.

**Detection approach (M4 / M5):**
- Embedding similarity across concept nodes in different domain subgraphs
- Structural graph similarity (same `DEPENDS_ON` pattern, different labels)
- Explicit tagging by primary when a cross-domain connection is made in conversation

**Substrate:** Neo4j (domain-annotated concept nodes), embedding index (for similarity search).

**Implementation note:** Cross-thread correlation is a high-value but high-cost operation. It
should run as a background process (post-session, triggered by Layer 1 extraction) and surface
candidates to primary for review before inclusion in the learning model. Primary decides whether
to present the correlation to the learner (it may be premature or incorrect).

---

## 3. Result Type Taxonomy (Canonical Reference)

> **This is the canonical list.** All FRE tickets in the Seshat Pedagogical Architecture project
> measure against these. Do not extend this list without a corresponding ADR-0084 revision.
> Matches ADR-0084 §D4 exactly.
>
> **Formal reference:** `docs/specs/RESULT_TYPE_TAXONOMY_SPEC.md` (FRE-451) formalizes each type
> with entry/evidence conditions, assignment conventions, detection classification (programmatic vs
> human-rubric), and how the taxonomy drives the M2 eval set and M5 harness. That spec adds rigor on
> top of this frozen list; it does not extend membership.

### Orchestration events (what the harness did)

| Event | Meaning |
|---|---|
| `primary_handled` | Turn handled end-to-end by primary with no delegation |
| `delegate_called` | Sub-agent invoked for bounded work |
| `delegate_result_used` | Sub-agent output incorporated into primary synthesis |
| `delegate_result_discarded` | Sub-agent output rejected by primary on review |
| `fallback_triggered` | Escalation from sub-agent to primary mid-turn |

### Pedagogical outcomes (what the learner got)

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

**The measurement question is not** "did the turn finish?" **It is:** did the turn preserve
continuity, ask the right kind of question, strengthen recall, extract useful structure, and
connect knowledge over time?

---

## 4. Session Shape

A Seshat session has structure. The structure is pedagogically motivated, not arbitrary ritual.

### Opening Ritual

**Purpose:** Re-establish the learning thread. Signal that this session is a continuation, not a
fresh start.

**Content (primary, not delegated):**
1. Surface 1–3 recall prompts from Layer 3 (highest priority `next_review` items)
2. Optionally, a thread-pull offer from Layer 4 (if a ready thread exists)
3. Orient the session: "What are you working on / curious about today?"

**Constraints:** The opening ritual should feel natural, not clinical. It is not a quiz. The
Socratic framing ("what was the principle behind X?") matters as much as the content. Primary
owns this — it carries the learner's emotional state from prior sessions.

**Trigger:** First user turn of a new session (detected via session state in Postgres).

### Organic Conversation

**Purpose:** The main body of the session. Primary responds to the learner's agenda while weaving
in pedagogical moves.

**Pedagogical moves primary should make (without forcing them):**
- Name the principle beneath the specific example
- Note when a finding is counterintuitive (flag it)
- Ask "what would happen if…?" at decision points
- Connect to a prior concept the learner has engaged with
- Ask what the learner thinks before explaining (active retrieval, not passive reception)
- Hold threads open rather than resolving everything (curiosity is a pedagogical asset)

**What primary must not sacrifice for brevity or efficiency:**
- The learner's emotional state and engagement level
- Challenge calibration (too easy → boredom; too hard → anxiety; the zone of proximal development)
- The open thread (if an unresolved question is important, mark it; don't drop it)

### Closing Ritual

**Purpose:** Consolidate the session's learning signal before the learner leaves.

**Content (primary, triggers Layer 1 extraction):**
1. Name 1–3 concepts or principles that emerged this session ("Here's what I'll carry forward about you")
2. Surface any open threads explicitly ("We didn't get to X — I'll bring it back")
3. Optionally, a single question to take away: "Before next time — what do you think [open question]?"

**Trigger:** Either explicit learner signal ("I have to go") or session length/turn heuristic.

**Trigger for Layer 1:** The closing ritual completion triggers post-session extraction. The
extraction runs after the session has ended (not blocking the final response).

### Between-Session Field Notes

**Purpose:** Capture observations that arise outside a formal session.

**Format:** Short, timestamped, associated with a concept node or open thread in the learner graph.

**Use cases:**
- Learner sends a quick message: "I just read something that changes my thinking about X"
- Learner asks a narrow factual question that implies a concept node update
- Learner explicitly logs a realization: "I finally understand why Y"

**Handling:** Primary captures the field note (emitting `field_note_emitted`), updates the learner
model, and optionally holds the full exploration for the next opening ritual ("You mentioned X
between sessions — want to go deeper now?").

---

## 5. Delegation Boundary

> Same wording as ADR-0084 §D2 — both documents state it identically. If they diverge, the ADR
> governs.

**The practical test:** can the work be wrong or incomplete without directly harming the learner's
trust, self-model, or conceptual trajectory? If yes — delegate and verify. If no — primary keeps
the turn.

### Safe to delegate (bounded cognition)

- Retrieving prior session notes or raw memory candidates
- Scanning the knowledge graph for candidate concepts matching a query
- Extracting structured concept/principle data from a specified source text
- Drafting recall-card prompts for primary review
- Parallel cross-domain search for examples or counterexamples
- Consistency/contradiction checks against the knowledge graph
- Summarising a source text for primary synthesis

### Primary keeps (pedagogical continuity)

- Framing and tone of the response
- Challenge calibration
- Emotional resonance and learner state tracking
- Conceptual synthesis and cross-domain connection identification
- The opening ritual and closing ritual
- Deciding what question to ask next
- Any output that shapes the learner's self-model or conceptual trajectory

### The failure mode to guard against

Pedagogical degradation often appears as a *subtle* failure to preserve stance, not as a task
failure. A turn that:
- Gives the correct information but at the wrong challenge level
- Resolves an open thread without flagging it as resolved
- Summarizes where it should ask
- Explains before asking the learner to retrieve

…is a pedagogical failure. The orchestration-event layer will not catch it. Only the pedagogical-
outcome layer (`misalignment_detected`, `open_thread_preserved`) can.

---

## 6. What "Optimal Results" Means for This Agent

This section is **the most important change for engineers** evaluating any routing, model, or
delegation change against the Seshat objective.

**Before the pedagogical North Star**, "optimal" meant:
- Turns complete without errors
- Latency is acceptable
- Cost is within budget
- Per-turn quality rating (FRE-407) is flat-or-up

**After the pedagogical North Star**, "optimal" means **all of the above, plus:**
- Recall is being practiced and the schedule is being maintained
- Concepts are being extracted and the learning model is being updated
- The learner's open threads are being preserved across sessions
- Cross-domain connections are being built over time
- The session shape (rituals) is being honored
- The challenge level is calibrated to the learner's current edge

A routing change that reduces latency by 30% but routes the Socratic dialogue to a `sub_agent`
that lacks the Socratic framing is **not an improvement**. It is an objective regression.

A delegation that retrieves memory candidates for primary to frame and present — with primary
retaining ownership of recall calibration and emotional tone — is a genuine improvement: it
reduces bounded cognition cost without surrendering pedagogical continuity.

**Evaluation criteria for M5 (Eval Harness):**
When the M5 behavioral eval harness is designed, it must include tests that can distinguish:
- A turn that produces the correct factual answer but skips the active-recall framing
- A turn that preserves the open thread vs one that resolves it silently
- A turn that correctly identifies and names a cross-domain principle vs one that treats
  each domain as isolated

These distinctions are invisible to an execution-success metric. They require pedagogical-outcome
labeling (§3 Result Type Taxonomy).

---

## 7. Relationship to Existing Architecture

### Memory protocol (`memory/protocol.py`)

The pedagogical layer is built **on top of** the existing memory substrate — it does not replace it.

| `MemoryType` | Pedagogical use |
|---|---|
| `EPISODIC` | Raw turn history — source material for Layer 1 extraction |
| `SEMANTIC` | Promoted concept nodes and principles — the learning model's long-term store |
| `PROCEDURAL` | Skill and ritual behavior — opening/closing ritual scripts |
| `PROFILE` | Learner model state — engagement depth, `next_review` dates, open threads |
| `WORKING` | Within-session state — current challenge level, active threads this session |
| `DERIVED` | Cross-thread correlations from Layer 5 — generated, not observed |

The existing episodic→semantic promotion pipeline (entity extraction via Qwen3-8b, Neo4j write) is
a **prerequisite** for Layer 1 extraction. Layer 1 adds pedagogical annotation to what the
promotion pipeline already writes.

### Captain's Log (`captains_log/`)

Captain's Log captures self-improvement signal — reflection via DSPy `ChainOfThought`. In the
pedagogical context, it captures:
- Quality of recall prompts generated (did the learner retrieve?)
- Accuracy of challenge calibration (did the learner express frustration or boredom?)
- Whether open threads were preserved across the session

This is the feedback signal that allows the pedagogical layer to improve its own behavior over time.

### Knowledge graph (Neo4j, `memory/`)

The concept graph is the core data structure for Layers 4 and 5. The existing Neo4j substrate
already stores entities and relationships from the promotion pipeline. The pedagogical layer
requires:

**New node types:**
- `Concept` (annotation over existing entity nodes: adds `engagement_depth`, `next_review`,
  `recall_history`, `pedagogical_filter_signals`)
- `OpenThread` (an unresolved question, linked to the concept(s) it touches)

**New relationship types:**
- `DEPENDS_ON` (concept A requires concept B to be meaningful)
- `BRANCH_OF` (concept A is a specialization of concept B)
- `CORRELATES_WITH` (structural similarity across domains — Layer 5 output)
- `ANCHORED_IN` (concept A first appeared in session S, emotionally resonant)

**M3 implementation note:** Start by annotating existing entity nodes, not replacing the entity
schema. The pedagogical layer is an annotation layer on the existing graph, not a parallel graph.

### PostgreSQL (`service/`)

Session state, turn metadata, and `next_review` scheduling data live in Postgres. The spaced
repetition engine (Layer 3) requires a queryable `next_review` index — either a column on the
concept node's Postgres mirror, or a scheduled-events table. Design is an M3 open decision.

---

## 8. Open Questions (M2 Must Answer)

Before M3 implementation begins, the M2 mapping instrument (route trace ledger) must resolve:

1. **The thinking-token hypothesis:** does `primary` actually use significant thinking tokens on
   `CONVERSATIONAL` and `MEMORY_RECALL` turns? (See ADR-0084 §Open decisions §1.)

2. **The deterministic-shell boundary:** for each turn type in the ~7-turn canonical eval set,
   does the gateway label match the actual cognitive work performed? (See ADR-0084 §Open decisions §2.)

3. **Fake-safe SINGLE detection:** which SINGLE turns are safe for lighter treatment, which appear
   safe but carry pedagogical continuity, and which are explicitly pedagogical? (See ADR-0084 §Open
   decisions §4.)

4. **Layer 1 extraction latency:** post-session extraction must not block the final response or
   the next session open. What is the p95 extraction latency on a representative session? Is async
   background execution sufficient, or is a separate worker required?

5. **Concept graph schema migration:** the existing Neo4j entity schema (from the promotion
   pipeline) needs pedagogical annotations. What is the migration path that does not break existing
   semantic memory consumers?

---

## 9. Milestone Sequence Reference

| Milestone | Gate | Depends on |
|---|---|---|
| **M1: Foundation** | ADR-0084 accepted; North Star spec committed; FRE-432 reframed | — |
| **M2: Mapping & Measurement** | Instrument labels any turn with orchestration event + pedagogical outcome | M1 |
| **M3: Pedagogical Layer** | A conversation produces a structured learning artifact; next-review tagging works | M2 |
| **M4: Delegation Policy** | Primary correctly delegates bounded cognition without surrendering pedagogical continuity | M3 |
| **M5: Eval Harness** | Any routing or model change measurable against pedagogical-outcome regression | M4 |

---

## References

- **ADR-0084** — `docs/architecture_decisions/ADR-0084-pedagogical-architecture-socratic-tutor-layer.md`
- **Research doc** — `docs/research/2026-06-03-pedagogical-architecture-origins.md`
- **ADR-0082** — `docs/architecture_decisions/ADR-0082-tier-aware-model-selection-for-single-tasks.md` (superseded for pedagogical routing)
- **Memory protocol** — `src/personal_agent/memory/protocol.py`
- **Captain's Log** — `src/personal_agent/captains_log/`
- **Tickets** — FRE-447 (ADR), FRE-448 (research doc), FRE-449 (this spec), FRE-450 (FRE-432 revision), FRE-432 (original scope)
- **Seshat Pedagogical Architecture project** — Linear, M1–M5
