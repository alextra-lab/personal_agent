# FRE-451 — Result Type Taxonomy: Formal Spec

**Ticket:** FRE-451 (Approved, Tier-1:Opus, project: Observability Foundation)
**Blocked by:** ADR-0084 (referenced as "ADR-0083" per ADR-0084's own numbering note) — **Accepted 2026-06-03** ✅
**Deliverable:** A spec document in `docs/specs/`.

## Context

ADR-0084 §D4 defines the canonical two-layer result type taxonomy (5 orchestration
events + 10 pedagogical outcomes). `docs/specs/PEDAGOGICAL_NORTH_STAR.md` §3 mirrors it
verbatim and states: *"Do not extend this list without a corresponding ADR-0084 revision."*

FRE-451 does **not** redefine the taxonomy. It **formalizes** it into a measurement
instrument spec — the canonical instrument for measuring whether a routing or model change
improved or degraded the pedagogical function. The four deliverables from the ticket:

1. Formal definitions for each event/outcome type (entry/exit conditions, not glosses)
2. Decision rules: how to assign outcomes to a turn (multi-label)
3. Which outcomes require programmatic detection vs human rubric review
4. How the taxonomy drives the canonical eval set (M2) and the eval harness (M5)

## Scope decision

**Definitional + detection sketch.** Formal definitions, decision rules, and a
programmatic-vs-human classification — with a *sketch* of where each programmatic signal
would originate (named modules / existing emit sites), but **no instrument design**
(no concrete log-call wiring, no ES field schema, no route-trace ledger). The concrete
instrument is M2's job (the next ticket). This keeps FRE-451 inside its stated deliverable
and avoids pre-empting M2.

**Detection-sketch caveat (per ADR-0084 §Open decisions §2):** the gateway label can *lie*
about the actual cognitive work performed. The detection sketch must therefore state that
orchestration events are reliably programmatic (they are harness execution facts), but
pedagogical outcomes are predominantly human-rubric / hybrid, and the gateway `TaskType`
label is **not** ground truth for a pedagogical outcome. No programmatic-detection claim in
this spec asserts a measured mechanism — those are M2's to confirm.

## Consistency constraints (must hold)

- The 5 events + 10 outcomes match ADR-0084 §D4 / North Star §3 **exactly** — same names,
  same meanings. No additions, no renames.
- Where this spec and the ADR could conflict, the ADR governs (state this explicitly).
- Milestone references (M1/M2/M5) match North Star §9 milestone table.
- Identity/joinability discipline (ADR-0074) is referenced as the emit-site contract the
  M2 instrument inherits — the spec notes it, does not re-specify it.

## Atomic steps

1. **Create the spec file** `docs/specs/RESULT_TYPE_TAXONOMY_SPEC.md` with sections:
   - Header block (Status, Origin, ADR, North Star, audience) matching North Star's style.
   - §1 Purpose & the measurement question (why two layers; what "improved/degraded" means).
   - §2 The two-layer separation (why orchestration ≠ pedagogy; the conflation failure mode).
   - §3 Orchestration events — formal definitions: for each of the 5, **quote the canonical
     meaning from ADR-0084 §D4 verbatim**, then add *trigger condition* and *detection source*
     (programmatic; named module). Do not replace the canonical meaning with the new fields —
     formalize alongside it.
   - §4 Pedagogical outcomes — formal definitions: for each of the 10, **quote the canonical
     meaning from ADR-0084 §D4 verbatim**, then add *entry condition*, *evidence required*,
     and *detection mode* (programmatic / human-rubric / hybrid).
   - §5 Assignment rules — **explicitly framed as "assignment convention, NOT taxonomy
     definition"** (the taxonomy membership is fixed by ADR-0084 §D4 and unchanged here):
     - The two layers are assigned independently: a turn carries an orchestration-event
       label *and* a pedagogical-outcome label set. This matches the M2 gate wording
       ("label any turn with an orchestration event **and** a pedagogical outcome").
     - Pedagogical outcomes are **multi-label** (a turn may carry several). Cardinality —
       in particular whether a turn may carry *no* pedagogical outcome — is a convention this
       spec *proposes for M2 to validate against the canonical eval set*, NOT a relaxation of
       canon. State it as an open assignment question, do not assert "zero is allowed."
     - Orchestration-event exclusivity (whether the 5 are mutually exclusive) is likewise a
       *proposed convention flagged for M2 validation*, since ADR-0084 §D4 lists the events
       without stating exclusivity. Do not assert it as canon.
     - Disambiguation guidance for near-miss outcomes (e.g. `concept_extracted` vs
       `principle_identified`; `open_thread_preserved` vs `synthesis_performed`) — framed as
       reviewer guidance for the human rubric, not as machine rules.
   - §6 Detection classification table — each type → {programmatic | human-rubric | hybrid}
     with a one-line justification and the signal-source sketch. Reinforce the §2 first-class
     separation: orchestration events ("what the harness did") are reliably programmatic;
     pedagogical outcomes ("what the learner got") are predominantly human-rubric/hybrid.
   - §7 How the taxonomy drives M2 (canonical eval set gate) and M5 (eval harness). Must be
     source-grounded against ADR-0084 §Open-decisions / §Verification and North Star §8:
     - The **labeling gate**: the M2 instrument must label every eval-set turn with one
       orchestration event + its pedagogical-outcome set.
     - The **~7-turn eval-set coverage categories** the taxonomy must span (ADR-0084 §Open
       decisions §3): trivial conversational, memory recall, opening ritual, closing ritual,
       cross-thread synthesis, emotionally loaded learning, tool-heavy research.
     - The **deterministic-shell boundary** question (ADR-0084 §Open decisions §2 / North
       Star §8): does the gateway label match the actual cognitive work? The taxonomy is what
       exposes a lying label (a `MEMORY_RECALL SINGLE` that actually performed
       `synthesis_performed` / `misalignment_detected`).
     - The **thinking-token measurement gate** (ADR-0084 §Verification M2 gate / North Star
       §8): the taxonomy is the labeling basis for measuring thinking-token usage per TaskType.
     - The **M5 regression criterion**: a routing/model change is judged by pedagogical-outcome
       regression, NOT execution-success regression.
   - §8 Relationship to existing taxonomy docs (ADR-0084 governs; this spec is the formal
     measurement instrument; North Star §3 is the canonical list) + references.
2. **Add cross-references** from the two source docs so the chain is navigable:
   - `PEDAGOGICAL_NORTH_STAR.md` §3 — add a pointer line to the new formal spec.
   - (ADR-0084 lives in the adr worktree's domain; do NOT edit it from build. Note in the
     spec that ADR-0084 §D4 is the governing source. If a back-reference in the ADR is
     desired, file it as a follow-up for the adr session.)
3. **No code, no tests.** Quality gates that apply: `pre-commit run --all-files`
   (check-no-personal-paths). `make test` / `make mypy` / `ruff` are no-ops for a doc-only
   change but will be run to confirm nothing regressed.

## Verification

- `pre-commit run --all-files` passes (no personal absolute paths in the doc).
- Manual review: 5 events + 10 outcomes present and name-identical to ADR-0084 §D4.
- Manual review: every type has a detection-mode classification (deliverable 3).
- Manual review: M2 + M5 driving sections present (deliverable 4).

## Out of scope (explicitly deferred to later tickets)

- The route-trace ledger instrument design and emit-site wiring → **M2**.
- The behavioral eval harness implementation → **M5**.
- The canonical ~7-turn eval set contents → **M2 next ticket**.
- Any change to the taxonomy membership → requires an ADR-0084 revision (adr session).
