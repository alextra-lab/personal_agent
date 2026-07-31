# ADR-0130: Two Tiers of Acceptance Criteria — A Sub-Ticket Proves Its Own Work, One Seam Ticket Proves the ADR

**Status:** Proposed
**Date:** 2026-07-31
**Deciders:** project owner (FRE-1078)
**Tags:** process, delivery, acceptance-criteria, lifecycle, skills

---

## Context

**What is the issue we're addressing?**

Acceptance criteria are being written in a form that cannot be evaluated at the gate. The ticket
therefore cannot close, so it sits; the deploy queue fills with work that is already deployed; the
queue stops meaning anything; and the response has been to file more tickets about the queue rather
than to fix where the criteria are written.

### What was measured

Nineteen tickets were closed on 2026-07-31 in a single sweep. Not one was held open because anything
was wrong with it. Every one had merged, deployed, and been running — several for weeks.

- The outcome-ingestion ticket had been deployed and running for **twenty-five days** while sitting in
  a queue labelled `Awaiting Deploy`, against a note reading *never checked*.
- The log-identity propagation ticket sat **eighteen days** on a criterion requiring documents from an
  emit path that has produced nothing since 2026-05-10. The criterion was not unmet — it was
  **unmeetable**. No work on that ticket could ever have satisfied it.
- Three tickets were held for seven-day observation windows. Two were held for a turn the owner might
  happen to run. One was held for a census whose own ticket text conceded the before and after
  populations were not comparable.
- The queue reached twenty-one entries while simultaneously holding three unrelated populations:
  merged-not-deployed, deployed-not-verified, and — through a separate integration defect — work that
  had never been implemented at all.

### Where the rule that licensed it is written

The failure is not nineteen independent drafting errors. It is the shared contract working exactly as
written, in four places:

| document | the sentence |
|---|---|
| `lifecycle-rules.md` § Guardian role | *"'Done' means proven against the backing ADR's criteria, not merged-and-runs."* |
| `lifecycle-rules.md` § Evidence contract | *"if the ticket cites a backing ADR with acceptance criteria, those are separately proven"* |
| `master` SKILL Step 4 | *"the specific acceptance criteria this ticket implements"*, proven *"at the altitude of the criterion"* |
| `build` SKILL Step 2 | *"Pull out the acceptance criteria this ticket carries from the backing ADR (adr SKILL Step 5)"* |
| `adr` SKILL Step 5 | *"Each ticket carries the slice of the ADR's acceptance criteria it must satisfy + how each is proven"* |

Each is defensible read alone. Together they push ADR-grade criteria — assembled, population-level,
long-horizon — down onto individual build tickets, and nothing anywhere says a build ticket's criterion
must be decidable when that ticket is finished. `adr` Step 5 is the origin: it instructs the authoring
session to slice the ADR's criteria across its children. `build` Step 2 receives the slice. `master`
Step 4 and the lifecycle rules then hold the ticket against it.

### Why this is an ADR and not a wording patch

Four patches were filed in one night against the *symptoms* — the queue, the watcher, the integration —
before the diagnosis surfaced. The five sentences above have to change **coherently or not at all**: fix
`build` alone and the ADR session keeps slicing; fix `adr` alone and master keeps demanding the slice.
That is a contract change, which is an architecture decision.

### The live case

ADR-0129's implementation chain is filed and awaiting approval — eight tickets. Its head, FRE-1064,
states its proof as *"over a 7-complete-day window after this lands, the share of `agent-logs-*` records
carrying a non-empty `trace_id` is measured against the recorded pre-change baseline of 11.36%"*, and
says outright that it *"owns the first half of ADR-0129 AC-3(a)"*. That is the conflation, already
written down, one approval away from being built eight times over. Master is holding the chain on this
ADR. ADR-0129's own nine criteria are correctly ADR-grade and are **not** rewritten here.

### The constraint that bounds the solution space

There is exactly one substrate on which this system executes, and it is production. The test stack
(`build-postgres-test` :5433, `build-neo4j-test` :7688, `build-elasticsearch-test` :9201) is running but
holds no production-shaped data and has nothing behind it that serves a turn. So *"verify against a real
substrate before deploy"* is not an available instruction, and any decision that depends on it is a
decision to build infrastructure first. This ADR does not take that path — see Alternatives, Option 3.

---

## Decision

**What did we decide?**

### D1 — Two tiers of acceptance criteria, and only one of them belongs to a ticket that builds

- **A sub-ticket's acceptance criteria cover only its own work** — the change that ticket makes, stated
  as an observable result of that change, decidable from that ticket's own deliverable.
- **An ADR's acceptance criteria cover the ADR's objective.** They stay with the ADR. They may be
  assembled, population-level, long-horizon, or require the owner to do something. They are never
  sliced across children.

Criterion inheritance is severed. A build ticket does not carry, quote, restate or discharge any part of
a backing ADR's criteria — in that ADR's wording or a paraphrase of it.

**Severing inheritance must not lose coverage.** The union of a chain's sub-ticket criteria has to cover
every design obligation the chain implements, or the ADR's design can be silently abandoned one child at
a time while every child passes. The session filing the chain owns that check (`adr` Step 5). The
distinction is *whose objective the criterion states*, not *which subjects may be mentioned*: ADR-0129's
`TraceContext` bridge must preserve `authenticated` and `user_id`, so the bridge ticket carries a
criterion about exactly that — an authenticated recall request returns `group`-visibility memory and an
unauthenticated one does not — because preserving those fields **is** that ticket's own work. What it may
not do is cite ADR-0129 AC-9 as the thing it discharges.

**Anything not decidable from a single child's deliverable is the seam ticket's, by definition.** That
includes cross-child integration obligations — a property observable only once two children interact
belongs to neither of them, and forcing it onto one would recreate the inheritance this decision
severs. The coverage check above is therefore a partition, not a filter: every obligation lands on
exactly one child or on the seam, and none is allowed to land nowhere.

### D2 — Every ADR with implementation tickets names exactly one seam ticket, and that is the only place its criteria are asserted

The seam ticket tests the ADR's overall objective, and it holds **all** of the ADR's criteria — not the
subset that happens to need the full chain. Exactly one per ADR, regardless of how many implementation
tickets there are; a single-ticket ADR still has a seam ticket, because D1 forbids that one ticket from
discharging the ADR's criteria.

- **Filed** with the chain, parked (Backlog, or Approved without a `stream:` label — undispatchable), and
  carrying a **Linear due date** set to the earliest date all its criteria become adjudicable. The due
  date is the wake-up; without one, "activate it later" has no event behind it.
- **Activated by master at the first advance-dispatch on or after the due date.** Master already
  re-derives each stream's eligible set at every merge (`master` Step 8); the seam sweep is one more
  line in that pass, and activation means `Approved` + `stream:adr`. **The due date is a marker, not an
  actuator** — the dispatch resolver reads state, labels, priority and blockers, and does not read due
  dates, so activation is bounded by the next merge rather than being instant. That latency is
  acceptable and is stated rather than glossed. Activating at the last child's merge instead would park
  a long-horizon ticket in the `adr` stream, where the busy guard would hold the stream for the length
  of the window — trading a full deploy queue for a stalled stream.
- **Adjudicated by the `adr` session that picks it up, and delivered the way that session already
  delivers.** It runs each criterion's stated check and writes the verdicts — one per criterion, with
  the evidence and its output — into the ADR's own **Status Updates** section, setting the `Status:`
  line to `Implemented` only if every verdict is green. That is a docs change, so it lands as an ADR PR
  through `adr` Step 4 and reaches master through the existing gate; no new event, trigger or channel is
  invented. Master then files remediation for any non-green verdict and closes the seam ticket. The
  session writes no `src/`.
- **Scope is frozen to evaluating.** The seam ticket produces verdicts; it never implements fixes. A red
  or inconclusive verdict spawns a separately-scoped remediation ticket, filed by master. This is what
  stops the seam absorbing work and accreting criteria until it cannot close.
- **Closes on adjudication, not on success** — its job is to produce a recorded verdict on every
  criterion, and that job is done whether the verdicts are green or red.
- **The verdict maps to the ADR's `Status`, not to the ticket's:** all green → `Implemented`; any red or
  inconclusive → the ADR stays `Accepted`, each non-green criterion gets a filed remediation ticket whose
  id is recorded on the seam ticket, and the seam ticket closes. **An ADR never reaches `Implemented` on
  a red or unadjudicated criterion.** The `adr` session proposes the Status line in its PR; master gates
  it, files the remediation tickets and closes the seam — master already owns ADR-status drift
  (`master` Step 3) and ticket closure.

**The seam ticket is allowed to be long-lived. That is the design, not a defect.** Nineteen long-lived
build tickets is the pathology; one long-lived seam ticket per ADR is correct and expected. An ADR whose
objective is not yet proven holds that open question in its own `Status` field, where it costs a review
line rather than a queue slot — not in the deploy queue, where it costs the queue its meaning.

**If an ADR's objective cannot be adjudicated at all**, say so when authoring it rather than filing a
seam ticket that can never return a verdict. An un-checkable decision is a design smell to surface — the
`adr` skill already says this, and it is the honest outcome for a decision whose effect is not observable.

### D3 — Design adherence is retained at every gate; criterion inheritance is not

These two were fused in `master` Step 4 and are separable. Master continues to gate that the diff
implements the backing ADR **as designed** — silent divergence from the ADR's design still bounces, and
a changed design still updates the ADR first. What master stops doing is requiring a child to prove the
ADR's criteria. Provenance survives; inheritance ends.

**Deploy verification is untouched.** `Done` still requires a deployed, health-verified change
(lifecycle-rules § Ticket state, § Evidence contract) — that is verification of *the deploy*, and it is
decidable in minutes. This ADR changes only what *acceptance criteria* a ticket must discharge. The two
were never the same thing, and nothing here relaxes the first.

### D4 — The no-BS bar is unchanged, and now applies at the sub-ticket's own scope

No new machinery. A sub-ticket's criterion must still name an observable result, not the existence of a
component: *"the record carries the id"*, not *"the processor is registered"*. A criterion satisfiable
by a broken or half-finished implementation is still rejected. The bar simply now has an unambiguous
scope — this ticket's change — instead of an inherited one.

### D5 — The five sentences are amended together, in one change

Five sentences carry the rule; the full edit surface is larger (a step here, a handoff field there, the
ADR template's seam line) and is enumerated in Implementation Notes. What matters is that no document is
left teaching the old rule — a partial amendment is worse than none, because the contradiction is then
resolved by whichever document a session happens to read first.

| where | becomes |
|---|---|
| `adr` SKILL Step 5 | the ADR's criteria stay with the ADR; each implementation ticket gets criteria written for **its own work**; the ADR names its seam ticket |
| `build` SKILL Step 2 | read the backing ADR for **design intent**, not for criteria to inherit; your criteria are your ticket's |
| `lifecycle-rules` § Guardian role | Done means proven against **this ticket's own criteria** |
| `lifecycle-rules` § Evidence contract | a backing ADR's criteria are proven **once, by its seam ticket** — not per child |
| `master` SKILL Step 4 | provenance still names the backing ADR (D3); **proof is against the ticket's own criteria**; the ADR's criteria are asserted only at the seam ticket |

`master` Step 4's existing **Seam ownership** bullet is retained and promoted: it stops being a flag to
raise and becomes the mechanism D2 names.

### D6 — A mis-scoped criterion is caught at dispatch, not at the gate

One line in `master` Step 8 (advance dispatch): before applying a `stream:` label **to an implementation
ticket**, confirm each of its criteria is **decidable from that ticket's own deliverable when the ticket
is finished** — D1's test, not the weaker "is it about its own work". The weaker test is what would let
FRE-1064 through unchanged: a seven-day identity-share census *is* about FRE-1064's own bootstrap work,
and is still undecidable at close-out. Decidability is the property that was missing, so decidability is
what is checked.

**A seam ticket is explicitly exempt** — it exists to carry criteria this test would reject, so applying
it there would make every seam ticket permanently unlabellable. Its dispatch check is D2's instead: all
of the ADR's criteria present, each with a stated evidence procedure, and the due date reached.

This is not a new mechanism — master already reads the ticket at that moment, and it is the last point
*before* the build. A criterion that first bites at the gate has already cost the build, and a criterion
that is **unmeetable** (the dead-emit-path case) cannot be fixed by bouncing at all.

### D7 — ADR-0127's Analyzer is the eventual automated re-check; nothing here depends on it

ADR-0127 (Proposed) designs an Analyzer that re-evaluates an accepted ADR's criteria against the running
system and reports green / red / inconclusive with the record that decided it. That is the natural
long-run home for seam-ticket verdicts. It is unbuilt, so this ADR names it as the destination and
depends on nothing from it. Until it exists, a seam ticket's verdict is produced by a person or a
committed script.

### D8 — ADR-0129's chain is re-scoped under this rule; its ADR criteria are untouched

ADR-0129's nine criteria stay exactly as written. Its eight children have their criteria rewritten to
their own work, and **FRE-1073 is designated its seam ticket, owning all nine** — not the six its
current seam declaration assembles. AC-4, AC-7 and AC-9 are decidable earlier than the rest; that
changes *when* the seam ticket can adjudicate them, not *who* owns them.

FRE-1064's identity-share measurement (11.36% baseline, 7-day window) moves to FRE-1073 — it is not
dropped, it changes owner. This is the ticket action that releases master's hold on the chain.

---

## Alternatives Considered

### Option 1: Keep the criteria; add post-deploy machinery to work the queue

**Description:** Retain criteria as written and add a `Verify Pending` state, a reconciler, or a nag that
drives held tickets to a verdict. This is the shape of the four patches filed on the night of 2026-07-30.

**Pros:**
- No contract change; the existing criteria are preserved verbatim.
- Addresses the visible symptom (the queue) directly and quickly.

**Cons:**
- The unclosable ticket survives; the graveyard is renamed, not removed.
- It cannot help the unmeetable criterion — no amount of driving satisfies a criterion whose emit path
  has produced nothing since 2026-05-10.
- It adds a fifth observer over the same inference.

**Why Rejected:** The convergence study (`docs/research/2026-07-29-why-pipeline-point-fixes-do-not-converge.md`)
measured six shipped fixes against this exact split: *fixes that removed the operation that could fail
converged; fixes that improved or observed an inference did not* — two held with zero relapse, four did
not. This option is squarely the second kind. D1 is the first kind: it removes the possibility of
writing an unclosable build-ticket criterion rather than observing the consequences better.

### Option 2: Make every criterion small enough to be a unit test

**Description:** Resolve the evaluability problem by shrinking criteria until each is satisfiable by an
assertion in the existing suite.

**Pros:**
- Every ticket closes at its gate, immediately.
- Requires no new concept and no seam ticket.

**Cons:**
- It is the failure mode this project has already suffered, at scale.
- It deletes the ADR's objective rather than relocating it: nothing would then test whether the decision
  delivered.

**Why Rejected:** Four telemetry ADRs shipped under criteria of this shape and changed nothing. The
canonical specimen is FRE-823, which shipped `session_is_idle` with **47 green unit tests over synthetic
fixtures containing marker text that does not occur in any real pane**; the watcher then skipped 100% of
sends. The tests were not too small — they were run against a fiction. Shrinking the criterion is the
wrong axis; D1 changes *whose objective* a criterion states, and D4 keeps the anti-triviality bar intact
at the smaller scope.

### Option 3: Build a production-shaped verification substrate so every criterion is literally pre-deploy

**Description:** Populate the test stack to production shape and put a servable turn behind it, so that
the strongest form of the rule — *verify against a real substrate before deploy* — becomes available.

**Pros:**
- Would make pre-deploy verification genuinely strong rather than mock-strong.
- Removes the residual reliance on production as the only executing substrate.

**Cons:**
- It is a project, not a line: production-shaped data, ES templates, Neo4j fixtures, and something that
  serves a turn.
- It blocks a contract fix behind infrastructure, while the queue keeps filling.

**Why Rejected:** Correct in itself, but it answers a different question. The nineteen tickets were not
stuck because their criteria ran against the wrong substrate — they were stuck because those criteria
were never the ticket's to discharge. Fixing where criteria are written is independent of, and cheaper
than, building where they run. Recorded here as the upgrade path; deliberately not filed as a child of
this ADR.

### Option 4: Have ADR-0127's Analyzer re-check all ADR criteria continuously

**Description:** Rely on the Analyzer (ADR-0127 decision three, FRE-1030) to re-evaluate accepted ADRs'
criteria against the running system, so held tickets can close and the Analyzer catches regressions.

**Pros:**
- Would eventually re-prove criteria on days other than the merge day, which nothing does today.
- Reuses a design already argued and filed.

**Cons:**
- Proposed and unbuilt; its own chain is Needs Approval and it is blocked by the evidence-package ticket.
- It re-checks criteria without changing where they are authored, so build tickets would still inherit
  ADR-grade criteria and still fail to close at their gate.

**Why Rejected:** As the primary answer, it treats the reading of criteria while leaving the writing of
them untouched. Adopted instead as D7 — the destination for seam-ticket verdicts once it exists.

---

## Consequences

### Positive Consequences

- A build ticket becomes closable at its own gate by construction; the merged-to-Done path stops
  depending on anything outside the ticket.
- `Awaiting Deploy` regains a single meaning — merged, not yet deployed — so the queue's length becomes
  a signal again.
- The ADR's objective is still tested, once, by a named owner, instead of being diffused across eight
  children where no child could prove it and none was accountable.
- An unproven objective becomes visible in the right place: an ADR sitting at `Accepted` rather than
  `Implemented` states the open question precisely, at zero carrying cost.
- Authoring gets simpler. `adr` Step 5 stops performing a slice that was never sound.

### Negative Consequences

- The seam ticket is long-lived by design, and its verdict may take weeks. The board will always carry
  a small number of open seam tickets; that is accepted, and it is the cost of not carrying nineteen.
- Cross-cutting outcomes are no longer proven at any single merge. Between the last child merging and
  the seam ticket running, the ADR's objective is unproven and known to be unproven.
- An ADR whose seam ticket is never run stays `Accepted` indefinitely. This is deliberate — a document
  holds an open question at zero cost — but it means `Accepted` no longer implies "and it worked".
- Two tiers is one more concept than one tier, and every authoring session must place a criterion in the
  right tier.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Seam tickets accumulate unrun, becoming the same graveyard at a slower rate | Medium | One per ADR bounds the population by ADR count; D2 names the activation trigger (master, at the first advance-dispatch on or after the due date) so it is never left waiting for someone to notice; D7 names the automated re-check as the destination |
| The seam ticket absorbs the remediation work its own red verdicts create, and stops being closable | High | D2 freezes its scope to *evaluating*; a red or inconclusive verdict spawns a separately-scoped ticket and the seam still closes |
| Sub-ticket criteria drift into wiring checks now that the ADR's criteria no longer backstop them | High | D4 keeps the no-BS bar in force at the ticket's own scope; D6 checks decidability at dispatch, before the build |
| A design obligation falls between children — every child passes and the ADR's design is quietly abandoned | High | D1's coverage clause makes it a partition: every obligation lands on one child or on the seam. AC-2 requires a written obligation → owner mapping and fails on an unowned row |
| An activated seam ticket occupies the `adr` stream for the length of its window, trading a full deploy queue for a stalled stream | Medium | D2 activates at the first advance-dispatch on or after the due date, not at the last child's merge; through the parked interval the ticket carries no stream label and the busy guard never sees it |
| The due date passes and nothing actuates, so the seam sits parked | Medium | Activation is a line in `master` Step 8's advance-dispatch, which runs at every merge — so the sweep happens on ordinary delivery traffic rather than needing a scheduler the project does not have. Bounded by the next merge, which is stated in D2 rather than assumed away |
| The two tiers are conflated again by a future author | Medium | D5 changes all five sentences together, so no document teaches the old rule; `adr` Step 5's slice instruction — the origin — is removed rather than softened |
| ADR-0129's chain is re-scoped incorrectly, losing the identity-share question entirely | Medium | D8 states the measurement **moves to FRE-1073**, and AC-1 fails if it is dropped rather than relocated |
| `Accepted`-not-`Implemented` is read as "shipped and working" by a later session | Low | The distinction already exists in the ADR `Status` field and the index; D2 gives it a defined meaning rather than inventing one |

---

## Implementation Notes

**Files affected:**

- `.claude/skills/lifecycle-rules.md` — § Guardian role ("delivery guardian / proof enforcer") and
  § Evidence contract.
- `.claude/skills/master/SKILL.md` — Step 4 acceptance-criteria gate (provenance bullet, proof bullet,
  seam-ownership bullet); Step 8 advance-dispatch (D6's one line).
- `.claude/skills/build/SKILL.md` — Step 2 (scope), Step 4 (TDD, "each acceptance criterion from Step 2"),
  Step 9 (handoff contract's acceptance-criteria-proof field).
- `.claude/skills/adr/SKILL.md` — Step 5 (implementation tickets), Step 6 (handoff comment's seam
  ownership field), and the Verification / Acceptance-Criteria authoring guidance in Step 2.
- `docs/architecture_decisions/ADR_TEMPLATE.md` — the "Seam owner (for a decomposed ADR)" line becomes
  the seam **ticket**, per D2.

**Not affected:** `docs/architecture_decisions/ADR-0129-*.md` — its nine criteria are unchanged (D8).
The ADR-0129 *tickets* change; the ADR does not.

**Dependencies:** none. This ADR is documentation and skill text only, and blocks nothing behind
infrastructure (Alternatives, Option 3).

**Sequence:** amend the five sentences together in one PR → re-scope the ADR-0129 chain's eight tickets
under the rule and designate FRE-1073 the seam ticket (releases master's hold) → this ADR's own seam
ticket, parked until both have landed.

**Testing strategy:** this decision's artifacts are documents and tickets, so its criteria are read from
the documents and from the board. **All five criteria belong to this ADR's seam ticket** (D2) — the
implementing tickets below carry their own criteria about their own edits and discharge none of these.
AC-1, AC-2 and AC-3 become decidable once the re-scope lands and ADR-0129's children have closed; AC-4
needs a thirty-day window and AC-5 a ninety-day one, so this ADR's seam ticket carries a due date at
ninety days and adjudicates all five in one pass. Early decidability changes *when* a criterion could be
adjudicated, not *who* owns it — the
first draft of this ADR assigned AC-1 to AC-3 to implementing tickets, which was the very slice D1
forbids.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

**All five belong to this ADR's seam ticket** (D2). None is discharged by an implementing ticket.

- **AC-1 — The live conflation is relocated with its decision procedure, and FRE-1064 actually closes
  without it.** · **Check:** (a) FRE-1073 states the identity-share measurement as one of its own
  criteria, naming the query that decides it (the `agent-logs-*` share carrying a resolvable `trace_id`),
  the 11.36% baseline and the window; (b) FRE-1064 reaches `Done` on a close-out comment whose evidence
  is entirely from its own deliverable — no production window, census or owner action. · *Fails if* the
  measurement appears on FRE-1073 only as prose with no deciding query, if it appears on neither ticket,
  or if FRE-1064's close-out cites time-windowed evidence. (b) is what makes this more than a wording
  check: the ticket has to *close* on the new basis, not merely be rewritten to claim it could.

- **AC-3 — Every rewritten sub-ticket criterion names an expected value, and is discharged with evidence
  at its gate.** · **Check:** for each of ADR-0129's eight children, (a) the criterion states what the
  observed thing must *equal or relate to* — this record's `trace_id` equals the enclosing span's; this
  tool span's parent is the step span; this config artifact names the Collector endpoint; (b) the
  ticket's handoff comment records the observed value, not the assertion that it was checked, **and that
  observed value satisfies the stated relation**. · *Fails if* any child's criterion is satisfied by a
  field being present, a component being registered, or a value being any value — **or** if any child
  closed without a recorded observed value, **or** if any recorded observed value does not satisfy its
  own stated relation (*expected parent A, observed parent B* is a failure, not a record). (b) is why eight
  well-worded criteria over eight broken implementations cannot pass: the gate's existing handoff contract
  already carries per-criterion evidence, so this costs nothing new to check.

- **AC-2 — Sub-ticket criteria partition the chain's design obligations, and none is an ADR criterion in
  disguise.** · **Check:** the session re-scoping the chain publishes an explicit
  **obligation → owner mapping** on the ADR-0129 umbrella, listing every obligation ADR-0129's Decision
  section places on the chain against the child or the seam that owns it. Then: (a) every row has an
  owner, including the five `TraceContext` fields D1 commits to preserving; (b) no child-owned row is
  decidable only from population-level or future-traffic evidence, whether or not it cites an `AC-n`;
  (c) exactly one ticket, FRE-1073, is named seam and owns all nine of ADR-0129's criteria plus every
  row not decidable from a single child. · *Fails if* the mapping is absent, any Decision-section
  obligation is missing from it, any row has no owner, any child-owned row needs a window or a census to
  decide, or the seam owns fewer than nine criteria. The mapping is the point: without a written
  inventory, "everything is covered" is unfalsifiable, and coverage is the guarantee severing
  inheritance puts at risk.

- **AC-4 — `Awaiting Deploy` holds one population.** Over the thirty days after the rule is in force,
  every ticket resident in `Awaiting Deploy` is there because it is merged and not yet deployed, and for
  no other reason. · **Check:** for every ticket resident at any point in the window, confirm from its
  merge SHA and the deploy history that it is merged-not-yet-deployed, and read its stated criteria; a
  ticket still resident at day thirty is judged on the same test, not excused by not having left yet. ·
  *Fails if* any resident ticket is already deployed, was never implemented, or is held by criteria
  requiring future traffic, an observation window or an owner action — **or** if fewer than fifteen
  tickets entered the queue during the window, which reports **inconclusive** rather than green. The
  fifteen is the prior thirty days' floor; a queue that is quiet because nothing shipped proves nothing,
  and is the most available way for this criterion to pass while the process is broken. Checking
  residency *reasons* rather than criteria text is what catches the already-deployed and
  never-implemented populations the queue held on 2026-07-31.

- **AC-5 — An ADR's objective is still tested after decomposition, and a failed one cannot be declared
  Implemented.** For **every** seam ticket activated in the first ninety days under this rule — and the
  set must include at least one seam other than this ADR's own, **FRE-1073 being the named one** — it
  records a verdict, green, red or inconclusive, on **every** one of its ADR's criteria, each with the
  query or record that decided it **and that evidence's actual output**. · **Check:** each seam ticket's
  verdicts in its ADR's Status Updates against that ADR's criteria list, **reperforming one cited
  evidence procedure per seam** — re-run the query, or re-read the cited immutable record — to confirm
  the recorded output reproduces; plus each ADR's `Status` field. · *Fails if* the set contains no seam
  but this ADR's own (a criterion that can only inspect itself proves nothing), any criterion is
  unadjudicated, any verdict cites evidence without its output, a reperformance does not reproduce the
  recorded output, an ADR reached `Implemented` while any verdict is red or inconclusive, or any
  non-green verdict has no filed remediation ticket. Ninety days rather than the first chain: automation
  that works once and fails
  silently afterwards is the exact shape of the four observers the convergence study found did not hold.
  Closing on adjudication is deliberate — the seam's commissioned work is *knowing* — but a red verdict
  must therefore be visible somewhere, and this criterion forces it into the ADR's `Status` and a
  remediation ticket rather than into silence.

**Seam ticket:** filed as a child of FRE-1078, parked, due-dated ninety days after the skill amendment
and the ADR-0129 re-scope have both landed, and activated by master per D2 at the first advance-dispatch
on or after that date. It adjudicates all five criteria in one pass and records them in this ADR's
Status Updates.

---

## References

- FRE-1078 — the originating ticket: the diagnosis, the nineteen-ticket evidence, and the two-tier distinction, all owner-stated
- ADR-0127 — The Harness Self-Analysis Pillar (Proposed): decision three and FRE-1030 design the re-check of ADR criteria against the running system; adopted as D7's destination
- ADR-0129 — OpenTelemetry Instrumentation, with Trace Visibility as the Acceptance Bar (Proposed): the live case; its criteria are untouched, its eight-ticket chain is re-scoped by D8
- FRE-1064 — ADR-0129 B1: the specimen sub-ticket carrying an ADR-grade criterion
- FRE-1073 — ADR-0129 B8: designated ADR-0129's seam ticket by D8
- FRE-1030 — ADR-0127 T5: the normative-spine ticket; its false-green/false-red asymmetry is the model for a seam ticket's verdict
- `docs/research/2026-07-29-why-pipeline-point-fixes-do-not-converge.md` — the convergence law used to reject Option 1, and the FRE-823 specimen used to reject Option 2
- `.claude/skills/lifecycle-rules.md` · `.claude/skills/master/SKILL.md` · `.claude/skills/build/SKILL.md` · `.claude/skills/adr/SKILL.md` — the four documents amended by D5
- `docs/architecture_decisions/ADR_TEMPLATE.md` — the authoring template, amended by D2's seam-ticket definition

---

## Status Updates

### 2026-07-31 — Proposed

**Changed By:** `/adr` session (FRE-1078)
**Reason:** Authored following owner-directed design discussion. The two-tier rule is the owner's; the
session's contribution is its consequences for the five contract sentences, the seam ticket's definition,
and the disposition of the held ADR-0129 chain.
