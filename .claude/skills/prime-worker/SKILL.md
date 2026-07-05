---
name: prime-worker
description: Run once in a build / build2 / adr worker session. Self-identifies the stream from its worktree, arms its own 20m monitor loop (survives /clear, runs till session close — no double-arm), then tests idle/building/awaiting-master from durable git state and ONLY when idle with an Approved NEXT surfaces a one-line dispatch card (model switch + CLEAR/KEEP + exact command). Advises only, except it self-fixes a marked master bounce on its own open PR (ack → fix → make test → push); never merges, deploys, or clears.
---

# Prime a Worker Session (build / build2 / adr)

Read `.claude/skills/lifecycle-rules.md` first. This is a **monitor, not an executor** — with one
carve-out: it self-fixes a marked master bounce on its own open PR (Step 3.2a). Otherwise it never
edits `src/`, never runs `/build` or `/adr`, never `/clear`s, never merges, never edits MASTER_PLAN.
It reads durable git + Linear state and, only when the stream is genuinely idle with a ready
NEXT, tells the owner exactly what to run. **When in doubt, stay silent.**

**Automated dispatch (ADR-0110).** An external orchestrator (`scripts/dispatch/`, systemd) can
*actuate* what this monitor only *advises* — resolve NEXT, set the model tier, and launch via Remote
Control, so the owner watches + answers prompts from any device instead of typing switch→clear→command.
Both read the **same** Linear-native dispatch contract; the orchestrator is dispatch-only and leaves
master's role and both approval gates unchanged. This monitor stays valuable either way (it surfaces
state and self-fixes bounces). See `docs/runbooks/dispatch-orchestrator.md`.

**Run `/prime-worker` once** when you open a worker session. It arms its own **20m loop** (Step 2),
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
- **none** → arm `/loop 20m /prime-worker`, then continue to Step 3.

## Step 3 — Determine state from durable git/gh (never from conversation memory)
Self-memory breaks the instant the session is `/clear`ed; git does not. `git fetch origin` (cheap —
keeps `origin/main` current), then:

1. **Building** — `git status --short` is non-empty **OR** `git rev-list --count origin/main..HEAD` > 0
   (uncommitted or unpushed work in flight) → **silent.**
2. **Awaiting master** — clean, and `gh pr list --head "$(git branch --show-current)"` shows an open PR.
   Resolve in order:

   **a. Master bounce to follow? (the one carve-out from advise-only.)** Fetch PR comments
   (`gh pr view <PR#> --json comments`). If the **latest** `## Master gate — BOUNCE` comment has **no
   worker ack after it** (an ack is a later PR comment containing `Ack: addressing master bounce`) →
   enter **FIX MODE**:
   - **Ack first**, before touching code: `gh pr comment <PR#> --body "Ack: addressing master bounce in next push."` — dedups against the next 20m tick.
   - Read the bounce + the PR diff, apply the fix on **this** branch, run `make test` to green, and push.
   - **Never merge, never deploy** — master still gates. Then **STOP** (CI re-runs; the next tick sees the ack and skips). A followed bounce ends when the PR merges — no other cleanup.

   **b. No unacked bounce → master-readiness by CI.** A PR is **not** master-ready until CI is green:
   `gh pr checks <PR#>` —
   - CI **all green** → **silent** (built + CI-green; master owns the gate).
   - CI **failing** → surface one line: *"Stream X · PR #N CI FAILING (`<failed check>`) — fix before the master gate; master won't merge red."* A red PR is the worker's to fix, not master's to wait on. (This also stops a session reporting "done / awaiting master" on a PR that never passed CI.)
   - CI **pending** → one line: *"Stream X · PR #N CI still running — not yet master-ready; re-check next tick."*
3. Otherwise (clean · nothing unpushed · no open PR) → **possibly idle** → go to Step 4. Whether it is
   truly idle vs. just-dispatched is decided by the NEXT ticket's **Linear state** in Step 4 — NOT by
   branch name (build branches are `fre-<id>-…`, adr branches are `<adr-slug>-…`; only Linear state is
   uniform across both).

## Step 4 — Resolve NEXT from Linear (the dispatch authority; uniform for build & adr)
Dispatch contract = lifecycle-rules § Dispatch (Linear-native). Resolve in two queries:

1. **Busy guard:** query the stream's label in BOTH occupied states —
   `list_issues(team="FrenchForest", state="In Progress", label="stream:<mine>")` and again with
   `state="In Review"` — any result in either → the stream is occupied (building, or PR at master's
   gate that could bounce back) → **silent.** The stream frees at `Awaiting Deploy` (merge landed;
   deploy is master's). *(Uniform across build & adr branch-naming schemes.)*
2. **Head of queue:** `list_issues(team="FrenchForest", state="Approved", label="stream:<mine>")` —
   order by priority (Urgent first, then High/Medium/Low, no-priority last), oldest created on ties;
   walk from the top and take the first issue with **no open "blocked by" relation**
   (`get_issue(<id>, includeRelations=true)` — a blocker is open until its merge lands: open =
   blocker in any state before `Awaiting Deploy`/`Done`/`Canceled`/`Duplicate`).
- No candidate → **silent** (nothing dispatched to this stream — master hasn't labeled work for it).
- Candidate found → read its **Tier label** ([O]/[S]/[H] → Opus/Sonnet/Haiku) and its **context
  flag** (`context:keep` label present → KEEP; absent → CLEAR) → **advise** (Step 5).

## Step 5 — Surface the dispatch card (one line), then STOP
Compare the ticket's Tier label to **this session's own model** (you know it from your system prompt), and
use this stream's dispatch command from Step 1. Emit exactly one line, for example:

> **Stream 1 ready → FRE-724 [Opus].** Context CLEAR. Model OK (this session is Opus). Run `/clear`, then `/build 1`.

> **Stream 2 ready → FRE-691 [Sonnet].** Context KEEP. ⚠️ Switch model to Sonnet first, then `/build 2` (no `/clear`).

> **adr ready → FRE-736 [Opus].** Context CLEAR. Model OK (adr is Opus-only). Run `/clear`, then `/adr`.

Then **STOP.** Do not run the command yourself. The owner performs the model switch, the `/clear`, and
the dispatch — those are the human-only gates. Master owns dispatch (labels/priority/relations), so you
only ever advise the ticket master routed to this stream; you never choose your own work.

## Boundary
Advises only, with **one** carve-out: it may edit/commit/push **to fix a marked master bounce on its
own open PR** (Step 3.2a) — ack first, `make test` green before pushing, and **never merge, deploy, or
`/clear`**. Outside that bounce-fix path it never edits `src/`, never runs `/build` / `/adr`, never
merges, never edits MASTER_PLAN. If any check is ambiguous, stay silent rather than guess.
