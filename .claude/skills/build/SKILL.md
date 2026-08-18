---
name: build
description: Use in the build session to ship a Linear FRE ticket from Approved to a master-ready PR (CI green + any bounce resolved) — fresh-start reset, plan with risk-tiered codex review, TDD, docs, PR, then self-complete via watcher/master pokes. Never merges or deploys.
---

# Build a Linear Ticket (build session)

Read `.claude/skills/lifecycle-rules.md` first. Argument: **a stream selector** (`1` or `2`), or
an explicit Linear issue ID (e.g. `FRE-471`).

**Stream selector (`1`/`2`) → resolve NEXT via the external dispatch resolver.** FIRST
`git fetch origin` (Step 0 needs latest main), then run
`python -m scripts.dispatch.next_resolver --stream build<N> --json`. A nonzero exit, invalid
JSON, or a printed error → STOP and surface stderr — never reconstruct the busy-guard /
priority / blocked-by logic inline. A `null` result (stream occupied, or no eligible candidate)
→ STOP and ask master. A non-null result names the ticket; honor its **context flag** from the
JSON's `labels`:
- **CLEAR** (default, no `context:keep`): you must be a blank/new session (freshly started or
  just `/clear`ed). If you still carry a previous ticket's context, STOP and tell the owner to
  `/clear` and re-run.
- **KEEP** (`context:keep` present): proceed on the warm context.

An explicit `FRE-…` id skips the queue (treat as CLEAR unless the owner says otherwise).

## Step 0 — Fresh-start (worktree reset + retire the merged branch)
1. `git fetch --prune origin`
2. Safety gate — BOTH must hold, else STOP and surface: `git status --short` empty, and the
   current per-ticket branch merged (or nothing unpushed: `git rev-list --count @{u}..HEAD` = 0).
3. `git switch -c fre-<id>-<slug> origin/main`
4. Retire the merged previous branch — local then remote (`git branch -d <branch>` first; `-d`
   refuses on unmerged, which is the verification; then `git push origin --delete <branch>`).
   **Never** delete the `worktree-build` / `worktree-build2` / `worktree-adrs` anchors.

## 1 — Ticket
`get_issue(<id>)` on FrenchForest; must be `Approved` — if not, STOP and tell the owner
(master may have approved it under its Tier-3 grant; the state still shows it). Then set the
ticket → `In Progress`.

## 1.5 — Done = master-ready (responding to a poke)
**Never poll CI — by any mechanism**: not a `/loop`, not a background shell, not a Monitor —
the watcher already covers both directions (it triggers master on green, pokes this seat on
red). You open the PR, then go idle — but you are done only when the PR is **master-ready**:
CI green AND any bounce resolved. On a poke (watcher red-CI, or a master bounce): read it, fix
on this branch, re-run the Step-6 gates, push, go idle again. Your context is warm; the fix is
cheap.

## 2 — Scope
Read the ticket body + linked ADRs/specs; summarize scope in 3–5 bullets. Read a backing ADR
for **design intent** — the diff must implement it as designed. Pull out **the acceptance
criteria written on this ticket** — testable, outcome-level, decidable from your own
deliverable. If a feature ticket names none, flag the gap before coding; master bounces a PR
with nothing to verify against.

## 3 — Plan + risk-tiered codex review
Write a plan: atomic steps, exact file paths, exact test commands. Self-classify:
- **Trivial** (docs / config / test-only / one-liner, no `src/` logic, no
  schema/security/cost/memory) → skip codex plan-review.
- **Standard / Complex** (touches `src/` logic, schema, security, cost, memory, or multi-file
  behaviour) → **codex plan-review required**: invoke `codex:rescue` on the plan, revise per
  findings. Codex is a reviewer, not a co-author.
When in doubt, treat as Standard. The dispatch may override with `[codex: required]` /
`[codex: skip]`. Master backstops mis-tiering at the gate. One phase = one PR.

## 4 — TDD implement
Failing test first → confirm it fails → implement. Each of this ticket's criteria gets a test
or probe asserting the **outcome**, not the wiring — that is the proof master reads. Standards:
`.claude/CLAUDE.md`; identity threading on every new `log.*` / `bus.publish` / Cypher write.

## 5 — Meet the objective — fold in, don't over-ticket
A ticket is an objective, not a box. Supporting changes needed to make this build function, and
reasonable review findings, are **folded into this PR** and noted in the handoff — no ticket.
File a new ticket ONLY for genuinely separate, sequenceable work or anything ADR-requiring.
When unsure, fold in. Update any docs the change touches.

## 6 — Quality gates (all pass before the PR)
`make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

**Self-review, once, at this gate** (shift-left — fix your own findings so master never
bounces them). **Commit first** — both reviewers diff the committed branch against main;
uncommitted work reads back as a clean empty-diff pass. Then, scoped explicitly to
`git diff origin/main...HEAD`:
- `feature-dev:code-reviewer` (Agent tool) — bugs + standards.
- `security-review` — when the diff touches inputs / subprocess / files / auth / secrets / network.
Fix every confirmed finding on-branch.

**Diff class** — escalate if ANY apply, else self-serve: production write path (or in its call
chain) · destructive/deleting code · schema change · cost or governance code. On escalate: note
in the PR body + handoff "diff class: escalated — flagged for owner `/code-review ultra` before
merge" (a flag, not a wait; go idle after the PR as usual).

## 7 — Codex rescue (escalation only)
3 failed attempts OR same error twice OR self-revert → `codex:rescue` with full error context.

## 8 — PR + handoff comment — then STOP
Sync first: `git fetch origin && git rebase origin/main`, resolve conflicts in-session, re-run
the Step-6 gates, `git push --force-with-lease`. Open the PR with the template — pre-merge
checklist only.

**Then post the handoff comment on the Linear ticket, addressed to master** (required):
- **Per-criterion evidence** — this ticket's own criteria, each with the observed value (test
  name + result, probe/query output, observed behaviour). Backing ADR named for design intent.
- **Self-review summary** — diff class (+ `/code-review ultra` status if escalated), security
  verdict, findings fixed on-branch, anything deliberately left unfixed and why.
- **Post-deploy runbook** + what to verify live (commands + expected output) + any gotchas.
- **Fold-ins** made and any follow-up tickets filed.
- **Context disposition** — keep or clear for the next ticket, and why.

**STOP. Do not merge, deploy, close the ticket, write the owner console, or mutate Linear
control-plane fields** beyond the `In Progress` move at pickup — that is master's role. Filing
tickets and posting comments stay open to you.
