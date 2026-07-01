---
name: build
description: Use in the build session to ship a Linear FRE ticket from Approved to PR — fresh-start reset, plan with codex review, TDD, follow-up tickets, docs, PR. Stops at PR; never merges or deploys.
---

# Build a Linear Ticket (build session)

Read `.claude/skills/lifecycle-rules.md` first. Argument: **a stream selector** (`1` or `2`), or an explicit Linear issue ID (e.g. `FRE-471`).

**Stream selector (`1`/`2`) → resolve from the Stream Board.** Read the `## 🎛️ Stream Board` in `docs/plans/MASTER_PLAN.md` (between the `<!-- STREAM-BOARD:START -->` / `END` markers). Find the `/build <N>` row; the **NEXT** cell's bold `FRE-…` is the ticket to build. Honor that row's **Context** flag:
- **CLEAR** (the default): this ticket wants a fresh slate. **First check: are you a blank/new session** — freshly started or just `/clear`ed, with essentially nothing in context but this invocation and session-start priming? If **yes**, proceed. If **no** (you still carry a previous ticket's work in context), STOP and tell the owner: "FRE-… is flagged CLEAR — run `/clear`, then `/build <N>` again." A stale prior-ticket context pollutes the plan.
- **KEEP**: the NEXT ticket is a direct follow-on (same files/substrate) — proceed on the current warm context regardless of the blank-session check; do not ask for a `/clear`.

An explicit `FRE-…` id skips the board and builds that ticket (treat Context as CLEAR unless the owner says otherwise). If the board row is missing/ambiguous, STOP and ask master.

## Step 0 — Fresh-start (worktree reset + retire the merged branch)
1. `git fetch --prune origin`
2. Safety gate — BOTH must hold, else STOP and surface:
   - `git status --short` is empty
   - the current per-ticket branch is merged (or nothing unpushed: `git rev-list --count @{u}..HEAD` is `0`)
3. Cut a fresh branch off latest main for the new ticket: `git switch -c fre-<id>-<slug> origin/main`.
4. **Retire the now-merged previous branch — local THEN remote** (so branches don't pile up on origin).
   The lowercase `-d` is the verification: it refuses on an unmerged branch, so only a merged branch is
   ever deleted; run the remote delete only after `-d` succeeds. **Never** delete the
   `worktree-build` / `worktree-build2` / `worktree-adrs` anchors.
   - `git branch -d <merged-branch>`
   - `git push origin --delete <merged-branch>`
5. Confirm branch + worktree (`git worktree list`, `git branch --show-current`); paste.

## 1 — Ticket
`get_issue(<id>)` on FrenchForest; must be `Approved`. If `Needs Approval`, STOP and tell the owner.
Then **set the ticket → In Progress** (`save_issue state="In Progress"`). Linear is disconnected from
GitHub (2026-06-26) — nothing auto-moves status anymore, so the session doing the work owns the
In Progress transition; master owns the Done transition at the gate.

## 2 — Scope
Read ticket body + linked ADRs + specs. Summarize scope in 3–5 bullets. **Pull out the acceptance
criteria this ticket carries from the backing ADR (adr SKILL Step 5) — the testable, outcome-level
invariants you must prove. They are the definition of done.** If a feature ticket names none and it is
not a standalone bug, get them from the ADR or flag the gap before coding — master will bounce a PR
with no provable criteria.

## 3 — Plan + (risk-tiered) codex review
Write a plan: atomic steps, exact file paths, exact test commands.

**Self-classify the work from the Step-2 scope (you have the most context here — master does not pre-route this):**
- **Trivial** — mechanical only: docs / config / test-only / a one-liner; **no `src/` logic change, no
  schema / security / cost / memory / new-ADR-implementation**. → **skip codex plan-review**; the Approved
  ticket is sufficient authorization, proceed straight to TDD.
- **Standard / Complex** — touches `src/` logic, schema, security, cost, memory, a new ADR's
  implementation, or multi-file behavior. → **codex plan-review REQUIRED**: invoke **codex:rescue** on the
  plan (approach second-opinion), revise per findings, and get explicit owner approval before coding.

**When in doubt, treat as Standard and run codex** — bias toward review. The owner/master may override per
ticket in the dispatch with `[codex: required]` (force it) or `[codex: skip]` (force-skip) when they know
something the scope doesn't show. Master backstops this at the gate — a mis-tiered Standard change that
skipped codex gets bounced. (One phase = one PR — see halt conditions.)

## 4 — TDD implement
Failing test first → confirm it fails → implement. **Each acceptance criterion from Step 2 gets a
test or probe that asserts the *outcome* — the invariant actually holds — not that the component is
wired; this is the proof master's gate reads.** Standards (`.claude/CLAUDE.md`) + ADR-0074 identity
threading on every new `log.*` / `bus.publish` / Cypher `MERGE|CREATE`.

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

## 9 — PR + final ticket comment for master — then STOP
**Sync to latest main FIRST** (prevents a stale-base collision / a DIRTY PR at master's gate when a
sibling PR merged during your session): `git fetch origin && git rebase origin/main` — resolve any
conflicts **in-session** (you have the context; master won't), re-run the Step 8 quality gates, then
`git push --force-with-lease`. Then open the PR with `.github/PULL_REQUEST_TEMPLATE.md`. Pre-merge
checklist ONLY (see lifecycle-rules PR hygiene).

**Then post a final comment on the Linear ticket addressed to master** (`save_comment` on the
issue) — this is required, not optional. It carries everything master needs that does NOT belong
in the PR's pre-merge checklist:
- **acceptance-criteria proof** (the master gate's input — master SKILL Step 4): the backing ADR + the
  specific criteria this ticket implements, and for each, the evidence it is delivered end to end
  (test name + result, probe/query output, or observed behaviour at the criterion's altitude). Without
  it master bounces the PR. *(Standalone bug: the reproducing test / verification stands in for ADR
  provenance.)*
- the **post-deploy runbook** (exact ES/Kibana/migration/verification steps, in order);
- any **safety constraints / gotchas** (e.g. "do NOT back-attach existing indices", "register the
  template before first write", "verify the code is generating the logs");
- **what to verify live** to prove the AC (commands + expected output);
- discovered follow-up tickets filed;
- the Linear auto-Done caveat if the deploy will be batched;
- **your context disposition for the next ticket** — whether you want your context **kept** (the next
  queued ticket is a direct follow-on — same files/feature, multi-phase, regression test for what you
  just built, depends on a fresh discovery) or **cleared** (`/clear` — different area; you know your own
  context best). State it plainly, e.g. "FRE-X next: keep — shares this refactor" / "clear before next".
Master reads this comment by default at the gate, so it is the handoff channel.

**STOP. Do not merge, deploy, close the ticket, or edit MASTER_PLAN** — that is master's role.
