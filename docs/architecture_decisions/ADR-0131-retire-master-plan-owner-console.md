# ADR-0131: Retire MASTER_PLAN — an Owner Console with an Explicit Trust Ladder, One Writer per Store

**Status:** Proposed
**Date:** 2026-07-31
**Deciders:** project owner (FRE-1082)
**Tags:** process, delivery, coordination, trust, documentation

---

## Context

**What is the issue we're addressing?**

`docs/plans/MASTER_PLAN.md` is the oldest document in the delivery process — it predates Linear-native
dispatch, the dispatch resolver, the session-delta artifact and the memory layer, and it has kept the
job it held before those existed: a hand-maintained account of what the project is doing. Every fact
it mirrors now has an authoritative source, so the file rots by construction, and the process spends
real effort failing to stop it.

### The operating model this document must serve

The project has **one human developer**. The objective of the delivery process is not autonomy — it is
**trust graduation**: agent sessions do the labor, the owner reads and acknowledges, and independence
expands only as fast as the owner's trust does. The working loop with the master session is a
partnership: **the owner reads, master informs, the owner decides, master acts.** Each component is
meant to carry its strength: Linear is the owner's review-and-approval surface, GitHub holds the code
and enforces CI mechanically, Claude Code sessions supply the labor and the gate judgment.

Under that model the owner's reading rate is the governor *by design*. A coordination document earns
its place only if the owner's decisions persist through it or master's briefings become more legible
because of it. MASTER_PLAN does neither.

### What was measured (2026-07-31, from the file and its history)

- **The file is 340 lines against its own stated limit of ~1 screen** — roughly seven times over. The
  limit is written into `prime-master` step 8, has a dedicated compaction ritual in `prepare-reset`
  step 3, and was enforced as recently as 2026-07-30 (header 74 → 49 lines). It grew back within a
  day. A rule re-violated immediately after every enforcement is not carelessness — the document has
  no natural size because nothing bounds what belongs in it.
- **Of its 340 lines, roughly 90 mirror derived state** (seat status, the verification queue, ticket
  states — all authoritative in the dispatch resolver, Linear and git), **roughly 205 are master's
  embedded analysis** (measurements, corrections to its own record, unticketed findings — each with an
  authoritative home in a ticket, a research document, or nowhere because it should have been filed),
  and **roughly 5 lines are the owner's voice** — the only content with no other home. The file's last
  40 commits are all master-session commits; the owner has never written into it directly.
- **The primed session reconciles two accounts.** `prime-master` builds current state from computable
  sources at steps 1–7, then reads this file at step 8 — which restates the same state, stale. At the
  time FRE-1082 was filed, section 0 said the adrs seat was free while an adr session worked in it.
- **Writing the file corrupts the board.** A docs PR whose title or body names a ticket id is matched
  by the GitHub–Linear integration, which attaches the PR and marches the ticket to `Awaiting Deploy`.
  It happened twice, both times from master's own plan updates; FRE-1036 sat in a false state for two
  days with no branch, no PR and no implementation.
- **Its largest section lost its reason to exist the same day.** Section 3, the verification-queue
  mirror, was obsoleted by ADR-0130's criteria split — nineteen of its rows closed in one sweep.

### Why the file grows: the economics of the escape valve

Writing a line into MASTER_PLAN is free. Filing the same fact where it belongs costs a round trip — a
ticket with a project and a tier label, a research note, a memory file. The file is therefore the
cheapest writing surface in the system, and **a free surface adjacent to costly ones absorbs
everything**. That is why it has no natural size, and why any successor document with the same
economics will reproduce the problem under a new name.

The project's own convergence law (`docs/research/2026-07-29-why-pipeline-point-fixes-do-not-converge.md`)
states the general form: **fixes that removed the operation that could fail converged; fixes that
improved or observed an inference did not.** The compaction ritual is an observe-and-improve fix on a
hand-reconciled copy — the non-converging kind. Deleting the copy is the converging kind.

### The half that must not be lost

The file's irreplaceable content is the owner's standing instruction: build-sequence guidance,
priority overrides, conditional directives that must survive session resets. Today that is ~5 lines,
but it is a **primary source** — the input to master's dispatch decisions, not a record of them — and
it currently has no dedicated home. The related gap: **a fresh master session is never told its
standing authority.** The deploy standing-approvals live as prose in lifecycle-rules; automation
posture is read fuzzily from "LAST_SESSION / MASTER_PLAN / memory" (`prime-master` step 7b — three
possible homes is zero authoritative homes). In a trust-graduation model, acting above granted
autonomy is the worst class of fresh-session error, and nothing in the priming stack guards it.

---

## Decision

Retire MASTER_PLAN. Replace it with a small owner-authored console file, make autonomy grants
explicit in that file, and give every coordination store exactly one writer.

### D1 — Retire `docs/plans/MASTER_PLAN.md`; nothing with an authoritative home is stored twice

The file is deleted after a one-time supervised migration (Implementation Notes). The binding rule,
stated once so later decisions can cite it:

> **A coordination document may hold only content that has no authoritative home elsewhere.** Derived
> state belongs to its computation (the dispatch resolver, Linear, git, the health probe). Analysis
> and findings belong to tickets and research documents. A fact whose home exists but is costly to
> reach is filed to that home or dropped — never parked on a cheaper surface.

No successor scratch surface is created. Displaced content does not relocate; it goes to its home or
it goes nowhere. The cheap path for an unticketed finding is a **`Backlog` ticket with a one-line
body** — Backlog is not a request for owner review, so filing there consumes no approval bandwidth.

### D2 — The owner console: `docs/plans/OWNER_CONSOLE.md`

One file, **owner-voice only**, holding the two things that have no other home:

1. **Standing directives** — sequence guidance, priority overrides, prohibitions, conditional
   instructions. *Every directive carries a retirement condition stated at write time* — the event
   that ends it. A line with no stateable retirement condition is not a directive and is refused.
2. **The trust ladder** (D3).

Authorship contract: **the owner writes; master transcribes and retires.** Master may append a
directive the owner gave conversationally — verbatim, attributed, dated — and may delete a directive
only when its stated retirement condition is met, citing the condition in the commit. Master never
authors content into this file. The file carries a stated size bound in its own header; exceeding it
is a contract violation to surface to the owner, not a compaction chore — by construction (owner-only
authorship, mechanical retirement) it has no growth engine.

### D3 — The trust ladder: autonomy is granted explicitly, per action class, by the owner

The console holds a table of action classes, each with its current autonomy level and the condition
that promotes or demotes it. Levels: **ask-first** → **do-and-report** → **standing-approved**. This
generalizes the one trust mechanism that already works — the deploy standing-approval classes granted
2026-06-26 — to every actuation class (deploys, dispatch, merges, board mutations, automation
posture). Rules:

- **Only the owner moves a row.** Promotions cite a track record; demotions cite an incident. Both
  are dated. An undated or unattributed row is invalid.
- **The ladder is the single source of standing authority.** Skills and lifecycle-rules may describe
  *mechanics* of an action class but no longer *grant* authority for it; a grant exists iff the
  console records it. `prime-master` step 7b's "intended automation posture" becomes a ladder row.
- **Initial rows are seeded by the owner at migration** — the ADR establishes the mechanism and
  deliberately does not decide current levels (e.g. the dispatch daemon's) — those are owner data,
  revocable without re-opening this ADR.

### D4 — One writer per store

| Store | Sole writer | Everyone else |
|---|---|---|
| Linear (states, labels, relations, priorities) | **master** (owner acts directly at will) | read-only |
| Code branches / PRs | **the owning worker seat** | read-only (master merges) |
| `OWNER_CONSOLE.md` | **the owner** (master as stenographer per D2) | read-only |
| `LAST_SESSION.md` | **the outgoing master session** | read-only |

Consequences: the remaining **GitHub–Linear integration transitions are disabled** (completing what
FRE-1075 started). The on-merge → `Awaiting Deploy` transition is performed by master inside the
advance-dispatch pass it already runs at every merge, where it costs one call. This removes the
board-corruption class (a docs PR naming a ticket id can no longer move it) and makes board↔reality
reconciliation structural rather than ritual: when only master writes, the board disagrees with
reality only if master erred, which is auditable from one actor's history.

### D5 — Consequences for the contract documents

- **`prime-master` step 8** re-pointed: the target is **the dispatch resolver's eligible sets** (the
  computed "what we do next, in order") **plus the console** (the owner's overlay: directives + the
  session's exact standing authority). Step 8 becomes the moment a fresh master reads its commission.
  Step 7b reads posture from the ladder. The step-8 drift rule ("~1 screen or strip it") is deleted
  along with its object.
- **`prepare-reset`** — step 3's MASTER_PLAN checkpoint-and-compact ritual is deleted, replaced by a
  single verification: *the console was not written by this session outside the D2 contract.* The
  step-1 gate condition "MASTER_PLAN ↔ Linear in sync" is dropped (D4 makes it structural).
  **`LAST_SESSION.md` is explicitly bound by D1's rule** — it carries only what no durable source can
  reconstruct (its existing contract), now with a stated size bound checked at write time, because it
  is the nearest cheap surface for displaced content to re-accrete on.
- **`master` skill step 8** — "update MASTER_PLAN if strategy changed" is deleted. Strategy changes
  are either the owner's (console, in the owner's voice) or they are ticket/ADR content.
- **`lifecycle-rules`** — the MASTER_PLAN section is replaced by a Coordination-stores section
  stating D1/D2/D4; the guardian-role "Plan owner" line becomes "Console reader / board writer."
  Worker-skill boundary lines ("never edit MASTER_PLAN") become "never write the console or Linear."
- **Root `CLAUDE.md`** authoritative-sources table: "what are we doing next, in order?" → the
  dispatch resolver + `OWNER_CONSOLE.md`.

---

## Alternatives Considered

### Option 1: Delete MASTER_PLAN outright, replace with nothing

**Description:** The file is rot; remove it and let Linear/git/resolver answer everything.

**Pros:**
- Maximal simplification; zero successor-growth risk.

**Cons:**
- Destroys the primary source: the owner's standing directives exist nowhere else, and dispatch
  decisions consume them across session resets.
- Leaves `prime-master` step 8 a hole. A hole where the target belongs invites the next master to
  invent a file to fill it — the file grows back under a new name, unauthored and uncontracted.

**Why Rejected:** It was the first recommendation made when FRE-1082 was raised, and the owner
rejected it for exactly the first con. The irreplaceable half is small but load-bearing.

### Option 2: Keep the file; enforce the existing drift rule harder (size cap in CI, stricter ritual)

**Description:** The contract ("forward plans only, ~1 screen") is fine; the failure is enforcement.
Add a line-count check to CI and make the compaction ritual mandatory per session.

**Pros:**
- No migration; no skill rewrites; familiar surface retained.

**Cons:**
- The rule was re-violated within a day of its most recent enforcement; the document has no natural
  size because *nothing bounds what belongs in it* — a cap bounds length, not content class, so the
  same rot compresses rather than disappears.
- It is an observe-and-improve fix on a hand-maintained copy — the class the convergence law shows
  does not converge. Four rounds of added verification did not stop the pipeline stalling; a fifth
  round of added enforcement would not stop the plan rotting.

**Why Rejected:** The mechanism that grows the file (free surface, unbounded content class, mandated
master writes) survives intact; enforcement fights it forever.

### Option 3: Split into two maintained documents — a state mirror and a directives file

**Description:** FRE-1082's literal reading: separate the derived half from the owner half, keep both
as maintained documents.

**Pros:**
- Preserves a human-readable "where are we" page some readers may want.

**Cons:**
- Keeps the hand-maintained copy of computable state, and with it the entire reconciliation burden,
  the staleness, and the board-corruption exposure of docs PRs that discuss tickets.
- Measurement showed the "state mirror" half is ~90 of 340 lines; the majority of the file is
  misplaced analysis that belongs in tickets/research either way — the two-way split answers the
  wrong question.

**Why Rejected:** The derived half rots by construction; maintaining it in a second file is the same
operation with a smaller blast radius, not a fix.

### Option 4: A bounded scratch surface — hard line-cap, per-entry expiry dates

**Description:** Sanction the escape valve: keep a deliberately cheap notes file with a hard cap and
expiring entries, so misplaced content at least drains.

**Pros:**
- Honest about the filing-cost economics; gives findings a legal cheap home.

**Cons:**
- It is the current file with a number written on it. The number was already written (one screen) and
  violated the day after every enforcement.
- Expiry-by-date deletes by age, not by having-a-home — wrong retirement condition for facts.

**Why Rejected:** The cheap-surface economics are fixed in D1 by making Backlog filing the cheap path
(one line, no approval cost), not by sanctioning a second cheap surface that competes with the
authoritative stores.

---

## Consequences

### Positive Consequences

- **The reconciliation class disappears structurally.** No hand-maintained copy of seat/ticket/queue
  state exists, so nothing needs reconciling at prime, at reset, or at ship. The two-accounts problem
  at `prime-master` step 8 is gone — the target is computed at read time.
- **A fresh master reads its exact authority.** The ladder closes the worst fresh-session error class
  (acting above granted autonomy) and gives trust graduation a deliberate, dated, owner-owned record —
  ending the ad-hoc oscillation between autonomy creep and re-imposed control.
- **Resets get cheaper on both ends** — no compaction ritual at wind-down, one small file at re-prime —
  which removes the standing disincentive to reset, and frequent resets are the hygiene the bookend
  skills exist to enable.
- **The board-corruption class closes** (D4): docs PRs can no longer move tickets, and every Linear
  mutation traces to one actor.
- **The growth engine is cut off**, not dieted: the mandated master-writes are deleted from the
  skills, the surviving file refuses master's authorship by contract, and displaced content has a
  stated cheap path (Backlog) to its real home.

### Negative Consequences

- **Filing discipline costs friction.** Findings master would have parked in the plan must be filed
  as Backlog tickets or dropped. Some marginal observations will be dropped; that is accepted — an
  observation not worth a one-line ticket was not worth a plan line either.
- **The resolver becomes more load-bearing.** Step 8's queue view has no prose fallback. The resolver
  already fails loudly rather than silently (FRE-785 contract), and Linear's own UI remains the
  human-readable fallback.
- **No synthesized "state of the project" page exists.** Anyone wanting the month-shape narrative
  reads the console's sequence directives plus Linear, or asks master to brief live. This is the
  point, not a regression — the synthesized page is what rotted — but it is a real loss of a single
  glanceable artifact.
- **Migration touches every contract document** (~30 references across four skills, lifecycle-rules,
  CLAUDE.md) and must land atomically per document to avoid a two-names period.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Displaced content re-accretes in `LAST_SESSION.md` (the next-cheapest surface) | High | D5 binds it to D1's rule with a stated size bound checked at write time; AC-2 measures exactly this over the observation window |
| The console grows anyway (directives accumulate, master over-transcribes) | Medium | Owner-only authorship + mandatory retirement conditions make deletion mechanical; size bound in the file's own header; violation is surfaced, not silently compacted |
| A grant exists that the console doesn't record (skills prose still read as authority) | Medium | D3's iff-rule; migration sweep moves the deploy classes into the ladder and rewrites the prose to reference it; AC-1 checks the closure |
| Master needs the on-merge transition and forgets it (integration used to do it) | Medium | It lands inside the advance-dispatch pass master already runs at every merge — an added call at an existing mandatory step, not a new step; AC-3's window catches omissions as board drift |
| Resolver outage blinds step 8 | Low | Resolver fails loudly by contract; Linear UI is the fallback; an outage blocks dispatch anyway, so the blindness is not additional |

---

## Implementation Notes

**Migration (one-time, owner-supervised):**

1. Author `docs/plans/OWNER_CONSOLE.md`: header states the D2 contract and the size bound; owner
   seeds the standing directives (including the current sequence guidance — the one owner-voice
   content in MASTER_PLAN today) and the initial trust-ladder rows (deploy classes from
   lifecycle-rules § Deploy; dispatch/automation posture; merge/board classes).
2. Disposition sweep of MASTER_PLAN's 340 lines, owner sign-off on the mapping: owner-voice lines →
   console; corrections-to-published-claims → a comment on the ticket that carried the claim;
   unticketed findings → `Backlog` tickets (one-line bodies) or dropped; derived state → deleted
   (its sources are authoritative). Then delete the file.
3. Contract-document edits per D5 (prime-master, prepare-reset, master, lifecycle-rules, worker-skill
   boundary lines, root CLAUDE.md).
4. Owner disables the remaining GitHub–Linear integration transitions in Linear team settings
   (owner-console action, not a repo change); master's advance-dispatch pass takes over the on-merge
   transition from that moment.

**Sequencing:** console must exist before MASTER_PLAN is deleted (no window with neither); skill
edits land with or after the deletion (no window where skills mandate writing a deleted file);
integration disable is independent and may land any time.

**Files affected:** `docs/plans/MASTER_PLAN.md` (deleted) · `docs/plans/OWNER_CONSOLE.md` (new) ·
`.claude/skills/prime-master/SKILL.md` · `.claude/skills/prepare-reset/SKILL.md` ·
`.claude/skills/master/SKILL.md` · `.claude/skills/lifecycle-rules.md` · `.claude/skills/adr/SKILL.md`
and `.claude/skills/build/SKILL.md` (boundary lines) · `CLAUDE.md` (root, sources table).

**Testing strategy:** docs-only chain — CI is the docs path; the behavioural assertions are the
acceptance criteria below, adjudicated by the seam ticket over a live observation window.

---

## Verification / Acceptance Criteria

These are the ADR's own criteria, asserted once by the seam ticket (ADR-0130 D1/D2). Observation
window: **14 days from the last child's merge**.

- **AC-1 — A fresh master's standing authority is resolvable from the console alone.** · **Check:**
  after a real `/clear` + `/prime-master`, ask the primed session, for each ladder action class, "may
  you do this without asking, and on what grant?" — every answer cites a console row (level + date +
  condition); separately, `grep` the skills + lifecycle-rules for authority-granting language
  (standing approval, may deploy without asking, posture) and confirm every hit references the
  console rather than granting. · *Fails if* any authority answer requires lifecycle-rules prose,
  memory, or a plan file — or any grant exists in prose that the console does not record.
- **AC-2 — Nothing re-grows, with no compaction ritual existing.** · **Check:** over the window and
  ≥3 session resets: `OWNER_CONSOLE.md` and `LAST_SESSION.md` each within their stated bounds;
  `git log --diff-filter=A -- docs/plans/` shows no new coordination file; no commit in the window is
  a narrative-stripping compaction of either file. · *Fails if* either file exceeds its bound, a
  successor scratch file appears, or a compaction had to run (regrowth under a new name is this
  criterion's target failure).
- **AC-3 — The board-corruption class is closed and single-writer holds.** · **Check:** reproduce the
  known-bad input — open a docs PR whose body names a designated test ticket's id; the ticket does
  not change state (the FRE-1036 trigger, replayed). Then audit the window's merged tickets in
  Linear history: every state transition was actuated by master or the owner. · *Fails if* the test
  ticket moves, or any transition in the window traces to the integration or a worker.
- **AC-4 — Every console line is owner-attributable and mechanically retirable.** · **Check:** walk
  the console: each directive carries attribution + date + a retirement condition decidable from a
  nameable event; each ladder row carries level + date + promotion/demotion condition; cross-check
  attributed conversational transcriptions against their date's session record for any line the
  owner does not recognise. · *Fails if* any line lacks attribution or a decidable condition, or the
  owner disowns a transcription — i.e. master has authored.
- **AC-5 — The primed target matches the computed queue at read time.** · **Check:** during a fresh
  prime, capture the session's stated NEXT per stream (its step-8 output) and, at the same moment,
  run the dispatch resolver's eligible-set mode per stream; diff. · *Fails if* the primed session
  reports a head or ordering the resolver disputes — the two-accounts failure, which retiring the
  mirror exists to eliminate.

**Seam ticket:** filed with the implementation chain (parked, due-dated to the earliest date all five
are adjudicable — the migration's land date + 14 days). It holds all five criteria; no implementation
ticket carries, restates or discharges any of them.

---

## References

- [FRE-1082](https://linear.app/frenchforest/issue/FRE-1082) — the commissioning ticket: MASTER_PLAN
  is two documents under one name
- [ADR-0130](ADR-0130-two-tiers-of-acceptance-criteria.md) — Proposed — the sibling decision one
  layer down: same thesis (no second copy of a fact with an authoritative source) applied to
  acceptance criteria; supplies the seam-ticket mechanism used here
- [ADR-0113](ADR-0113-self-driving-delivery-loop.md) — Superseded — the autonomy-first delivery-loop
  design whose disproof-in-use motivates making autonomy grants explicit and owner-owned (D3)
- [ADR-0116](ADR-0116-event-driven-dispatch-actuation.md) — Accepted — the actuation channel; D4's
  integration-disable is consistent with its master-actuates model
- [ADR-0110](ADR-0110-external-dispatch-orchestrator.md) — Proposed (transport half superseded by
  ADR-0116) — the dispatch resolver this ADR makes step 8's computed target
- `docs/research/2026-07-29-why-pipeline-point-fixes-do-not-converge.md` — the convergence law: the
  analytical basis for deleting the copy rather than enforcing its size
- `.claude/skills/prime-master/SKILL.md` · `.claude/skills/prepare-reset/SKILL.md` ·
  `.claude/skills/master/SKILL.md` · `.claude/skills/lifecycle-rules.md` — the contract documents
  D5 amends
- [FRE-1075](https://linear.app/frenchforest/issue/FRE-1075) — the partial integration-disable D4
  completes
- [FRE-1036](https://linear.app/frenchforest/issue/FRE-1036) — the board-corruption specimen AC-3
  replays

---

## Status Updates

### 2026-07-31 - Proposed
**Changed By:** adr session (with the owner, FRE-1082)
**Reason:** Initial proposal following the FRE-1082 exploration: measurement of the file's content
classes, the trust-graduation reframing of the delivery process, and the owner's partnership model
(owner reads, master informs, owner decides, master acts).
