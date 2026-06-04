---
name: build
description: Use in the build session to ship a Linear FRE ticket from Approved to PR — fresh-start reset, plan with codex review, TDD, follow-up tickets, docs, PR. Stops at PR; never merges or deploys.
---

# Build a Linear Ticket (build session)

Read `.claude/skills/lifecycle-rules.md` first. Argument: a Linear issue ID (e.g. `FRE-471`), or omitted (pick the top Approved ticket from MASTER_PLAN).

## Step 0 — Fresh-start (worktree reset)
1. `git fetch origin`
2. Safety gate — BOTH must hold, else STOP and surface:
   - `git status --short` is empty
   - `git rev-list --count @{u}..HEAD` is `0` (nothing unpushed)
3. Sync the persistent branch: `git merge --ff-only origin/main` then `git push origin worktree-build`.
4. Confirm branch + worktree (`git worktree list`, `git branch --show-current`); paste.

## 1 — Ticket
`get_issue(<id>)` on FrenchForest; must be `Approved`. If `Needs Approval`, STOP and tell the owner.

## 2 — Scope
Read ticket body + linked ADRs + specs. Summarize scope in 3–5 bullets.

## 3 — Plan + codex review
Write a plan: atomic steps, exact file paths, exact test commands. Then invoke **codex:rescue**
to review the plan (approach second-opinion). Revise per findings. Get explicit owner approval
before coding. (One phase = one PR — see halt conditions.)

## 4 — TDD implement
Failing test first → confirm it fails → implement. Standards (`.claude/CLAUDE.md`) + ADR-0074
identity threading on every new `log.*` / `bus.publish` / Cypher `MERGE|CREATE`.

## 5 — Follow-up tickets
File any discovered work as new issues — Needs Approval, under a Linear project (default: the
project of the ticket being worked).

## 6 — Documentation
Update docs the change touches (skill docs, READMEs, doc-strings).

## 7 — Codex rescue (escalation only)
3 failed attempts OR same error twice OR self-revert → invoke **codex:rescue** with full error context.

## 8 — Quality gates (all pass before PR)
`make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
`pre-commit run --all-files`.

## 9 — PR — then STOP
Open the PR with `.github/PULL_REQUEST_TEMPLATE.md`. Pre-merge checklist ONLY (see lifecycle-rules
PR hygiene). Push the branch. **STOP. Do not merge, deploy, close the ticket, or edit MASTER_PLAN** —
that is master's role.
