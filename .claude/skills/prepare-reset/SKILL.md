---
name: prepare-reset
description: Owner-invoked before resetting (/clear-ing) a session. Verifies a safe reset boundary, writes the session-delta artifact (LAST_SESSION.md) so the next session inherits this session's decisions, distills standing facts into memory, checkpoints MASTER_PLAN, and emits a go/no-go verdict. The safe wind-down bookend to prime-master.
---

# Prepare for reset — safe wind-down before `/clear`

Invoked by the owner in the session about to be reset. This is the bookend to `prime-master`:
`prime-master` **rebuilds** from durable sources *after* a reset; `prepare-reset` **captures + checkpoints**
*before* one. **It is prime-master run in reverse** — prime-master READS current-state → target → process;
prepare-reset WRITES the conversational overlay for what prime-master will read.

**The core problem this solves.** `prime-master` rebuilds only from DURABLE sources and deliberately
ignores prior conversation. So the **conversational layer** — the *why*, the *was-doing*, the
*drift-and-why* that git / Linear / MASTER_PLAN cannot hold — is LOST on `/clear` unless written down.
Step 2 is the point of this skill: write that layer to the **session-delta artifact**, and *only* that
layer — everything else the re-prime re-reads fresh. The safety gate and the MASTER_PLAN checkpoint support.

Detect the session role from its RC/tmux name: `cc-master` → **master**; `cc-build`/`cc-build2`/`cc-adrs`
→ **worker**. Run the matching track.

---

## 1 — Safety gate (refuse a mid-flight reset)

**Master.** ALL must hold, or the reset is BLOCKED:
- No active Pending Verification (nothing merged-but-unverified in Awaiting Deploy that you own).
- No PR mid-merge, no ticket half-closed.
- MASTER_PLAN ↔ Linear in sync (no undocumented status drift).
- Working tree clean on `main` (`git -C /opt/seshat status`).
If any fails: name exactly what's blocking and finish/record it first — do NOT bless the reset.

**Worker.** Its PR is pushed AND its handoff comment is posted on the ticket. A worker resumes its own
context on wake-up (`claude -c` in its seat), so its "memory" is the git working tree + the transcript,
not this skill — the only hard gate is: no unpushed in-flight work (finish or stash first).

---

## 2 — Write the session-delta artifact (the part that's been missing)

**Master.** `prime-master` will NOT see this conversation. Write the overlay it would otherwise lose to
**`docs/plans/LAST_SESSION.md`** — the rolling #2 artifact prime-master reads first. Copy the structure
from `docs/plans/templates/LAST_SESSION.md` and fill it in; **overwrite** the prior file (it is always
"the LAST session," a bridge, not an archive). Its sections mirror what prime-master reads:
- **Doing / discussing** (≤5 sentences) — the thread to pick up.
- **Commits — the story behind the last 10** — the outside factors the commit messages don't carry.
- **Worktrees — anything special** — priority build · preserved WIP · blocked (skip the merely-idle).
- **Plan position + drift** — where this session sits vs. MASTER_PLAN; did we deviate, and why.
- **Answers for the fresh start** — the questions the next session will ask, pre-empted.

Keep it LEAN — just enough context, no data dump; the live sources reconstruct everything else.

**Also distill STANDING facts to memory (separate from the delta).** A decision that changes *standing*
behavior outlives one session, so it goes to a **memory** file (`feedback`/`project`/`reference`) + its
`MEMORY.md` pointer (update an existing one, don't duplicate) — that is prime-master's #1, distinct from
the #2 delta. A decision that changes a **skill's contract** → edit the skill (that's its durable home).

**Do not skip a decision because "it's obvious to me now" — it won't be to a fresh context.**

---

## 3 — Checkpoint + compact MASTER_PLAN (the target — keep it pure)

MASTER_PLAN is prime-master's **#8: the target.** It holds current live-env / standing state + active
priorities & sequencing — **NOT** the session-decisions bridge (that is now the #2 artifact, Step 2).
Keep it that way; a purified target is the point of the current-state/target split.
- **`docs/plans/MASTER_PLAN.md`** = concise plan: state + priorities/sequencing. **The ONLY plan file the
  re-prime loads at #8** — every line is context paid for on every reset, so keep it lean. No decisions block.
- **`docs/plans/MASTER_PLAN_HISTORY.md`** = the grepable, append-only narrative. Never auto-loaded.

Do:
- Bump MASTER_PLAN "Last updated" to today; reflect what shipped (one line each) + what's genuinely next.
- **Compact — required, not optional:** MASTER_PLAN holds **only** current state + priorities +
  sequencing + Needs-Approval. Move completed / superseded narrative out and **append it to
  MASTER_PLAN_HISTORY** (move, don't delete — it stays grepable). **A header longer than ~1 screen means
  shipped-work narrative is still in it — do the move now, don't defer.**
- **Judgment guard (bounded — the safe pass may not repeat):** a *deep* restructure is riskiest in the
  heavy session a reset follows, so a **one-time** safe pass (append clearly-completed narrative, keep the
  unsure) is allowed — **but only if the previous reset did not already defer.** If the header is already
  over ~1 screen (a prior reset punted), the deep move is **required this reset** regardless of context
  weight. Never leave the header bloated two resets running.
- Commit via docs-to-main (`git switch -c docs/<slug-no-fre-token>` → PR → `--auto --squash`).
- Confirm Linear reflects reality (Done tickets closed with evidence, Awaiting-Deploy queue accurate).

---

## 4 — Emit the verdict

Print a tight block the owner (and the next session) can act on:
- **`SAFE TO /clear`** — plus the re-prime pointer (`run /prime-master` for master; a worker just wakes and
  resumes its seat with `claude -c` — no re-prime skill) and a 3–5 line "where we are / what's next", OR
- **`BLOCKED`** — the exact unmet condition(s) from Step 1 and what to do to clear them.

Never assert `SAFE` on an unverified Step 1 — a blocked reset that loses in-flight state is the failure
this skill exists to prevent.

## Identity
You operate under the guardian role (lifecycle-rules § Guardian role) — continuity keeper. A smooth
reset is a continuity duty: the next session should wake up knowing what *this* one decided, not just
what the repo records.
