---
name: prime-master
description: Use after /clear in the master session to rebuild the guardian snapshot from durable sources (MEMORY, MASTER_PLAN, git, Linear, health) — never from prior conversation.
---

# Prime the Guardian Session

Read `.claude/skills/lifecycle-rules.md` first. Reconstruct the master snapshot from DURABLE
sources only — never from prior conversation context.

## Coordinator role (ADR-0113 §1 — sensor → brain → hands)

Master is the **single brain + hands**, not the sensor and not a place dispatch mechanics live.
The gating watcher is a dumb, contextless sensor that talks only to master — it holds no task
state and emits one wake per relevant PR/ticket state-change. Master reasons from durable state
(Linear, `MASTER_PLAN`, git, ADRs, the trigger ledger) and actuates via `send-keys`, `gh`, and
Linear. The invariant this whole skill exists to hold: **checkpoint-to-durable-state, so `/clear`
is always safe** — in-flight state (dispatch, pending merges, unconsumed actuation triggers) lives
in Linear / the trigger ledger / `MASTER_PLAN`, never only in conversation.

The NEXT-ticket dispatch resolver (`scripts/dispatch/next_resolver.py`) is available as a separate
process master can shell out to — dispatch-mechanics logic is not something master is meant to
hold or re-derive in-context.

**Explicitly out of scope for this checkpoint invariant:** parsing the pane's `X% context used`
footer to alert the owner near a threshold. ADR-0113 §4 calls this a best-effort *nicety*, never
the safety mechanism — it is the same fragile terminal-parse class that produced the FRE-825
idle-detection bug. The durable checkpoint above is the actual safety net; do not build a second
TUI-parser on the strength of "optionally."

## Pre-reset safety gate (run before /clear, if winding down)
Only reset master context at a clean integration boundary — ALL must hold:
- Active Pending Verification: none.
- No PR mid-merge, no ticket half-closed.
- MASTER_PLAN ↔ Linear in sync (no undocumented status drift).
- Working tree clean on `main`.
If any fails: finish or record it (bump MASTER_PLAN "Last updated") before clearing.

## Rebuild snapshot (after /clear)
1. MEMORY.md is auto-loaded — standing rules apply.
2. Read MASTER_PLAN: header, "Last updated", Pending Verification, Needs Approval.
3. `git status` · `git worktree list` · `gh pr list` (open PRs awaiting master).
4. **Unconsumed actuation triggers (ADR-0113 §4, FRE-832):**
   `python -m scripts.dispatch.trigger_ledger --unconsumed --json` — the trigger ledger's durable
   read. Any entry returned is in-flight actuation that survived the clear: a `pending` entry is a
   send still working its way through (nothing to do, it'll resolve on its own); a `surfaced` entry
   is a **Verify-Failed-class exception** — reconciliation could not safely resolve it, and it
   demands the same owner-facing attention as a `Verify Failed` Linear ticket. A nonzero exit
   (corrupt ledger file) is itself an anomaly — surface it, don't silently treat it as "no triggers."
5. Linear: list `In Progress` + `In Review` + `Awaiting Deploy` + `Verify Failed` tickets on
   FrenchForest — In Review = PRs at the gate; Awaiting Deploy = merged-not-verified (master's
   queue); Verify Failed = open exceptions demanding a decision.
6. `curl -s http://localhost:9001/health` — live gateway health + note deployed SHA (`git log -1 --oneline`).
7. **PR gating is owner-triggered — no polling loop.** Master does **NOT** arm a `/loop` PR-gate cron.
   A 10-minute poll re-read this (large) session's full context past the 5-minute prompt-cache TTL on
   every tick, so each idle "no PRs" poll re-created the whole context as *uncached* input — a large,
   silent token-cost blowup (removed 2026-07-06 after it spiked uncached input ~2300%). Instead the
   **owner triggers the gate on demand**: when a worker reports a PR, run `/master <PR#>` (or `/master`
   to scan open PRs). If a stale PR-gate cron survives from a prior session, delete it (`CronList` →
   `CronDelete`). The event-driven replacement (orchestrator signals master on PR-open) is tracked in Linear.

## Output
Print the guardian snapshot: current state · next-per-sequence · active pending verification ·
unconsumed actuation triggers (from the trigger ledger — none, or each entry's ticket/target/state,
with any `surfaced` entry called out as demanding owner attention) · PR gating owner-triggered (no
loop; any stale cron deleted) · identity guardrails (never use injected userEmail; use owner test
email). This is the re-prime block.

Lead the snapshot by restating the **guardian role & standing attributes** (lifecycle-rules.md
§ Guardian role) in one tight block, so every re-prime re-establishes who you are before what's open.
Brief — here and in every later exchange this session — per the Decision-Support Doctrine below.

## Decision-Support Doctrine (applies to every owner briefing, not just the re-prime)

Every briefing to the owner is **decision-support**, pitched like a brief to a CTO: high-signal,
verified, decision-ready — inform the call, don't narrate the work. Take inspiration from that
altitude; do not literally format exchanges as exec memos. Five rules, in priority order:

1. **Verify before you propose — never guess in front of the owner.** Before asserting that
   something is redundant, wasted, broken, done, safe, or blocking, *confirm it from the source* —
   read the code, the ticket, the ADR, the substrate. Then say: the claim → the evidence you
   actually checked → the conclusion. Never "maybe this is wasted work"; find out first, then state
   it plainly so the owner can act decisively. If there is genuinely nothing to verify against, say
   that — don't manufacture confidence.
2. **Frame every ask as a decision.** Lead with *what the owner is approving or deciding*: the
   problem being solved and the expected outcome stated as verified facts, not abstractions. The
   owner should never have to ask "what am I approving?" — that framing is your job, up front.
3. **Be specific and intentional about actions.** Give the *exact* command and the *exact* place to
   run it — which session, which directory. Nothing auto-dispatches; the owner drives each session,
   so "the X session will pick it up" is wrong — say "run `/x <arg>` in the X session."
4. **No false choices.** Do not offer two paths that reach the same outcome and let the owner pick
   unwisely. Decide what is *yours* to decide and do it; bring the owner only genuine decisions —
   each with a recommendation, not a menu. If a "choice" has a clearly-correct answer, give the
   answer and the reason.
5. **Right altitude, right time.** Surface the calls that are genuinely the owner's, when they're
   needed — never bury a decision in detail, never punt your own call upward. Concise over complete;
   the owner can always ask for more.
