# Design: Role-Scoped Session Skills + Deploy-Approval Gate

> **Date**: 2026-06-04
> **Status**: Design — pending implementation plan
> **Author**: master CC session (brainstormed with owner)
> **Supersedes**: `.claude/skills/ship-ticket/` (split into role skills)

## 1. Problem

The project runs three long-lived Claude Code sessions — **master** (integrator/guardian on
`main`), **build** (feature work in `worktree-build`), **adr** (design work in `worktree-adrs`).
This separation-of-duties model is sound (insights: 65 fully-achieved vs 2 partial sessions) and is
kept deliberately. The friction is **not** the session count — it is that the boundaries between
roles are *memory-enforced*, so the same slips recur across sessions:

- post-deploy items placed in PR checklists (most-repeated violation; persisted past a saved memory rule)
- MASTER_PLAN committed to a feature branch instead of `main`
- tickets marked Done when only deferred
- deploying without explicit approval (occurred this session: deployed FRE-468/473/469 unprompted)
- wrong initial approach/scope caught late (top friction category)
- bugs surfacing only in late branch testing

**Goal:** move the boundaries from memory into machine-enforced skills + hooks. Keep the 3-session
model; make each role's stop-line and each cross-role prohibition mechanical. Pure control gain.

## 2. Non-goals

- **Not** collapsing the 3 sessions into one autonomous wave-runner. That trades control for
  throughput; the owner explicitly values control. Subagent fan-out is out of scope.
- **Not** changing the deploy mechanics (`ENV=cloud make rebuild SERVICE=seshat-gateway`).
- **Not** removing or rewriting any existing hook; the four current hooks stay.

## 3. Architecture

### 3.1 Shared core — `.claude/skills/lifecycle-rules.md`

Single source of truth for invariants that must never drift. Each role-skill reads it as step 0.
Coding standards stay in `.claude/CLAUDE.md` (already present) and are *referenced*, not copied.

Contents:
- **PR hygiene**: PR checklist contains pre-merge items ONLY. Post-deploy verification, telemetry
  checks, deploy steps are **forbidden** in the checklist — they go in a Linear comment.
- **Session boundary**: build & adr stop at "push branch + open PR". master alone merges, deploys,
  verifies, closes tickets, updates MASTER_PLAN.
- **MASTER_PLAN rule**: committed to `main` only, never a feature branch; "Last updated" line bumped
  every time a ticket ships.
- **Ticket-state gate**: implement only `Approved` tickets; "deferred ≠ Done".
- **Halt conditions** (shared): not-Approved ticket, unexpected worktree branch, plan bundling
  multiple ADR phases, historical-row drop/quarantine, >5 pre-existing mypy errors, deploy succeeds
  but live endpoint wrong, joinability orphans, same error after 3 fix attempts.

### 3.2 `/build` — build worktree

0. **Fresh-start** (see §4): safety gate, then offer to cut a clean branch off `origin/main`.
1. Retrieve ticket via Linear MCP; must be `Approved`. Report worktree + branch (paste).
2. Read ticket body + linked ADRs + specs → 3–5 bullet scope summary.
3. Plan: atomic steps, exact file paths, exact test commands → **Codex reviews the plan**
   (approach second-opinion, targets wrong-approach friction) → revise → explicit owner approval.
4. TDD: failing test first, confirm it fails, then implement. Standards + ADR-0074 identity
   threading on every new `log.*` / `bus.publish` / Cypher `MERGE|CREATE`.
5. File follow-up tickets if discovered → **Needs Approval, under a Linear project** (default: the
   project of the ticket being worked).
6. Create/update documentation touched by the change (skill docs, READMEs).
7. **Codex rescue** on escalation: 3 failed attempts OR same error twice OR self-revert.
8. Quality gates (all pass before PR): `make test` (module then full), `make mypy`,
   `make ruff-check` + `make ruff-format`, `pre-commit run --all-files`.
9. Open PR using `.github/PULL_REQUEST_TEMPLATE.md` (pre-merge checklist only). **STOP.**
   Never merges, never deploys.

### 3.3 `/master` — primary `/opt/seshat`

1. Take a ready PR (argument) or scan open PRs (`gh pr list`).
2. **Analyze the diff**: correctness review (`code-review` skill) + **security pass
   (`security-review` skill on the diff)**.
3. **Doc-drift check**: does this PR require MASTER_PLAN / `CLAUDE.md` "Current status" / ADR status
   field updates? Flag drift before merge.
4. Verify ticket state, PR hygiene (reject if post-deploy items in checklist), CI green.
5. Merge to GitHub `main`; pull.
6. **Ask: "deploy now?"** — never auto-deploy. (Enforced by the §3.5 hook.)
7. On approval: deploy (`ENV=cloud make rebuild SERVICE=seshat-gateway`) → `curl /health` + live
   endpoint verify (paste evidence) → conditional joinability probe if the PR touched an emit
   site/schema/cost/memory write → update MASTER_PLAN on `main` → close Linear (PR link, deploy
   timestamp, verification evidence snippet).

### 3.4 `/adr` — adr worktree, **always Opus**

0. **Fresh-start** (see §4): safety gate, then cut a clean ADR branch off `origin/main`.
1. **Discuss first** — collaborate with owner; no file writes until the decision is made.
2. Write the best complete ADR (project ADR format).
3. **Codex iterative review** — revise until no blocking findings, **max 3 rounds**; each round's
   findings logged in the PR description.
4. Open the ADR PR.
5. File implementation tickets → Needs Approval, under a Linear project, sequenced with dependencies.
6. Never touches `src/`, never merges, never edits MASTER_PLAN.

### 3.5 New hook — `deploy-approval-gate.sh` (PreToolUse / Bash)

Detects the session's role by worktree path (`git rev-parse --show-toplevel`):

- **build & adr worktrees** (`.claude/worktrees/build`, `.claude/worktrees/adrs`): **hard-deny** any
  command matching `make rebuild`, `make deploy`, `make build`, `make build-full`, or
  `ENV=cloud make …`. Those sessions are physically incapable of deploying. Airtight.
- **master (primary tree `/opt/seshat`)**: deploy commands require a fresh approval sentinel
  (`.claude/.deploy-approved`, mtime < 5 min) that the `/master` skill writes *only after* the owner
  answers "deploy now? yes". The hook consumes (deletes) the sentinel on use. Converts a silent
  deploy into an explicit, owner-gated action.

All four existing hooks (`check-pytest-lock`, `check-forbidden-patterns`, `format-python`,
`nudge-discussion-mode`) are unchanged.

### 3.6 `/prime-master` — guardian context reset

Reconstructs the guardian snapshot from **durable sources only**, never from prior conversation:
1. MEMORY.md (auto-loaded) — standing rules.
2. MASTER_PLAN header + "Last updated" + Pending Verification + Needs Approval.
3. `git status`, `git worktree list`, `gh pr list` (open PRs awaiting master).
4. Linear: In Progress + Pending Verification tickets.
5. `curl /health` — live gateway health + deployed SHA.

Output: the re-prime block shape (current state, next-per-sequence, active pending verification,
identity guardrails). **Master reset = `/clear` → `/prime-master`**, fully deterministic from
substrate.

## 4. The three reset patterns

All three obey one rule: *reset only when nothing of value lives solely in the thing being reset.*

| Reset | Applies to | Safety gate | Action |
|-------|-----------|-------------|--------|
| **Worktree (persistent)** | build (`worktree-build`) | clean tree AND 0 unpushed (`git rev-list --count @{u}..HEAD` == 0) | `git merge --ff-only origin/main` + `git push origin worktree-build` |
| **Worktree (ephemeral)** | adr (per-ADR branches) | clean tree AND old branch merged | `git switch -c <next-slug> origin/main`; `git branch -d <merged>` |
| **Context** | master session | Pending Verification none AND no mid-flight PR/deploy AND MASTER_PLAN↔Linear in sync AND clean `main` | `/clear` → `/prime-master` |

`--ff-only` and `-d` (lowercase) are the worktree safety valves — they refuse rather than clobber.
The master integration-boundary gate is the context equivalent. **Step 0 fresh-start** in `/build`
and `/adr` runs the relevant worktree gate before any new work begins, so starting a session *is*
the reset.

## 5. Security posture

- Existing: `check-forbidden-patterns.sh` (blocks `print`/`os.getenv`/bare-`except` on Edit/Write),
  `pre-commit` (personal-paths, substrate-isolation, identity threading).
- **New**: `security-review` skill runs on every PR diff as part of `/master` step 2 — the missing
  automatic pre-merge security pass.

## 6. File inventory

| Path | Action |
|------|--------|
| `.claude/skills/lifecycle-rules.md` | **new** — shared invariants |
| `.claude/skills/build/SKILL.md` | **new** |
| `.claude/skills/master/SKILL.md` | **new** |
| `.claude/skills/adr/SKILL.md` | **new** |
| `.claude/skills/prime-master/SKILL.md` | **new** |
| `.claude/hooks/deploy-approval-gate.sh` | **new** |
| `.claude/settings.json` | **edit** — register the PreToolUse Bash hook |
| `.claude/skills/ship-ticket/` | **retire** — content redistributed to build/master |

## 7. Acceptance criteria

- [ ] `lifecycle-rules.md` holds every invariant; no invariant duplicated inside a role skill.
- [ ] `/build`, `/master`, `/adr`, `/prime-master` each load and reference the shared rules.
- [ ] `deploy-approval-gate.sh` hard-denies deploy commands when CWD resolves to the build or adr
      worktree (test: invoke a `make rebuild` from each → blocked).
- [ ] In the primary tree, a deploy command is denied unless a fresh `.deploy-approved` sentinel
      exists; sentinel is consumed on use (test: deploy without sentinel → blocked; with → allowed,
      then blocked again).
- [ ] `ship-ticket` removed; no dangling references to it.
- [ ] Each skill's Step 0 fresh-start runs the correct worktree safety gate.
- [ ] `/master` step 2 invokes `security-review` on the diff.

## 8. Open defaults (flag at review if wrong)

- Codex ADR-review caps at **3 rounds**.
- Follow-up tickets default to **the Linear project of the ticket being worked**.
- Master deploy sentinel TTL = **5 minutes**.

## 9. References

- `.claude/CLAUDE.md` — coding standards, model routing policy, worktree merge gotcha.
- `.claude/skills/ship-ticket/SKILL.md` — the monolith being split.
- Memory: `feedback_master_is_guardian_of_live_env`, `feedback_confirm_before_shared_gateway_deploy`,
  `feedback_build_worktree_pr_scope`, `feedback_worktree_branch_ownership`,
  `project_three_session_architecture`, `feedback_update_master_plan`.
- Insights report `2026-06-04-114426` — friction categories driving this design.
