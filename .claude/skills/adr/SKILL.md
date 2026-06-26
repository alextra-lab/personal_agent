---
name: adr
description: Use in the adr session (Opus) to produce a complete ADR — discuss first, write, iterate with codex review, open ADR PR, then file sequenced implementation tickets. Never touches src/ or merges.
---

# Author an ADR (adr session — always Opus)

Read `.claude/skills/lifecycle-rules.md` first. Confirm the session model is Opus; if not, STOP
and tell the owner (ADR authoring is Opus-only).

## Step 0 — Fresh-start (worktree reset)
1. `git fetch origin`
2. Safety gate — BOTH must hold, else STOP and surface:
   - `git status --short` is empty
   - the current per-ADR branch is merged (or there is nothing unpushed: `git rev-list --count @{u}..HEAD` is `0`)
3. Cut a fresh branch off latest main: `git switch -c <next-adr-slug> origin/main`.
4. Retire the merged branch: `git branch -d <merged-adr-branch>` (lowercase `-d` refuses if unmerged).

## 1 — Discuss first
Collaborate with the owner on the decision. Do NOT write any file until the decision is settled
(discussion-mode default). If this work has an Approved ADR ticket (e.g. FRE-582), **set it →
In Progress** now (`save_issue state="In Progress"`) — Linear is disconnected from GitHub
(2026-06-26), so status no longer moves automatically; the working session owns the In Progress
transition, master owns Done.

## 2 — Write the ADR
Author the best, complete ADR in the project ADR format under `docs/architecture_decisions/`.

## 3 — Codex iterative review
Invoke **codex:rescue** to review the ADR. Revise per findings. Repeat until no blocking findings,
**max 3 rounds**. Log each round's findings in the PR description.

## 4 — PR
Open the ADR PR (docs). Pre-merge checklist only.

## 5 — Implementation tickets
File the implementation tickets in Linear: Needs Approval, under a Linear project, sequenced with
dependencies. The owner approves → the build session picks them up.

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
