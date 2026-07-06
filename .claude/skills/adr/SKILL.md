---
name: adr
description: Use in the adr session (Opus) to produce a complete ADR — discuss first, write, iterate with codex review, open ADR PR, then file sequenced implementation tickets. Never touches src/ or merges.
---

# Author an ADR (adr session — always Opus)

Read `.claude/skills/lifecycle-rules.md` first. Confirm the session model is Opus; if not, STOP
and tell the owner (ADR authoring is Opus-only).

**Argument: none → resolve NEXT from Linear** (dispatch contract: lifecycle-rules § Dispatch).
**FIRST `git fetch origin`** (you still need latest main for Step 0). Then: busy guard —
`list_issues(team="FrenchForest", label="stream:adr")` with `state="In Progress"`, and again with
`state="In Review"`: either non-empty → STOP (the stream is occupied — building, or a PR at master's
gate that could bounce back; it frees at `Awaiting Deploy`). Otherwise take the head of
`list_issues(team="FrenchForest", state="Approved", label="stream:adr")` ordered by priority then
oldest-created, skipping any issue with an open "blocked by" relation (`get_issue` with
includeRelations — a blocker is open until its merge lands: any state before
`Awaiting Deploy`/`Done`/`Canceled`/`Duplicate`). Honor its **context flag** exactly as `/build` does —
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

## 1 — Discuss first
Collaborate with the owner on the decision. Do NOT write any file until the decision is settled
(discussion-mode default). **adr dev is always tracked by a Linear ticket** (same as build) — dispatch
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

## 1.5 — (no monitor loop)
Do **not** arm a `/loop` monitor. A 20-minute poll re-read the session context past the 5-minute
prompt-cache TTL every tick → an uncached-token cost blowup (removed 2026-07-06). This ADR session
does its work and **stops at PR**. If master bounces the PR or CI goes red, the **owner re-runs
`/prime-worker` in this session** on demand to self-fix (its Step 3.2).

## 2 — Write the ADR
Start from **`docs/architecture_decisions/ADR_TEMPLATE.md`** — the project ADR format (mirrors the
`alextra-lab/ai_operations` canonical). Author the best, complete ADR under
`docs/architecture_decisions/`. **Two structural rules history kept drifting from:** the **References**
section is a bulleted list, one ref per line — **never** a run-on `**Related:**` paragraph; and **keep
your own Status line current** — never cite another ADR by a stale status. The template also requires
**≥2 Alternatives Considered** (with why-rejected) — not a single option presented as a pure win.

**Every ADR MUST carry a Verification / Acceptance-Criteria section** — the upstream half of the
master acceptance-criteria gate (master SKILL Step 4). Each criterion is a **testable, discriminating
invariant stated at the outcome altitude** — the observable result that proves the decision delivered,
NOT a restatement of the mechanism. State *how* each is checked, reusing existing instrumentation
where it exists (a Neo4j/ES query, the joinability probe, a test assertion, a curl) so it is provable
without new test infrastructure.
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
acceptance criteria are testable and outcome-level** — could a build session prove each, and would a
bad implementation fail it? Treat any mechanism-restatement or un-checkable criterion as a blocking
finding.

## 4 — PR
**Sync to latest main FIRST** (a sibling PR may have merged during your session): `git fetch origin &&
git rebase origin/main` — resolve any conflicts in-session, then `git push --force-with-lease`. Then
open the ADR PR (docs). Pre-merge checklist only.

## 5 — Implementation tickets
File the implementation tickets in Linear: Needs Approval, under a Linear project, sequenced with
dependencies. The owner approves → the build session picks them up. **Each ticket carries the slice of
the ADR's acceptance criteria it must satisfy + how each is proven** — so criteria flow unbroken
ADR → ticket → build handoff → master gate, and no child is "done" without its invariant shown.
**Name who owns the assembled-ADR seam** — the criterion that holds only once all children land — so
a decomposed ADR does not close just because its last child did.

## 6 — Handoff comment for master — then STOP
**Post a final comment on the ADR's Linear ticket addressed to master** (`save_comment` on the ADR
umbrella issue) — required, not optional. It carries what master needs at the integration gate that
does NOT belong in the ADR PR's pre-merge checklist:
- the **intended ADR status** on merge (Proposed / Accepted / Implemented) and any status-field change
  master should make;
- the **implementation tickets filed + sequence/dependencies** (so master can track the chain);
- any **doc-drift** master should reconcile (related ADRs, MASTER_PLAN, CLAUDE.md status);
- **your context disposition for the next ADR** — kept or cleared (`/clear`), and why.
Master reads this comment by default at the gate, so it is the handoff channel.

**STOP. Never edit `src/`, never merge, never deploy, never edit MASTER_PLAN** — that is master's role.

## Boundary
Never edit `src/`, never merge, never deploy, never edit MASTER_PLAN.
