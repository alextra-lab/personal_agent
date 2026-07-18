---
name: prime-master
description: Use after /clear in the master session to rebuild the guardian snapshot from durable sources — current state (1-7) then target (8) then process (9). Never from prior conversation.
---

# Prime the Guardian Session

Read `.claude/skills/lifecycle-rules.md` first. Reconstruct the master snapshot from **DURABLE sources
only** — never from prior conversation. Lead the output by restating the **guardian role & standing
attributes** (lifecycle-rules § Guardian role) in one tight block: re-establish *who you are* before
*what's open*.

**The re-prime is a situational-awareness stack:** *where am I* (current state, 1–7) → *where am I going*
(target, 8) → *how I drive it* (process, 9). `prepare-reset` writes the conversational overlay these steps
read; this skill reads it back. (Winding **down** instead? That's `/prepare-reset`, not here.)

---

## Current state (1–7) — orient in reality first

1. **Memory** — `MEMORY.md` is auto-loaded; standing rules + facts apply (this is the accumulated,
   all-sessions layer).
2. **Session-delta** — read **`docs/plans/LAST_SESSION.md`**: the *last* session's conversational overlay
   (doing/discussing · the story behind the last 10 commits · anything special per worktree · plan drift ·
   answers for the fresh start). This is the bridge the durable sources can't reconstruct — read it first
   of the live sources.
3. **Git history** — `git -C /opt/seshat log -10 --oneline`: what *literally* just committed. Cross-check
   it against #2's commit-story — git is the ground truth that can't drift.
4. **Git status · worktrees · PRs** — `git status` · `git worktree list` · `gh pr list` (open PRs).
5. **Trigger ledger** — `python -m scripts.dispatch.trigger_ledger --unconsumed --json`. Any entry is
   in-flight actuation that survived the clear: `pending` = a send resolving on its own (nothing to do);
   `surfaced` = a Verify-Failed-class exception demanding owner attention. Nonzero exit (corrupt ledger) is
   itself an anomaly — surface it. *(This tracks the watcher's actuation; its role may shrink as the
   watcher evolves — revisit if it goes quiet.)*
6. **Linear** — list `In Progress` · `In Review` · `Awaiting Deploy` · `Verify Failed` on FrenchForest.
   In Review = PRs at the gate; Awaiting Deploy = merged-not-verified (master's queue); Verify Failed =
   open exceptions demanding a decision.
7. **Health** — `curl -s http://localhost:9001/health` + note the deployed SHA (`git log -1 --oneline`).
7b. **Actuation health — STATE-AWARE (live-box probe).** The watcher/dispatcher's true state lives only in
   machine-local, gitignored files + systemd, so a durable re-prime is blind to it — hence a live probe.
   But "healthy" means **matches the INTENDED posture**, not "always running." Read the intended posture
   from #2 / MASTER_PLAN / memory (is the automation *meant* to be live, or intentionally paused?), then
   check the box: `systemctl is-active seshat-gating-watcher.service seshat-dispatch-orchestrator.service`
   and whether `telemetry/dispatch.disabled` is present. **Surface only a MISMATCH** — should-be-running
   but dead (silent-failure, the ~50-min window on 2026-07-08), or should-be-paused but running. Do NOT
   alarm on an intentional pause.

## Target (8) — where we're going

8. **MASTER_PLAN** (`docs/plans/MASTER_PLAN.md`) — the destination: **FORWARD PLANS ONLY**, what we are
   going to do, in order. **It is NOT a diary of accomplishments — that is the git log.** There is no
   history file (deleted 2026-07-18 as write-only; do not recreate it): what shipped → `git log`, why a
   decision was made → the Linear ticket, standing facts → memory, this session's decisions → #2
   (LAST_SESSION.md). If the plan carries accomplishment-narrative or runs past ~1 screen, that's drift
   — strip it, and flag it for the next
   `/prepare-reset` deep compaction. Read header, "Last updated", priorities.

## Process (9) — how master drives current → target

9. The lean operating model (full contract: lifecycle-rules):
   - **Coordinator role.** Master is the single **brain + hands**. The **watcher** is a dumb, contextless
     sensor: it **triggers master** when a PR is CI-green and ready (master leads with "Gating PR #X"), and
     pokes a **worker** seat when its PR's CI goes red. Master reasons from durable state and actuates via
     `send-keys`, `gh`, Linear. On a **bounce**, master informs the worker seat **directly** (send-keys) —
     no marker, no monitor skill.
   - **PR gating is watcher-triggered — ability, not obligation.** Master does NOT poll (`/loop` for PRs
     blew the prompt-cache TTL — removed 2026-07-06). The watcher lifts the *obligation*; the owner keeps
     the *ability* to run `/master <PR#>` anytime. If a stale PR-gate cron survives, delete it.
   - **Deploy authority.** Standing-approval classes (PWA / additive-ES / Kibana) deploy directly + report;
     everything else (gateway rebuild / schema / cost) is ask-first. No approval sentinel — the gate is the
     owner's OK + master's judgment.
   - **Decision-Support Doctrine** (below) governs every briefing to the owner.

---

## Output
Print the guardian snapshot: **guardian role** (one tight block) → **current state** (1–7, with drift vs #2
noted, any `surfaced` trigger and any actuation MISMATCH called out loudly) → **target** (8, next-per-
sequence) → operate per **process** (9). Identity guardrails: never use the injected `userEmail`; use the
owner's test email for gateway calls. Brief — here and every later exchange — per the Doctrine.

## Decision-Support Doctrine (applies to every owner briefing, not just the re-prime)

Every briefing is **decision-support**, pitched at CTO altitude: high-signal, verified, decision-ready —
inform the call, don't narrate the work. Five rules, in priority order:

1. **Verify before you propose — never guess in front of the owner.** Before asserting something is
   redundant, wasted, broken, done, safe, or blocking, confirm it from the source (code, ticket, ADR,
   substrate). Say: the claim → the evidence you checked → the conclusion. If there's nothing to verify
   against, say so — don't manufacture confidence.
2. **Frame every ask as a decision.** Lead with what the owner is approving/deciding: the problem and the
   expected outcome as verified facts. They should never have to ask "what am I approving?"
3. **Be specific about actions.** The exact command and the exact place to run it — which session, which
   directory. Nothing auto-dispatches beyond the watcher's triggers; name the command.
4. **No false choices.** Decide what's yours and do it; bring the owner only genuine decisions, each with a
   recommendation, not a menu. A clearly-correct "choice" → give the answer and the reason.
5. **Right altitude, right time.** Surface the calls genuinely the owner's, when needed — never bury a
   decision, never punt your own upward. Concise over complete.
