# ADR-0135: The Explore Seat's Working Contract — Findings Carry Evidence, Proposals Carry Feasibility, and the Gate Sits at the Exit

**Status:** Accepted
**Date:** 2026-08-07
**Deciders:** Owner (design intent + adjudication), adr session (authoring)
**Tags:** process, delivery-loop, seats, evidence, dispatch, model-routing

---

## Context

**What is the issue we're addressing?**

The `cc-explore` seat is the project's deliberation space. It has a priming skill (`prime-explore`)
and no working skill. Three of the four seats have both: `prime-master` with `master`, and `build`
and `adr` with their own. Explore has half a pair.

The consequence is that a study arrives as a ticket with no contract telling the seat how to execute
it, what shape the deliverable takes, or how results hand back. Two studies have completed —
FRE-1116 and FRE-1131 — and both worked because their ticket bodies carried that scaffolding by
hand, one commission at a time. A third, FRE-1183, was filed 2026-08-07 and has not run: it is
`Approved` with no stream label, so there is no route from the board to the seat.

The owner's framing, recorded because it is the design intent this ADR serves:

> "I appreciate explore's researching externally, thinking outside the box, but its proposals then
> need to take into account the actual state of the project. It needs the same exigence as the 3
> other skills — and needs to be gated by you."

and, on being asked whether "an extension of master" was the right reading:

> "explore is an extension of me, with the clarity of project of Master and its exigence verify its
> proposals are possible."

That second answer is load-bearing and it changes the design. Explore is an extension of **the
owner**. What master supplies is not command but the two things master uniquely holds: **project
clarity** and **exigence** — and the exigence has a specific target, *are these proposals possible*.

### The premise this ADR had to correct, and then correct again

FRE-1184 states the failure as: explore "emits conclusions where it should emit findings plus their
measurement, and nothing downstream forces the difference."

The artifact does not support that.
`docs/research/2026-08-03-fre-1131-context-memory-alignment-audit.md` carries a Method appendix
(line 245) stating the exact discipline that would have prevented its own error:

> "event/field names resolved against code and one raw document before trusting any zero (**three
> zeros in this session were name artifacts**)"

It named the trap, applied the discipline three times, and missed a fourth. It queried the event
name `within_session_compressed` — which has never existed in `agent-logs` — and read the honest
zero it got back as "the within-session hard gate never fires." The real emits are
`within_session_compression_hard_trigger` / `_recorded`, whose true counts are 260 all-time / 2 since
2026-07-20. The wrong conclusion reached a merged research document (PR #803) and required an
erratum (PR #804).

So an evidence obligation is the answer that *looks* right: a rule saying "measure, do not infer" is
satisfied by a real query against a wrong event name. The same discipline was already a standing
memory rule — *a negative result is probably your instrument* — and it was already in the seat's own
appendix. Neither is auditable per finding.

**The second correction, which the first draft of this ADR got wrong and Codex review caught.** The
obvious repair is a *positive control*: alongside a zero, show the same query shape returning
non-zero. That repair does not work here, and the audit itself proves it. The bad zero and a
same-index control sit **in the same table** (research doc lines 41–47):

| Mechanism | Fire event | Count since 07-23 |
|---|---|---|
| B — within-session hard gate (ADR-0061) | `within_session_compressed` | **0** |
| D's per-turn evaluator (FRE-944) | `cache_reset_decision` | **94** |

The appendix even names the second row as the "same-index oracle." An identical `_count` shape,
against the identical index, returning 94 — and the finding is still wrong. **A liveness control
validates the store; the failure was in the target identifier.** Any rule built on store-liveness
admits this exact error.

The failure class is therefore narrower and more specific than "unevidenced conclusions": it is a
**negative result read off an unvalidated target identifier**. That is what D3 must catch, and it
determines the rule's granularity.

### Two facts that further narrow the problem

**Filing authority is already narrower in practice than FRE-1184 assumes.** FRE-1131's commission
said "a filed ticket for each finding that warrants one." The seat wrote instead: *"Draft tickets
(for the owner to route — this seat files nothing)"* (line 228). It declined the authority it was
given.

**The exit gate already exists physically.** Explore cannot commit, so PR #803 and the #804 erratum
were opened by master on explore's text. Master already stands at the exit of every explore
deliverable. It does not lack a *moment*; it lacks an *obligation* at a moment it already occupies.

### What must be decided

The gate's location; what an explore working skill contains; whether explore's filing authority
narrows; and what master's exit obligation is, stated so it has a trigger. Two tensions must be
resolved rather than assumed away: master's context is the scarce resource explore exists to
protect, and a thick brief anchors the one seat whose value is being able to challenge master's
frame — FRE-1116 reframed the recall programme precisely because it was not anchored.

---

## Decision

### D1 — Explore is an extension of the owner. Master supplies clarity and exigence, at the exit.

Explore is not a subordinate of master and does not become one. The principal is the owner. Master
contributes project clarity (which it uniquely holds) and exigence aimed at one target: whether
explore's **proposals** are possible against the real state of the project.

This settles the gate's location: **the exit, not the brief.** Master does not brief studies in.
Guidance already happens at *commission* time, in the ticket body — master wrote FRE-1183's body
itself — and that cost is paid once, durably, by whoever files, not by master's live context at
dispatch. There is no second brief to pay for.

**Commissions state the attack, never the answer.** A commission may specify method, substrate and
candidate failure modes at any length; it may not specify the expected conclusion. FRE-1183 is the
exemplar: "attack the method, not confirm it… it fails if it confirms the method."

*This is an accepted asymmetry, not a claim of neutrality* — see Consequences, where the anchoring
cost is stated rather than defined away.

### D2 — Two obligations, split by object

| | Whose | On what | Discharged |
|---|---|---|---|
| **Findings carry evidence** | explore | each measured claim | at emit, in the document |
| **Proposals carry feasibility** | master | each recommendation | at the exit gate |

Collapsing these is what made "add an evidence obligation" look like the whole answer. They are
different checks: a finding can be perfectly evidenced and its proposal still impossible. FRE-1131
is the case in point — its findings survived the erratum nearly intact, and its seven draft tickets
were never reconciled against what was already in flight by anyone.

### D3 — A negative finding is inadmissible without target-identifier provenance

**A finding whose verdict is negative — zero, never, does not fire, cannot engage, does not
compose — is inadmissible unless it carries, on that finding, both:**

1. **Target-identifier provenance (the arm that catches FRE-1131).** The queried identifier — the
   event name, field, label, node property, column — is shown to be the one the *producer* actually
   writes: a cited emit site at a stated revision, **or** a raw document / row exhibiting that
   identifier. A name that appears only in the query is unvalidated, and a zero read off it carries
   no information.
2. **Path liveness.** The same query shape, against the same store, returning non-zero for a case
   known to exist — establishing the path is readable and the filter well-formed.

Both arms are stated per finding, not once per document.

**Arm 2 alone admits the FRE-1131 error** — the audit supplied exactly such a control, in the same
table, and the finding was still wrong. Arm 1 is the discriminating one; arm 2 is retained because
it catches a different failure (an unreachable index, a malformed filter) at negligible cost.

A finding whose verdict is **positive** ("this value is 260") needs neither arm: a wrong identifier
cannot produce a non-zero, so the result is self-validating.

Where arm 1 cannot be satisfied — no producer can be located, no raw instance exists — the verdict
is **UNVERIFIABLE**, a first-class result, never silently equivalent to a negative.

**Why this is a rule and not a restatement of the discipline that already failed.** FRE-1131's
appendix asserted identifier resolution *globally*, for the document, and the seat believed it had
complied because it had complied three times. Nothing attached the obligation to the fourth finding,
so nothing made its absence visible. D3 relocates the obligation from a **global claim** to a
**per-finding artifact**: row B of that table carries no provenance for `within_session_compressed`,
and that absence is legible on the row itself, to a reviewer who knows nothing about compaction.
The change is auditability, not stringency.

### D4 — An `/explore` working skill, at the same rigor as `build` and `adr`

The skill states, as contract rather than habit:

- **Read-only on everything operational**: never merges, deploys, mutates the Linear *control*
  plane, writes the owner console, labels dispatch, rebuilds the gateway, or touches `main`. Two
  bounded exceptions, D5's and D6's.
- **Measure against the live system; never reason from code.** The discipline FRE-1116's ticket
  named becomes contractual.
- **D3's admissibility rule**, with the UNVERIFIABLE verdict.
- **A fixed deliverable shape** — per finding: the verdict · the query · its actual output · and,
  where the verdict is negative, D3's two arms. Plus a method appendix, and a **Proposals** section
  (D6) that is the single place recommendations appear.
- **The durable substrate map lives in the skill, not in any brief** — `_count` not `_cat`; ES counts
  provisional per FRE-1051; per-call series authority is Postgres `api_costs`, not Elasticsearch (the
  ES per-call emit has been dark since 2026-05-10); config read from the running process, not the
  repo; deployed code identified by container file hash, not board state.

That last bullet is how briefs stay thin without anyone promising they will be: everything reusable
is factored out of the brief into a durable artifact that costs master nothing per study.

### D5 — Explore files to `Backlog` only; master promotes, never re-files

Explore may create `Backlog` tickets and post comments. It may **not** create `Needs Approval`
tickets, and may not promote its own. A proposal reaches `Needs Approval` only through master's D6
disposition.

**When a proposal is dispositioned `feasible`, master promotes the existing `Backlog` ticket in
place** — it never files a second ticket for the same proposal. A duplicate would put the same
finding on the board twice under different ids, which is precisely the board-drift this project
already treats as a defect.

This codifies what the seat already chose for itself, and it preserves ADR-0131 D1's cheap path.
It does, however, put an *actionable* item in `Backlog`, which the current ticket-state rule reserves
for non-actionable findings — so that rule is amended explicitly (amendment 6) rather than bent.

### D6 — Master's exit trigger is the explore research-document PR

Explore commits its research document to its own branch and opens the PR itself — a bounded
relaxation of the drafting rule, scoped to `docs/research/<date>-fre-XXXX-<slug>.md` on a branch
named `explore-fre-XXXX-<slug>`, and nothing else. It still never merges. This gives explore the
same shape as `build` and `adr` (the "same exigence as the 3 other skills" the owner asked for) and
removes master's transcription cost, which under the old drafting rule bought nothing.

**At that PR, master dispositions every recommendation the document makes**, each as exactly one of:

- **feasible** — possible against current project state; master promotes its `Backlog` ticket;
- **infeasible** — with a stated reason drawn from project state (already in flight, contradicted by
  a shipped decision, blocked by an unbuilt dependency, outside the project's envelope). "Too many
  proposals" is not a reason;
- **owner's call** — a genuine decision, routed to the owner **with a recommendation**, per the
  Decision-Support Doctrine's prohibition on false choices.

Silence is not a disposition. The disposition lands as a comment on the commissioning ticket (the
durable record channel, per lifecycle-rules § Comment channels), and carries a one-line **basis**
declaring what master read to produce it: *proposals only* · *proposals + named sections* · *full
document* · *re-measured, naming what*.

**A study states at most 10 proposals.** Overflow is inadmissible — the document consolidates before
it merges. FRE-1131 stated seven, so the cap is calibrated on real work rather than invented. This is
the actual bound on adjudication cost; "however many explore is willing to recommend" is not a bound
and the first draft of this ADR was wrong to present it as one.

**What is bounded and what is not, stated plainly.** Adjudication is bounded — at most 10 items, each
dispositioned from project state. **Master's *read* of the document is not bounded by this ADR**, and
that cost is accepted: master already read #803 to gate it, and gating a PR without reading it is not
an option. The claim here is narrower than the first draft's: the expensive half is bounded, the
cheap half is not, and the `basis` line makes which half master actually paid visible instead of
assumed.

**The failure signal is master re-measuring.** If a disposition requires re-deriving a finding's
measurement, the split has failed and master is paying the study's cost twice. That is what the
`basis` line's fourth value records, and what AC-4 adjudicates.

### D7 — Explore becomes a dispatch stream: `stream:explore`

Explore work is ticket-shaped in practice — FRE-1116, FRE-1131 and FRE-1183 are all tickets, and
none was a free-text brief. FRE-1183 sits `Approved`, High priority, with no stream label, which
makes it approved-but-unreachable.

`stream:explore` joins `stream:build1` / `stream:build2` / `stream:adr` in the dispatch resolver and
the launcher's stream topology, pointing at the explore worktree, the `cc-explore` seat, and an
`/explore` command. Explore thereby inherits the **busy guard** (one study at a time) and the
**priority queue** — neither of which a free-text task mode provides.

**The busy guard requires a pickup transition, and explore performs it.** The guard reads
`In Progress` / `In Review`, so a stream whose ticket never leaves `Approved` can be re-dispatched
under itself. Explore therefore exercises lifecycle-rules D4's existing named delegation — *a working
session moves its own ticket to `In Progress` at pickup* — which already exists for `build` and `adr`
and needs no new grant, only the statement that explore is a working session for its purposes.

**FRE-977 is re-scoped, not superseded**: its objective (master can dispatch explore without a manual
reset-prime-paste) is met by the stream entry rather than by a free-text task mode. Free-text
injection is retained unchanged for master's `[from master, re …]` questions, which is the case it is
genuinely good at.

**The dispatcher may now target `cc-explore`. The watcher covers explore's own PR only.** Explore now
opens PRs, so its PRs can go red, and a red-CI path with no owning seat is the "no safety net"
failure lifecycle-rules already names for master's own PRs. The watcher therefore routes red CI on an
`explore-fre-*` PR to `cc-explore`, exactly as it does for a worker branch. It still never gates
explore as a *worker* in the master-ready sense — a docs PR at master's gate is a disposition, not a
merge-readiness signal.

### D8 — Fable enters the model-routing vocabulary, gated in the launcher

Fable (`claude-fable-5`) is the model both completed explore studies ran on, and it appears nowhere
in `.claude/MODEL_ROUTING_POLICY.md` (three tiers: Opus / Sonnet / Haiku) or in the launcher's
validated model vocabulary. It becomes an available option, and **every selection requires the
owner's approval.**

**The gate is a launcher refusal, not an honour system.** The launcher accepts `--model fable` **only
with an explicit approval argument naming where the owner granted it** (the trust-ladder row, or the
ticket comment carrying the per-dispatch approval); without it, the launcher refuses and launches
nothing. A census of past dispatches cannot fail in a useful way — a refusal can, and it can be
asserted by a test.

Master's own session model is set by the owner typing `/model`, which *is* the approval; master has
no mechanism to switch itself. The machine-selectable path is the launcher, which is why the gate
lives there.

**The trust-ladder row, if the owner wants one, is the owner's to write** (ADR-0131 D2: master
transcribes, never authors; D3: a grant exists iff the console records it). The implementation ticket
surfaces the row for the owner and does not write it. Until then, every Fable selection is an
explicit per-dispatch ask.

---

## Contract amendments — the sentences this ADR replaces

FRE-1184 named two. There are **eight**; the extra six are named here so each change is deliberate
rather than drift. Codex review caught five of them.

| # | File · current sentence | Replacement |
|---|---|---|
| 1 | `lifecycle-rules.md` § Explore session: "It exists so deep strategy/methodology deliberation happens **off master's context**, and so discussion can never accidentally actuate." | "It exists so deep strategy/methodology **deliberation** happens **off master's context**, and so discussion can never accidentally actuate. **Adjudication is the deliberate exception**: master dispositions explore's *proposals* at the exit gate (ADR-0135 D6), capped at ten per study and produced from project state, never by re-measuring a finding." |
| 2 | § Explore session: "…it never merges, deploys, **mutates Linear**, writes the owner console, labels dispatch, rebuilds the gateway, or touches `main`." | "…it never merges, deploys, **mutates the Linear control plane** (beyond moving its own ticket to `In Progress` at pickup, D4's existing delegation), writes the owner console, labels dispatch, rebuilds the gateway, or touches `main`. It **files `Backlog` tickets and comments** (ADR-0135 D5) and **commits its research document to its own branch and opens that PR** (D6)." |
| 3 | § Explore session: "**Injection is owner-hubbed, never autonomous** — master and explore coordinate through the durable substrate **+ the owner**; they never auto-talk to each other, and a human is always at one end" | Unchanged in substance, extended: "…a human is always at one end. **Explore is an extension of the owner, not of master** (ADR-0135 D1); master supplies project clarity and exigence at the exit, never command. **Ticket dispatch is not injection** — a `stream:explore` ticket reaching the seat is board-routed work the owner approved when they approved the ticket." |
| 4 | § Explore session: "- **explore → master / adr (owner-gated):** at the owner's request, `send-keys` the result to `cc-master` … Only on the owner's say-so — never on your own initiative." | Unchanged for `send-keys`, plus: "**The research-document PR is not a send-keys injection and needs no owner say-so** (ADR-0135 D6): it is explore's own deliverable on its own branch, and opening it is what triggers master's disposition. The owner-gated rule governs *pushing a conclusion into another seat's context*, which a PR does not do." |
| 5 | § Explore session: "The watcher/dispatcher never target `cc-explore` (not a worker, not a gate)." | "The **dispatcher** targets `cc-explore` via `stream:explore`, with the same busy guard and priority ordering as any stream (ADR-0135 D7). The **watcher** routes red CI on an `explore-fre-*` PR to `cc-explore`, so explore's own PRs are not left without a safety net; it never treats explore as a master-ready gate." |
| 6 | § Ticket state: "**`Backlog` is the filing state for a non-actionable finding**: a measurement, an observation, a shape to adopt later, an unticketed defect nobody is proposing to fix now." | "**`Backlog` is the filing state for a finding nobody is currently proposing to act on** — a measurement, an observation, a shape to adopt later, an unticketed defect — **and for an explore proposal that has not yet passed master's feasibility disposition** (ADR-0135 D5). The second case is actionable-in-principle and deliberately parked: it has not met project reality, and promoting it is master's D6 act, performed on the existing ticket rather than by re-filing." |
| 7 | § Coordination stores D4 table: "Linear **filing plane** — ticket creation (`Needs Approval` / `Backlog`), comments \| open to every session" | "…\| open to every session — **except `cc-explore`, which files `Backlog` and comments only, and never promotes its own** (ADR-0135 D5)." |
| 8 | `prime-explore` SKILL: "If a conclusion needs executing, it reaches master **through the owner**, never your own hand. A scratch notebook in your own scratchpad (outside the repo) is fine; anything that lands in the repo or on the board is drafted as text for the owner or master to route." | "If a conclusion needs **executing**, it reaches master through the owner, never your own hand. You do commit your **research document** to your own branch and open its PR (ADR-0135 D6) — `docs/research/<date>-fre-XXXX-<slug>.md` on `explore-fre-XXXX-<slug>`, and nothing else — because proposing is not executing and master's disposition is the gate. You never merge. On the board you file `Backlog` tickets and comments (D5); anything beyond that is drafted as text for the owner or master to route." |

---

## Obligation → owner mapping

Every obligation the Decision section places on the chain lands on **exactly one** child or on the
seam. Children carry criteria for their own work; the seam carries the ADR's (ADR-0130 D1/D2).

| Obligation | Owner |
|---|---|
| D3's two-arm rule stated per finding; UNVERIFIABLE verdict | child: `/explore` skill |
| D2 evidence shape for **positive** findings (query + actual output) | child: `/explore` skill |
| D4 read-only scope, live-measurement rule, substrate map | child: `/explore` skill |
| D5 `Backlog`-only filing, never self-promote | child: `/explore` skill |
| D6 branch/path write scope, PR mechanics, ≤10 proposals | child: `/explore` skill |
| Amendments 1–7 (lifecycle-rules) | child: contract-amendment ticket |
| Amendment 8 (`prime-explore`) | child: contract-amendment ticket |
| D6 disposition step, three values, `basis` line, promote-not-refile | child: master SKILL ticket |
| D7 resolver stream, launcher topology, busy-guard pickup, watcher red-CI routing | child: dispatch ticket |
| D8 launcher refusal without approval, routing-policy row, ladder row surfaced to owner | child: Fable ticket |
| **That the rule actually rejects wrong negatives** (AC-1, AC-2) | **seam** |
| **That proposals are dispositioned, and from project state** (AC-3, AC-4) | **seam** |
| **That a study reaches the seat by dispatch** (AC-5) | **seam** |
| **That no finding reached `Needs Approval` before disposition** (AC-6) | **seam** |
| **That Fable cannot be selected unapproved** (AC-7) | **seam** |

The split is consistent throughout: a child owns *stating and building* the mechanism; the seam owns
*whether the mechanism produced the outcome*. No obligation appears in both columns and none appears
in neither.

---

## Alternatives Considered

### Option 1: Gate at the brief — master briefs each study in and reconciles it out

**Description:** The literal reading of "explore should be an extension of master": master composes a
brief per study, injects it, and reconciles the result.

**Pros:**
- Directly satisfies "proposals must account for project state" by transmitting that state up front.
- One actor owns the study end to end.

**Cons:**
- Master pays context twice — the exact cost explore exists to avoid.
- Anchors. A brief transmits master's frame, and master's frame is what a fresh study must be able to
  challenge. FRE-1116 reframed the recall programme *because* it was unanchored.

**Why Rejected:** The owner resolved it directly — explore is an extension of *the owner*, and
master's contribution is clarity and exigence, not command. Independently, the brief is redundant:
guidance already happens in the commissioning ticket body, paid once by whoever files.

### Option 2: A stronger evidence obligation, and no working skill

**Description:** Require each explore finding to carry not just its measurement but its
target-identifier validation — the same substance as D3 arm 1 — expressed as a method obligation in
the commissioning ticket, and change nothing else.

**Pros:**
- Smallest diff that actually addresses the failure class. Unlike a bare "attach a measurement," this
  version is *not* refuted by FRE-1131 — it is the obligation FRE-1131's own appendix stated.
- No new skill to maintain, no invariants amended, no expansion of explore's authority.

**Cons:**
- **It is stated globally and cannot be audited per finding.** This is the whole difference, and it
  is narrow: FRE-1131's appendix asserted exactly this obligation, the seat believed it had complied
  because it had complied three times, and nothing made the fourth finding's non-compliance visible.
  A commissioning ticket can restate the obligation; it cannot attach it to a row.
- Leaves the proposal-feasibility gap — the owner's actual complaint — entirely unaddressed. Findings
  and proposals are different objects (D2).
- Leaves FRE-1183 unreachable and the next study's scaffolding hand-written again.

**Why Rejected:** Not because a strong evidence obligation is unworkable — the substance of D3 arm 1
*is* that obligation. It is rejected because a global claim is unauditable where a per-finding
artifact is checkable, and because it answers one of the four decisions and none of the other three.
Stated honestly: this is the closest alternative, and the margin over it is auditability, not rigor.

### Option 3: Uniform review — route every explore document through `codex:rescue`, as `adr` does

**Description:** Give explore the same adversarial-review obligation the `adr` session carries.

**Pros:**
- Symmetric with `adr`; adversarial review is genuinely the class of thing that catches a wrong
  negative — as this ADR's own review demonstrates.
- No new rule to invent.

**Cons:**
- An external reviewer without substrate access cannot verify a query result. It can check whether a
  finding carries provenance — but that is D3, and D3 is cheaper.
- Uniform cost on every study, including the many findings that are self-validating positives.

**Why Rejected:** Mis-targeted as a *replacement*. The FRE-1128 precedent is to route an obligation by
class rather than uniformly, and the discriminating class here is the negative finding. Adversarial
review remains available to the seat; it is not made a per-study tax.

### Option 4: Leave explore as it is; fix only the dispatch gap

**Description:** Build FRE-977 as approved, change no contract.

**Pros:** Cheapest; unblocks FRE-1183 immediately.

**Cons:** The seat keeps no stated contract, so the next study's scaffolding is hand-written again,
and the erratum class of failure has nothing standing against it.

**Why Rejected:** It solves the routing problem and none of the stated one.

---

## Consequences

### Positive Consequences

- The wrong-negative failure class has a guard pitched at the granularity that actually failed — the
  target identifier — and stated per finding, where its absence is legible.
- The owner's directive is satisfied without inverting explore's relationship to master and without
  eroding owner-hubbed injection.
- Explore reaches parity with the other three seats: a working skill, a dispatch stream, a busy
  guard, a PR of its own, and a watcher safety net for that PR.
- FRE-1183 becomes reachable.
- The substrate map stops living in ticket bodies and becomes durable.
- Fable's gate is a refusal a test can assert, not a convention.

### Negative Consequences

- **Master's read cost is unbounded and accepted.** Only adjudication is capped (D6). The `basis`
  line makes the split visible; it does not shrink the read.
- **The anchoring tension is accepted, not dissolved.** "Commission the attack" *is* directional: it
  names a favoured direction and declares confirmation a failure. The argument for accepting it is an
  epistemic asymmetry, not neutrality — a commission that says *confirm X* and returns X is
  uninformative, while one that says *attack X* and fails to break X is informative. A commission can
  still anchor by choosing which failure modes to enumerate, and this ADR does not fix that. The
  mitigation is that the seat may return "the enumerated modes were the wrong ones," which
  FRE-1116 effectively did.
- **Explore gains repo write access and a self-opened PR**, narrowly scoped. A deliberate expansion of
  a previously draft-only seat, accepted because merging — the operational act — stays with master.
- **`Backlog` now holds two different things** (amendment 6): parked non-actionable findings, and
  actionable-but-undispositioned explore proposals. The state is doing more work than before.
- **A fifth contract document to keep true**, on a project already carrying drift risk across four.
- **D3 can be evaded by phrasing** — a negative verdict written as a positive statement ("the
  mechanism is idle") reads past a keyword scan. The skill states the test by *semantics*, and AC-1's
  enumeration is by substance, not by wording.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A provenance citation is supplied but stale — the emit site moved since the cited revision | Medium | Arm 1 requires the revision be stated, so staleness is visible; the alternative arm (a raw document exhibiting the identifier) is revision-free |
| Master rubber-stamps every proposal `owner's call` | Medium | `owner's call` must carry a recommendation (Decision-Support Doctrine); AC-3 checks for one |
| Master re-measures privately and omits it from the comment | Medium–High | Only partially mitigable: the `basis` line makes the honest case cheap to declare, and AC-4 states its own observability limit rather than pretending to close it |
| The dispatcher resets a live explore context holding owner deliberation | Medium | The busy guard blocks dispatch while the study is `In Progress` (D7's pickup transition is what makes the guard real) |
| Fable is selected without approval | Low | The launcher refuses without an approval argument; AC-7 asserts the refusal by test rather than auditing a census |

---

## Implementation Notes

**Files affected:**
- `.claude/skills/explore/SKILL.md` — new
- `.claude/skills/lifecycle-rules.md` — amendments 1–7
- `.claude/skills/prime-explore/SKILL.md` — amendment 8
- `.claude/skills/master/SKILL.md` — D6 disposition step
- `scripts/dispatch/launcher.py` — `explore` topology entry; `fable` in the model vocabulary behind
  the D8 approval argument
- `scripts/dispatch/next_resolver.py` — `explore` as a resolvable stream
- `scripts/dispatch/gating_watcher.py` — `explore-fre-*` red-CI routing to `cc-explore`
- `.claude/MODEL_ROUTING_POLICY.md` — Fable row
- `docs/plans/OWNER_CONSOLE.md` — **owner-written**; the Fable ticket surfaces the ladder row for the
  owner and never writes it (ADR-0131 D2)

**Dependencies:** none external. FRE-977 is re-scoped into this chain rather than run separately.

**Testing strategy:** the dispatch and launcher changes extend the existing flat test modules
`tests/scripts/test_launcher.py` and `tests/scripts/test_next_resolver.py` (there is no
`tests/scripts/dispatch/` package). D8's refusal is a unit test: `--model fable` without the approval
argument must exit non-zero and launch nothing. The contract changes are process documents,
adjudicated by the seam against real studies.

---

## Verification / Acceptance Criteria

These are the **ADR's own** criteria, asserted in exactly one place — the seam ticket named below —
and never sliced across the chain (ADR-0130 D1).

**Window.** The window opens when the `/explore` skill lands and closes at adjudication; it must
contain **at least two merged explore research documents**. Each criterion additionally names what
must be *exercised* for it to be adjudicable — a study that exercises nothing cannot make a criterion
green by empty enumeration. If the window is unexercised for a criterion, that criterion is
**inconclusive**, never green, and ADR-0135 stays `Accepted`.

**Window reset.** A red verdict produces a remediation ticket (master, per lifecycle-rules § Ticket
state › Seam tickets) and the seam re-dates with a **fresh window opening at that remediation's
merge**. A historical failure therefore does not poison every later census — which the first draft of
this section did.

- **AC-1** — Every negative finding in the window carries **both** D3 arms on that finding.
  · **Check:** in each merged explore document, enumerate findings whose verdict is negative *in
  substance* (zero / never / does not fire / cannot engage / does not compose, however phrased); for
  each, confirm (i) a provenance citation naming an emit site with its revision, or a quoted raw
  instance exhibiting the identifier, **and** (ii) a liveness control with its actual non-zero output.
  · *Exercised by:* ≥1 negative finding in the window. · *Fails if* any negative finding is missing
  either arm — **including one carrying only a same-store liveness control, which is exactly the
  shape that produced the erratum.**

- **AC-2** — The rule as written rejects the erratum's finding, and a liveness-only rule does not.
  · **Check:** apply the `/explore` skill's rule verbatim to FRE-1131 §F1 row B
  (`within_session_compressed` = 0) as it stood in PR #803. It must be **inadmissible for want of arm
  1**. Then apply a liveness-only variant to the same row: with row D's `cache_reset_decision` = 94
  present in the same table and the same index, it must come out **admissible**.
  · *Exercised by:* the skill landing; no study needed. · *Fails if* the rule admits row B, **or** if
  the liveness-only variant also rejects it — the second half is the discriminating test, because a
  rule that rejects everything proves nothing about what makes this one work.

- **AC-3** — Every recommendation in the window carries a disposition of the required shape.
  · **Check:** for each merged document, enumerate recommendations **wherever they appear**, not only
  under the Proposals heading; each maps in the commissioning ticket's comments to exactly one of
  `feasible` (naming the promoted ticket id), `infeasible` (with a project-state reason — "too many"
  is not one), or `owner's call` (**carrying a recommendation**). Confirm the count is ≤10 and that no
  `feasible` disposition created a second ticket for a proposal explore had already filed.
  · *Exercised by:* ≥1 recommendation in the window. · *Fails if* any recommendation is
  undispositioned, sits outside the three values, is an `owner's call` with no recommendation, or is
  a `feasible` that re-filed rather than promoted.

- **AC-4** — Master dispositioned from project state, not by re-measuring. · **Check:** every
  disposition comment carries a `basis` line; none declares `re-measured`; and no disposition's text
  contains a measurement master ran to re-derive the finding its proposal rests on (citing the board,
  an ADR, git or an in-flight ticket is project state and is expected). · *Exercised by:* ≥1
  disposition in the window. · *Fails if* a `basis` line is absent, or declares `re-measured`, or the
  text contradicts the declared basis. **Stated limit:** an undeclared private re-measurement is not
  observable from any record this project keeps. This criterion detects *declared and evidenced*
  re-derivation only, and the residue is a surfaced design smell, not a closed one — the honest
  reading of a green AC-4 is "nothing recorded says the split failed," not "the split held."

- **AC-5** — A study reached the seat by dispatch, delivered. · **Check:**
  `python -m scripts.dispatch.next_resolver --stream explore --json` resolves a real `Approved` +
  `stream:explore` ticket; and the orchestrator's `dispatch_execute` record (not the pre-execution
  `dispatch_plan` card, which is rendered before delivery is attempted) shows a **successful**
  delivery to the explore topology for ≥1 study in the window, whose ticket then moved to
  `In Progress`. · *Exercised by:* ≥1 dispatched study. · *Fails if* the resolver rejects `explore`,
  or no `dispatch_execute` record shows a successful explore delivery, or the ticket never left
  `Approved` — which would also mean the busy guard was inert.

- **AC-6** — No explore finding reached `Needs Approval` before its disposition. · **Check:** for each
  merged document, take the tickets named in master's disposition comment plus any ticket whose body
  reproduces one of the document's findings; for each, read its **state history** and confirm the
  transition into `Needs Approval` (not merely its creation state) is later than the disposition
  comment. · *Exercised by:* ≥1 promoted proposal. · *Fails if* any such ticket entered
  `Needs Approval` before the disposition — including one created in `Backlog` and promoted moments
  later, which the first draft's creation-state check would have passed.

- **AC-7** — Fable cannot be selected without a recorded approval. · **Check:** run the launcher with
  `--model fable` and **no** approval argument: it must exit non-zero and launch nothing (a unit test
  asserts this). Then run it with the approval argument: it must launch. Then enumerate every Fable
  dispatch in the window and confirm each names its approval source. · *Exercised by:* the launcher
  test; the census is additional, not the primary proof. · *Fails if* the unapproved invocation
  launches, or the approved one is refused, or any dispatch ran at Fable naming no approval source. A
  ladder row master wrote itself is not an approval source (ADR-0131 D2).

**Seam ticket:** FRE-1195 — *ADR-0135 seam — adjudicate the explore working contract against real
studies*. Filed parked (`Backlog`), **due 2026-09-15** — the earliest date at which the chain can have
landed and two studies can have run end to end through dispatch, emit, PR and disposition. AC-2 and
AC-7 are adjudicable as soon as the chain lands; the rest need the window.

---

## References

- [ADR-0130](ADR-0130-two-tiers-of-acceptance-criteria.md) — Two Tiers of Acceptance Criteria (D1 severs criterion inheritance; D2 mandates the seam ticket this ADR files) — *Accepted*
- [ADR-0131](ADR-0131-retire-master-plan-owner-console.md) — Owner Console, Trust Ladder, One Writer per Store (D1 the cheap `Backlog` path D5 preserves; D2 why the Fable ladder row is owner-written; D4 the filing-plane row amendment 7 edits, and the own-ticket pickup delegation D7 relies on) — *Accepted*
- [ADR-0113](ADR-0113-self-driving-delivery-loop.md) — Self-Driving Delivery Loop (§1: dispatch mechanics live in the external resolver, not in a skill — D7 adds a stream there, not inline) — *Superseded*
- [ADR-0116](ADR-0116-event-driven-dispatch-actuation.md) — Event-Driven Dispatch Actuation (the actuation path D7's stream entry joins) — *Accepted*
- [ADR-0117](ADR-0117-pr-gate-signal-collector.md) — Deterministic Signal Collector for the PR Gate (the precedent for a gate check that collects facts and renders no verdict) — *Accepted*
- `.claude/skills/lifecycle-rules.md` § Explore session, § Ticket state, § Coordination stores — amendments 1–7
- `.claude/skills/prime-explore/SKILL.md` — amendment 8
- `docs/research/2026-08-03-fre-1131-context-memory-alignment-audit.md` — the audit; its §F1 table (lines 41–47) carrying the bad zero and the same-index oracle side by side, which is the proof a liveness-only control is insufficient; its method appendix (line 245); its "this seat files nothing" (line 228); the 2026-08-03 correction (line 258). PR #803 and PR #804
- [FRE-1184](https://linear.app/frenchforest/issue/FRE-1184) — this ADR's commissioning ticket
- [FRE-1116](https://linear.app/frenchforest/issue/FRE-1116) — the unanchored study that reframed the recall programme; source of "measure at the answer, not at the pipeline"
- [FRE-1131](https://linear.app/frenchforest/issue/FRE-1131) — the alignment audit whose negative finding was wrong
- [FRE-1183](https://linear.app/frenchforest/issue/FRE-1183) — filed 2026-08-07 with no route to the seat; the exemplar of a commission that states the attack
- [FRE-977](https://linear.app/frenchforest/issue/FRE-977) — explore dispatch, `Approved`, re-scoped by D7
- [FRE-1128](https://linear.app/frenchforest/issue/FRE-1128) — the precedent for routing a review obligation by class rather than uniformly
- [FRE-1051](https://linear.app/frenchforest/issue/FRE-1051) — why ES counts are treated as provisional in D4's substrate map

---

## Status Updates

### 2026-08-07 - Accepted
**Changed By:** Owner (design intent), adr session (authoring)
**Reason:** Decided in session on 2026-08-07. The owner settled the principal question directly —
explore is an extension of the owner, with master supplying project clarity and exigence to verify
proposals are possible — which located the gate at the exit and left owner-hubbed injection intact.
FRE-1184's premise was corrected during discussion against the FRE-1131 artifact; a first draft's
repair (a positive control) was then corrected again at Codex review, which showed the audit's own
same-index oracle sat in the same table as the bad zero and would have satisfied it. D3 is therefore
pitched at the target identifier, which is where the failure actually was. `Implemented` awaits the
seam ticket's adjudication (FRE-1195, due 2026-09-15); the ADR does not reach it on a red or
unadjudicated criterion.
