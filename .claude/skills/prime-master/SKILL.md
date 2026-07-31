---
name: prime-master
description: Use after /clear in the master session to rebuild the guardian snapshot from durable sources — current state (1-7), then the computed target and your standing authority (8), then process (9). Never from prior conversation.
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
   (doing/discussing · **what was decided and why** · anything special per worktree · plan drift · answers
   for the fresh start). It carries **only what no durable source can reconstruct** — the reasoning behind
   the work, not a retelling of it. It does **not** narrate the commits; that is yours, at #3.
3. **Git history — yours to read, and nobody else's to summarise.** `git -C /opt/seshat log -10 --oneline`:
   what *literally* just committed. This is the ground truth that cannot drift, so it is derived fresh here
   rather than inherited as prose. Pair each commit with #2's *decisions* to get why it exists — the delta
   supplies the reasoning, git supplies the record. If #2 ever narrates commits, that is drift: delete it
   there rather than reconciling two accounts.
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
   from the **trust ladder's dispatch-actuation row** in `docs/plans/OWNER_CONSOLE.md` — one authoritative
   home, not three (is the automation *meant* to be live, and what may you do about it unasked?) — then
   check the box: `systemctl is-active seshat-gating-watcher.service seshat-dispatch-orchestrator.service`
   and whether `telemetry/dispatch.disabled` is present. **Surface only a MISMATCH** — should-be-running
   but dead (silent-failure, the ~50-min window on 2026-07-08), or should-be-paused but running. Do NOT
   alarm on an intentional pause.

## Target (8) — where we're going, and what you may do unasked

8. **The computed queue + the owner's overlay.** This is the moment a fresh master reads its
   commission. Two reads, in order (ADR-0131 D5):

   a. **What we do next, in order — computed at read time, never a stored copy.** Per stream, run
      `python -m scripts.dispatch.next_resolver --stream <s> --eligible --json`: every `Approved` +
      `stream:<s>` ticket with no open blocked-by, in priority / oldest-created order. A nonzero exit,
      invalid JSON, or a printed error → **STOP and surface stderr**; never reconstruct the ordering
      inline. Linear's own UI is the human-readable fallback if the resolver is down (an outage blocks
      dispatch anyway, so it is not additional blindness).

   b. **`docs/plans/OWNER_CONSOLE.md`** — the owner's overlay, and the only thing the resolver cannot
      compute: the **standing directives** (sequence guidance, priority overrides, prohibitions, each
      with a retirement condition) and the **trust ladder**, which is **the single source of your
      standing authority**. Read the ladder as *your commission*: for each action class, what you may
      do unasked, what you must report, and what you must ask for. **A grant exists iff the ladder
      records it** — prose elsewhere describes mechanics, never authority. Acting above your granted
      level is the worst class of fresh-session error; this step is the guard.

   **You do not write this file.** The owner authors it; you transcribe a directive they gave
   conversationally (verbatim, attributed, dated) and retire one only when its stated condition is met,
   citing that condition. If the file exceeds its stated size bound, **surface that to the owner** — it
   is a contract violation, not a compaction chore.

   There is no plan document and no history file. What shipped → `git log`; why a decision was made →
   the Linear ticket; standing facts → memory; this session's decisions → #2 (LAST_SESSION.md); what we
   do next → (a). **Do not recreate a plan file, and do not park narrative on a cheaper surface** — a
   finding with no home is a one-line `Backlog` ticket (lifecycle-rules § Coordination stores).

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
   - **Deploy authority — read it off the ladder, not from here.** The console's trust ladder holds the
     current level for each deploy class; this line describes only the *mechanics* (a standing-class
     deploy runs, then you verify + report; an ask-first class waits for the owner's explicit OK). No
     approval sentinel — the gate is the owner's OK + master's judgment.
   - **Decision-Support Doctrine** (below) governs every briefing to the owner.

---

## Output
Print the guardian snapshot: **guardian role** (one tight block) → **current state** (1–7, with drift vs #2
noted, any `surfaced` trigger and any actuation MISMATCH called out loudly) → **target** (8: the resolver's
eligible head per stream, then the console's directives and **your standing authority stated per action
class**) → operate per **process** (9). Identity guardrails: never use the injected `userEmail`; use the
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
