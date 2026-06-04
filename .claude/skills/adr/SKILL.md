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
(discussion-mode default).

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

## Boundary
Never edit `src/`, never merge, never deploy, never edit MASTER_PLAN.
