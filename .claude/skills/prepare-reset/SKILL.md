---
name: prepare-reset
description: Owner-invoked before resetting (/clear-ing) a session. Verifies a safe reset boundary, DISTILLS the session's fresh decisions into durable memory so the next session inherits them, checkpoints MASTER_PLAN, and emits a go/no-go verdict. The safe wind-down bookend to prime-master.
---

# Prepare for reset — safe wind-down before `/clear`

Invoked by the owner in the session about to be reset. This is the bookend to `prime-master`:
`prime-master` **rebuilds** from durable sources *after* a reset; `prepare-reset` **captures + checkpoints**
*before* one.

**The core problem this solves.** `prime-master` rebuilds only from DURABLE sources (MASTER_PLAN, Linear,
git, memory) — it deliberately ignores prior conversation. So **every decision made this session that
isn't written down is LOST on `/clear`** — and that fresh decision trail is exactly what the next session
comes up short on. Step 2 is the point of this skill: write the decisions down. The safety gate and the
MASTER_PLAN bump are supporting.

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

**Worker.** Its PR is pushed AND its handoff comment is posted on the ticket AND its context-disposition
(keep/clear) is stated. If mid-implementation with unpushed work → BLOCKED: finish or stash first.

---

## 2 — Distill the session's fresh decisions into durable memory (the part that's been missing)

`prime-master` will NOT see this conversation. So harvest what it would otherwise lose. Walk the session
and list **every decision that isn't already durably recorded** — not "what shipped" (Linear has that),
but the *decisions and their rationale*:
- design calls made (what we chose, and what we rejected and why),
- policy / process changes,
- corrections and course-reversals (X was wrong, we now do Y — with the reason),
- things deliberately deferred / out of scope, and why,
- anything the owner steered that changes standing behavior.

Route each to the right durable home so a rebuild inherits it:
- **Standing behavior / preference / correction** → a **memory** file (`feedback`/`project`/`reference`)
  + its `MEMORY.md` pointer. This is auto-loaded next session. (If a relevant memory already exists,
  UPDATE it, don't duplicate.)
- **In-flight work / sequencing / decision that shapes what's next** → the **MASTER_PLAN** "Recent
  decisions" block (see Step 3) and/or the relevant Linear ticket comment.
- **A decision that changes a skill's contract** → edit the skill (that's the durable home).

Then write, near the top of MASTER_PLAN, a compact **`## Recent decisions (last session — YYYY-MM-DD)`**
block: 5–12 bullets, each `decision → why`, in the next session's language. This is the first thing the
re-prime reads. Keep it a *rolling* block — replace the prior one; it is a bridge, not an archive.

**Do not skip a decision because "it's obvious to me now" — it won't be to a fresh context.** When unsure
whether something is durable-worthy, capture it: an over-captured decision costs a line; a lost one costs
the next session a wrong turn.

---

## 3 — Checkpoint + compact MASTER_PLAN

The plan is split in two — keep it that way:
- **`docs/plans/MASTER_PLAN.md`** = master's *concise* plan: current live-env / standing state + active
  priorities & sequencing + the Recent-decisions block. **The ONLY plan file the re-prime loads** — every
  line here is context paid for on every reset, so keep it lean.
- **`docs/plans/MASTER_PLAN_HISTORY.md`** = the grepable, append-only narrative (what shipped, when, why).
  Searched on demand, **never** auto-loaded.

Do:
- Bump MASTER_PLAN "Last updated" to today; reflect what shipped (one line each) + what's genuinely next.
- **Compact:** move any completed / superseded session narrative out of MASTER_PLAN and **append it to
  MASTER_PLAN_HISTORY** (move, don't delete — it stays grepable, nothing is lost). A bloated concise-plan
  is dead weight the re-prime re-reads forever.
- **Judgment guard (important):** a *deep* restructure of the concise plan is riskiest in exactly the
  heavy/dull session a reset follows. If unsure what's still load-bearing, do the **safe** compaction
  (append only clearly-completed narrative to HISTORY; keep anything you're unsure about) and **flag a deep
  pass as an early-fresh-session task** in the Step-4 verdict. Recoverability is high — everything moved is
  grepable in HISTORY — so bias toward moving *completed* narrative, not toward rewriting live state at 60%.
- Commit via docs-to-main (`git switch -c docs/<slug-no-fre-token>` → PR → `--auto --squash`).
- Confirm Linear reflects reality (Done tickets closed with evidence, Awaiting-Deploy queue accurate).

---

## 4 — Emit the verdict

Print a tight block the owner (and the next session) can act on:
- **`SAFE TO /clear`** — plus the one-line re-prime pointer (`run /prime-master` for master;
  `/prime-worker` for a worker) and a 3–5 line "where we are / what's next" so the transition is warm, OR
- **`BLOCKED`** — the exact unmet condition(s) from Step 1 and what to do to clear them.

Never assert `SAFE` on an unverified Step 1 — a blocked reset that loses in-flight state is the failure
this skill exists to prevent.

## Identity
You operate under the guardian role (lifecycle-rules § Guardian role) — continuity keeper. A smooth
reset is a continuity duty: the next session should wake up knowing what *this* one decided, not just
what the repo records.
