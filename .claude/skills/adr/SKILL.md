---
name: adr
description: Use in the adr session (Opus) to explore an idea with the owner and — if it earns it — produce a complete ADR. Discuss/explore first, write, iterate with codex review, open ADR PR, then file sequenced implementation tickets. An ADR is a possible outcome, not a mandate. Never touches src/ or merges.
---

# ADR session — explore, then (if earned) author (always Opus)

Read `.claude/skills/lifecycle-rules.md` first. Confirm the session model is Opus; if not, STOP
and tell the owner (ADR authoring is Opus-only).

**Argument: none → resolve NEXT via the external dispatch resolver** (`scripts/dispatch/next_resolver.py`,
FRE-785; the busy guard, priority ordering, and blocked-by skip are NOT logic held in this skill —
ADR-0113 §1). **FIRST `git fetch origin`** (you still need latest main for Step 0). Then run
`python -m scripts.dispatch.next_resolver --stream adr --json`. A nonzero exit, invalid JSON, or a
printed error (e.g. missing `AGENT_LINEAR_API_KEY`) → STOP and surface stderr — never fall back to
reconstructing the busy-guard/priority/blocked-by logic inline. A `null` result (the stream is
occupied — building, or a PR at master's gate that could bounce back; it frees at `Awaiting Deploy` —
OR there is no eligible `Approved` candidate; the resolver conflates both) → STOP. A non-null result
names the ticket — read its `context:keep` status straight off the JSON's `labels` field. Honor its
**context flag** exactly as `/build` does —
**CLEAR** (default, no `context:keep` label): **first check if you are a blank/new session** (freshly
started or just `/clear`ed, essentially nothing in context but this invocation); if yes, proceed; if no
(you still carry a previous ADR's work), STOP and tell the owner to `/clear` then re-run `/adr`.
**KEEP** (`context:keep` label present): proceed on the warm context regardless. An explicit `FRE-…` id
overrides the queue. If the queue is empty or ambiguous, STOP and ask master.

## Step 0 — Fresh-start (worktree reset)
1. `git fetch origin`
2. Safety gate — BOTH must hold, else STOP and surface:
   - `git status --short` is empty
   - the current per-ADR branch is merged (or there is nothing unpushed: `git rev-list --count @{u}..HEAD` is `0`)
3. Cut a fresh branch off latest main: `git switch -c <next-adr-slug> origin/main`.
4. **Retire the merged branch — local THEN remote** (so branches don't pile up on origin). The lowercase
   `-d` is the verification: it refuses if unmerged, so only a merged branch is deleted; run the remote
   delete only after `-d` succeeds.
   - `git branch -d <merged-adr-branch>`
   - `git push origin --delete <merged-adr-branch>`

## 1 — Discuss / explore FIRST — genuinely, with the owner (hard gate, not a formality)

**This is the load-bearing step. Violating it is the single worst failure of this skill.**

**Two modes — sense which one you're in:**
- **Assemble** — the decision already lives in the ticket; your job is to piece the ADR together from
  what's settled. Faster, artifact-first. Discussion is lighter (confirm the shape), but still real.
- **Explore** — the session opens as a half-formed idea or research thread and develops *— or doesn't —*
  into an ADR. Here **the ADR is a possible outcome, not the goal.** Your job is to **explore + teach +
  ideate as a peer AND a tutor**: surface the concepts, weigh alternatives out loud, cite references, go
  deep. The exploration itself is the value (the owner is "forever the student"). **An ADR · a research
  note · a parked idea · "not ADR-worthy yet" are ALL valid successes** — never force an artifact the
  exploration didn't earn. The "explicit go to write" (below) is reached only if a decision crystallizes.

The anti-pattern that is FORBIDDEN (it happened on FRE-809 and the owner rejected the ADR outright):
*read the ticket → ask 2–3 clarifying questions → immediately write the ADR and open the PR.* That is
NOT discussion. "Three questions and voilà, a PR and an ADR" is exactly what must never happen.

What discussion actually means here — you are ideating WITH the owner, as a peer, before a single
file is written:
- **Open by laying out the decision and the real tension** — what's genuinely hard or contested about it,
  not a checklist of clarifications.
- **Weigh pros and cons out loud, together.** Present the alternatives (the ADR needs ≥2 anyway) and
  argue them — surface trade-offs, name what you'd reject and why, and invite the owner to push back.
- **Expect multiple rounds of back-and-forth.** The owner will challenge, add constraints, redirect,
  and ideate. Follow their lead; change your position when they're right; defend it when you have
  reason. This is a conversation, possibly a long one — not an interview.
- **Do NOT write ANY file, and do NOT open a PR, until the owner EXPLICITLY signals the design is
  settled** (e.g. "write it up", "draft it now"). Absent that explicit go, you are still discussing.
  If unsure whether they've said go, ask — never assume.

Only after that explicit go do you proceed to Step 2 (write). The discussion is the ADR's real work;
the document just records a decision you and the owner reached together.

**adr dev is always tracked by a Linear ticket** (same as build) — dispatch
resolves from Linear (`Approved` + `stream:adr` label), and `prime-worker` monitors the same queue, so
untracked ADR work is invisible to the loop. Therefore:
- If an ADR umbrella ticket already exists (e.g. FRE-582), **set it → In Progress** now
  (`save_issue state="In Progress"`).
- If this is ad-hoc work with no ticket yet, **file the umbrella ticket first** (Needs Approval, under a
  Linear project) so the work is referenced from the start; the owner approves it + master labels it
  `stream:adr` → it becomes the queue head → then set it In Progress.

The GitHub integration automates only the PR transitions (PR opened → `In Review`, PR merged →
`Awaiting Deploy` — retargeted 2026-07-04); the working session owns the In Progress transition,
master owns Done.

## 1.5 — Done = master-ready (responding to a poke)
**Never arm a `/loop` monitor** — polling re-reads the session context past the prompt-cache TTL every
tick (an uncached-cost blowup, removed 2026-07-06). You do the work and open the ADR PR, then go idle —
but you are done only when it is **master-ready** (any bounce resolved; CI is docs-only so red is rare).
You don't *wait* — you go idle, and something re-engages this warm seat: a **master bounce** (doc-drift, a
stale Status line) comes **directly from master**; a red docs check comes from the **watcher**. Either
poke: read it, fix on this branch, push, go idle again. *(This is the old `/prime-worker`, folded in.)*

## 2 — Write the ADR
**Number it authoritatively — never eyeball `ls`** (that caused the ADR-0117 double-number,
2026-07-14): `git fetch origin && python scripts/next_adr.py --next` prints the next free number, read
from `origin/main` so a stale worktree can't re-pick a just-merged number. **After writing the ADR, add
its row to the `docs/architecture_decisions/README.md` index in the same commit** — the
`check-adr-index` pre-commit hook fails a commit where a file has no index row (or an index row has no
file).

Start from **`docs/architecture_decisions/ADR_TEMPLATE.md`** — the project ADR format (mirrors the
`alextra-lab/ai_operations` canonical). Author the best, complete ADR under
`docs/architecture_decisions/`. **Two structural rules history kept drifting from:** the **References**
section is a bulleted list, one ref per line — **never** a run-on `**Related:**` paragraph; and **keep
your own Status line current** — never cite another ADR by a stale status. The template also requires
**≥2 Alternatives Considered** (with why-rejected) — not a single option presented as a pure win.

**Every ADR MUST carry a Verification / Acceptance-Criteria section.** These are the **ADR's own**
criteria: they state the ADR's objective, they **stay with the ADR**, and they are asserted in exactly
one place — its **seam ticket** (Step 5; ADR-0130 D1/D2). They are never sliced across implementation
tickets. Because they are the ADR's, they are *allowed* to be assembled, population-level,
long-horizon, or to require the owner to do something; the constraint that a criterion be decidable
from a single ticket's own deliverable applies to **implementation tickets**, not here.

The outcome-altitude bar below is therefore the bar for the **ADR's** criteria. Each is a **testable,
discriminating invariant stated at the outcome altitude** — the observable result that proves the
decision delivered, NOT a restatement of the mechanism. State *how* each is checked, reusing existing
instrumentation where it exists (a Neo4j/ES query, the joinability probe, a test assertion, a curl) so
it is provable without new test infrastructure.
- Good (outcome): "owner facts are queryable from the `is_owner:true` node" · "a dormant edge past
  TTL is actually evicted, not just flagged" · "the guard fails CI on a known-bad input".
- Bad (mechanism, not outcome): "the curation gate is wired in" · "the freshness consumer runs".

**No-BS bar — the criterion must be able to fail.** Before accepting any criterion, ask: *could a
broken or half-finished implementation still satisfy it?* If yes, it is BS — rewrite it until only a
working outcome passes. Reject the usual fakes: existence-checks standing in for behaviour ("the field
exists" vs "the field holds the *right* value"), "tests pass" where no test asserts the actual
invariant, vanity counts decoupled from the outcome, and any line that just restates the task. A
criterion no plausible bug can violate verifies nothing. Codex enforces this at Step 3.

If a criterion genuinely cannot be made checkable, say so and explain why — an un-testable decision is
a design smell to surface, not paper over.

## 3 — Codex iterative review
Invoke **codex:rescue** to review the ADR. Revise per findings. Repeat until no blocking findings,
**max 3 rounds**. Log each round's findings in the PR description. **Codex must explicitly check the
acceptance criteria are testable and outcome-level** — could the ADR's **seam ticket** prove each from
the stated check, and would a bad implementation fail it? Treat any mechanism-restatement or
un-checkable criterion as a blocking finding.

## 3.5 — Code-review obligation when this diff carries executable code
A session producing executable code — not just the ADR document — carries the same code-review
obligations as build, regardless of stream (FRE-1128; the FRE-1122 diff shipped 4,258 lines
including a graph-deleting script with no security verdict, because this contract never asked).

**Executable code, decided by path, not by reading intent:** any diff file **outside**
`docs/architecture_decisions/**/*.md` that is itself a runnable artifact — a script (`.py`/`.sh`), a
SQL migration, a CI workflow file, a notebook, or anything under `scripts/`/`sandbox/`/`src/`. A
fenced code block inside the ADR's own markdown (a design recipe, not a checked-in runnable file)
does **not** count. Genuinely ambiguous → treat as executable.

If present: commit to the branch first (same commit-first precondition as build — both reviewers
diff committed-branch-vs-main, so uncommitted work reads back as a clean empty-diff pass). Run
`feature-dev:code-reviewer` and `security-review` on `git diff origin/main...HEAD`, fix confirmed
findings on-branch, and route the diff through build SKILL Step 8's same self-serve/escalate
triggers (production write path, destructive/deleting, schema change, cost/governance code). On
escalate, flag it the same way build does — PR body + ticket handoff: "diff class: escalated —
flagged for owner `/code-review ultra` before merge."

## 4 — PR
**Sync to latest main FIRST** (a sibling PR may have merged during your session): `git fetch origin &&
git rebase origin/main` — resolve any conflicts in-session, then `git push --force-with-lease`. Then
open the ADR PR (docs). Pre-merge checklist only.

## 5 — Implementation tickets
File the implementation tickets in Linear: Needs Approval, under a Linear project, sequenced with
dependencies. The owner approves → the build session picks them up.

**Never slice the ADR's acceptance criteria across its children** (ADR-0130 D1). The ADR's criteria
stay with the ADR. **Each implementation ticket gets acceptance criteria written for its own work** —
the change that ticket makes, stated as an observable result of that change, **decidable from that
ticket's own deliverable when the ticket is finished**. Step 2's no-BS bar applies unchanged at that
smaller scope. A child never carries, quotes, restates or discharges any part of the ADR's criteria,
in the ADR's wording or a paraphrase of it.

**Severing inheritance must not lose coverage, and the check is yours.** The union of the chain's
sub-ticket criteria has to cover **every** design obligation the ADR's Decision section places on the
chain, or the design can be abandoned one child at a time while every child passes. Publish an explicit
**obligation → owner mapping** on the umbrella — every obligation against the child or the seam that
owns it. It is a **partition, not a filter**: every obligation lands on exactly one child or on the
seam, and none is allowed to land nowhere. The distinction is *whose objective the criterion states*,
not which subjects it may mention — if preserving a field **is** a ticket's own work, that ticket
carries a criterion about exactly that; what it may not do is cite the ADR's `AC-n` as the thing it
discharges. **Anything not decidable from a single child's deliverable belongs to the seam by
definition**, including cross-child integration obligations, which belong to neither child.

**An obligation naming two parties may not carry a single non-seam owner** (ADR-0137 D1). A sentence
with two parties — *A ships to B*, *A reads B's output*, *A reaches B*, *A gets X from B* — is **split**,
under two rules applied in order:
1. **Assignment follows who provisions the thing, never the grammatical subject.** ADR-0129's row D5.d
   read *"`slm_server` gets a network endpoint to ship to"* and was assigned to `slm_server` — the
   consumer, in another repository, which could provision nothing. FRE-1220 is what that cost.
2. **Each half must be stated at an altitude its own owner's deliverable can decide.** A half still
   undecidable after the split is **not** forced onto either ticket — it goes to the seam. So a split
   is often **three rows**: provider, consumer, and the end-to-end property at the seam. A two-way-only
   split re-parks a cross-child property on a child, which is the ADR-0130 D1 violation this rule exists
   to prevent.

A provider half **no ticket in the chain can make true** has exactly two resolutions: file a
provisioning ticket **and write its `blockedBy` relation in the same action**, or assign the half to the
seam. **Recording it in prose with no owner is not a resolution** — that is the state FRE-1220 was
already in. The signal to watch for is an **unfillable "Proved by" cell** on a provider row; that is the
gap becoming visible at authoring time, which is the cheapest moment it can.

**A row's named criterion must *entail* the obligation, not merely sit on the right ticket**
(ADR-0137 D2). **For every row** — one-party and split alike — read the criterion named in "Proved by"
and confirm that **a passing verdict on it makes the obligation true**. Presence asks *is there an owner
and a criterion?*; sufficiency asks *would this criterion, passing, make this sentence true?* D5.d passed
the first and failed the second, and an adversarial review that caught six owner-without-criterion rows
did not catch it. A row that fails is resolved three ways: name a criterion that does entail it, file a
covering ticket with its id on the row, or assign the row to the seam. A flag may be **withdrawn only**
on the finding that the original criterion does entail the obligation after all — never on a judgement
that the gap is acceptable.

**Name the ADR's seam ticket and file it with the chain** (ADR-0130 D2) — exactly one per ADR, even a
single-ticket one. It holds **all** of the ADR's criteria, is filed **parked** (`Backlog`, or
`Approved` with no `stream:` label), and carries a Linear **due date** set to the earliest date all of
those criteria become adjudicable. Master activates it at the first advance-dispatch on or after that
date, and an `adr` session adjudicates it. Full lifecycle and actors: lifecycle-rules § Ticket state ›
Seam tickets. If the ADR's objective genuinely cannot be adjudicated at all, say so when authoring it
rather than filing a seam ticket that can never return a verdict.

## 6 — Handoff comment for master — then STOP
**Post a final comment on the ADR's Linear ticket addressed to master** (`save_comment` on the ADR
umbrella issue) — required, not optional. It carries what master needs at the integration gate that
does NOT belong in the ADR PR's pre-merge checklist:
- the **intended ADR status** on merge (Proposed / Accepted / Implemented) and any status-field change
  master should make;
- the **implementation tickets filed + sequence/dependencies** (so master can track the chain);
- the **seam ticket** (ADR-0130 D2) — its Linear id, its **due date**, and the **obligation → owner
  mapping** from Step 5, listing every Decision-section obligation against the child or the seam that
  owns it, so master can confirm the partition has no unowned row before dispatching the chain;
- any **doc-drift** master should reconcile (related ADRs, CLAUDE.md, a skill or lifecycle-rules contract);
- **if this diff carried executable code (§ 3.5)** — the same self-review summary build reports (diff
  class self-serve/escalated, owner `/code-review ultra` outcome if escalated, findings fixed/deferred,
  security-review verdict), **plus per-criterion evidence**: for any of the ADR's own stated criteria
  this diff's code already demonstrates, the observed value — labeled explicitly as **preliminary
  observed evidence from authoring time, not a substitute for the seam ticket's eventual adjudication**
  (Step 2's criteria are still asserted in exactly one place — the seam ticket);
- **your context disposition for the next ADR** — kept or cleared (`/clear`), and why.
Master reads this comment by default at the gate, so it is the handoff channel. **These fields are the
handoff contract** master trusts without re-deriving (lifecycle-rules § Signal trust boundary).

**STOP. Never edit `src/`, never merge, never deploy, never write the owner console, and never mutate
Linear control-plane fields beyond moving your own ticket to `In Progress`** — that is master's role.
Filing tickets and posting comments stay open to you (lifecycle-rules § Coordination stores).

## Boundary
Never edit `src/`, never merge, never deploy, never write `docs/plans/OWNER_CONSOLE.md`, never mutate
Linear control-plane fields (states beyond your own pickup, labels, relations, priorities).
