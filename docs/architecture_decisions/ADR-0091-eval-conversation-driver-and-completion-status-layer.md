# ADR-0091 — Eval Conversation Driver and Turn Completion-Status Layer

**Status:** Accepted — 2026-06-21 (FRE-582; status reconciled — being implemented via FRE-541, In Progress). Originally Proposed 2026-06-14.
**Related:** ADR-0084 (**amends §D4** — adds the completion-status layer to the result-type taxonomy), `docs/specs/RESULT_TYPE_TAXONOMY_SPEC.md` (FRE-451, mirrors §D4), FRE-541 (this ADR), FRE-453 (the canonical eval set this unblocks), FRE-523 (eval-mode validity sibling), `docs/specs/PEDAGOGICAL_NORTH_STAR.md`, `scripts/eval/fre453_canonical_evalset/`
**Project:** Observability Foundation (L0/L1)

---

## Context

### The eval conflates *response quality* with *harness completion*

The FRE-453 canonical eval harness (`scripts/eval/fre453_canonical_evalset/harness.py`) drives each
case as **one scored stimulus** — optional unscored `setup_messages`, then a single `stimulus`, then
it reads back the route-trace row and renders a human rubric checklist. It **never replies to the
agent**. The conversation is, by construction, exactly one turn long.

That single-shot shape silently merges two unrelated things into one undifferentiated "the expected
outcome didn't happen":

- **(a) a response-quality miss** — the conversation reached a natural end, but the expected
  tutoring outcome didn't occur; versus
- **(b) a harness artifact** — the model **correctly paused to ask the user for more information**
  (an under-specified stimulus), and the single-shot harness never answered, so the conversation
  never reached the point where a pedagogical outcome *could* occur.

A pause-for-input turn is **correct behavior, not a failure**. But under the current harness its
rubric is unfillable (the conversation never concluded), and the report cannot tell (a) from (b). A
human rubric pass over these baselines would therefore be rating *quality* and *the harness's ability
to finish a conversation* through one lens. The motivating observation (FRE-541, owner, 2026-06-08)
was a run whose dominant per-case status was a single "did-not-fire-in-window" bucket that hid this
distinction.

### The capability is uncaptured in the taxonomy

The result-type taxonomy (`RESULT_TYPE_TAXONOMY_SPEC.md`, governed by **ADR-0084 §D4**) has two
layers — **orchestration events** ("what the harness did") and **pedagogical outcomes** ("what the
learner got") — and **no** notion of "the turn paused for user input." Its nearest neighbour,
`open_thread_preserved` (§4.5 — *"an unresolved question was explicitly held open"*), is the
**opposite** semantics: the *tutor* deliberately leaving a Socratic loop open while still moving the
turn forward, not the agent *blocked on the user* and unable to proceed.

### Taxonomy membership is frozen — this must be an ADR-0084 change

The spec is explicit and self-denying: the five orchestration events and ten pedagogical outcomes
are fixed by **ADR-0084 §D4**, and "adding, removing, or renaming a type requires an ADR-0084
revision, never an edit" to the spec (§Authority, §8). So FRE-541's "add a result type" cannot be a
spec edit; it is an ADR-level taxonomy change — which is why it routed to the adr session.

### Detection is unusually hard *here*, because the agent is a Socratic tutor

Seshat's North Star (ADR-0084 §D1) is a personal Socratic tutor: **ending a turn with a question is
the desired behaviour**, not a stall. So a syntactic detector ("the reply ends with `?`") would
mislabel nearly every good tutoring turn as a clarification pause. The distinction between
*blocked-on-the-user* and *a Socratic prompt that still advances the turn* is **semantic, not
syntactic**. This shapes the detector decision (D4).

### Scope

This is needed **for the test harness only** (owner, 2026-06-14): to carry each eval case to a
natural end automatically, so a human rubric pass can rate *quality* on conversations that actually
concluded. It deliberately introduces **no production behaviour change** — the agent is not taught a
new "awaiting input" state, and production telemetry gains nothing in this ADR.

### Why this gates FRE-453

FRE-453's single-shot baselines are only valid for cases that *happened* to reach a natural end; the
rest measure harness completion, not quality. A meaningful human rubric pass on FRE-453 waits on this
driver (FRE-453 is `blocked by` FRE-541).

---

## Decision

### D1 — Add a third, orthogonal taxonomy layer: **turn completion status** (amends ADR-0084 §D4)

The taxonomy gains a **third layer**, orthogonal to the existing two. A labelled turn (or, under the
driver, a labelled *conversation*) now carries **one orchestration event**, a **pedagogical-outcome
set**, **and exactly one completion status**:

| Completion status | Meaning | Entry condition |
|---|---|---|
| `natural_end` | The conversation reached a natural conclusion. | The agent's reply resolves the exchange (an answer, a synthesis, or a Socratic move that advances rather than blocks); the driver has no scripted user turn it is obligated to send. |
| `clarification_requested` | The turn paused, **blocked on the user**, for information it cannot proceed without. | The reply's primary function is to request information/decision from the user that is genuinely required to continue, *and* it is not the deliberate held-open Socratic loop of `open_thread_preserved` (see D2). **Continuation signal**, not a verdict. |
| `incomplete` | The conversation did not conclude within bounds. | The max-turns guard (D3) was hit, or the turn errored, before a `natural_end` was reached. |

**Orthogonality.** Completion status answers *"did the exchange finish, and if not, why?"* — which is
neither "what the harness did" (orchestration) nor "what the learner got" (pedagogical). The three
layers are assigned independently. Critically: **pedagogical outcomes are scored only on conversations
whose completion status is `natural_end`.** A `clarification_requested` conversation is carried
forward by the driver (D3) until it concludes; an `incomplete` one is excluded from the
quality rubric and reported as a completion failure, never as a pedagogical miss.

**`clarification_requested` is a continuation signal, never a quality verdict.** It must **never** be
recorded as, or collapsed into, a pedagogical-outcome miss (the old undifferentiated bucket).

### D2 — Crisp distinction: `clarification_requested` (status) vs `open_thread_preserved` (outcome)

These live on different layers and must never be confused:

| | `clarification_requested` (completion status, D1) | `open_thread_preserved` (pedagogical outcome, §4.5) |
|---|---|---|
| Who is blocked | The **agent** — it cannot proceed without the user. | **No one** — the turn proceeds; the *tutor* chooses to defer a thread. |
| Turn outcome | The turn **pauses**; no conclusion yet. | The turn **concludes**; a thread is explicitly marked for later. |
| Driver action | **Supply the next scripted user turn and continue.** | None — the conversation may already be at `natural_end`. |
| Layer | Completion status (3rd layer). | Pedagogical outcome (2nd layer). |

A single concluded turn may legitimately carry `natural_end` **and** `open_thread_preserved` at once
(the tutor wrapped up while deferring a sub-question). It can never carry `natural_end` **and**
`clarification_requested` — those are mutually exclusive completion states.

### D3 — Eval conversation driver (scripted multi-turn cases)

Each eval case becomes a **scripted dialogue**, not a single stimulus:

- **Dataset schema** gains an ordered list of **scripted user follow-up turns** per case (a new
  optional field on `EvalCase`; cases that need no follow-up keep today's single-stimulus shape).
  Follow-ups are authored canned text — the lightweight "user simulator" is a script, not a model.
- **Driver loop (exhaustive — every detector output has one deterministic action).** Send
  `setup_messages` (unscored) → `stimulus` → then, after each agent reply, consult the
  completion-status detector (D4) and act:
  - `natural_end` → **stop**; the final agent reply is the scored conclusion.
  - `clarification_requested` **and a scripted user turn remains** → send the **next scripted user
    turn** and continue.
  - `clarification_requested` **and the script is exhausted** → record **`incomplete`** (reason:
    *clarification with no scripted turn remaining*) and stop. This is a normal outcome that flags the
    case as under-scripted: the report surfaces it so the author adds a follow-up turn. The driver
    never improvises a user turn.
  - `incomplete` (the canonical "detector inconclusive" label — neither a conclusion nor a recognised
    clarification pause) → record **`incomplete`** (reason: *detector inconclusive*) and stop. The
    turn neither concluded nor posed an answerable clarification, so the driver has no defensible
    scripted turn to send; the max-turns guard below is the separate safety net for genuine loops.
  - **any off-vocabulary label or a parse failure** → **hard fallback to `incomplete`** (reason:
    *unparseable detector output*) and stop. The driver never "continues blindly" on an unrecognised
    signal (see D4).
- **Max-turns guard.** An independent upper bound (per-case agent-turn cap, applied regardless of
  detector output); on reaching it without a `natural_end`, the conversation is recorded `incomplete`
  (reason: *turn cap exhausted*). Never silently dropped.
- **Determinism.** The driver itself is deterministic (fixed scripts, fixed order); the only model
  in the *control* loop is the cheap detector, pinned to temperature 0 (D4).
- **Scored unit.** Pedagogical-outcome rubric and the programmatic findings are evaluated against the
  **concluding** turn's route-trace row; setup and intermediate clarification turns are context, not
  scored for quality (their rows remain available for instrument-health checks).

### D4 — Completion-status detector: cheap, non-thinking, hypothesis-grade

The driver needs an **automatic, in-the-loop** signal to carry conversations to completion hands-free
(full automation is the requirement; a human continuing dialogues or hand-labelling completion **as
the standing operating mode** was explicitly rejected, owner 2026-06-14). The one-time human
calibration pass below is the deliberate exception — a setup step that validates the detector, not the
running mode. The detector:

- **Is a 3-way classifier** over the agent's latest reply (with the dialogue so far as context),
  emitting one canonical label from the closed set `{natural_end | clarification_requested |
  incomplete}` — where the detector's `incomplete` means "neither a conclusion nor a recognised
  clarification pause" — plus a one-line rationale for the report. This is the single emitted-label
  vocabulary used everywhere in this ADR (D1 statuses, D3 loop, D4 fallback).
- **MUST run on a small, non-thinking model at temperature 0** (e.g. the `sub_agent`/Haiku-class
  tier or the local SLM). Routing the detector to the thinking/reasoning tier is **forbidden** — this
  is a bounded classification, not a reasoning task, and the eval must not acquire a per-turn
  reasoning-model cost. (At eval scale — ~tens of turns per occasional run — the detector cost is
  negligible against the agent turns it gates.)
- **Drives control flow, but never gates pass/fail.** The detector's label *does* steer the driver
  (stop vs send-next-turn) — it is the one model in the control loop (D3). What it never does is
  decide a case's pass/fail or affect the run's exit code: MATCH/MISMATCH findings and the human
  rubric remain the only quality signals, and the exit code reflects instrument health only (a missing
  route-trace row), consistent with the harness's existing posture. A detector misclassification can
  only make one run's *completion* labelling wrong (self-correcting on re-run), never a verdict.
- **Treats dialogue as data, not instructions, and emits a closed set.** The detector consumes
  model-generated text; its prompt must fence that text as untrusted **data** (it must not follow
  instructions embedded in the dialogue) and it must emit only `{natural_end | clarification_requested
  | incomplete}`. Any off-vocabulary output or parse failure is a **hard fallback to `incomplete`**
  (D3), so a prompt-injection attempt or a malformed reply degrades to a recorded completion failure,
  never a control-flow hijack or a silent continue.
- **Is validated once against ground truth.** On the **baseline** run, the human rubric pass also
  labels completion status; detector-vs-human agreement is measured. If agreement is inadequate, the
  detector prompt is tightened or its model bumped (still non-thinking) before the detector is trusted
  to run unattended. The exact detector (a syntactic pre-filter, a cheap judge, or a judge with a
  heuristic guard) is an **eval-data-gated open decision** (below), chosen by measured agreement on
  the 18-case baseline — not asserted here.

### D5 — Report separates completion status from pedagogical quality

The run report gains a **completion-status section** distinct from the pedagogical-quality rubric:

- Every case reports its completion status (`natural_end` / `clarification_requested` /
  `incomplete`) and, for non-`natural_end`, the driver's turn-by-turn trace (which scripted turns
  were sent, where the guard fired).
- The **pedagogical-outcome rubric is rendered only for `natural_end` conversations.** No case may
  show a silent "outcome did not fire" that actually means "the harness cut the conversation short."
- This is a **different axis** from the existing backend-surface `not_fired_within_window` status
  (`observe_background_surfaces`), which observes whether async backend prompt surfaces fired for a
  trace (v0.1, observational). That axis is unchanged; D5 governs *conversation completion*, not
  backend-surface firing. The report must not let the two be read as the same thing.

### D6 — Scope boundary: eval-only now; production emit deferred

- **Eval-only.** The completion-status layer is computed and recorded **by the harness** (driver +
  report). `RouteTraceRow` and the production route-trace ledger are **unchanged** by this ADR.
- **Deferred future (not in scope, no work implied).** A production `completion_status` field on
  `route_traces` — answering "what fraction of *prod* turns stall for clarification" — would be
  genuine L0/L1 observability, but it requires a reliable production signal. The cheapest reliable
  production signal is an **agent-emitted terminal "awaiting input" state** (a behaviour change to the
  orchestrator/prompt), which this ADR deliberately avoids. If that production metric is later wanted,
  it is a separate ADR/ticket that adds the agent-emitted signal first; the eval layer defined here
  does not block on it and is not blocked by it.

### D7 — Governance: ADR-0084 amendment + spec mirror

Because ADR-0084 §D4 governs taxonomy membership, this ADR **amends ADR-0084 §D4** to introduce the
completion-status layer (the canonical definition lives in §D4, mirrored from D1 above). The
reference spec (`RESULT_TYPE_TAXONOMY_SPEC.md`) is then updated to document the third layer — a
mirroring edit authorised by this ADR revision (filed as an implementation ticket, not done in the
adr PR's decision scope beyond the ADR-0084 amendment itself).

---

## Open decisions (eval-data-gated)

These are resolved by the implementation's baseline run, in the measure-don't-assert style; the ADR
fixes the architecture, not these values:

1. **Detector mechanism.** Syntactic pre-filter vs cheap judge vs judge-with-heuristic-guard —
   selected by measured agreement against the human baseline labels on the 18 canonical cases. The
   only hard constraint (D4) is *non-thinking, temperature 0*.
2. **Max-turns guard value.** A small default (proposed: 6 agent turns) validated against the
   baseline — large enough that no genuine multi-turn case is truncated, small enough to bound cost.
3. **`incomplete` sub-typing.** Whether `incomplete` needs to distinguish `max_turns_exhausted` from
   `errored` is deferred until the baseline shows a case that needs it; until then `incomplete` is
   single-valued with the reason carried in the report text.
4. **Scripted-followup authoring depth.** How many follow-ups each case needs is discovered by the
   baseline (most cases likely need zero or one); the schema supports an arbitrary ordered list.
5. **Detector-agreement threshold for unattended use.** What detector-vs-human agreement on the
   baseline counts as "adequate" to trust the detector unattended, and who confirms it, is proposed by
   the implementation's baseline report and confirmed by the owner — not fixed here. Until the
   threshold is met, the detector's completion labels are shown **alongside** the human labels in the
   report rather than replacing them.

---

## Consequences

### Positive

- The human rubric pass rates **quality only**, on conversations that actually concluded — the FRE-453
  baselines become meaningful (the gate this unblocks).
- A correct pause-for-input is recorded as **correct behaviour** (`clarification_requested`), never as
  a pedagogical failure.
- **Full automation**: the harness carries each case to completion with no human in the dialogue loop
  and no human completion-labelling, at negligible added cost (cheap non-thinking detector, eval-only,
  occasional runs).
- The taxonomy gains a clean orthogonal axis without disturbing the frozen 5 + 10, and the
  `clarification_requested` / `open_thread_preserved` confusion is resolved by construction (different
  layers, explicit entry conditions).
- No production blast radius: the agent, gateway, orchestrator, and prod telemetry are untouched.

### Negative / tradeoffs

- The eval now depends on a model-in-the-loop (the detector). Mitigations: it is cheap and
  non-thinking, pinned to temp 0, non-gating **for pass/fail** (it steers control flow only, D4), and
  validated against human ground truth before it is trusted. A misclassification degrades a single
  eval run's completeness, never production and never a pass/fail verdict.
- Cases must be authored with scripted follow-ups to exercise multi-turn arcs — added dataset
  authoring effort, bounded by the small canonical set.
- The "what % of prod turns stall for clarification" metric is **not** delivered here (D6); it is a
  deliberate deferral, not an oversight.

---

## Verification

This ADR is satisfied when its implementation (sequenced tickets, Observability Foundation) achieves:

1. Every canonical case reaches a `natural_end` under the driver **or** is explicitly recorded as
   `clarification_requested` (driver continued) or `incomplete` (guard/error) — **no** silent
   "outcome did not fire" standing in for a harness-truncated conversation.
2. The completion-status layer is documented in `RESULT_TYPE_TAXONOMY_SPEC.md` (mirroring the ADR-0084
   §D4 amendment): the three statuses, entry conditions, and the §4.5 distinction.
3. The eval report separates **completion status** from **pedagogical-outcome quality**; the rubric is
   rendered only for `natural_end` conversations.
4. The completion-status detector runs non-thinking at temp 0, emits the closed label set as
   hypotheses that never gate case pass/fail or the run's exit code (it steers driver control flow
   only), and its agreement with the human baseline labels is measured and recorded
   (measure-don't-assert).
5. FRE-453 is re-runnable end-to-end under the driver; its rubric pass becomes meaningful.

---

## References

- **ADR-0084** — `docs/architecture_decisions/ADR-0084-pedagogical-architecture-socratic-tutor-layer.md`
  (§D4 amended by this ADR)
- **Result Type Taxonomy spec** — `docs/specs/RESULT_TYPE_TAXONOMY_SPEC.md` (FRE-451)
- **Pedagogical North Star** — `docs/specs/PEDAGOGICAL_NORTH_STAR.md`
- **Eval harness** — `scripts/eval/fre453_canonical_evalset/{harness.py,dataset.yaml}`
- **Route-trace types/classifier** — `src/personal_agent/observability/route_trace/{types.py,classifier.py}`
- **Tickets** — FRE-541 (this ADR), FRE-453 (unblocked), FRE-451 (taxonomy spec), FRE-523 (eval-mode
  validity sibling)
