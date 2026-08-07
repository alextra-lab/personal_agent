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
it, what shape the deliverable takes, or how results hand back. The three studies that have run —
FRE-1116, FRE-1131, FRE-1183 as filed — worked because their ticket bodies carried that scaffolding
by hand, one commission at a time.

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

### The premise this ADR had to correct before it could decide anything

The commissioning ticket (FRE-1184) states the failure as: explore "emits conclusions where it should
emit findings plus their measurement, and nothing downstream forces the difference."

The artifact does not support that. `docs/research/2026-08-03-fre-1131-context-memory-alignment-audit.md`
carries a Method appendix (line 245) stating the exact discipline that would have prevented its own
error:

> "event/field names resolved against code and one raw document before trusting any zero (**three
> zeros in this session were name artifacts**)"

It named the trap, applied the discipline three times, and missed a fourth. It queried
`within_session_compressed` — an event name that has never existed in `agent-logs` — and read the
honest zero it got back as "the mechanism never fires." The real emits are
`within_session_compression_hard_trigger` / `_recorded`, whose true counts are 260 all-time / 2 since
2026-07-20. The wrong conclusion reached a merged research document (PR #803) and required an erratum
(PR #804).

Every finding in that document carries its query. The verdict that was wrong was **measured**, not
inferred. And the same discipline is a standing memory rule — *a negative result is probably your
instrument* — that was already in force.

**So an evidence obligation is the answer that looks right and would not have caught this.** A rule
that says "measure, do not infer" is satisfied by a real query against a misspelled index. This ADR
therefore does not decide *whether* explore owes evidence; it decides **what shape of evidence is
inadmissible on its own**, which is a different and narrower question with a mechanical answer.

### Two facts that further narrow the problem

**Filing authority is already narrower in practice than the ticket assumes.** FRE-1131's commission
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

**Commissions state the attack, never the answer.** A thick commission is not automatically an
anchoring one: FRE-1183 is thick *and* anti-anchoring, because it commissions falsification —
"attack the method, not confirm it… it fails if it confirms the method." This is the rule that
resolves the anchoring tension: a commission may specify method, substrate and failure modes at any
length, and may never specify the expected conclusion.

### D2 — Two obligations, split by object

| | Whose | On what | Discharged |
|---|---|---|---|
| **Findings carry evidence** | explore | each measured claim | at emit, in the document |
| **Proposals carry feasibility** | master | each recommendation | at the exit gate |

Collapsing these is what made "add an evidence obligation" look like the whole answer. They are
different checks: a finding can be perfectly evidenced and its proposal still impossible. FRE-1131
is the case in point — its findings survived the erratum nearly intact, and its seven draft tickets
were never reconciled against what was already in flight by anyone.

### D3 — The positive-control rule: a negative finding is inadmissible without a control

**A finding whose verdict is negative — zero, never, does not fire, cannot engage, does not
compose — is inadmissible unless it carries a positive control: the same query shape, against the
same index or store, returning a non-zero result for a case known to exist.** The control's actual
output is quoted in the document alongside the finding.

A finding whose verdict is positive ("this value is 260") is self-evidencing: the query and its
output are the proof, and a misspelled field cannot produce a non-zero.

This is the one rule that catches the FRE-1131 error, and it is deliberately mechanical rather than
dispositional. It is also **checkable without re-measuring** — a reviewer asks *does this negative
finding have a control?*, which is a scan, not a re-derivation. That property is what makes the exit
gate affordable, and it is why the rule is stated as an admissibility test rather than as a
discipline to observe.

Where a control genuinely cannot be constructed, the verdict is **UNVERIFIABLE**, which is a
first-class result and never silently equivalent to a negative.

### D4 — An `/explore` working skill, at the same rigor as `build` and `adr`

The skill states, as contract rather than habit:

- **Read-only on everything operational**, unchanged from `prime-explore`: never merges, deploys,
  mutates the Linear control plane, writes the owner console, labels dispatch, rebuilds the gateway,
  or touches `main`. The one bounded relaxation is D6's.
- **Measure against the live system; never reason from code.** The discipline FRE-1116's ticket
  named becomes contractual.
- **D3's admissibility rule**, with the UNVERIFIABLE verdict.
- **A fixed deliverable shape** — per finding: the verdict · the query · its actual output · the
  positive control where the verdict is negative. Plus a method appendix, and a **Proposals**
  section that is the single place recommendations appear.
- **The durable substrate map lives in the skill, not in any brief** — `_count` not `_cat`; ES counts
  provisional per FRE-1051; per-call series authority is Postgres `api_costs`, not Elasticsearch (the
  ES per-call emit has been dark since 2026-05-10); config read from the running process, not the
  repo; deployed code identified by container file hash, not board state.

That last bullet is how briefs stay thin without anyone promising they will be: everything reusable
is factored out of the brief into a durable artifact that costs master nothing per study.

### D5 — Explore files to `Backlog` only

Explore may create `Backlog` tickets and post comments. It may **not** create `Needs Approval`
tickets. A proposal reaches `Needs Approval` only after master's D6 disposition, filed by master or
the owner.

This codifies what the seat already chose for itself. It preserves ADR-0131 D1's cheap path
(`Backlog` costs no approval bandwidth, so "file it or drop it" stays a genuine choice) while
stopping an unreconciled finding from reaching the board already wearing an actionable shape.
Approval bandwidth is the project's stated scarce resource, and D1's whole point is that explore's
proposals have not yet met project reality when they are written.

### D6 — Master's exit trigger is the explore research-document PR

Explore commits its research document to its own branch and opens the PR itself — a bounded
relaxation of the drafting rule, scoped to `docs/research/<date>-fre-XXXX-<slug>.md` on a branch
named `explore-fre-XXXX-<slug>`, and nothing else. It still never merges. This gives explore the
same shape as `build` and `adr` (the "same exigence as the 3 other skills" the owner asked for) and
removes master's transcription cost, which under the old drafting rule was pure overhead.

**At that PR, master dispositions every item in the document's Proposals section**, each as exactly
one of:

- **feasible** — possible against current project state; master files or sequences it;
- **infeasible** — with the stated reason (already in flight, contradicted by a shipped decision,
  blocked by an unbuilt dependency, out of the project's envelope);
- **owner's call** — a genuine decision, routed to the owner with a recommendation.

Silence is not a disposition. The disposition lands as a comment on the commissioning ticket (the
durable record channel, per lifecycle-rules § Comment channels).

**This is where the context-cost tension is answered, and it is answered by the deliverable's shape
rather than by a promise.** The exigence check is scoped to the Proposals section. FRE-1131 is 286
lines; its proposals are one section of seven bullets. Master reads the document to gate the PR — the
cost it already paid for #803 — but *adjudicates* only the proposals, and adjudication is the
expensive half.

**What happens when the document is long anyway:** the read cost scales with the document and the
adjudication cost does not, because proposals are bounded by what explore is willing to recommend,
not by how much it measured. If a study's proposal list is itself long enough to hurt, that is a
finding about the study — explore proposed too much — and master dispositions the excess as
`infeasible` rather than absorbing it. The failure mode this ADR refuses to accept is master
re-deriving a finding's measurement in order to disposition its proposal; **master dispositions from
project state, never by re-measuring.** If master finds it must re-measure, the split has failed and
that is the signal, not a chore to absorb.

### D7 — Explore becomes a dispatch stream: `stream:explore`

Explore work is ticket-shaped in practice — FRE-1116, FRE-1131 and FRE-1183 are all tickets, and
none was a free-text brief. FRE-1183 sits `Approved`, High priority, with no stream label, which
makes it approved-but-unreachable: there is no route from the board to the seat.

`stream:explore` joins `stream:build1` / `stream:build2` / `stream:adr` in the dispatch resolver and
the launcher's stream topology, pointing at the explore worktree, the `cc-explore` seat, and an
`/explore` command. Explore thereby inherits the **busy guard** (one study at a time) and the
**priority queue** for free — neither of which a free-text task mode provides.

**FRE-977 is re-scoped, not superseded**: its objective (master can dispatch explore without a manual
reset-prime-paste) is met by the stream entry rather than by a free-text task mode. Free-text
injection is retained unchanged for master's `[from master, re …]` questions, which is the case it is
genuinely good at.

**The dispatcher may now target `cc-explore`; the watcher still may not.** The watcher's job is red-CI
and master-ready poking on a worker's PR. Explore's PR is a docs PR whose failure modes the watcher
does not serve, so the exclusion is narrowed rather than removed.

### D8 — Fable enters the model-routing vocabulary, `ask-first`

Fable (`claude-fable-5`) is the model both completed explore studies ran on, and it appears nowhere
in `.claude/MODEL_ROUTING_POLICY.md` (three tiers: Opus / Sonnet / Haiku) or in the launcher's
validated model vocabulary. It becomes an available option — selectable for a dispatched seat and for
master itself — and **every selection requires the owner's approval.**

The mechanism exists already: the launcher takes `--model <tier>` and issues `/model <tier>` into the
seat. What is added is the vocabulary entry, a routing-policy row, and the gate.

**The gate is a trust-ladder row, and the owner writes it** (ADR-0131 D2: master transcribes, never
authors; D3: a grant exists iff the console records it). The implementation ticket surfaces the row
for the owner; it does not write it. Until the owner records it, no standing grant exists and every
Fable selection is an explicit ask.

### The invariants this ADR amends — with replacement text

The commissioning ticket named two. There are **five**; the extra three are named here so the change
is deliberate rather than drift.

| # | File · current sentence | Replacement |
|---|---|---|
| 1 | `lifecycle-rules.md` § Explore session: "It exists so deep strategy/methodology deliberation happens **off master's context**, and so discussion can never accidentally actuate." | "It exists so deep strategy/methodology **deliberation** happens **off master's context**, and so discussion can never accidentally actuate. **Adjudication is the deliberate exception**: master dispositions explore's *proposals* at the exit gate (ADR-0135 D6), scoped to the deliverable's Proposals section and never by re-measuring a finding." |
| 2 | § Explore session: "**Injection is owner-hubbed, never autonomous** — master and explore coordinate through the durable substrate **+ the owner**; they never auto-talk to each other, and a human is always at one end" | Unchanged in substance, extended: "…a human is always at one end. **Explore is an extension of the owner, not of master** (ADR-0135 D1); master supplies project clarity and exigence at the exit, never command. **Ticket dispatch is not injection** — a `stream:explore` ticket reaching the seat is board-routed work, and the owner approved it when they approved the ticket." |
| 3 | § Explore session: "The watcher/dispatcher never target `cc-explore` (not a worker, not a gate)." | "The **dispatcher** targets `cc-explore` via `stream:explore`, with the same busy guard and priority ordering as any stream (ADR-0135 D7). The **watcher** still never targets it: explore's deliverable is a docs PR, whose red-CI and master-ready paths the watcher does not serve." |
| 4 | § Coordination stores D4 table: "Linear **filing plane** — ticket creation (`Needs Approval` / `Backlog`), comments \| open to every session" | "…\| open to every session — **except `cc-explore`, which files `Backlog` and comments only** (ADR-0135 D5). A proposal reaches `Needs Approval` after master's exit disposition, filed by master or the owner." |
| 5 | `prime-explore` SKILL: "A scratch notebook in your own scratchpad (outside the repo) is fine; anything that lands in the repo or on the board is drafted as text for the owner or master to route." | "You commit your **research document** to your own branch and open its PR (ADR-0135 D6) — `docs/research/<date>-fre-XXXX-<slug>.md` on `explore-fre-XXXX-<slug>`, and nothing else. You never merge. Anything else that would land in the repo, and anything on the board beyond a `Backlog` ticket or a comment, is drafted as text for the owner or master to route." |

---

## Alternatives Considered

### Option 1: Gate at the brief — master briefs each study in and reconciles it out

**Description:** The literal reading of "explore should be an extension of master": master composes a
brief per study, injects it, and reconciles the result.

**Pros:**
- Directly satisfies "proposals must account for project state" by transmitting that state up front.
- One actor owns the study end to end.

**Cons:**
- Master pays context twice — the exact cost explore exists to avoid. lifecycle-rules states
  explore's purpose as deliberation happening off master's context.
- Anchors. A brief transmits master's frame, and master's frame is what a fresh study must be able to
  challenge. FRE-1116 reframed the recall programme *because* it was unanchored.

**Why Rejected:** The owner resolved it directly — explore is an extension of *the owner*, and
master's contribution is clarity and exigence, not command. Independently, the brief is redundant:
guidance already happens in the commissioning ticket body, paid once by whoever files.

### Option 2: Add an evidence obligation; write no working skill

**Description:** The minimum change implied by FRE-1184's framing — require each explore finding to
carry its measurement, and change nothing else.

**Pros:**
- Smallest possible diff; no new skill to maintain.
- Preserves every existing invariant untouched.

**Cons:**
- **It would not have caught the failure it is proposed for.** The FRE-1131 document already carried
  a method appendix stating that discipline, applied it three times, and shipped a wrong negative
  anyway. So did the standing memory rule.
- Leaves the proposal-feasibility gap — the owner's actual complaint — entirely unaddressed.
- Leaves FRE-1183 unreachable; leaves the next study's scaffolding to be hand-written again.

**Why Rejected:** Falsified by the artifact. A stated discipline is satisfied by a real query against
a misspelled index; only an admissibility rule with a positive control is not.

### Option 3: Uniform review — route every explore document through `codex:rescue`, as `adr` does

**Description:** Give explore the same adversarial-review obligation the `adr` session carries.

**Pros:**
- Symmetric with `adr`; adversarial review is genuinely the class of thing that catches a wrong
  negative.
- No new rule to invent.

**Cons:**
- An external reviewer without substrate access cannot verify a query result; it would review the
  *prose*, which is exactly the layer that read fine in FRE-1131.
- Uniform cost on every study, including the many findings that are self-evidencing positives.

**Why Rejected:** Mis-targeted. The FRE-1128 precedent is to route an obligation **by class** rather
than uniformly, and here the discriminating class is the *negative* finding. D3 puts the burden
exactly there and nowhere else.

### Option 4: Leave explore as it is; fix only the dispatch gap

**Description:** Build FRE-977 as approved, change no contract.

**Pros:** Cheapest; unblocks FRE-1183 immediately.

**Cons:** The seat keeps no stated contract, so the next study's scaffolding is hand-written again,
and the erratum class of failure has nothing standing against it. Does not address the owner's
directive at all.

**Why Rejected:** It solves the routing problem and none of the stated one.

---

## Consequences

### Positive Consequences

- The wrong-negative failure class has a mechanical guard that a reviewer can check **without
  re-measuring** — the property that makes the exit gate affordable at all.
- The owner's directive is satisfied without inverting explore's relationship to master, and without
  eroding owner-hubbed injection.
- Explore reaches parity with the other three seats: a working skill, a dispatch stream, a busy
  guard, and a PR of its own.
- FRE-1183 becomes reachable.
- The substrate map — hard-won, repeatedly re-derived across three studies — stops living in ticket
  bodies and becomes durable.

### Negative Consequences

- **Master's context cost is not zero.** Master gates the document PR and dispositions its proposals.
  This ADR bounds that cost by the deliverable's shape rather than eliminating it, and states the
  failure signal (master re-measuring) explicitly.
- **Explore gains repo write access**, narrowly scoped. This is a deliberate expansion of a seat
  previously restricted to drafting text, accepted because it removes master's transcription cost and
  because merging — the operational act — stays with master.
- **A fifth contract document to keep true.** The project already carries drift risk across four; this
  adds one, and the amendment table above is the mitigation for the initial edit only.
- **D3 can be gamed by phrasing** — a negative verdict written as a positive statement ("the mechanism
  is idle") evades a naive reading of the rule. The skill states the test by *semantics*, not by
  keyword.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Master absorbs the study's full cost anyway, and explore's purpose erodes | High | D6 states the failure signal — a disposition that required re-measuring — and AC-4 adjudicates it directly rather than assuming it away |
| D3 becomes a box-tick: a control is named but does not actually exercise the same path | Medium | The control must quote its **actual non-zero output** from the same query shape; AC-1 checks the output, not the claim |
| Filing narrowed to `Backlog` slows real findings into the queue | Medium | `Backlog` costs no approval bandwidth and master's D6 disposition is the promotion path, fired at a PR that already happens; nothing waits on a new event |
| The dispatcher now resets a live explore context that held owner deliberation | Medium | The busy guard blocks dispatch while a study is `In Progress`; dispatch is board-routed work the owner approved, and the reset is the same CLEAR semantics every stream carries |
| Fable is selected without approval, silently | Medium | No standing grant exists until the owner writes the ladder row; AC-7 checks every occurred selection against a recorded approval |

---

## Implementation Notes

**Files affected:**
- `.claude/skills/explore/SKILL.md` — new (D2 emit obligation, D3, D4 shape + substrate map, D5, D6
  PR mechanics)
- `.claude/skills/lifecycle-rules.md` — amendments 1–4
- `.claude/skills/prime-explore/SKILL.md` — amendment 5
- `.claude/skills/master/SKILL.md` — D6 exit disposition at the explore-document PR
- `scripts/dispatch/launcher.py` — `stream:explore` topology entry; `fable` in the validated model
  vocabulary
- `scripts/dispatch/next_resolver.py` — `explore` as a resolvable stream
- `scripts/dispatch/gating_watcher.py` — keep `cc-explore` excluded from watcher routing while the
  dispatcher gains it (amendment 3)
- `.claude/MODEL_ROUTING_POLICY.md` — Fable row
- `docs/plans/OWNER_CONSOLE.md` — **owner-written**; the implementation ticket surfaces the Fable
  ladder row for the owner and never writes it (ADR-0131 D2)

**Dependencies:** none external. FRE-977 is re-scoped into this chain rather than run separately.

**Testing strategy:** the dispatch changes carry unit tests in the existing
`tests/scripts/dispatch/` pattern (stream resolution, topology lookup, model-vocabulary validation).
The contract changes are process documents and are adjudicated by the seam ticket against real
studies, not by unit test.

---

## Verification / Acceptance Criteria

These are the **ADR's own** criteria. They are asserted in exactly one place — the seam ticket named
below — and are never sliced across the implementation chain (ADR-0130 D1).

Unless stated otherwise, "the window" means explore research documents merged to `main` **after** the
`/explore` skill lands. **If the window contains no merged explore document, the affected criterion is
`inconclusive`, never green** — the ADR then stays `Accepted` and the seam re-dates.

- **AC-1** — Every negative finding in the window carries a positive control whose **actual non-zero
  output** is quoted. · **Check:** read each merged `docs/research/` explore document; enumerate
  findings whose verdict is negative in substance (zero / never / does not fire / cannot engage /
  does not compose, however phrased); count those lacking a control with a quoted non-zero output.
  · *Fails if* that count is ≥ 1 — including a control that names a query but quotes no output, or
  quotes a zero.

- **AC-2** — D3 as written rejects the specific claim that produced the erratum. · **Check:** apply
  the `/explore` skill's admissibility rule verbatim to FRE-1131 §F1's mechanism-B row and §F3's
  "fired 0×", as they stood in PR #803. The rule must classify both inadmissible for want of a
  control. · *Fails if* the rule's text would have admitted either — which is what a rule phrased as
  a discipline ("state your method") rather than an admissibility test would do. Adjudicable
  immediately once the skill lands; needs no window.

- **AC-3** — Every proposal in the window carries a master disposition. · **Check:** for each merged
  explore document, read the commissioning ticket's comments; every item in the document's Proposals
  section maps to exactly one of `feasible` / `infeasible` + reason / `owner's call`. · *Fails if* any
  proposal is undispositioned, or is dispositioned as anything outside those three (a "noted" or an
  acknowledgement is not a disposition).

- **AC-4** — Master dispositioned from project state, not by re-measuring. · **Check:** read each
  disposition comment; no disposition rests on a measurement master ran against the substrate to
  re-derive the finding the proposal sits on. Citing the board, an ADR, git, or an in-flight ticket
  is project state and is expected. · *Fails if* any disposition contains master's own re-derivation
  of a finding's measurement — that is the split having failed and master paying the study's cost
  twice, which is the tension this ADR claims to have answered.

- **AC-5** — A study reaches the seat by dispatch, with no manual paste. · **Check:**
  `python -m scripts.dispatch.next_resolver --stream explore --json` resolves a real `Approved` +
  `stream:explore` ticket; and at least one study in the window was launched through the launcher's
  explore topology, evidenced by its dispatch card. · *Fails if* the resolver rejects `explore` as a
  stream, or if every study in the window still required a manual reset-prime-paste.

- **AC-6** — No explore finding reached `Needs Approval` before its disposition. · **Check:** for each
  merged explore document, list the Linear tickets traceable to its proposals; each was created in
  `Backlog`, **or** created in `Needs Approval` with a `createdAt` **later than** master's disposition
  comment on the commissioning ticket. · *Fails if* any `Needs Approval` ticket carrying an explore
  finding predates that comment.

- **AC-7** — Fable is selectable, and no selection happened without a recorded owner approval.
  · **Check:** the launcher accepts `--model fable` and rejects an unknown tier (existing validation
  test); then enumerate every dispatch that ran at Fable in the window and match each to a recorded
  owner approval — the trust-ladder row if the owner wrote one, otherwise an explicit per-dispatch
  approval in the ticket thread. · *Fails if* `--model fable` is rejected, **or** if any Fable
  dispatch has no recorded approval. A ladder row master wrote itself is a fail, not a pass
  (ADR-0131 D2).

**Seam ticket:** FRE-1195 — *ADR-0135 seam — adjudicate the explore working contract against real
studies*. Filed parked (`Backlog`), **due 2026-09-15** — the earliest date at which the chain can have
landed and at least one study can have run end to end through dispatch, emit, PR and disposition.
AC-2 is adjudicable earlier; the rest need that one study, which is why the seam waits on it rather
than on the last child's merge.

---

## References

- [ADR-0130](ADR-0130-two-tiers-of-acceptance-criteria.md) — Two Tiers of Acceptance Criteria (D1 severs criterion inheritance; D2 mandates the seam ticket this ADR files) — *Accepted*
- [ADR-0131](ADR-0131-retire-master-plan-owner-console.md) — Owner Console, Trust Ladder, One Writer per Store (D1 the cheap `Backlog` path D5 preserves; D2 why the Fable ladder row is owner-written; D4 the filing-plane row amendment 4 edits) — *Accepted*
- [ADR-0113](ADR-0113-self-driving-delivery-loop.md) — Self-Driving Delivery Loop (§1: dispatch mechanics live in the external resolver, not in a skill — D7 adds a stream there, not inline) — *Superseded*
- [ADR-0116](ADR-0116-event-driven-dispatch-actuation.md) — Event-Driven Dispatch Actuation (the actuation path D7's stream entry joins) — *Accepted*
- [ADR-0117](ADR-0117-pr-gate-signal-collector.md) — Deterministic Signal Collector for the PR Gate (the precedent for a gate check that collects facts and renders no verdict) — *Accepted*
- `.claude/skills/lifecycle-rules.md` § Explore session, § Coordination stores — the four sentences amended
- `.claude/skills/prime-explore/SKILL.md` — the fifth sentence amended
- `docs/research/2026-08-03-fre-1131-context-memory-alignment-audit.md` — the audit, its method appendix (line 245), its "this seat files nothing" (line 228), and the 2026-08-03 correction (line 258); PR #803 and PR #804
- [FRE-1184](https://linear.app/frenchforest/issue/FRE-1184) — this ADR's commissioning ticket
- [FRE-1116](https://linear.app/frenchforest/issue/FRE-1116) — the unanchored study that reframed the recall programme; source of "measure at the answer, not at the pipeline"
- [FRE-1131](https://linear.app/frenchforest/issue/FRE-1131) — the alignment audit whose negative finding was wrong
- [FRE-1183](https://linear.app/frenchforest/issue/FRE-1183) — the study filed 2026-08-07 with no route to the seat; the exemplar of a commission that states the attack
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
The commissioning ticket's premise was corrected during discussion against the FRE-1131 artifact:
the failure was not an absent evidence obligation but an admissible wrong negative, which is why D3
is an admissibility rule rather than a stated discipline. `Implemented` awaits the seam ticket's
adjudication (FRE-1195, due 2026-09-15); the ADR does not reach it on a red or unadjudicated
criterion.
