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

**Failure 2 — the named criteria do not entail the obligation.** D5.d cites FRE-1071's first and third
criteria (B6-1 and B6-3 in the FRE-1080 rescope plan). The first is traceparent continuation, so the
exported span's trace id equals the caller's. The third is that no code path writes telemetry to
Elasticsearch at all — the writer and the client-side index-URL formatting removed, with a test
observing zero outbound Elasticsearch requests. Neither asserts that an endpoint exists, or that it is
reachable from the host the producer runs on. A green verdict on both leaves the obligation entirely
unproven.

This is precisely the hole the existing integrity check does not cover. Codex's review of the mapping's
first draft caught **six rows with an owner and no criterion** (D2.b, D2.c, D3.c, D3.e, D8.a, D8.c) and
all six were fixed. Nobody tested for a row with an owner and the **wrong** criterion, because a
presence check cannot distinguish the two — both look complete.

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

Five decisions across three mechanisms. D1 and D2 change how the obligation → owner mapping is written
(authoring, `adr` Step 5). D3 is a bounded backstop at dispatch for chains authored before them. D4
changes how a cutover is decomposed and dispatched. D5 states the edit surface.

### D1 — An obligation naming two parties is split by provisioner, and every half must be decidable by its own owner

In the obligation → owner mapping, an obligation whose sentence names **two parties** — *A ships to B*,
*A reads B's output*, *A reaches B*, *A gets X from B* — may not carry a single non-seam owner. It is
**split**, and the split is governed by two rules applied in order:

1. **Assignment follows who provisions the thing, never the grammatical subject.** D5.d's subject is
   `slm_server`; its provisioner is the Collector.
2. **Each half must be stated at an altitude its own owner's deliverable can decide** (ADR-0130 D1).
   A half that remains undecidable after the split is **not** forced onto either ticket — it goes to the
   seam, which is where a property observable only once two children interact belongs by definition.

Applied to D5.d, this yields three rows, not two:

| half | obligation | owner | decidable there because |
|---|---|---|---|
| provider | the Collector publishes an OTLP receiver on a port reachable from outside its container network | FRE-1070 | its own compose file, plus a probe from the container host |
| consumer | `slm_server` addresses that endpoint instead of formatting index URLs | FRE-1071 | its own effective-configuration artifact |
| end-to-end | a span emitted by `slm_server` on its own host arrives at the VPS Collector | **seam** (FRE-1073) | needs both deliverables and the edge between them |

**The split is what makes the defect visible**, and it does so at the provider row: FRE-1070 has **no
criterion asserting a published, host-reachable port** — its AC-4 constrains exporting off-box and no
other criterion of its addresses ingress. The "Proved by" cell is unfillable, which is a legible,
unmissable signal at authoring time. The single-owner form concealed exactly that behind a citation
that read as complete.

**Note what the third row does *not* do.** Sending end-to-end reachability to the seam is the same
disposition ADR-0130 D1 already mandates, and it is emphatically not where the fix lives — the seam
adjudicates late, which is the cost FRE-1221 correctly identified. The fix lives in the **provider
row**, which is decidable early and was missing entirely.

**A provider half that no ticket in the chain can make true has exactly two resolutions, and neither is
prose:**

1. **A provisioning ticket is filed, and its `blockedBy` relation is written in the same action.** One
   action, not two — a ticket entering a chain without its relations is a dispatch bug that surfaces as
   a false head (lifecycle-rules § Dispatch), and a provisioning ticket with no relation is precisely a
   precondition with no ordering.
2. **The half is assigned to the seam**, so its adjudication is expected rather than discovered.

**Recording the assumption in prose with no owner is not a resolution.** That is the state FRE-1220 was
already in, and it is what a mapping exists to make impossible.

### D2 — A row's named criterion must *entail* the obligation, not merely sit on the right ticket

The mapping's integrity check gains a **sufficiency** test alongside its existing **presence** test.
**It applies to every row**, one-party and split alike. For each: read the criterion named in "Proved
by" on the owning ticket, and confirm that **a passing verdict on that criterion makes the obligation
true**. If it does not, the row is not covered, whatever its cells contain.

Presence asks *is there an owner and a criterion?* Sufficiency asks *would this criterion, passing, make
this sentence true?* D5.d passes the first and fails the second, which is why it shipped.

**D1's split is not a filter on which rows are tested.** It is the operation that produces the rows most
likely to fail sufficiency — a provider half with no available criterion fails it on sight — but a
one-party row citing a criterion that does not prove it is covered by this test and is not exempted by
anything in D1.

A row failing sufficiency is resolved the same three ways: a criterion that does entail the obligation
is named instead, a covering ticket is filed with its id recorded on the row, or the row is assigned to
the seam. A sufficiency flag may be **withdrawn** only on the finding that the original criterion does
entail the obligation after all — not on a judgement that the gap is acceptable.

### D3 — The dispatch backstop: one question, bounded, recorded, with a retirement condition

For chains whose mappings were published **before** D1 and D2 are in force, the mapping cannot be
relied on, so one question is added to the existing pre-`stream:`-label list in `lifecycle-rules`
§ Dispatch and `master` SKILL Step 8 — an added read at a step already mandatory, not a new step:

> **Does any criterion of this ticket depend on a network path, a host, a credential or a deployed
> component that does not exist yet and that no ticket in this chain delivers?**

The existence-and-ownership clause is **inside** the question, not stated separately as a boundary. A
criterion relying on Postgres, the test stack, or any already-running service answers it "no" without
requiring master to remember an unstated exception — which a question phrased as *"does this depend on
anything outside the ticket"* would not, since nearly every criterion does.

If the answer is yes, D1's two resolutions apply unchanged: a provisioning ticket with its relation
written in the same action, or the assumption assigned to the ADR's seam ticket.

**The question is about the ticket in front of master, not about the chain's inventory.** FRE-1221's
form asked whether *any ticket in this chain builds* the assumed thing, which returns a reassuring "yes"
whenever a component with the right name exists anywhere in the decomposition — the Collector did.
This form asks whether the thing **this ticket's criteria depend on** exists and is delivered by
something, which admits no such substitution.

**The check does not ask whether the ticket integrates correctly** — that remains the seam ticket's job.

**The answer is recorded on the ticket**, in the same pass and on the same surface as the open-remedy
disposition, so the gate leaves evidence rather than only a decision.

**This is the weakest of the four mechanisms and is labelled as such rather than dressed up.** It is an
observer; it depends on master reading carefully; the convergence study predicts observers do not hold.
It is adopted only because D1 and D2 are prospective and cannot reach mappings already published.

**Retirement condition.** D3 retires when every obligation → owner mapping published before this ADR has
either been re-audited under D1 and D2, or its chain has reached a terminal state. That population is
enumerable rather than notional: it is every ADR in `docs/architecture_decisions/README.md` carrying
implementation tickets filed before the amendment, whose umbrella ticket holds a mapping. Retirement is
recorded in this ADR's Status Updates and the question is removed from both documents.

### D4 — A cutover is two tickets; the removal waits on observed data, at dispatch as well as at close

Where a change **replaces** an existing working path, the addition and the removal are **separate
tickets**. Three things follow, and the third is what makes the first two bind:

1. The removal is `blockedBy` the addition.
2. **Observed data on the new path is a dispatch precondition of the removal, not the removal's
   acceptance criterion.** The removal does not receive its `stream:` label until evidence that the new
   path is carrying data is recorded on the addition ticket. A `blockedBy` relation clears when its
   blocker **merges** (lifecycle-rules § Dispatch), which is earlier than deploy and much earlier than
   proof; the relation therefore supplies *ordering* and this gate supplies *proof*. Without it the
   removal can be dispatched and merged while the new path carries nothing, which is the precise harm.
3. **The removal ticket's own acceptance criterion is about the removal**: once it lands, the old path
   emits nothing **and the new path is still observed carrying data**. Making the precondition serve as
   the criterion instead — as this ADR's second draft did — yields a criterion that is already true
   before the ticket is dispatched, which a no-op removal satisfies and which master's own decidability
   check (ADR-0130 D6) would rightly reject at the label gate. The precondition proves the new path
   works *before* the removal; the criterion proves it still works *after*, with the old path gone.

**The scope test is whether the old and new paths can both be live at the same time.** Different
processes, hosts, repositories or deploy units are **indicators** that they can — not the definition.
The indicator misleads in both directions: a protocol change spanning two repositories may still have
to be atomic, and a single process can dual-write, expose two routes, or switch behind a flag. **Where
the indicator and the test disagree, the test governs**, and the reasoning is recorded on the ticket.

**A blanket rule would be wrong**, which is why the scope test exists. ADR-0033 decided the opposite
deliberately and correctly for an in-process rename:

> *"This is a clean break: every reference to the old enum values is updated in the same commit. No
> backward-compat code that exists only for migration."*

That decision stands untouched. The two paths there cannot both be live — there is no interval for the
rule to create — so forcing expand-contract onto it would manufacture exactly the migration-only
compatibility code ADR-0033 rejected, for nothing.

**This is checked at dispatch, from the ticket's scope text — not at merge.** FRE-1071 lives in a
separate repository; master never saw its diff and never could. A merge-gate diff-shape check is
structurally blind to exactly the cross-boundary class this decision exists for. The ticket body,
however, said plainly that it removes the Elasticsearch writer and adds OTLP export, and that sentence
is in front of master at the pre-label gate.

**The mechanism's value is structural rather than vigilant.** The question is asked once; its output is
a permanent change to the shape of the work. Once the chain carries two tickets and a relation, the
ordering is enforced by the resolver rather than by attention.

**What it buys, stated precisely:** it does not *find* a missing precondition — D1, D2 and D3 do that.
It makes a missed one **survivable**, converting a regression into a no-op. It is therefore the only one
of the four mechanisms immune to preconditions nobody thought to name at all.

### D5 — Four documents are amended in one change, and ADR-0130 is pointed here

A partial amendment is worse than none: the contradiction is then resolved by whichever document a
session reads first (ADR-0130 D5's reasoning, applied to its own successor).

| where | becomes |
|---|---|
| `.claude/skills/adr/SKILL.md` Step 5 | the mapping splits two-party obligations by provisioner and states each half at an altitude its owner can decide (D1); the coverage check tests sufficiency on every row, not presence (D2) |
| `.claude/skills/lifecycle-rules.md` § Dispatch | the pre-label list gains D3's backstop question and D4's cutover question, including the removal-label gate |
| `.claude/skills/master/SKILL.md` Step 8 | the same two questions in the advance-dispatch bullets, alongside the existing ADR-0130 D6 decidability check and the open-remedy disposition |
| `docs/architecture_decisions/ADR-0130-*.md` | a Status Update recording that D1's coverage clause and D6's dispatch check are extended here — so no document teaches the unextended rule |

ADR-0130 is **amended by reference, not edited in place**, and this is sound rather than evasive
precisely because nothing here contradicts its Decision text. D1's second rule *applies* ADR-0130 D1's
own altitude requirement to each half of a split, and sends the residual end-to-end property to the
seam exactly as ADR-0130 D1 already directs. Had the split instead parked a cross-child property on a
child — as this ADR's first draft did — a Status Update would not have been enough, and ADR-0130's
Decision section would have had to change.

Editing it in place is refused for a separate reason: its seam ticket is unadjudicated and due-dated
against the criteria it carried at filing, so adding criteria to it now would make that seam accrete
work after its due date was set — the failure ADR-0130 D2 froze seam scope to prevent.

---

## Alternatives Considered

### Option 1: FRE-1221 as filed — the path-assumption question alone, at the dispatch gate

**Description:** Add the ticket's question verbatim to master's pre-label check and change nothing else.

**Pros:**
- One line, in a step master already performs; the cheapest possible intervention.
- Exactly what the commissioning ticket asked for, with no scope growth.
- Requires no change to how any chain is authored, so it applies immediately to every existing mapping.

**Cons:**
- Answers "no" on the case that motivated it. FRE-1070 builds the Collector, the Collector is a deployed
  component, and it is in the chain.
- Leaves the mapping — where the obligation was actually mis-assigned — completely untouched.
- Is an observer, which ADR-0130's own Option 1 rejection and the convergence study both argue does not
  hold.

**Why Rejected:** As the *primary* answer it treats the reading of a mapping while leaving the writing
of it unchanged — the same shape ADR-0130 rejected when it declined to add queue machinery around
unchanged criteria. Adopted as **D3**, re-phrased around the ticket's own dependencies with the
existence-and-ownership test folded into the question, and explicitly scoped as a backstop with a
retirement condition.

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
**contradiction**, not two documents, and this ADR contradicts none of its Decision text (D5). A
successor that names what it extends, plus a Status Update on the predecessor pointing forward, leaves
nothing teaching the unextended rule; ADR-0129 already carries exactly this shape through its D6
amendments (FRE-1193, FRE-1213). The seam objection has no such answer, so a new ADR with its own seam
is correct.

### Option 3: A blanket "never remove the old path in the same change"

**Description:** State D4 without a scope test — every removal of an existing path is a separate ticket
from its replacement, unconditionally.

**Pros:**
- Trivially checkable, with no judgement about whether both paths can be live at once.
- No class of cutover can slip through by being argued into the exception.

**Cons:**
- Directly contradicts ADR-0033, which decided a clean-break enum rename in one commit *specifically to
  avoid* backward-compat code existing only for migration — and was right to.
- Manufactures a two-ticket chain and a compatibility shim for changes where both paths cannot
  coexist, so the interval the rule exists to create does not exist.

**Why Rejected:** Adopted **with** the both-live-at-once scope test (D4). A rule that contradicts a
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
- An unfillable "Proved by" cell on a provider row becomes the visible artifact of a missing provider,
  at authoring time, before any child is dispatched — the cheapest moment the gap can surface.
- The sufficiency test closes the owner-and-wrong-criterion hole that the presence test cannot see, and
  that a full adversarial codex round did not catch.
- A cutover's ordering becomes structural, and its *proof* becomes a dispatch precondition rather than
  a close-out hope — the removal cannot be labelled while the new path carries nothing.
- A missed precondition of *any* kind degrades to a no-op rather than a regression, which is the only
  protection here that does not require anyone to have anticipated the specific gap.
- The weakest mechanism carries a retirement condition over an enumerable population, so the gate does
  not accumulate questions permanently.

### Negative Consequences

- The pre-label list reaches three questions plus the open-remedy disposition. That is real cost at a
  step master runs at every merge, and two of the three depend on master reading carefully.
- D2's sufficiency test is a judgement applied to every row, which is more expensive than the presence
  check it augments, and two readers can disagree about entailment.
- Splitting two-party obligations lengthens mappings — ADR-0129's would have gained rows — and a longer
  inventory is read less carefully.
- D4 makes some chains longer by one ticket and one relation, and the removal ticket may sit unlabelled
  until the addition's evidence lands, so the old path lingers by design.
- Judging whether both paths can be live at once is itself a judgement at the dispatch gate, and a wrong
  call in the permissive direction reproduces exactly the coupling D4 forbids.
- **This is the third and fourth checkpoint on criterion quality, and a fifth is already filed.**
  ADR-0126 D7 asks whether a criterion can fail; ADR-0130 D6 asks whether it is decidable from the
  ticket's own deliverable; this ADR adds D3's dependency question and D4's cutover question; and
  FRE-1112 (Needs Approval) proposes a further one — whether the stated check is *executable* against
  the fixtures it names, from where it must run. That ticket warns in its own body that *"three rules
  that each reject a different defect class are harder to apply than one that composes them"*, and this
  ADR makes that warning more pressing rather than less. **Composing them is deliberately not attempted
  here**: FRE-1112 is an open decision the owner has not approved, and folding an unapproved rule into
  this one would repeat the over-reach this ADR's own scope discipline exists to avoid. It is named as
  the next question, not answered.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| The pre-label list grows until master skims it, and all three questions degrade together | High | D3 carries a retirement condition over an enumerable population, so one of them is removable by construction; D1/D2 move the load to authoring, where it is spent once per chain rather than once per ticket; AC-4 fails if labelled tickets carry no recorded answer |
| D2's sufficiency test is applied as a rubber stamp — every row judged sufficient | High | AC-2 reperforms entailment on the rows the audit marked **sufficient**, so a stamp is caught by finding what it passed rather than by trusting a claim about reading order; a positive control of synthetic rows with known status fails the criterion if the rubric itself cannot classify them. Comparing two flag sets was the second draft's answer and was weaker — two correlated readers produce identical sets that are identically wrong |
| The split parks a cross-child property on a child, contradicting ADR-0130 D1 | High | D1's second rule requires each half to be decidable by its own owner and sends the residual to the seam; the worked example carries three rows, not two, and AC-1 fails if any half left undecidable by the split was not assigned to the seam. This is the defect codex found in this ADR's own first draft |
| "Two parties" is read narrowly, reproducing FRE-1221's original narrowness | Medium | D1 states four sentence shapes covering shipping, reading, reaching and obtaining; D3's question separately names a network path, a host, a credential and a deployed component; AC-1 enumerates two-party obligations from the ADR's **Decision section** rather than from the mapping's own phrasing, so a passively-worded row cannot escape classification |
| The both-live-at-once test is judged permissively so a coupled cutover ships as one ticket | Medium | D4 states the test as the property itself and demotes process/host/repository/deploy-unit to indicators that mislead in both directions, with the reasoning recorded on the ticket; AC-3 fails on any replacement that shipped whole |
| The removal merges before the new path carries anything, because the relation cleared at the addition's merge | High | D4 clause 3 withholds the removal's `stream:` label until the addition's observed-data evidence is recorded; AC-3 reads the label's timing against that evidence and fails if the label came first |
| The removal is never built, so the old path runs forever | Medium | Not fully mitigated, and stated as such: the removal is a filed ticket with a relation, visible on the board and subject to the same approval and labelling as any other work. What the rule buys is that a lingering old path is a queue entry rather than a silent state — not a guarantee it is removed |
| D3 is never retired because nobody re-audits the pre-existing mappings | Medium | The audit is a filed implementation ticket in this ADR's own chain, not an aspiration; AC-4 fails if the retirement condition is unmet at adjudication **regardless of the audit ticket's state**, so the condition cannot be dodged by leaving that ticket open — which is what the second draft's "after the audit has closed" wording allowed |
| The whole thing is a fourth observer over the same inference and does not converge | High | Deliberately confronted rather than mitigated away: D1 and D4 change the artifact's shape permanently and require no vigilance once applied; D2 and D3 are reads. AC-5 is the honest test — it audits post-amendment chains directly rather than waiting for someone to file a defect report |

---

## Implementation Notes

**Files affected — by this ADR's implementation chain, not by the PR that lands this ADR.** The ADR PR
carries the decision record and its index row only; the four document edits below are the first
implementation ticket's deliverable and land in a separate PR, which is why this branch shows no change
to any skill file. This is the same sequence ADR-0130 followed (authored under FRE-1078; its four
contract sentences amended later by FRE-1079).

- `.claude/skills/adr/SKILL.md` — Step 5, the obligation → owner mapping paragraph: D1's split rules and
  its two resolutions, D2's sufficiency test.
- `.claude/skills/lifecycle-rules.md` — § Dispatch, the pre-`stream:`-label bullet list: D3's backstop
  question with its recording surface and retirement condition, D4's cutover question and removal-label
  gate.
- `.claude/skills/master/SKILL.md` — Step 8 advance-dispatch bullets: the same two questions, stated
  alongside the existing ADR-0130 D6 decidability check and the open-remedy disposition.
- `docs/architecture_decisions/ADR-0130-two-tiers-of-acceptance-criteria.md` — a Status Update only.
- `docs/architecture_decisions/README.md` — the index row for this ADR, in the authoring commit.

**Not affected:** ADR-0130's Decision and Verification sections, and ADR-0033 in its entirety — D4's
scope test exists to preserve its clean-break decision rather than override it.

**Dependencies:** none. This ADR is documentation and skill text only.

**Sequence:** amend the four documents in one PR (D5) → re-audit every obligation → owner mapping
published before that PR under D1 and D2, dispositioning each flagged row → this ADR's seam ticket,
parked until both have landed.

**Testing strategy:** the artifacts are documents, published mappings and the board, so the criteria are
read from those plus the deployed system. AC-2 is adjudicable as soon as the audit lands. AC-1, AC-3 and
AC-5 need chains authored under the rule and therefore a window; AC-4 reads the retirement condition at
the window's end. All five belong to the seam ticket (ADR-0130 D2) and none is discharged by an
implementing ticket.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

**All five belong to this ADR's seam ticket.** None is discharged by an implementing ticket.

- **AC-1 — A two-party obligation's provider half is actually delivered before its consumer's ticket
  closes.** · **Check:** for each ADR chain whose implementation tickets were filed after the amendment,
  enumerate the two-party obligations **from that ADR's own Decision section**, independently of how the
  mapping phrased them. For each, identify the provider half and verify **from the system** — the
  deployed artifact, the committed configuration, or a probe — that it was delivered on or before the
  date the consumer's ticket reached `Done`. Then read the mapping's rows for the same obligation. ·
  *Fails if* any two-party obligation's provider half was undelivered when its consumer ticket reached
  `Done`; if any two-party obligation appears in the mapping under a single non-seam owner; if any half
  left undecidable by the split was parked on a child rather than assigned to the seam — **or**, if no
  post-amendment chain contains a two-party obligation, reports **inconclusive** rather than green.
  Enumerating from the Decision section rather than the mapping is what stops a passively-phrased row
  from escaping classification, and verifying delivery from the system rather than from the rows is what
  makes this an outcome rather than a paperwork check: perfectly split rows over an unbuilt endpoint
  fail.

- **AC-2 — The sufficiency test catches rows the audit passed, and its rubric survives a control.** ·
  **Check:** three parts, and the ordering is structural rather than promised — the audit ticket closes
  and publishes its flag set before the seam is activated, so that set is a durable, timestamped record
  the seam cannot retro-fit. (a) **Reperformance on passed rows:** the seam adjudicator draws the rows
  ADR-0129's audit marked *sufficient* and independently tests entailment on each, reading the named
  criterion on its owning ticket. (b) **Positive control:** the adjudicator constructs synthetic rows of
  known status — some whose named criterion demonstrably entails the obligation, some whose plainly does
  not, at least three of each — and runs the same rubric over them without knowing which is which by
  construction order. (c) **Dispositions:** read every flagged row's resolution. · *Fails if* any
  audit-passed row fails entailment on reperformance — that is the rubber stamp, caught without relying
  on anyone's claim about reading order; if the rubric misclassifies any control row, which means the
  instrument does not work and the whole audit is uninterpretable; if D5.d appears in neither the audit's
  flags nor the reperformance; or if any flagged row's disposition is anything other than D1's and D2's
  permitted resolutions — a criterion that does entail the obligation, a filed covering ticket with its
  id on the row, or assignment to the seam. A flag withdrawn on the judgement that the gap is acceptable
  is a failure, not a disposition. Testing *passed* rows rather than comparing two flag sets is the
  point: two correlated readers can produce identical sets that are identically wrong, and comparing
  them would call that agreement.

- **AC-3 — A path replacement ships as two tickets, and the removal is dispatched only after the new
  path is observed carrying data.** · **Check:** enumerate every path replacement whose **addition
  ticket was filed** during the window — *begun*, not *completed*, so an abandoned removal, a merged
  no-op removal and a removal left permanently unlabelled all stay inside the population instead of
  escaping it by never finishing. The inventory is bounded and enumerable: FrenchForest Linear tickets
  filed in the window, plus merged changes in the repositories those tickets name — which is what makes
  a cross-repository case like `slm_server` reachable rather than out of scope by accident. Discovery is
  from ticket bodies and ADR Decision sections, **not** from a single ticket's scope text, since a
  compliant split leaves no one ticket carrying both operations. For each: confirm two tickets with a
  `blockedBy` relation; read the removal ticket's own criterion; and read the removal ticket's history
  against the addition ticket's evidence timestamps to establish whether the `stream:` label was applied
  before or after the observed-data evidence was recorded. · *Fails if* any replacement shipped as one
  ticket; if the relation is absent; if the removal's own criterion does not assert the old path gone
  **and** the new path still carrying data — in particular if it merely restates the dispatch
  precondition, which is already true before the ticket starts; if the removal was labelled before the
  addition's evidence existed; if a removal merged without its old path actually being gone — **or**,
  if no path replacement was begun during the window, reports **inconclusive** rather than green.

- **AC-4 — The backstop produces recorded answers, those answers hold, and an incomplete audit cannot
  excuse a live D3.** · **Check:** (a) list implementation tickets labelled during the window whose
  chain's mapping predates the amendment, and confirm each carries a recorded answer to D3's question on
  the ticket; (b) enumerate every pre-amendment mapping — each ADR in the index carrying implementation
  tickets filed before the amendment, whose umbrella holds a mapping — and confirm each has been
  re-audited under D1 and D2 or its chain has reached a terminal state. · *Fails if* any such ticket was
  labelled with no recorded answer; if any ticket answered "no" subsequently produced a filed defect
  reporting that it depended on something nothing provisions — the answer having been **wrong** is the
  substantive failure, not the missing paperwork; or if **the retirement condition is unmet at
  adjudication, whatever the audit ticket's state**. An open audit ticket is not an excuse: making it
  one would let the condition be dodged indefinitely by never closing that ticket, which is the cheapest
  possible evasion. A red verdict here is a legitimate and expected output — the seam closes on
  adjudication rather than on success (ADR-0130 D2), master files remediation, and D3 simply stays live
  with its population named.

- **AC-5 — No post-amendment chain closes a child against a dependency that was never delivered.** ·
  **Check:** two parts, and the first is the substantive one. (a) **Active:** for each post-amendment
  chain whose last child reached `Done` during the window, determine whether any child's criteria
  depended on a network path, host, credential or deployed component that was undelivered when that
  child closed. This is a **historical** question and must be decided from records that carry time, not
  from current state, which would report today's world and pass a gap since filled: the child's
  close-out evidence comment (master writes one on every `Done` — lifecycle-rules § Evidence contract),
  the git history of the provisioning artifact, and the deploy history. Where no such timestamped record
  exists for a child, that chain reports **inconclusive** rather than green. (b) **Passive:** search
  Linear for tickets filed during the window reporting that merged work depends on an unprovisioned
  precondition, and classify each hit's chain as pre- or post-amendment. · *Fails if* (a) finds any
  post-amendment child that closed against an undelivered dependency, or (b) finds any such ticket
  naming a post-amendment chain. Reports **inconclusive** if fewer than three post-amendment chains
  qualify, where qualifying means containing at least one path replacement, **or** at least one
  two-party obligation whose provider half required something **not already deployed when the chain was
  filed** — an obligation resting on Postgres or any already-running service is an easy negative, and
  three of those would turn this green without exercising provisioning at all. The active half exists
  because an absence-of-filed-reports check passes whenever nobody notices; a chain nobody audited is
  not a chain that worked. A **pre-amendment** chain producing such a ticket is not a failure here —
  those are D3's population, and AC-4 judges them.

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
- FRE-1073 — ADR-0129 B8: that ADR's seam ticket, and the owner of the end-to-end row in D1's corrected split
- FRE-1230 — owns the `slm_server` restart gate, where FRE-1071's coupling actually bites; the instance of the pattern D4 generalises
- FRE-1253 — whether a seam ticket's due date should be derived from its chain's last dependent; filed separately and deliberately not folded in here
- FRE-1112 (Needs Approval) — the sibling gap in the same layer: whether a criterion's stated check is *executable* against the fixtures it names; its own body raises the checkpoint-composition question this ADR's Negative Consequences names and does not answer
- FRE-1081 — ADR-0130's seam ticket, `Approved` and parked with a 2026-10-29 due date; the unadjudicated objective that makes Option 2's in-place amendment unsafe
- ADR-0130 — Two Tiers of Acceptance Criteria (**Accepted**): D1's coverage clause and D6's dispatch check are extended by this ADR; its Option 1 rejection is the argument this ADR had to answer about its own D3
- ADR-0129 — OpenTelemetry Instrumentation, with Trace Visibility as the Acceptance Bar (**Accepted**, D6 amended 2026-08-07 and 2026-08-08): the chain whose mapping is the worked example; unchanged by this ADR
- ADR-0033 — Multi-Provider Model Taxonomy (**Accepted**): its clean-break decision is the case D4's scope test exists to preserve
- ADR-0127 — The Harness Self-Analysis Pillar (**Proposed**): the eventual home for a mechanical mapping validator (Option 4); nothing here depends on it
- ADR-0131 — Retire MASTER_PLAN for the Owner Console (**Accepted**): D4's one-writer rule, which places the mapping and the filing of provisioning tickets on the filing plane open to every session
- ADR-0136 — The Cloudflare Edge Carries HTTP, Not gRPC (**Accepted**): the protocol constraint recorded from the same FRE-1220 study
- `docs/superpowers/plans/2026-07-31-fre-1080-adr-0129-chain-rescope.md` — the committed rescope carrying FRE-1070's and FRE-1071's criteria and the mapping's D5.d row
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
natural home is dispatch rather than merge. Codex round one returned ten blocking findings against the
first draft; the two most consequential were that AC-3's population was empty by construction under
compliance, and that D1's own worked split parked a cross-child reachability property on a child — the
exact violation of ADR-0130 D1 this ADR exists to prevent. Round two verified six of those repairs and
returned five further blocking findings, the sharpest being that D4 had made the dispatch precondition
serve as the removal ticket's acceptance criterion — a criterion already true before the ticket is
dispatched, which ADR-0130 D6 would reject at the label gate. All are fixed here; D4 now separates the
precondition from the criterion explicitly.
