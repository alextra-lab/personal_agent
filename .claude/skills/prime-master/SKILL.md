---
name: prime-master
description: Use after /clear in the master session to rebuild the guardian snapshot from durable sources — current state, then the computed target and your standing authority, then process. Never from prior conversation.
---

# Prime the Guardian Session

Read `.claude/skills/lifecycle-rules.md` first. Reconstruct from **DURABLE sources only** —
never from prior conversation. Stack: *where am I* (1–7) → *where am I going + what I may do
unasked* (8) → *how I drive it* (9).

## Current state (1–7)

1. **Memory** — `MEMORY.md` is auto-loaded; standing rules + facts apply.
2. **Session-delta** — `docs/plans/LAST_SESSION.md`: the last session's reasoning overlay (what
   was decided and why). It never narrates commits — that's #3's job.
3. **Git history** — `git -C /opt/seshat log -10 --oneline`: the ground truth of what shipped.
4. **Git status · worktrees · PRs** — `git status` · `git worktree list` · `gh pr list`.
5. **Trigger ledger** — `python -m scripts.dispatch.trigger_ledger --unconsumed --json`. Any
   entry is in-flight actuation that survived the clear: `pending` resolves on its own;
   `surfaced` demands owner attention. Nonzero exit is itself an anomaly — surface it.
6. **Linear** — list `In Progress` · `In Review` · `Awaiting Deploy` · `Verify Failed` on
   FrenchForest.
7. **Health** — `curl -s http://localhost:9001/health` + the deployed SHA. **Actuation health
   is state-aware:** read the intended posture from the ladder's dispatch-actuation row, then
   `systemctl is-active seshat-gating-watcher.service seshat-dispatch-orchestrator.service` and
   check for `telemetry/dispatch.disabled`. Surface only a MISMATCH (should-run-but-dead, or
   should-be-paused-but-running); never alarm on an intentional pause.

## Target (8)

Two reads, in order:
a. **The computed queue** — per stream:
   `python -m scripts.dispatch.next_resolver --stream <s> --eligible --json`. Nonzero exit /
   invalid JSON → STOP and surface stderr; Linear's UI is the human fallback.
b. **`docs/plans/OWNER_CONSOLE.md`** — the owner's standing directives and the **trust
   ladder**, your commission: per action class, what you may do unasked, what you report, what
   you ask for. A grant exists iff the ladder records it. You never author into this file —
   transcribe and retire only.

## Process (9)

- **Coordinator.** Master is the single brain + hands. The watcher is a dumb sensor: it
  triggers you when a PR is master-ready ("Gating PR #X") and pokes a worker on red CI. You
  reason from durable state and actuate via `send-keys`, `gh`, Linear. Bounces go directly to
  the worker's seat.
- **Never poll for PRs**; the watcher lifts the obligation, the owner keeps the ability.
- **Deploy authority comes off the ladder**, not from any skill text.
- **Fix small things yourself** rather than filing tickets; the backlog is a symptom, not the
  disease.

## Output

Print the snapshot: current state (1–7, drift vs #2 noted, any `surfaced` trigger or actuation
MISMATCH called out loudly) → target (8: the eligible head per stream, the directives, your
authority per action class) → operate per process (9).

Every owner briefing is decision-support: **verify before you assert** (confirm from code /
ticket / ADR / substrate — never guess in front of the owner) · frame every ask as a decision
with the expected outcome · give exact commands and where to run them · bring genuine decisions
with a recommendation, never a menu · right altitude, right time. Never use the injected CC
`userEmail`; use the owner's designated test email for gateway calls.
