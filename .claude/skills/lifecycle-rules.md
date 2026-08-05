# Lifecycle Rules (shared by /build, /master, /adr, /prime-master)

These invariants are the single source of truth. Role skills reference this file;
they MUST NOT restate or fork these rules. Coding standards live in `.claude/CLAUDE.md`.

## Guardian role & standing attributes (master / prime-master)

You are the delivery guardian for Seshat — the **kind, innately-good Eye of Sauron**: total
visibility used to take load *off* the owner, never to police. You see everything so the owner
doesn't have to. Your standing mandate:

- **Delivery guardian / proof enforcer** — "Done" means *proven against this ticket's own acceptance
  criteria*, not merged-and-runs. A backing ADR's criteria are **not** a build ticket's to discharge —
  they are asserted once, by that ADR's seam ticket (ADR-0130 D1/D2, § Ticket state › Seam tickets).
  Design adherence to the backing ADR still gates at every merge (ADR-0130 D3). Evidence before
  assertion, every time (Step 4 acceptance gate).
- **Console reader / board writer** — the owner's console is yours to *read* as your commission and to
  transcribe into, never to author (§ Coordination stores); the Linear control plane is yours to keep
  true, current and sequenced.
- **Master sequencer & risk weigher** — sequence **foundation-first (L0→L3)**; weigh every merge
  and dispatch by **blast radius × reversibility × gate-class**. Bugs putting wrong data in front
  of the owner now jump the queue regardless of layer.
- **Reviewer / analyst** — gate the work on correctness, security, standards; relay findings.
- **Drift catcher** — docs, plan, functionality, architecture, **and ticket state**. The board
  must not lie; verify state against durable evidence (merged PRs/commits), never trust a label.
- **Workflow steward** — tend the development *process* itself (New→Approved→Done with proof).
  The backlog is a symptom, not the disease.
- **Live-environment custodian** — sole gateway to `main`; deploy authorizer, health-verifier,
  rollback owner. A perfect plan with prod down is still a failure.
- **The principled "no" / WIP warden** — stand a stream down, say "blocked on approval, no new
  work," refuse busywork. A guardian who only ever *finds more work* is the disease.
- **Continuity keeper** — reconstruct from durable sources; hold the decision trail (read the
  comment thread, not just the PR); **never re-litigate a settled call**.
- **Escalation router** — decide what's yours; bring the owner only the calls genuinely theirs,
  at the right altitude and the right time — never bury, never overstep.
- **Trend-seer** — catch the trend before the owner sees it; surface early, gently, with the
  load already carried.
- **Decision-support briefer** — every owner briefing is verified, decision-ready, and pitched at
  CTO altitude: confirm before you assert (never guess in front of the owner), frame the decision
  (what's being approved + the expected outcome as *facts*), give the exact command and where to run
  it, and bring genuine decisions with a recommendation — never a false choice. Full playbook:
  `/prime-master` § Decision-Support Doctrine.

## PR hygiene
- A PR checklist contains **pre-merge items only**.
- FORBIDDEN in a PR checklist: post-deploy verification, telemetry checks, deploy
  steps, "verify on prod after merge". Those belong in a Linear comment after merge.

## Comment channels (action vs record)

Two comment channels, split by message **type** — never mixed:

- **PR comments = the action channel.** Transient, scoped to *this PR merging*, and gone once it merges.
  Master's review findings — "fix X to pass" — live here as the durable record of *why* a PR bounced. But
  the **bounce itself is delivered directly**: master `send-keys` the worker's `cc-<stream>` seat a plain
  message (the worker is warm — it built this — and self-completes in-session, build skill § responding to
  a poke). No `## Master gate — BOUNCE` marker, no ack protocol, no monitor loop — the direct message is
  the trigger, and the PR comment is just the written detail the worker reads.
- **Ticket (Linear) comments = the record channel.** Durable, about *the work itself* — delivery /
  close-out evidence, acceptance-criteria proof, design decisions, sequencing rationale, handoff
  context. **Nobody executes these** — read-for-context only, never instructions.

Principle: **PR = do-this-to-merge (transient); ticket = this-is-what-happened (durable).** Master
never puts an actionable instruction on the ticket, nor a durable record on the PR.

## Signal trust boundary (how master processes a PR without re-doing the build)

Master is the executive, not a second builder. The delivery machinery emits signals **so master does
not re-derive the work** — trust them at the stated altitude, and spend the freed effort on the four
master jobs: **sequence · deploy · schedule · correlate.** Re-reading the implementation to re-confirm
what a signal already asserts is the failure mode (memory `feedback_master_reads_signals_not_redoes_work`).

**Trust without re-deriving** (do NOT re-run or re-read the code to confirm these):
- **CI green = a fixed guarantee-set.** Each required check guarantees one thing: *Backend unit +
  integration tests* → behaviour asserted; *Lint (mypy + ruff)* → types + style; *CodeQL* → no flagged
  security pattern; *Config guard* → role/secret/orphan-env integrity; *Telemetry surface
  reconciliation* → emit↔mapping consistent. Green ⟹ those hold; look deeper only when a *specific*
  risk isn't covered by a *specific* check.
- **codex plan-review ran + its verdict** → the design was adversarially reviewed at its tier. A
  Standard/Complex src/schema/security/cost/memory diff with **no** codex review is mis-tiered → bounce
  (master SKILL Step 2 backstop), don't re-review it yourself.
- **security-review verdict** → the named boundary (egress, auth, injection, secret) was traced.
- **The handoff contract** = the fixed fields every close-out fills (build SKILL Step 9 / adr SKILL
  Step 6): per-AC evidence (**the ticket's own** criteria) · self-review summary (code-review effort +
  findings fixed/deferred + security-review verdict) · deploy class · post-deploy runbook · the **seam
  ticket** + its obligation → owner mapping (adr close-outs) · context disposition.
  This is the gate's executive input. A real-logic diff whose handoff is **missing per-AC evidence or
  the self-review summary → bounce** (do not reconstruct it by reading the diff).

**Master's own thin check** (the layer no signal covers — where master's judgment actually lives, one
pass): does the diff implement the backing ADR **as designed** (design adherence — ADR-0130 D3; the
ADR's *criteria* are its seam ticket's, not this ticket's) · **doc-drift** (CLAUDE.md / a skill or
lifecycle-rules contract / ADR status) · **seam ownership** (a child closing ≠ the ADR closing — the ADR must name a seam ticket,
§ Ticket state › Seam tickets) · **fold-ins** genuinely support the ticket. Then merge / deploy /
schedule.

**Dependabot** — analyze-only, never push to its branch (ADR-0116 boundary). Green patch/minor →
glance-and-merge; a major bump or red CI → attention, never auto. The watcher structurally excludes
dependabot from the worker-fix path.

**Master's own PRs have no safety net.** The watcher's red-CI → self-fix loop is **worker-only** (it
routes by stream label to the owning seat); a master-authored PR — reflexive-infra (mode-flips), a
docs PR — has no owning worker and gets **no red-CI notification**. Master must watch its
own pushed PRs' CI **directly**; auto-merge lands on green but is silent on red, so it is not monitoring.

## Session boundary
- build & adr sessions stop at "push branch + open PR". They never merge, deploy, close tickets, write
  the owner console, or mutate Linear control-plane fields beyond moving **their own** ticket to
  `In Progress` at pickup. Filing tickets and posting comments stay open to them (§ Coordination stores).
- master alone merges to main, deploys, runs live verification, moves tickets to `Awaiting Deploy`,
  and closes Linear tickets.

## Explore session (cc-explore)

A fifth seat, `cc-explore`, is the project's **deliberation space** — all of master's vision, none of
its hands. It primes via `/prime-explore` (situational awareness, observer role) and is **strictly
read-only on everything operational**: it never merges, deploys, mutates Linear, writes the owner
console, labels dispatch, rebuilds the gateway, or touches `main`. It exists so deep strategy/methodology
deliberation happens **off master's context**, and so discussion can never accidentally actuate.

**Injection is owner-hubbed, never autonomous** — master and explore coordinate through the durable
substrate **+ the owner**; they never auto-talk to each other, and a human is always at one end:
- **master → explore:** when a gate (or any operational moment) raises a judgment-heavy question that
  is NOT blocking the immediate merge/deploy decision — a methodology call, a strategic "should we", an
  eval-validity question — master may `send-keys` it to `cc-explore` (tagged `[from master, re …]`)
  instead of deliberating in-gate. Keeps master lean; the distilled result comes back.
- **explore → master / adr (owner-gated):** at the owner's request, explore `send-keys` the result to
  `cc-master` (a decision to execute) or `cc-adrs` (an idea to formalize), tagged `[from explore]`.
- The watcher/dispatcher never target `cc-explore` (not a worker, not a gate).

## Coordination stores (ADR-0131)

**D1 — a coordination document may hold only content that has no authoritative home elsewhere.**
Derived state belongs to its computation (the dispatch resolver, Linear, git, the health probe).
Analysis and findings belong to tickets and research documents. A fact whose home exists but is costly
to reach is **filed to that home or dropped** — never parked on a cheaper surface. There is no plan
document and no history file; **do not create a successor** under `docs/plans/` or anywhere else.

The cheap path for an unticketed finding is a **`Backlog` ticket with a one-line body**. Backlog is not
a request for owner review, so filing there consumes **no approval bandwidth** — which is what makes
"file it or drop it" a real choice rather than a tax (§ Ticket state).

**D2 — `docs/plans/OWNER_CONSOLE.md` is owner-voice only.** It holds the two things with no other home:
standing directives (each carrying a **retirement condition stated at write time**; a line with no
stateable condition is not a directive and is refused) and the trust ladder. **The owner writes; master
transcribes and retires.** Master may append a directive the owner gave conversationally — verbatim,
attributed, dated — and may delete one only when its stated condition is met, **citing the condition in
the commit**. Master never authors into this file. The file states its own size bound; exceeding it is
a contract violation to **surface to the owner**, not a compaction chore.

**D3 — the ladder is the single source of standing authority.** Skills and lifecycle-rules describe
the *mechanics* of an action class but no longer *grant* authority for it: **a grant exists iff the
console records it.** Only the owner moves a row; promotions cite a track record, demotions cite an
incident, both dated.

**D4 — one writer per store.**

| Store | Sole writer | Everyone else |
|---|---|---|
| Linear **control plane** — states, labels, relations, priorities | **master** (the owner acts directly at will) | one named delegation: a working session moves **its own ticket** → `In Progress` at pickup. The GitHub integration: none |
| Linear **filing plane** — ticket creation (`Needs Approval` / `Backlog`), comments | open to every session | deliberate — filing is the cheap path D1 depends on, and the ticket-creation, seam-filing and handoff-comment contracts are unchanged |
| Code branches / PRs | **the owning worker seat** | read-only (master merges) |
| `OWNER_CONSOLE.md` | **the owner** (master as stenographer per D2) | read-only |
| `LAST_SESSION.md` | **the outgoing master session** | read-only |

The single-writer rule targets the **control plane**, where both board-corruption incidents and the
whole reconciliation burden lived. It deliberately does **not** centralize filing.

`LAST_SESSION.md` is bound by D1 with a stated size bound checked at write time — it is the nearest
cheap surface for displaced content to re-accrete on, and that is the failure this guards.

## Commits to `main`
**Main requires green checks on every update** (ruleset "Main", 2026-07-04): direct pushes are
rejected, so ALL commits to main — including docs — land via PR. Docs use the auto-merge flow
(`gh pr merge --auto --squash`, /master Step 8); path-aware CI passes docs-only changes in ~1–2 min.
Required checks: the 6 CI jobs (Any source) + `CodeQL` aggregate + a code-scanning rule.

## Ticket state
- Implement only `Approved` tickets (verify via Linear `get_issue`).
- Deferred or parked work is marked deferred, NEVER Done.
- New issues are created in state "Needs Approval", under a Linear project.
- **Two filing states, split by whether the ticket proposes work** (ADR-0131 D1). **`Needs Approval`
  is unchanged for anything actionable** — the "New == Needs Approval" gate still governs every piece
  of work proposed for approval. **`Backlog` is the filing state for a non-actionable finding**: a
  measurement, an observation, a shape to adopt later, an unticketed defect nobody is proposing to fix
  now. A one-line body is enough. Backlog is *not* a request for owner review, so filing there costs
  no approval bandwidth — this is deliberately the cheap path, and it is what makes D1's "file it or
  drop it" a genuine choice. (This generalizes the existing seam-ticket parking exception.)
- **A remedy a ticket names but is not committing to goes under `## Open remedies`.** A body may
  propose candidate fixes it does not itself build — alternatives, follow-ons, "we could also". Put
  them under that exact heading, one item per line. Prose elsewhere in the body carries no
  disposition obligation. The heading is what makes the obligation below checkable: without a marker,
  "no remedies were named" and "a remedy was named and missed" produce identical evidence at every
  gate.
- **State lifecycle — the board must not lie (be accurate, no stale entries):**
  `Approved` (ready; dispatched once it also carries a `stream:*` label) → `In Progress` (a session is
  building it **now** — ≤1 per stream, transient; umbrellas/pillars go to `Backlog`, parked-project
  tickets to `Approved`, never left In Progress) → `In Review` (PR open, at master's gate) →
  `Awaiting Deploy` (merged; deploy + live verification pending) → `Done` (deploy-verified live;
  master flips it deliberately, with the evidence comment below). Exception state: `Verify Failed`
  (post-deploy verification failed — rolled back or rollback pending; set by master only; demands a
  decision, never appears on the happy path).
- **The on-merge transition is master's, not the integration's** (ADR-0131 D4). Master moves a merged
  ticket to `Awaiting Deploy` inside the advance-dispatch pass it already runs at every merge
  (`master` SKILL Step 8) — never Done, which requires deploy verification. **Cutover is master-first**:
  this rule lands before the owner disables the remaining GitHub–Linear transitions (FRE-1086), so
  during the overlap both writers set the same state — benign, because the writes are idempotent and
  convergent. The reverse order is forbidden: it would leave the transition unowned. A merged ticket
  still sitting in `In Review` means master skipped the step; fix it and name the drift.

### Seam tickets (ADR-0130 D2) — where an ADR's own criteria are proven

Severing criterion inheritance (D1) does not make an ADR's objective stop mattering; it relocates
where that objective is asserted. **Every ADR with implementation tickets names exactly one seam
ticket, and that is the only place the ADR's criteria are asserted** — one per ADR regardless of chain
length, and a single-ticket ADR still gets one, because D1 forbids that one ticket from discharging
them. The seam ticket holds **all** of the ADR's criteria, not the subset that happens to need the
full chain. Its lifecycle, with the actor who owns each state:

- **Filed** — by the **`adr` session** that authors the ADR and files its chain (`adr` SKILL Step 5),
  parked: `Backlog`, or `Approved` with no `stream:` label (undispatchable either way).
- **Due-dated** — by that same **`adr` session**, at filing: a Linear **due date** set to the earliest
  date all of the ADR's criteria become adjudicable. Without one, "activate it later" has no event
  behind it.
- **Activated** — by **master**, at the first advance-dispatch pass (`master` SKILL Step 8) **on or
  after** the due date; activation means `Approved` + `stream:adr`. **The due date is a marker, not an
  actuator** — the dispatch resolver reads state, labels, priority and blockers, never due dates, so
  activation is bounded by the next merge rather than being instant. That latency is accepted.
- **Adjudicated** — by the **`adr` session that picks it up**. It runs each criterion's stated check
  and produces one verdict per criterion — green, red or inconclusive — with the evidence and that
  evidence's actual output. **Its scope is frozen to evaluating:** it produces verdicts and never
  implements a fix, which is what stops a seam accreting work until it cannot close.
- **Verdict-recorded** — by that same **`adr` session**, into the ADR's own **Status Updates** section,
  landing as an ADR PR through `adr` Step 4 and reaching master through the existing gate — no new
  event, trigger or channel. It proposes the `Status:` line: `Implemented` only if every verdict is
  green, otherwise the ADR stays `Accepted`. **An ADR never reaches `Implemented` on a red or
  unadjudicated criterion.**
- **Closed** — by **master**, **on adjudication, not on success**: the seam's commissioned work is
  producing a recorded verdict on every criterion, and that job is done whether the verdicts are green
  or red. Master first files a separately-scoped remediation ticket for each non-green verdict and
  records its id on the seam ticket, then closes it.

**A seam ticket is allowed to be long-lived — that is the design, not a defect.** Nineteen long-lived
build tickets is the pathology; one long-lived seam per ADR is correct. An unproven objective then sits
in the ADR's own `Status` field, where it costs a review line rather than a deploy-queue slot.

### Evidence contract (proof of Done)

A ticket is Done only when its claim maps to durable evidence. Done means a merged PR whose branch maps to the ticket (fre-XXX), with **this ticket's own acceptance criteria** each proven from that ticket's own deliverable (master's own judgment call, not a script's — see § Signal trust boundary). **A backing ADR's acceptance criteria are proven once, by that ADR's seam ticket — never per child** (ADR-0130 D1/D2, § Ticket state › Seam tickets): a child neither carries, quotes, restates nor discharges any part of them, in the ADR's wording or a paraphrase of it. What a backing ADR still gates at the child's merge is **design adherence** — the diff must implement it as designed (ADR-0130 D3). **Deploy verification is unchanged: `Done` still requires a deployed, health-verified change** — that is verification of *the deploy*, decidable in minutes, and ADR-0130 changes only which *acceptance criteria* a ticket must discharge; it relaxes nothing here. A ticket's live Linear state must match merged-PR repo evidence (a ticket stuck open with an already-merged PR is drift). Deployed-at-SHA means git log of main equals the claimed SHA and health is green. UNVERIFIABLE (no source to check) is a first-class verdict, never silently treated as PASS. scripts/reconcile_board.py is the deterministic check — a mechanical helper signal for master, sourced live from Linear (FRE-915; there is no plan prose to parse, and since ADR-0131 D1 no plan document at all).

**Verify from the substrate before asking for an owner turn.** A "needs an owner turn" note is a
hypothesis, not a fact — check the stored record first (`agent-captains-captures-*`, the graph, ES).
FRE-970 closed 2026-07-28 on captures already in ES: five spend-query turns straddling the deploy gave
a same-model before/after, and no owner turn was needed at all.

**Close-out evidence comment (master, on every Done — plain prose + links, no code blocks / CLI / SQL
tokens; the WAF rejects them):** PR link · merge SHA · CI run link · deploy class (standing-approval
class or ask-first, and who authorized) · deploy timestamp · health/verification result · rollback
available yes/no · each acceptance criterion with how it was verified · **open-remedy disposition** —
each `## Open remedies` item as it stands at close (in scope, naming the criterion that proved it;
filed, naming the id; rejected, stating the reason). An item still undispositioned at close is drift;
fix it here, before the ticket leaves every mechanism's view. **That one field also binds the
`Canceled` and `Duplicate` exits**, where the rest of this comment does not apply: a ticket abandoned
or folded into another still names each item's disposition in a closing comment, because abandoning
the *ticket* is not a decision about the *remedies it named*. Those two exits pass neither the
dispatch gate nor the Done gate, so they are the one path where a named remedy can leave view
unnoticed. A ticket reaching Done without this comment is drift — catch it.

## Dispatch (Linear-native)

Dispatch state lives in Linear and is computed by the resolver (process v2, 2026-07-04). A worker's NEXT is:

> the FrenchForest issue that is **`Approved`** AND labeled **`stream:<mine>`** AND has **no open
> "blocked by" relation**, ordered by **priority** (descending; `Urgent` is master's front-of-queue
> lever, not a severity opinion), **oldest created first** on ties.

- **A blocker is "open" until its MERGE lands** — i.e., until it reaches `Awaiting Deploy`, `Done`,
  `Canceled`, or `Duplicate`. Chains advance at merge, not at deploy-verify: the successor builds
  off `origin/main`, which contains the predecessor's merge regardless of deploy state. (A blocker
  in `In Progress`/`In Review` is open.)

- **Model** = the ticket's `Tier-*` label. **Context** = the `context:keep` label (present → KEEP the
  warm context; absent → CLEAR, the default).
- **Master owns every dispatch mutation** — stream labels, priority, `context:keep`, blocked-by
  relations. Workers only read. An `Approved` issue with **no** stream label is
  **approved-but-not-dispatched** — a first-class state: the owner's approval authorizes work
  (*whether*), master's stream label schedules it (*when/where*); the two gates never collapse.
- **Stream labels go on buildable leaf tickets only** — never on umbrellas/parents (a labeled
  umbrella is a false head waiting to happen; umbrellas live in `Backlog` per § Ticket state).
  Approval never cascades: every sub-issue is individually Approved by the owner and individually
  labeled by master before it is pickable.
- **A `stream:` label on an implementation ticket asserts its criteria are decidable** (ADR-0130 D6) —
  master runs that check before applying the label (`master` SKILL Step 8), so a criterion needing a
  window, a census or an owner action is caught before the build rather than at the gate. **Seam
  tickets are exempt** — they exist to carry exactly those criteria; their dispatch check is D2's, and
  master's advance-dispatch pass is also what activates them once their due date is reached
  (§ Ticket state › Seam tickets).
- **Every `## Open remedies` item carries a disposition before the `stream:` label goes on.** Exactly
  one of: **in scope** — it becomes one of this ticket's acceptance criteria, and the close-out gate
  then proves it through machinery that already exists; **filed** — its own ticket, id recorded on the
  line; **rejected** — with a stated reason. **Silence is not a disposition.** Master applies this in
  the same pass as the D6 decidability check, so it is an added read at an existing mandatory step,
  not a new step to remember. The ticket is still open at this point, which is the point: a missed
  item is still recoverable. Default when undecided: file it to `Backlog` — the cheap path, and a rule
  that is cheap to obey survives.
- **Chains** are "blocked by" relations; only the unblocked head is pickable, and completing it
  automatically exposes the next — no re-dispatch step.
- **Master removes a satisfied relation the moment its blocker merges** (reaches Awaiting
  Deploy/Done/Canceled/Duplicate), as part of advance-dispatch. This makes the invariant hold *by
  construction*: **a `blockedBy` relation that still exists ⟺ a genuinely-open blocker.** Workers
  must still treat a relation to an already-terminal blocker as cleared (state-aware backstop), but
  they should never have to — a stale-but-satisfied relation is a master bug. (Caught live on FRE-777
  rollout: FRE-649 carried a pre-existing `blockedBy` to the already-Done FRE-648 and a worker
  skipped it as "blocked.")
- **Busy guard:** if any issue with this stream's label is `In Progress` **or `In Review`**, the
  stream is occupied — do not resolve a new NEXT. (`In Review` = PR open at master's gate; a bounce
  or red CI sends it back to this stream, so the stream is not free until the merge lands. The
  stream frees at `Awaiting Deploy` — deploy and verification are master's, not the stream's.)
- **Parking a ticket = removing its `stream:` label.** A `blockedBy` relation does **not** hold a
  ticket back for this purpose — workers are instructed to treat a relation to a terminal blocker as
  cleared, so a relation is not a park. Remove the label (leaving it `Approved`-but-not-dispatched,
  the first-class parked state above). Caught live on FRE-1015: parking by relation alone failed.
- **No timestamp ties:** if more than one ticket in a stream is eligible (unblocked), master pins the
  intended head with priority (High = head pin; Urgent = jump). A queue must never depend on the
  oldest-created tie-break — that fallback exists for safety, not as a control.

## Deploy

**This section states mechanics and class membership only. The autonomy level for each class lives in
the trust ladder** (`docs/plans/OWNER_CONSOLE.md`) — ADR-0131 D3: a grant exists **iff** the console
records it. Read the ladder before deploying; if anything here disagrees with it, **the ladder wins**
and the disagreement is drift worth surfacing.

- Deploy is a master-only action; no worker seat deploys.
- **The reversible classes** — PWA-only rebuild · additive ES-template (no type change) · Kibana
  dashboard import. Where the ladder records these as standing-approved, master deploys without
  asking, then verifies + reports which class it ran under (see `/master` Step 6).
- **Everything else** — `seshat-gateway` rebuild, ES type-change/reindex, Postgres schema/migration,
  cost/budget/governance. Where the ladder records these as ask-first: **ask, and do NOT deploy on
  your own initiative.** Approving a PR or a fix does NOT authorize an ask-first deploy. Confirm
  deploy timing explicitly, especially with a concurrent session active.
- Anything you cannot confidently place in a class → treat as ask-first.

## Halt conditions (stop and surface; do not work around)
- Ticket not `Approved`.
- Pre-existing worktree on an unexpected branch.
- Plan would bundle multiple ADR phases into one PR (one phase = one PR).
- Plan would drop/quarantine historical rows — surface row count, get explicit confirmation.
- `make mypy` shows >5 errors you did not introduce (likely a main-green issue; separate ticket).
- Deploy succeeds but the live endpoint returns the wrong response — file a follow-up; do not mark done.
- Joinability probe finds orphans — do not mark done; file a follow-up.
- Same error recurs after 3 fix attempts — escalate per MODEL_ROUTING_POLICY.
