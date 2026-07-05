---
name: prime-worker
description: Run once in a build / build2 / adr worker session. Self-identifies the stream from its worktree, arms its own 20m monitor loop (survives /clear, runs till session close — no double-arm), then becomes a pure PR-feedback monitor over its own open PR — self-fixing on a marked master bounce OR a red CI (ack → fix → make test → push → stop). It never resolves NEXT, never advises what to run, never merges, deploys, or clears — the orchestrator owns dispatch.
---

# Prime a Worker Session (build / build2 / adr)

Read `.claude/skills/lifecycle-rules.md` first. This is a **pure PR-feedback monitor** — after a
build opens a PR, its only ongoing job is the watch loop over its own PR until that PR merges, with
**one** action carve-out: it self-fixes its own open PR on a marked master bounce **or** a red CI
(Step 3.2). Otherwise it never edits `src/` outside that fix, never runs `/build` or `/adr`, never
`/clear`s, never merges, never deploys, never edits MASTER_PLAN. **When in doubt, stay silent.**

**Dispatch is the orchestrator's, not this monitor's (ADR-0110).** The external orchestrator
(`scripts/dispatch/`, systemd) resolves each stream's NEXT, sets the model tier, and launches the
worker via Remote Control, seeding the resolved ticket (`/build <FRE-id>` / `/adr <FRE-id>`). So this
monitor **no longer resolves NEXT or advises the owner what to run** — that was duplicated logic and a
drift trap (two resolvers of the same NEXT). The orchestrator is dispatch-only and leaves master's
role and both approval gates unchanged. See `docs/runbooks/dispatch-orchestrator.md`.

**Run `/prime-worker` once** when you open a worker session (the orchestrated launch path arms this
loop for you — see the build/adr skills). It arms its own **20m loop** (Step 2), a cron that
**survives `/clear` and runs until the session is closed** — so you never type `/loop` and never
re-arm. The same command works in all three worker sessions; this skill self-identifies from its
worktree.

## Step 1 — Self-identify the stream from the worktree
`git rev-parse --show-toplevel` → map the basename:
- `…/worktrees/build`  → **Stream 1**
- `…/worktrees/build2` → **Stream 2**
- `…/worktrees/adrs`   → **adr** (the adr session is **Opus-only**)
- **anything else** (primary `/opt/seshat`, `looptest`, `kibana`, …) → **STOP. Arm nothing. Output
  nothing.** Not a worker session — do not act. (This gate runs *before* arming, so a stray
  `/prime-worker` in master or a test worktree never leaves a loop behind.)

## Step 2 — Arm the loop, once (idempotent — never double-arm)
Only reached in a real worker session. Check `CronList` for an existing `/prime-worker` loop:
- **already armed** (you ran this after a `/clear`, this *is* a loop tick, or the orchestrated build
  entry armed it) → do **not** arm again; continue to Step 3.
- **none** → arm `/loop 20m /prime-worker`, then continue to Step 3.

## Step 3 — Determine state from durable git/gh (never from conversation memory)
Self-memory breaks the instant the session is `/clear`ed; git does not. `git fetch origin` (cheap —
keeps `origin/main` current), then:

1. **Building** — `git status --short` is non-empty **OR** `git rev-list --count origin/main..HEAD` > 0
   (uncommitted or unpushed work in flight) → **silent.**
2. **Awaiting master** — clean, and `gh pr list --head "$(git branch --show-current)"` shows an open PR.
   This is the monitor's whole job — resolve the two self-fix triggers, else report/stay silent:

   **a. Self-fix trigger — a marked master bounce OR a red CI on this PR (the action carve-out).**
   A worker self-fixes its own open PR in the **same shape** for both triggers: **detect → ack → fix on
   this branch → `make test` green → push → STOP** (CI re-runs; a later tick re-checks). **Never merge,
   never deploy** — master still gates.

   - **Master bounce.** Fetch PR comments (`gh pr view <PR#> --json comments`). If the **latest**
     `## Master gate — BOUNCE` comment has **no worker ack after it** (an ack is a later PR comment
     containing `Ack: addressing master bounce`) → **FIX MODE**:
     - **Ack first**, before touching code:
       `gh pr comment <PR#> --body "Ack: addressing master bounce in next push."` — dedups the next tick.
     - Read the bounce + the PR diff, apply the fix on **this** branch, `make test` to green, push, STOP.

   - **Red CI (SHA-keyed dedup).** With no unacked bounce, read `gh pr checks <PR#>`. If CI is
     **failing** (a required check FAILED, not merely pending) on the PR's **current head SHA**
     (`git rev-parse --short HEAD`) **and** there is no `Ack: addressing red CI at <that-sha>` PR
     comment for that SHA → **FIX MODE**:
     - **Ack first**, naming the SHA:
       `gh pr comment <PR#> --body "Ack: addressing red CI at <short-sha> in next push."` — the SHA is
       the idempotency key.
     - Read the failing check's log, fix on **this** branch, `make test` to green, push, STOP. The push
       changes the head SHA, so CI re-runs against a *new* SHA (pending, not failing) — the trigger
       cannot re-fire for the same SHA, so no thrash. A later tick re-checks the new SHA.
     - (The monitor loop is single-session and serial — one tick at a time — so two ticks never enter
       FIX MODE for the same SHA concurrently.)

   **b. No self-fix trigger → report CI readiness (advisory, no action).** A PR is **not** master-ready
   until CI is green:
   - CI **all green** → **silent** (built + CI-green; master owns the gate).
   - CI **failing** but already acked for this SHA (fix in flight) → **silent** (do not re-enter).
   - CI **pending** → one line: *"Stream X · PR #N CI still running — not yet master-ready; re-check
     next tick."*
3. Otherwise (clean · nothing unpushed · no open PR) → **idle → stay silent.** There is nothing to
   monitor and nothing to resolve — the orchestrator owns dispatch and will seed the next ticket. The
   monitor never picks work or advises a command.

## Boundary
A **pure PR-feedback monitor** with **one** action carve-out: it may edit/commit/push **to fix its own
open PR** on a marked master bounce **or** a red CI (Step 3.2) — ack first, `make test` green before
pushing, and **never merge, deploy, or `/clear`**. Outside that fix path it never edits `src/`, never
runs `/build` / `/adr`, never resolves NEXT, never advises what to run, never merges, never edits
MASTER_PLAN. The orchestrator owns dispatch (resolve + launch); master owns the gate (review, merge,
deploy, close). If any check is ambiguous, stay silent rather than guess.
