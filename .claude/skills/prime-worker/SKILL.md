---
name: prime-worker
description: Run once in a build / build2 / adr worker session. Self-identifies the stream from its worktree, arms its own 10m monitor loop (survives /clear, runs till session close — no double-arm), then tests idle/building/awaiting-master from durable git state and ONLY when idle with an Approved NEXT surfaces a one-line dispatch card (model switch + CLEAR/KEEP + exact command). Advises only — never builds, clears, merges, or edits anything.
---

# Prime a Worker Session (build / build2 / adr)

Read `.claude/skills/lifecycle-rules.md` first. This is a **monitor, not an executor.** It never
edits `src/`, never runs `/build` or `/adr`, never `/clear`s, never merges, never edits MASTER_PLAN.
It reads durable git + board + Linear state and, only when the stream is genuinely idle with a ready
NEXT, tells the owner exactly what to run. **When in doubt, stay silent.**

**Run `/prime-worker` once** when you open a worker session. It arms its own **10m loop** (Step 2),
which is a cron — it **survives `/clear` and runs until the session is closed** — so you never type
`/loop` and never re-arm. The same command works in all three worker sessions; this skill
self-identifies from its worktree.

## Step 1 — Self-identify the stream from the worktree
`git rev-parse --show-toplevel` → map the basename:
- `…/worktrees/build`  → **Stream 1**, dispatch command `/build 1`
- `…/worktrees/build2` → **Stream 2**, dispatch command `/build 2`
- `…/worktrees/adrs`   → **adr**, dispatch command **`/adr`** (no number; the adr session is **Opus-only**)
- **anything else** (primary `/opt/seshat`, `looptest`, `kibana`, …) → **STOP. Arm nothing. Output
  nothing.** Not a worker session — do not act. (This gate runs *before* arming, so a stray
  `/prime-worker` in master or a test worktree never leaves a loop behind.)

## Step 2 — Arm the loop, once (idempotent — never double-arm)
Only reached in a real worker session. Check `CronList` for an existing `/prime-worker` loop:
- **already armed** (you ran this after a `/clear`, or this *is* a loop tick) → do **not** arm again;
  continue to Step 3.
- **none** → arm `/loop 10m /prime-worker`, then continue to Step 3.

## Step 3 — Determine state from durable git/gh (never from conversation memory)
Self-memory breaks the instant the session is `/clear`ed; git does not. `git fetch origin` (cheap —
keeps `origin/main` current), then:

1. **Building** — `git status --short` is non-empty **OR** `git rev-list --count origin/main..HEAD` > 0
   (uncommitted or unpushed work in flight) → **silent.**
2. **Awaiting master** — clean, but `gh pr list --head "$(git branch --show-current)"` shows an open PR
   → **silent** (the ticket is built; master owns the gate).
3. Otherwise (clean · nothing unpushed · no open PR) → **possibly idle** → go to Step 4. Whether it is
   truly idle vs. just-dispatched is decided by the NEXT ticket's **Linear state** in Step 4 — NOT by
   branch name (build branches are `fre-<id>-…`, adr branches are `<adr-slug>-…`; only Linear state is
   uniform across both).

## Step 4 — Resolve NEXT + decide via Linear state (the dispatch authority; uniform for build & adr)
`git show origin/main:docs/plans/MASTER_PLAN.md` → the `## 🎛️ Stream Board` between
`<!-- STREAM-BOARD:START -->` / `END`. Take THIS stream's row **NEXT** (bold `FRE-…`), its **Context**
flag (CLEAR/KEEP), and its model tag ([O]/[S]/[H]).
- NEXT missing/ambiguous, or a non-ticket note (e.g. "owner bug investigation") → **silent.**

Then `get_issue(<NEXT id>)` on FrenchForest and branch on its state:
- **Approved** → ready and not yet started → **advise** (Step 5).
- **In Progress** → already dispatched (a session is on it — both `/build` and `/adr` set In Progress at
  their Step 1) → **silent.** *This is the reliable "already dispatched" guard: it does not depend on
  branch names, so it holds for `/adr`'s ADR-slug branches just as for build's `fre-<id>` branches.*
- **Needs Approval** → one line: *"Stream X NEXT FRE-Y not yet Approved — nothing to dispatch."*
- **Done / anything else** → the board is stale (master hasn't advanced NEXT) → **silent.**

## Step 5 — Surface the dispatch card (one line), then STOP
Compare the board's model tag to **this session's own model** (you know it from your system prompt), and
use this stream's dispatch command from Step 1. Emit exactly one line, for example:

> **Stream 1 ready → FRE-724 [Opus].** Context CLEAR. Model OK (this session is Opus). Run `/clear`, then `/build 1`.

> **Stream 2 ready → FRE-691 [Sonnet].** Context KEEP. ⚠️ Switch model to Sonnet first, then `/build 2` (no `/clear`).

> **adr ready → FRE-736 [Opus].** Context CLEAR. Model OK (adr is Opus-only). Run `/clear`, then `/adr`.

Then **STOP.** Do not run the command yourself. The owner performs the model switch, the `/clear`, and
the dispatch — those are the human-only gates. Master owns the board, so you only ever advise the ticket
master assigned to this stream; you never choose your own work.

## Boundary
Advises only. Never edits `src/`, never runs `/build` / `/adr`, never `/clear`s, never merges, never
edits MASTER_PLAN. If any check is ambiguous, stay silent rather than guess.
