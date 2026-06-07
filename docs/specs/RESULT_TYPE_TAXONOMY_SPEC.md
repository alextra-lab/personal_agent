# Result Type Taxonomy — Formal Spec

> **Status:** Active — 2026-06-06
> **Origin:** Observability Foundation project (FRE-451). Pedagogical taxonomy inherited from the
> Seshat Pedagogical Architecture project (M1: Foundation).
> **Governing ADR:** `docs/architecture_decisions/ADR-0084-pedagogical-architecture-socratic-tutor-layer.md` §D4
> **Canonical list:** `docs/specs/PEDAGOGICAL_NORTH_STAR.md` §3
> **Identity contract:** `docs/architecture_decisions/ADR-0074-*` (joinability / emit-site discipline)
> **Audience:** Engineers building the M2 route-trace instrument and the M5 eval harness, and
> anyone evaluating a routing, model, or delegation change against the agent's objective.

> **This spec is the canonical *reference document* — the instrument by which a turn is read and
> labeled.** It is not a software mechanism; it defines the labels, their meanings, and the rules a
> labeler (programmatic or human) applies. The M2 instrument and M5 harness are built *against* this
> reference; they do not live here.

> **Authority.** This spec does **not** define or extend the taxonomy. Taxonomy membership — the 5
> orchestration events and 10 pedagogical outcomes — is fixed by **ADR-0084 §D4** and mirrored in
> **North Star §3**. Adding, removing, or renaming a type requires an ADR-0084 revision, never an
> edit here. Where this document and ADR-0084 could conflict, **the ADR governs.** This document adds
> rigor (formal definitions, assignment conventions, detection classification, M2/M5 linkage) on top
> of a frozen list.

---

## 1. Purpose & the measurement question

Seshat's objective is pedagogical (see North Star §1): it is a personal Socratic tutor, not a
general assistant. That changes what "a good turn" means. The measurement question is **not**:

> "Did the turn finish? Was it cheap? Was it fast?"

It is (ADR-0084 §D4, North Star §3):

> **Did the turn preserve continuity, ask the right kind of question, strengthen recall, extract
> useful structure, and connect knowledge over time?**

A turn that completes correctly but at the wrong challenge level, that resolves an open thread
without flagging it, or that summarizes where it should have asked, is a **pedagogical failure even
though it is an execution success**. An execution-success metric cannot see that failure. The result
type taxonomy exists to make it visible — so that any routing, model, or delegation change can be
judged against the pedagogical function rather than against turn completion.

This spec is the reference that makes that judgement repeatable: a labeler reads a turn and assigns
labels from a fixed vocabulary, and the resulting labels are what M2 measures and M5 regresses
against.

---

## 2. The two-layer separation

The taxonomy deliberately separates two layers that naïve telemetry conflates. ADR-0082's original
measurement framing conflated them; ADR-0084 §D4 split them, and that split is the load-bearing
structural decision of this spec.

| Layer | Question it answers | Nature | Primary detection mode |
|---|---|---|---|
| **Orchestration events** | *What did the harness do?* | Execution facts — observable from the harness's own control flow | Programmatic (reliable) |
| **Pedagogical outcomes** | *What did the learner get?* | Learning-facing effects — observable only by judging the response against the learner's trajectory | Human-rubric / hybrid |

**Why the separation matters.** A single turn can both execute delegation *and* produce a recall
check. If those are recorded as one undifferentiated "result," the pedagogically important signal
(the recall check) is hidden behind the orchestration fact (the delegation). Worse, an orchestration
fact can look like success while the pedagogical layer silently fails: a turn the harness labels
`MEMORY_RECALL SINGLE / primary_handled` may actually have performed conceptual synthesis the gateway
label does not name — or may have stripped the Socratic framing entirely. Only a separate
pedagogical-outcome layer can expose that.

**The two layers are assigned independently.** A turn always carries exactly one orchestration-event
label *and* a (possibly multi-valued) pedagogical-outcome label set. Neither layer is derivable from
the other.

---

## 3. Orchestration events (formal definitions)

> **Membership and canonical meaning are fixed by ADR-0084 §D4.** The "Canonical meaning" column is
> quoted verbatim from that ADR. The "Trigger condition" and "Detection source" columns are the
> formalization this spec adds; they describe *how a labeler recognizes the event*, not a new
> definition.

The five orchestration events describe how the harness routed and executed the turn. They are read
from the harness's own control flow — the decomposition strategy chosen by the gateway
(`request_gateway/`) and the sub-agent execution path in the orchestrator
(`orchestrator/executor.py`, `orchestrator/sub_agent.py`).

### 3.1 `primary_handled`

- **Canonical meaning (ADR-0084 §D4):** *Turn handled end-to-end by primary with no delegation.*
- **Trigger condition:** The gateway selected the `SINGLE` strategy (no expansion), or expansion was
  assessed but no sub-agent was invoked; the primary model produced the user-facing response without
  any sub-agent contribution.
- **Detection source:** Programmatic. The gateway `DecompositionStrategy` is `SINGLE`, and no
  sub-agent execution record exists for the turn (`ctx.sub_agent_results` empty).

### 3.2 `delegate_called`

- **Canonical meaning (ADR-0084 §D4):** *Sub-agent invoked for bounded work.*
- **Trigger condition:** The harness invoked at least one sub-agent on the `HYBRID` or `DECOMPOSE`
  path (`orchestrator/executor.py` expansion), regardless of whether the sub-agent's output was later
  used.
- **Detection source:** Programmatic. A sub-agent execution record exists for the turn.

### 3.3 `delegate_result_used`

- **Canonical meaning (ADR-0084 §D4):** *Sub-agent output incorporated into primary synthesis.*
- **Trigger condition:** A sub-agent was called *and* its output was incorporated into the primary's
  final synthesis (the primary's response depends on the sub-agent result).
- **Detection source:** Programmatic for the structural fact that a result was passed to the primary
  synthesis step; **hybrid** for confirming genuine *incorporation* (vs. the result being present but
  ignored), which can require reading the response. Treat the structural pass-through as the
  programmatic signal and flag genuine-incorporation confirmation as a hybrid check.
- **Refinement mechanism (FRE-515):** applied **post-hoc by rubric**, never at write time — the
  harness has no incorporate/reject decision point (all sub-agent summaries are injected into one
  synthesis message unconditionally), so the row keeps the `delegate_called` floor and the label is
  refined during the eval-set human pass. Candidate-grade structural signals on the row order the
  queue: per-sub `reply_overlap` (summary-token containment in the final reply — weak/noisy, informs
  the rubric, never decides it) and the read-time `delegate_disposition_candidate` heuristic
  (`route_trace/classifier.py`). Rubric: Q1 dependence → used; Q2 explicit rejection → discarded;
  Q3 implicit non-use (e.g. an error/apology reply) → discarded; partial incorporation counts as
  used. Verdicts are recorded in the run report + Linear, not written back to the row.

### 3.4 `delegate_result_discarded`

- **Canonical meaning (ADR-0084 §D4):** *Sub-agent output rejected by primary on review.*
- **Trigger condition:** A sub-agent was called and the primary, on review, rejected its output and
  did not incorporate it.
- **Detection source:** Programmatic where the harness records an explicit reject/skip decision;
  **hybrid** where rejection is implicit (the primary silently produced an answer that does not use
  the result). M2 must decide where the threshold sits.
- **Refinement mechanism (FRE-515) — where the threshold sits:** no explicit reject/skip decision
  exists in the harness today, so detection is fully hybrid: post-hoc rubric over the row's
  disposition signals (see §3.3 refinement note). The `fre453-baseline-02` exemplar is the
  implicit-non-use shape: sub-agent summaries passed to synthesis, but the turn errored downstream
  (`error_type=LLMServerError`) and the reply is a 501-char apology using none of them. If a future
  ADR adds an explicit primary review/reject decision, that decision becomes the programmatic
  signal and write-time refinement becomes defensible.

### 3.5 `fallback_triggered`

- **Canonical meaning (ADR-0084 §D4):** *Escalation from sub-agent to primary mid-turn.*
- **Trigger condition:** A sub-agent path failed, timed out, or was escalated mid-turn and the
  primary took over to complete the turn.
- **Detection source:** Programmatic. The harness records the escalation/fallback transition
  (e.g. an expansion failure that falls back to a primary reply).

---

## 4. Pedagogical outcomes (formal definitions)

> **Membership and canonical meaning are fixed by ADR-0084 §D4.** The "Canonical meaning" line is
> quoted verbatim. "Entry condition", "Evidence required", and "Detection mode" are the
> formalization this spec adds.

The ten pedagogical outcomes describe what the *learner* got. They are observable only by judging the
turn against the learner's conceptual trajectory — which is why most of them are human-rubric or
hybrid, not programmatic (see §6). Until the M3 pedagogical layer emits structured extraction signal
(`captains_log/`, `memory/`, `second_brain/`), these are predominantly rubric judgements.

A turn may carry **several** of these, or — pending the §5 cardinality convention — possibly none.

### 4.1 `recall_practiced`
- **Canonical meaning:** *Active recall was exercised during the turn.*
- **Entry condition:** Seshat asked the learner to retrieve something (a concept, principle, prior
  answer) rather than supplying it, and the learner attempted retrieval.
- **Evidence required:** A retrieval prompt in the response *and* a learner retrieval attempt; a
  rhetorical question that Seshat immediately answers itself does not qualify.
- **Detection mode:** Hybrid — a recall prompt may be pattern-detectable, but confirming genuine
  active recall (not passive restatement) is a rubric judgement.

### 4.2 `concept_extracted`
- **Canonical meaning:** *A concept was identified and tagged for the learning model.*
- **Entry condition:** A concept surfaced in the turn was identified and written to the learning
  model (Layer 1 extraction).
- **Evidence required:** A learning-model write tied to the turn (concept node created/annotated).
- **Detection mode:** Programmatic once the M3 extraction pipeline emits concept-node writes; until
  then, rubric.

### 4.3 `principle_identified`
- **Canonical meaning:** *An underlying principle was named and anchored.*
- **Entry condition:** The turn named the *principle beneath* a specific example (not just the
  example) and anchored it for reinforcement.
- **Evidence required:** The response articulates a principle at the abstraction level above the
  immediate example, and the principle is anchored (tagged/stored).
- **Detection mode:** Hybrid — anchoring is programmatic; recognizing that a *principle* (not a
  surface fact) was named is a rubric judgement. Distinguish from `concept_extracted` (see §5.4).

### 4.4 `counterintuitive_finding_marked`
- **Canonical meaning:** *A result marked for reinforcement (surprises stick).*
- **Entry condition:** A counterintuitive or surprising result was explicitly flagged for spaced
  return (Layer 2 filter).
- **Evidence required:** An explicit reinforcement tag on a result identified as surprising.
- **Detection mode:** Hybrid — the tag is programmatic; judging "counterintuitive" is rubric.

### 4.5 `open_thread_preserved`
- **Canonical meaning:** *An unresolved question was explicitly held open.*
- **Entry condition:** An unresolved question was explicitly held open (marked for return) rather
  than dropped or silently resolved.
- **Evidence required:** An explicit open-thread marker (an `OpenThread` node, or response text that
  defers the question for a later session). Silent non-resolution does **not** qualify — the failure
  mode this guards against is dropping the thread without marking it.
- **Detection mode:** Hybrid. Distinguish from `synthesis_performed` (see §5.4).

### 4.6 `cross_connection_made`
- **Canonical meaning:** *A principle from one domain linked to another.*
- **Entry condition:** A principle from one domain was explicitly linked to a structurally similar
  principle in another domain (Layer 5 correlation surfaced in-turn).
- **Evidence required:** The response names both domains and the shared structural principle; a
  graph `CORRELATES_WITH` edge, where emitted.
- **Detection mode:** Hybrid.

### 4.7 `field_note_emitted`
- **Canonical meaning:** *A between-session observation was captured.*
- **Entry condition:** An observation arising outside a formal session was captured as a field note
  (North Star §4 "Between-Session Field Notes").
- **Evidence required:** A timestamped field-note record associated with a concept node or open
  thread.
- **Detection mode:** Programmatic once field-note capture emits a record; rubric until then.

### 4.8 `learner_state_updated`
- **Canonical meaning:** *The learner model was updated with new engagement signal.*
- **Entry condition:** The learner model was updated with a new engagement signal (engagement depth,
  recall grade, curiosity signal).
- **Evidence required:** A learner-model (PROFILE) write tied to the turn.
- **Detection mode:** Programmatic once the learner-model write path emits; rubric until then.

### 4.9 `synthesis_performed`
- **Canonical meaning:** *Multiple prior concepts connected into a new structure.*
- **Entry condition:** Two or more prior concepts were connected into a *new* structure within the
  turn (more than recalling them side by side).
- **Evidence required:** The response constructs a new relationship/structure over prior concepts;
  not merely listing or recalling them.
- **Detection mode:** Human-rubric. This is the canonical example of work the gateway label can hide
  (a `MEMORY_RECALL SINGLE` turn that actually performed synthesis — see §7.3).

### 4.10 `misalignment_detected`
- **Canonical meaning:** *A divergence between the learner's model and the actual concept was
  identified.*
- **Entry condition:** The turn surfaced a divergence between what the learner appears to understand
  and the actual concept (a misconception caught).
- **Evidence required:** The response identifies a specific divergence and (ideally) the corrected
  understanding.
- **Detection mode:** Human-rubric. Together with `open_thread_preserved`, this is one of the two
  outcomes that catch *subtle* pedagogical failures invisible to the orchestration layer
  (North Star §5 "failure mode to guard against").

---

## 5. Assignment rules

> **These are assignment *conventions*, not taxonomy definitions.** The taxonomy membership is fixed
> by ADR-0084 §D4. The rules below describe how a labeler attaches the fixed labels to a turn. Where a
> rule is *not* stated by ADR-0084 §D4, it is flagged as **[proposed — M2 validates]** and must be
> confirmed against the canonical eval set before it hardens into practice. It must not be treated as
> canon on the strength of this document alone.

### 5.1 Two independent layers
Every labeled turn carries **one orchestration-event label** and a **pedagogical-outcome label set**.
This matches the M2 gate wording (ADR-0084 §Verification): *"the instrument can label any turn with an
orchestration event **and** a pedagogical outcome."* The layers are assigned independently — the
orchestration event never implies a pedagogical outcome, and vice versa.

### 5.2 Orchestration-event cardinality and exclusivity — **[provisionally supported — FRE-515]**
ADR-0084 §D4 lists the five events without stating whether they are mutually exclusive. This spec
*proposes* the convention that a turn is labeled with the **single** orchestration event that best
describes its terminal control-flow outcome (e.g. a turn that called a sub-agent and then used its
output is `delegate_result_used`, which subsumes `delegate_called`). This is a proposed convention for
M2 to validate against the eval set, **not** a claim from ADR-0084. M2 may instead find that a layered
event model (e.g. `delegate_called` + `delegate_result_used`) labels real turns more faithfully; if so,
M2's finding governs.

**FRE-515 finding (2026-06-07):** provisionally supported by `fre453-baseline-02` for the
delegate-called cases observed. The layered alternative adds no information: the `delegate_called`
fact is structurally preserved on every row by `sub_agent_count > 0` (and the `sub_agents` JSONB),
so refining the single label loses nothing — both baseline delegate rows remain fully interpretable
under one refined event. Two rows are too small a sample to harden the convention; continue
validating as fallback / explicit-discard cases appear.

### 5.3 Pedagogical-outcome cardinality — **[proposed — M2 validates]**
Pedagogical outcomes are **multi-label**: a single turn may exercise recall, preserve an open thread,
and update the learner state all at once. Whether a turn may legitimately carry **zero** pedagogical
outcomes (e.g. a pure tool-fetch turn with no learning-facing effect) is an **open assignment
question** this spec does not resolve. The M2 canonical eval set is the place to answer it: if the
eval set contains turns with no defensible pedagogical outcome, the "zero allowed" convention is
confirmed; if every representative turn carries at least one, it is not. **This spec does not assert
that zero is allowed** — doing so would silently relax the canonical sources, which speak of labeling
a turn with an orchestration event *and* a pedagogical outcome.

### 5.4 Disambiguation guidance for near-miss outcomes (reviewer guidance, not machine rules)
The following are guidance for the human rubric in §6, to keep reviewers consistent. They are not
mechanical rules and not part of the taxonomy:

- **`concept_extracted` vs `principle_identified`:** `concept_extracted` is identifying *and storing* a
  concept that appeared (a "what"); `principle_identified` is naming the *underlying principle* beneath
  it (a "why it generalizes") and anchoring it. A turn can have both: the concept is extracted and its
  principle is named.
- **`open_thread_preserved` vs `synthesis_performed`:** preserving an open thread *defers* an
  unresolved question for later; synthesis *connects* resolved concepts into a new structure now. A
  turn can do both — synthesize what is known and explicitly hold open what is not.
- **`recall_practiced` vs passive restatement:** recall requires the *learner* to retrieve; Seshat
  restating a prior concept is not `recall_practiced`.

---

## 6. Detection classification

Each type is classified by how it is detected: **programmatic** (read reliably from harness/substrate
records), **human-rubric** (requires a reviewer judging the turn against the pedagogical objective),
or **hybrid** (a programmatic signal narrows the candidate set, but a rubric judgement confirms it).

> **Caveat — the gateway label can lie (ADR-0084 §Open decisions §2).** The gateway's `TaskType` /
> strategy label is a reliable signal for the *orchestration* layer (it *is* the harness's own
> decision), but it is **not ground truth for a pedagogical outcome**. A turn labeled
> `MEMORY_RECALL SINGLE` may have performed synthesis or caught a misalignment. No "programmatic"
> classification below claims a *measured* pedagogical mechanism; programmatic detection of a
> pedagogical outcome means only that a substrate *write* (a concept node, a learner-model update)
> exists — and those writes do not yet exist until the M3 pedagogical layer ships. Until then, the
> pedagogical column is effectively human-rubric/hybrid in practice.

| Type | Layer | Detection mode | Signal-source sketch |
|---|---|---|---|
| `primary_handled` | Orchestration | Programmatic | Gateway `DecompositionStrategy=SINGLE`; no sub-agent record |
| `delegate_called` | Orchestration | Programmatic | Sub-agent execution record exists (`orchestrator/executor.py` expansion) |
| `delegate_result_used` | Orchestration | Hybrid | Result passed to primary synthesis (programmatic) + incorporation confirmed (rubric) |
| `delegate_result_discarded` | Orchestration | Hybrid | Explicit reject (programmatic) or implicit non-use (rubric) |
| `fallback_triggered` | Orchestration | Programmatic | Harness-recorded escalation/fallback transition |
| `recall_practiced` | Pedagogical | Hybrid | Retrieval prompt pattern + learner-retrieval rubric |
| `concept_extracted` | Pedagogical | Programmatic† | Concept-node write (M3 `memory/` extraction); rubric until M3 |
| `principle_identified` | Pedagogical | Hybrid | Anchor write + "is a principle, not a fact" rubric |
| `counterintuitive_finding_marked` | Pedagogical | Hybrid | Reinforcement tag + "is counterintuitive" rubric |
| `open_thread_preserved` | Pedagogical | Hybrid | `OpenThread` marker + "explicitly held, not dropped" rubric |
| `cross_connection_made` | Pedagogical | Hybrid | `CORRELATES_WITH` edge / two-domain link + rubric |
| `field_note_emitted` | Pedagogical | Programmatic† | Field-note record (North Star §4); rubric until emitted |
| `learner_state_updated` | Pedagogical | Programmatic† | PROFILE/learner-model write; rubric until M3 |
| `synthesis_performed` | Pedagogical | Human-rubric | No reliable programmatic signal; reviewer judges new structure |
| `misalignment_detected` | Pedagogical | Human-rubric | No reliable programmatic signal; reviewer judges divergence |

† **Programmatic *once the M3 pedagogical layer emits the corresponding substrate write.*** Until M3
ships those emit sites, these outcomes are detected by human rubric. This spec does not specify the
emit sites — that is M2/M3 work (see §7 and "Out of scope").

---

## 7. How the taxonomy drives M2 and M5

This taxonomy is the canonical reference the M2 instrument and the M5 harness are built against. The
relationships below are grounded in ADR-0084 §Open decisions / §Verification and North Star §8–§9.

### 7.1 M2 labeling gate
The M2 route-trace instrument's gate criterion (ADR-0084 §Verification, M2 gate) is: **it can label
any turn with one orchestration event *and* its pedagogical-outcome set, drawn from this taxonomy.**
This spec is the label vocabulary and the rules for that labeling. The instrument's emit-site wiring,
the route-trace ledger schema, and the ES field layout are **M2's design**, not this spec's.

### 7.2 M2 eval-set coverage categories
The canonical ~7-turn eval set (ADR-0084 §Open decisions §3) is the gate against which the assignment
conventions in §5 are validated. The taxonomy must label every turn type in that set:

- trivial conversational turns
- memory recall turns
- opening ritual turns
- closing ritual turns
- cross-thread synthesis turns
- emotionally loaded learning turns
- tool-heavy research turns

If any turn type cannot be cleanly labeled with one orchestration event + its outcome set, that is a
signal to revisit either the assignment conventions (§5) here or the taxonomy itself (via an ADR-0084
revision — not via this spec).

### 7.3 M2 deterministic-shell boundary
The taxonomy is the instrument that exposes a **lying gateway label** (ADR-0084 §Open decisions §2,
North Star §8). The M2 instrument must record, per turn, whether the gateway's label matches the
actual cognitive work — and the pedagogical-outcome layer is how that mismatch becomes visible. A turn
the gateway labels `MEMORY_RECALL SINGLE` that a reviewer labels `synthesis_performed` or
`misalignment_detected` is a deterministic-shell boundary violation: the label says "trivial lookup,"
the pedagogical layer says "this was the tutor doing tutoring." Fake-safe SINGLE routing that strips
the tutor is invisible in the orchestration layer and visible only here.

### 7.4 M2 thinking-token measurement gate
The M2 gate also requires measuring (not asserting) thinking-token usage per `TaskType` on the labeled
eval set (ADR-0084 §Verification M2 gate, North Star §8). This taxonomy provides the labeling basis:
thinking-token usage is measured against turns labeled by both layers, so the cost question is answered
in pedagogical terms (does the tutor *need* the thinking budget on this turn type?) rather than purely
cost terms.

### 7.5 M5 regression criterion
The M5 behavioral eval harness (North Star §6 "Evaluation criteria for M5") judges any routing, model,
or delegation change by **pedagogical-outcome regression, not execution-success regression.** Concretely,
the harness must be able to distinguish (North Star §6):

- a turn that gives the correct factual answer but skips the active-recall framing
  (`recall_practiced` lost),
- a turn that preserves the open thread vs one that resolves it silently
  (`open_thread_preserved` lost),
- a turn that names a cross-domain principle vs one that treats each domain as isolated
  (`cross_connection_made` lost).

A change that improves latency or cost while regressing any pedagogical outcome is an **objective
regression**, not an improvement (North Star §6). This taxonomy is the vocabulary in which that
regression is stated.

---

## 8. Relationship to source documents

| Document | Role |
|---|---|
| **ADR-0084 §D4** | *Governs.* Defines taxonomy membership (5 events + 10 outcomes) and their canonical meanings. Any change to membership happens here, via an ADR revision. |
| **North Star §3** | The canonical list, mirrored from ADR-0084 §D4. "Do not extend without an ADR-0084 revision." |
| **This spec** | The formal *reference instrument*: definitions, assignment conventions, detection classification, and M2/M5 linkage built on the frozen list. Adds rigor, not membership. |
| **M2 instrument (next ticket)** | Built against this reference. Owns emit-site wiring, ledger schema, the ~7-turn eval set contents, and validation of the §5 proposed conventions. |
| **M5 harness** | Built against this reference. Owns the behavioral tests that detect pedagogical-outcome regression. |

> **Note on back-references.** A back-reference from ADR-0084 §D4 to this formal spec would aid
> navigation, but ADR-0084 is owned by the ADR session, not the build session, and is not edited here.
> If desired, that one-line cross-reference should be filed as a follow-up for the ADR session.

### Out of scope (deferred)

- Route-trace ledger instrument design and emit-site wiring → **M2**.
- The canonical ~7-turn eval-set contents → **M2**.
- The behavioral eval harness implementation → **M5**.
- Any change to taxonomy membership → requires an **ADR-0084 revision** (ADR session).

## References

- **ADR-0084** — `docs/architecture_decisions/ADR-0084-pedagogical-architecture-socratic-tutor-layer.md`
  (referenced as "ADR-0083" in some Linear tickets; see ADR-0084's numbering note)
- **Pedagogical North Star** — `docs/specs/PEDAGOGICAL_NORTH_STAR.md`
- **Research** — `docs/research/2026-06-03-pedagogical-architecture-origins.md`
- **ADR-0074** — identity / joinability emit-site discipline (inherited by the M2 instrument)
- **ADR-0082** — `docs/architecture_decisions/ADR-0082-tier-aware-model-selection-for-single-tasks.md`
  (superseded for the pedagogical routing question by ADR-0084 §D6)
- **Tickets** — FRE-451 (this spec), Observability Foundation project; Seshat Pedagogical Architecture
  project M1–M5
