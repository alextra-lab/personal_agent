# ADR-0137: A Two-Party Obligation Is Split at Authoring, and a Cutover Is Two Tickets — the Dispatch Question Is Only the Backstop

**Status:** Proposed
**Date:** 2026-08-13
**Deciders:** project owner (FRE-1221)
**Tags:** process, delivery, acceptance-criteria, lifecycle, skills, integration

---

## Context

**What is the issue we're addressing?**

FRE-1071 merged against a precondition nothing provisioned. It removed `slm_server`'s Elasticsearch
writer and added OTLP export in the same change, so restarting `slm_server` would have taken its
telemetry from working to dark. The chain that produced it had a published obligation → owner mapping,
a partition check, an adversarial codex round, and a dispatch gate. All four passed.

FRE-1221 diagnosed this as ADR-0130 D1 **evicting** integration properties: a property between two
deliverables cannot be decidable from either one alone, so a criterion asserting it would be rejected at
the dispatch gate, and the property lands nowhere. That diagnosis is wrong on the record, and the
correction is the reason this ADR exists.

### What the record actually shows

**The obligation was inventoried, and it had an owner.** ADR-0129's mapping, published on FRE-1043 by
FRE-1080, carries one row per Decision-section obligation. Row D5.d reads:

| # | Obligation (ADR-0129 Decision) | Owner | Proved by |
|---|---|---|---|
| D5.d | `slm_server` gets a network endpoint to ship to, replacing client-side index URL formatting | FRE-1071 | its AC-1, AC-3 |

Nothing was evicted. The obligation was named, owned, and cited a proving criterion. It still produced
the defect, and it did so through three distinct failures that a presence check cannot see.

**Failure 1 — the row was assigned by grammatical subject, not by who provisions the thing.** The
sentence *"`slm_server` gets a network endpoint to ship to"* has two parties: `slm_server`, which
consumes the endpoint, and the Collector, which provides it. The row was assigned to the consumer.
`slm_server` runs on the owner's Mac and its ticket lives in a separate repository; nothing it can
deliver brings a VPS ingress into existence. The provider, FRE-1070, deployed the Collector into the
cloud compose stack with no host port publication at all, and its AC-4 constrains exporters — *no
Collector exporter may address anything off-box* — which is the opposite direction and adjudicates
nothing about receiving.

**Failure 2 — the named criterion does not entail the obligation.** D5.d cites FRE-1071's AC-1 and AC-3.
AC-1 is traceparent continuation, so the exported span's trace id equals the caller's. AC-3 is
Elasticsearch-writer removal with existing documents' `ts` untouched. Neither asserts that an endpoint
exists, or that it is reachable from the host the producer runs on. A green verdict on both leaves the
obligation entirely unproven.

This is precisely the hole the existing integrity check does not cover. Codex's review of the mapping's
first draft caught **six rows with an owner and no criterion** (D2.b, D2.c, D3.c, D3.e, D8.a, D8.c) and
all six were fixed. Nobody tested for a row with an owner and the **wrong** criterion, because a
presence check cannot distinguish the two — both look complete. As a minor corroborating detail, the
mapping's own header states 43 rows while the published table enumerates 41; a count that does not
reconcile is the same class of defect at a different altitude, and it survived the same review.

**Failure 3 — the removal and its replacement shipped as one change.** Had FRE-1071 shipped only the
OTLP export, a missing ingress would have been a silent no-op: spans go nowhere, Elasticsearch keeps
receiving logs, nothing regresses, and the gap surfaces whenever someone first asks whether spans are
arriving. Coupling the removal to the addition is what converted an incomplete chain into a live
telemetry regression, and it is the reason the gap was blocking rather than cosmetic.

### The framing FRE-1220 itself corrected

FRE-1220 was filed as *"no ingress path exists for off-box OTLP producers"* and master corrected it on
the ticket after the commissioned study measured the live system. An authenticated ingress **did** exist
and served this exact producer: Caddy's Elasticsearch host block, path-scoped by regex to
`slm-requests` indices and bulk, reverse-proxied to Elasticsearch, reached through a Cloudflare tunnel
public hostname and Access-gated, with everything else answered 403. Measured live: a POST to an
`slm-requests` document path returning 201, the same hostname unauthenticated returning 403, and 586
documents all-time with the latest timestamp on 2026-08-08.

So the true statement is narrower and more damning than the original: no **OTLP-shaped** ingress
existed, while a proven, authenticated, path-scoped ingress for the same producer sat one configuration
block away, and FRE-1071's change deleted that path's consumer. The work was never a from-scratch
security design; it was pointing an OTLP-shaped path at the Collector following a pattern already
proven for this producer.

### Why the ticket's proposed remedy does not answer this

FRE-1221 proposes adding one question to the D6 decidability check master already runs before applying
a `stream:` label:

> Does any criterion on this ticket assume a network path, a host, a credential or a deployed component
> that no ticket in this chain builds?

Read literally against the actual chain, the answer at FRE-1071's dispatch is **no**. FRE-1070 builds
the Collector; the Collector is a deployed component; it is in the chain. The question does not ask
whether the built thing is reachable from where this ticket runs, which is the gap. It fires only if
master reads it in the sharper sense the ticket intends — which is the same reliance on master's
attention that failed the first time.

It is also an observer rather than a removal, and ADR-0130 rejected its own Option 1 on exactly that
ground, citing the convergence study: *fixes that removed the operation that could fail converged; fixes
that improved or observed an inference did not* — two held with zero relapse, four did not. A question
added to the gate that missed it is squarely the second kind.

### What the failures have in common

All three are failures of **how the work was shaped**, not of how carefully it was read. An obligation
naming two parties was allowed to have one owner. A row was allowed to name a criterion that does not
prove it. A cutover was allowed to be one ticket. Each is a property of the artifact, decidable by
looking at the artifact, and each — once fixed — stays fixed without vigilance.

That is the axis this ADR acts on.

---

## Decision

**What did we decide?**

Four decisions across three mechanisms. D1 and D2 change how the obligation → owner mapping is written
(authoring, `adr` Step 5). D3 is a bounded backstop at dispatch for chains authored before them. D4
changes how a cutover is decomposed. D5 states the edit surface.

### D1 — An obligation naming two parties may not carry a single non-seam owner, and assignment follows the provisioner

In the obligation → owner mapping, an obligation whose sentence names **two parties** — *A ships to B*,
*A reads B's output*, *A reaches B*, *A gets X from B* — is **split into a provider row and a consumer
row**, each owned by the deliverable that makes its own half true.

**Assignment follows who provisions the thing, never the grammatical subject.** D5.d's subject is
`slm_server`; its provisioner is the Collector. Splitting it yields:

| half | obligation | owner |
|---|---|---|
| provider | an OTLP endpoint exists that is reachable from off-box | FRE-1070 |
| consumer | `slm_server` addresses that endpoint instead of formatting index URLs | FRE-1071 |

The split is what makes the defect visible, because the provider half then has **no criterion available
on FRE-1070** — its AC-4 constrains exporting off-box, and no criterion of its asserts an inbound path.
An unfillable "Proved by" cell is a legible, unmissable signal at authoring time. The single-owner form
concealed it behind a citation that read as complete.

**A half that no ticket in the chain can make true has exactly two resolutions, and neither is prose:**

1. **A provisioning ticket is filed, and its `blockedBy` relation is written in the same action.** One
   action, not two — a ticket entering a chain without its relations is a dispatch bug that surfaces as
   a false head (lifecycle-rules § Dispatch), and a provisioning ticket with no relation is precisely a
   precondition with no ordering.
2. **The half is assigned to the ADR's seam ticket**, which is where anything not decidable from a
   single child's deliverable belongs by definition (ADR-0130 D1), so its adjudication is expected
   rather than discovered.

**Recording the assumption in prose with no owner is not a resolution.** That is the state FRE-1220 was
already in, and it is what a mapping exists to make impossible.

### D2 — A row's named criterion must *entail* the obligation, not merely sit on the right ticket

The mapping's integrity check gains a **sufficiency** test alongside its existing **presence** test. For
each row: read the criterion named in "Proved by" on the owning ticket, and confirm that **a passing
verdict on that criterion makes the obligation true**. If it does not, the row is not covered, whatever
its cells contain.

Presence asks *is there an owner and a criterion?* Sufficiency asks *would this criterion, passing, make
this sentence true?* D5.d passes the first and fails the second, which is why it shipped.

This test is a judgement, and it is deliberately applied to a **small, pre-filtered set**: D1's split is
mechanical from the sentence's grammar and is what surfaces the rows most likely to fail sufficiency.
The two compose — the cheap grammatical filter produces the candidates, and the expensive semantic read
is spent only on them.

### D3 — The dispatch backstop, narrowed, bounded, and carrying its own retirement condition

For chains whose mappings were published **before** D1 and D2 are in force, the mapping cannot be
relied on, so one question is added to the existing pre-`stream:`-label list in `lifecycle-rules`
§ Dispatch and `master` SKILL Step 8 — an added read at a step already mandatory, not a new step:

> **Is this ticket's own deliverable what makes each of its criteria true, or does something outside it
> have to exist first?**

If something outside it has to exist first, D1's two resolutions apply unchanged: a provisioning ticket
with its relation written in the same action, or the assumption recorded on the ADR's seam ticket.

**The question is deliberately about the ticket in front of master, not about the chain.** FRE-1221's
form asked whether *any ticket in this chain* builds the assumed thing, which returns a reassuring "yes"
whenever a component with the right name exists anywhere in the decomposition. This form asks whether
**this** deliverable is the thing that makes the criterion true, which is answerable by reading the
ticket and admits no such substitution.

**The boundary is stated, because an unbounded version reintroduces what ADR-0130 D1 removed.** The
check does **not** ask whether the ticket integrates correctly — that remains the seam ticket's job. It
asks only whether the ticket depends on something that does not exist and has no owner.

**This is the weakest of the four decisions and is labelled as such rather than dressed up.** It is an
observer; it depends on master reading carefully; the convergence study predicts observers do not hold.
It is adopted only because D1 and D2 are prospective and cannot reach mappings already published.

**Retirement condition:** D3 retires when every obligation → owner mapping published before this ADR has
either been re-audited under D1 and D2, or its chain has reached a terminal state. At that point no
un-audited mapping remains and the backstop has no population. Retirement is recorded in this ADR's
Status Updates and the question is removed from both documents.

### D4 — A cutover is two tickets, and the removal's precondition is observed data

Where a change **replaces** an existing working path, the addition and the removal are **separate
tickets**. The removal is `blockedBy` the addition, and its acceptance criterion is **observed data on
the new path** — not that the new path is configured, registered, or deployed.

**The scope condition is load-bearing, and a blanket rule would be wrong.** This applies when the old
and new paths are **separable in time** — different processes, hosts, repositories, or deploy units, so
both can run at once. It does **not** apply to an in-process rename, where ADR-0033 decided the opposite
deliberately and correctly:

> *"This is a clean break: every reference to the old enum values is updated in the same commit. No
> backward-compat code that exists only for migration."*

That decision stands untouched. Forcing expand-contract onto an enum rename would manufacture exactly
the migration-only compatibility code ADR-0033 rejected, for no benefit: the two paths are not
separable, so there is no interval in which both run.

**This is checked at dispatch, from the ticket's scope text — not at merge.** FRE-1071 lives in a
separate repository; master never saw its diff and never could. A merge-gate diff-shape check is
structurally blind to exactly the cross-boundary class this decision exists for. The ticket body,
however, said plainly that it removes the Elasticsearch writer and adds OTLP export, and that sentence
is in front of master at the pre-label gate.

**The mechanism's value is structural rather than vigilant.** The question is asked once; its output is
a permanent change to the shape of the work. Once the chain carries two tickets and a relation, nothing
further has to be remembered, and the ordering is enforced by the resolver rather than by attention.

**What it buys, stated precisely:** it does not *find* a missing precondition — D1, D2 and D3 do that.
It makes a missed one **survivable**, converting a regression into a no-op. It is therefore immune to
preconditions nobody thought to name at all, which is the only one of the four mechanisms that is.

### D5 — Four documents are amended in one change, and ADR-0130 is pointed here

A partial amendment is worse than none: the contradiction is then resolved by whichever document a
session reads first (ADR-0130 D5's reasoning, applied to its own successor).

| where | becomes |
|---|---|
| `.claude/skills/adr/SKILL.md` Step 5 | the mapping splits two-party obligations by provisioner (D1); the coverage check tests sufficiency, not presence (D2) |
| `.claude/skills/lifecycle-rules.md` § Dispatch | the pre-label list gains D3's backstop question and D4's cutover question |
| `.claude/skills/master/SKILL.md` Step 8 | the same two questions in the advance-dispatch bullets, alongside the D6 decidability check and the open-remedy disposition |
| `docs/architecture_decisions/ADR-0130-*.md` | a Status Update recording that D1's coverage clause and D6's dispatch check are extended here — so no document teaches the unextended rule |

ADR-0130 is **amended by reference, not edited in place.** Its decisions and criteria are untouched,
because its seam ticket is unadjudicated and due-dated against the criteria it carried at filing;
adding criteria to it now would make that seam accrete work after its due date was set, which is the
failure ADR-0130 D2 froze seam scope to prevent.

---

## Alternatives Considered

### Option 1: FRE-1221 as filed — the path-assumption question alone, at the dispatch gate

**Description:** Add the ticket's question verbatim to master's pre-label check and change nothing else.

**Pros:**
- One line, in a step master already performs; the cheapest possible intervention.
- Exactly what the commissioning ticket asked for, with no scope growth.
- Requires no change to how any chain is authored, so it applies immediately to every existing mapping.

**Cons:**
- Answers "no" on the case that motivated it. FRE-1070 builds the Collector, the Collector is in the
  chain, and the question asks whether any chain ticket builds the assumed component.
- Leaves the mapping — where the obligation was actually mis-assigned — completely untouched.
- Is an observer, which ADR-0130's own Option 1 rejection and the convergence study both argue does not
  hold.

**Why Rejected:** As the *primary* answer it treats the reading of a mapping while leaving the writing
of it unchanged — the same shape ADR-0130 rejected when it declined to add queue machinery around
unchanged criteria. Adopted as **D3**, narrowed to a question about the ticket's own deliverable rather
than the chain's inventory, and explicitly scoped as a backstop with a retirement condition.

### Option 2: A D9 on ADR-0130 rather than a new ADR

**Description:** Extend ADR-0130 in place with a ninth decision covering the split rule, sufficiency and
the cutover, and update its criteria accordingly.

**Pros:**
- One document holds one rule; no risk of a reader finding the older, narrower statement first.
- The change is genuinely within ADR-0130's subject matter — it extends D1's coverage clause and D6.
- No new seam ticket, no new due date, no new entry in the index.

**Cons:**
- ADR-0130's seam ticket is filed and due-dated against the five criteria it carried at filing. New
  decisions need new criteria, and adding them makes that seam adjudicate work scheduled after its due
  date was set — the accretion D2 froze seam scope to prevent.
- An `Accepted` ADR with an unadjudicated objective would gain decisions never covered by the
  adjudication that eventually runs, so its `Implemented` verdict would overclaim.

**Why Rejected:** The multiplicity objection does not survive inspection — ADR-0130 D5's concern is
**contradiction**, not two documents. A successor that names what it extends, plus a Status Update on
the predecessor pointing forward, leaves nothing teaching the unextended rule; ADR-0129 already carries
exactly this shape through its D6 amendments (FRE-1193, FRE-1213). The seam objection has no such
answer, so a new ADR with its own seam is correct.

### Option 3: A blanket "never remove the old path in the same change"

**Description:** State D4 without a scope condition — every removal of an existing path is a separate
ticket from its replacement, unconditionally.

**Pros:**
- Trivially checkable, with no judgement about separability at the gate.
- No class of cutover can slip through by being argued into the exception.

**Cons:**
- Directly contradicts ADR-0033, which decided a clean-break enum rename in one commit *specifically to
  avoid* backward-compat code existing only for migration — and was right to.
- Manufactures a two-ticket chain and a compatibility shim for in-process changes where both paths
  cannot meaningfully coexist, so the interval the rule exists to create does not exist.

**Why Rejected:** Adopted **with** the separability scope condition (D4). A rule that contradicts a
standing, correct decision would be resolved by whichever document the session read first — the exact
failure D5 exists to prevent — and would be quietly ignored in practice, which is worse than a narrower
rule that is obeyed.

### Option 4: Enforce the mapping mechanically, with a validator script

**Description:** Parse the published obligation → owner mapping and fail a chain's dispatch on a
two-party row with one owner, or a row whose "Proved by" cell does not resolve.

**Pros:**
- Removes the judgement entirely — the strongest available form of D1, and immune to attention.
- Would produce a durable, re-runnable verdict rather than a one-time read.

**Cons:**
- The mapping is prose in a Linear comment with no schema; parsing it is brittle, and a validator that
  silently fails to parse reads as green — the FRE-823 failure shape.
- The two-party test is grammatical and might be approximated; the sufficiency test (D2) is semantic
  and is not mechanizable with anything this project has today.

**Why Rejected:** Recorded as the upgrade path, not the decision — the same disposition ADR-0130 gave
its Option 3. ADR-0127's Analyzer (**Proposed**) is the natural home once it exists; nothing here
depends on it.

### Option 5: Put all the checks at master's merge gate instead of at dispatch

**Description:** Ask the path-assumption and cutover questions when master reviews the diff, where the
actual code is visible, rather than from ticket text at dispatch.

**Pros:**
- The diff is ground truth; ticket scope text can be stale or aspirational.
- Master already performs a thin design-adherence read at that moment, so the read is co-located.

**Cons:**
- **Structurally blind to the motivating case.** FRE-1071 is in a separate repository. Master never saw
  that diff and merged nothing for it.
- Merge is after the build. A precondition caught there has already cost the implementation, which is
  the argument ADR-0130 D6 used to move the decidability check to dispatch in the first place.

**Why Rejected:** The one case this ADR most needs to catch is invisible at merge by construction.
Dispatch is both earlier and, for cross-repository work, the only gate that sees the ticket at all.

---

## Consequences

### Positive Consequences

- A two-party obligation can no longer be assigned to one side, so the specific mis-assignment that
  produced FRE-1220 stops being available rather than becoming better-observed.
- An unfillable "Proved by" cell becomes the visible artifact of a missing provider, at authoring time,
  before any child is dispatched — the cheapest moment the gap can surface.
- The sufficiency test closes the owner-and-wrong-criterion hole that the presence test cannot see, and
  that a full adversarial codex round did not catch.
- A cutover's ordering becomes structural — a relation the resolver enforces — instead of a sequencing
  fact someone has to hold in mind across two merges in two repositories.
- A missed precondition of *any* kind degrades to a no-op rather than a regression, which is the only
  protection here that does not require anyone to have anticipated the specific gap.
- The weakest mechanism carries a stated retirement condition, so the gate does not accumulate questions
  permanently.

### Negative Consequences

- The pre-label list reaches three questions plus the open-remedy disposition. That is real cost at a
  step master runs at every merge, and two of the three depend on master reading carefully.
- D2's sufficiency test is a judgement, so two readers can disagree about whether a criterion entails an
  obligation. D1's split narrows where the judgement is spent but does not remove it.
- Splitting two-party obligations lengthens mappings — ADR-0129's would have gained rows — and a longer
  inventory is read less carefully, which is the mechanism that produced the unreconciled row count.
- D4 makes some chains longer by one ticket and one relation, and the removal ticket may sit unbuilt for
  a while, so the old path lingers.
- Judging separability is itself a judgement at the dispatch gate, and a wrong call in the permissive
  direction reproduces exactly the coupling D4 forbids.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| The pre-label list grows until master skims it, and all three questions degrade together | High | D3 carries a retirement condition that removes one of them by construction; D1/D2 move the load to authoring, where it is spent once per chain rather than once per ticket; AC-4 tests whether the backstop is producing recorded answers or is being skipped |
| D2's sufficiency test is applied as a rubber stamp — every row judged sufficient | High | AC-2 reperforms the test against ADR-0129's already-published mapping and **fails if it does not flag D5.d**, so a test too weak to catch its own motivating case is caught by the criterion rather than assumed to work |
| "Two parties" is read narrowly (only network paths), reproducing FRE-1221's original narrowness | Medium | D1 states four sentence shapes and names credentials, hosts and deployed components alongside paths; AC-1 fails on a chain whose two-party rows were assigned by grammatical subject regardless of the resource kind |
| Separability is judged permissively so a coupled cutover ships as one ticket anyway | Medium | D4 states the test as different processes/hosts/repositories/deploy units — an observable property of the two artifacts, not an intent judgement; AC-3 fails on any qualifying ticket that shipped whole |
| The removal ticket is never built, so the old path runs forever and the migration is half-done | Medium | The removal is a filed `Approved` ticket with a relation, so it is visible in the resolver's eligible set rather than living in prose; a lingering old path is a queue entry, not a silent state |
| Splitting inflates mappings until the inventory is skimmed, hiding a row rather than a sentence | Medium | The split adds rows only where two parties are named, not uniformly; the unreconciled 43-vs-41 count is itself dispositioned by the retrospective audit rather than left as a known-bad exemplar |
| D3 is never retired because nobody re-audits the pre-existing mappings | Medium | The audit is a filed implementation ticket in this ADR's own chain, not an aspiration; AC-4 reads the retirement condition's status at the window's end and records it either way |
| The whole thing is a fourth observer over the same inference and does not converge | High | Deliberately confronted rather than mitigated away: D1 and D4 change the artifact's shape permanently and require no vigilance once applied; D2 and D3 are reads. AC-5 is the honest test — it fails if the defect class recurs on a chain authored under this ADR, whatever the mechanism's theory says |

---

## Implementation Notes

**Files affected:**

- `.claude/skills/adr/SKILL.md` — Step 5, the obligation → owner mapping paragraph: D1's split-by-
  provisioner rule and its two resolutions, D2's sufficiency test.
- `.claude/skills/lifecycle-rules.md` — § Dispatch, the pre-`stream:`-label bullet list: D3's backstop
  question with its boundary and retirement condition, D4's cutover question with its scope condition.
- `.claude/skills/master/SKILL.md` — Step 8 advance-dispatch bullets: the same two questions, stated
  alongside the existing ADR-0130 D6 decidability check and the open-remedy disposition.
- `docs/architecture_decisions/ADR-0130-two-tiers-of-acceptance-criteria.md` — a Status Update only.
- `docs/architecture_decisions/README.md` — the index row for this ADR, in the authoring commit.

**Not affected:** ADR-0130's Decision and Verification sections, and ADR-0033 in its entirety — D4's
scope condition exists to preserve its clean-break decision rather than override it.

**Dependencies:** none. This ADR is documentation and skill text only.

**Sequence:** amend the four documents in one PR (D5) → re-audit every obligation → owner mapping
published before that PR under D1 and D2, dispositioning each flagged row → this ADR's seam ticket,
parked until both have landed.

**Testing strategy:** the artifacts are documents, a published mapping and the board, so the criteria
are read from those. AC-2 is adjudicable as soon as the audit lands. AC-1, AC-3 and AC-5 need chains
authored under the rule and therefore a window; AC-4 reads the retirement condition at the window's end.
All five belong to the seam ticket (ADR-0130 D2) and none is discharged by an implementing ticket.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

**All five belong to this ADR's seam ticket.** None is discharged by an implementing ticket.

- **AC-1 — Two-party obligations are actually split, and split toward the provisioner.** For every ADR
  chain whose obligation → owner mapping is published after the amendment lands, every row whose
  obligation sentence names two parties appears as two rows with two owners, or as one row owned by the
  seam. · **Check:** read each such mapping row by row; classify each obligation as one-party or
  two-party from its sentence; for each two-party obligation confirm the split, then confirm each
  provider half is owned by the ticket whose deliverable **brings the thing into existence** rather than
  the one that consumes it. · *Fails if* any two-party obligation carries a single non-seam owner; if
  any provider half is owned by the consumer's ticket; if any half no chain ticket can make true was
  recorded as prose rather than resolved by a filed provisioning ticket **with its relation written** or
  by assignment to the seam — **or**, if no chain published a mapping containing a two-party obligation
  during the window, reports **inconclusive** rather than green, since a rule proves nothing against a
  population of zero.

- **AC-2 — The sufficiency test is discriminating enough to catch the case that motivated it.**
  Reperformed against ADR-0129's already-published mapping on FRE-1043, D2's test flags row D5.d, and
  every row it flags carries a disposition. · **Check:** for each row of that mapping, read the criterion
  named in "Proved by" on the owning ticket and judge whether a passing verdict on it makes the
  obligation true; compare the resulting flag set against the audit published by this ADR's chain. ·
  *Fails if* D5.d is not among the flagged rows — a test that cannot catch its own motivating case
  verifies nothing — **or** if any flagged row carries no disposition (in scope naming the criterion,
  filed naming the id, or rejected with a reason), **or** if the mapping's stated row count still does
  not reconcile with its enumerated rows at adjudication.

- **AC-3 — A cutover ships as two tickets, and the removal waits on observed data.** For every
  implementation ticket filed after the amendment lands whose scope both adds a replacement path and
  removes an existing working one across a process, host, repository or deploy-unit boundary, the work
  is two tickets with a `blockedBy` relation, and the removal ticket's acceptance criterion names
  **observed data arriving on the new path**. · **Check:** identify qualifying tickets from their scope
  text; for each, confirm two tickets and the relation, then read the removal ticket's criterion. ·
  *Fails if* any qualifying ticket shipped as one change; if the relation is absent; if the removal
  ticket's criterion is satisfied by the new path being configured, registered, exported or deployed
  rather than by data observed on it — **or**, if no qualifying ticket was filed during the window,
  reports **inconclusive** rather than green.

- **AC-4 — The backstop either produces recorded answers or is retired, and is not silently skipped.**
  Every implementation ticket labelled during the window whose chain's mapping predates the amendment
  carries a recorded answer to D3's question, and at the window's end D3's retirement condition is
  evaluated and its status recorded in this ADR's Status Updates. · **Check:** list tickets labelled
  during the window belonging to pre-amendment chains; confirm each carries a recorded answer; then list
  every pre-amendment mapping and confirm each has been re-audited or its chain has reached a terminal
  state. · *Fails if* any such ticket was labelled with no recorded answer; if a ticket whose answer was
  "no" subsequently produced a filed defect reporting that it depended on something nothing provisions
  — the answer having been wrong is the substantive failure, not the paperwork — or if the retirement
  condition is neither met nor recorded as unmet with the remaining population named.

- **AC-5 — The defect class stops recurring on chains authored under this rule.** No ticket is filed
  during the window reporting that merged work depends on a precondition nothing provisions, where the
  chain in question was authored after the amendment landed. · **Check:** search Linear for tickets
  filed in the window whose body reports a merged ticket assuming a component, path, credential or host
  that no ticket built; for each hit, determine whether its chain's mapping predates or postdates the
  amendment. · *Fails if* any such ticket names a post-amendment chain — **or**, if fewer than three
  ADR chains were authored during the window, reports **inconclusive** rather than green. Three is the
  non-vacuity floor: a quiet period in which nothing was decomposed cannot distinguish a working rule
  from an unexercised one, and is the most available way for this criterion to pass while the process is
  still broken. A pre-amendment chain producing such a ticket is **not** a failure — those are D3's
  population, and AC-4 judges them.

**Seam ticket:** filed with the chain, parked (`Backlog`), carrying a **due date of 2026-11-12** —
ninety days after the contract amendment is expected to merge, which is the earliest date AC-1, AC-3,
AC-4 and AC-5 all become adjudicable. AC-2 is adjudicable earlier and is nonetheless owned here, since
early decidability changes *when* a criterion could be adjudicated, not *who* owns it (ADR-0130 D2).
Master re-dates it if the amendment's merge slips materially from 2026-08-14, and activates it at the
first advance-dispatch on or after the due date.

---

## References

- FRE-1221 — the commissioning ticket: the eviction diagnosis this ADR corrects, and the dispatch-gate remedy adopted in narrowed form as D3
- FRE-1220 (Canceled) — the concrete gap, and master's own on-ticket correction establishing that an authenticated path-scoped ingress already existed for this producer
- FRE-1043 — carries ADR-0129's obligation → owner mapping, including row D5.d, in a comment published by FRE-1080
- FRE-1080 — ADR-0130 T2: re-scoped the ADR-0129 chain and published the mapping; its codex round caught six owner-without-criterion rows and no owner-with-wrong-criterion row
- FRE-1070 — ADR-0129 B5: the Collector, deployed with no host port publication; its AC-4 constrains exporting off-box and adjudicates nothing about receiving
- FRE-1071 — ADR-0129 B6: shipped the Elasticsearch-writer removal and the OTLP export as one change, in a separate repository
- FRE-1073 — ADR-0129 B8: that ADR's seam ticket, where the gap was scheduled to surface after the whole chain had shipped
- FRE-1230 — owns the `slm_server` restart gate, where FRE-1071's coupling actually bites; the instance of the pattern D4 generalises
- FRE-1253 — whether a seam ticket's due date should be derived from its chain's last dependent; filed separately and deliberately not folded in here
- ADR-0130 — Two Tiers of Acceptance Criteria (**Accepted**): D1's coverage clause and D6's dispatch check are extended by this ADR; its Option 1 rejection is the argument this ADR had to answer about its own D3
- ADR-0129 — OpenTelemetry Instrumentation, with Trace Visibility as the Acceptance Bar (**Accepted**, D6 amended 2026-08-07 and 2026-08-08): the chain whose mapping is the worked example; unchanged by this ADR
- ADR-0033 — Multi-Provider Model Taxonomy (**Accepted**): its clean-break decision is the case D4's scope condition exists to preserve
- ADR-0127 — The Harness Self-Analysis Pillar (**Proposed**): the eventual home for a mechanical mapping validator (Option 4); nothing here depends on it
- ADR-0131 — Retire MASTER_PLAN for the Owner Console (**Accepted**): D4's one-writer rule, which places the mapping and the filing of provisioning tickets on the filing plane open to every session
- ADR-0136 — The Cloudflare Edge Carries HTTP, Not gRPC (**Accepted**): the protocol constraint recorded from the same FRE-1220 study
- `docs/research/2026-08-08-fre-1220-otlp-ingress-security-and-cloudflare-capability.md` — the commissioned study that measured the existing ingress and corrected FRE-1220's framing
- `docs/research/2026-07-29-why-pipeline-point-fixes-do-not-converge.md` — the convergence law used to reject Option 1 and to grade D3 as the weakest mechanism
- `.claude/skills/adr/SKILL.md` · `.claude/skills/lifecycle-rules.md` · `.claude/skills/master/SKILL.md` — the three contract documents amended by D5

---

## Status Updates

### 2026-08-13 — Proposed

**Changed By:** `/adr` session (FRE-1221)
**Reason:** Authored following owner-directed design discussion. The session's exploration established
that FRE-1221's premise — an integration property evicted by ADR-0130 D1 into no ticket at all — is
contradicted by the published mapping, which named the obligation and gave it an owner. The owner chose
the combined shape (authoring rule plus dispatch backstop) over either alone, and directed that the
cutover rule be included after the session checked that it is absent from the contract and that its
natural home is dispatch rather than merge.
