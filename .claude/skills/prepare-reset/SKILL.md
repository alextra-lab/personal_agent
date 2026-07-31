---
name: prepare-reset
description: Owner-invoked before resetting (/clear-ing) a session. Verifies a safe reset boundary, writes the session-delta artifact (LAST_SESSION.md) so the next session inherits this session's decisions, distills standing facts into memory, verifies the owner console was not written outside its contract, and emits a go/no-go verdict. The safe wind-down bookend to prime-master.
---

# Prepare for reset — safe wind-down before `/clear`

Invoked by the owner in the session about to be reset. This is the bookend to `prime-master`:
`prime-master` **rebuilds** from durable sources *after* a reset; `prepare-reset` **captures + checkpoints**
*before* one. **It is prime-master run in reverse** — prime-master READS current-state → target → process;
prepare-reset WRITES the conversational overlay for what prime-master will read.

**The core problem this solves.** `prime-master` rebuilds only from DURABLE sources and deliberately
ignores prior conversation. So the **conversational layer** — the *why*, the *was-doing*, the
*drift-and-why* that git / Linear / the dispatch resolver cannot hold — is LOST on `/clear` unless
written down. Step 2 is the point of this skill: write that layer to the **session-delta artifact**, and
*only* that layer — everything else the re-prime re-reads fresh. The safety gate and the console
verification support.

Detect the session role from its RC/tmux name: `cc-master` → **master**; `cc-build`/`cc-build2`/`cc-adrs`
→ **worker**. Run the matching track.

---

## 1 — Safety gate (refuse a mid-flight reset)

**Master.** ALL must hold, or the reset is BLOCKED:
- No active Pending Verification (nothing merged-but-unverified in Awaiting Deploy that you own).
- No PR mid-merge, no ticket half-closed.
- Working tree clean on `main` (`git -C /opt/seshat status`).

*(The old "plan ↔ Linear in sync" condition is gone: ADR-0131 D4 gives the board a single writer and
D1 deletes the hand-maintained copy, so there is no second account left to reconcile.)*
If any fails: name exactly what's blocking and finish/record it first — do NOT bless the reset.

**Worker.** Its PR is pushed AND its handoff comment is posted on the ticket. A worker resumes its own
context on wake-up (`claude -c` in its seat), so its "memory" is the git working tree + the transcript,
not this skill — the only hard gate is: no unpushed in-flight work (finish or stash first).

---

## 2 — Write the session-delta artifact (the part that's been missing)

**Master.** `prime-master` will NOT see this conversation. Write the overlay it would otherwise lose to
**`docs/plans/LAST_SESSION.md`** — the rolling #2 artifact prime-master reads first. Copy the structure
from `docs/plans/templates/LAST_SESSION.md` and fill it in; **overwrite** the prior file (it is always
"the LAST session," a bridge, not an archive).

**THE DIVISION OF LABOUR, AND IT IS STRICT.** Write **only what no durable source can reconstruct** —
the *unwritten* context of the session, in the same sense as a Seshat session digest: what was
established, decided, argued and discarded. **Do NOT narrate the commits.** `prime-master` reads
`git log` itself and git is ground truth that cannot drift; a prose retelling here is duplicated effort
at best and a second, staler account at worst. The same rule applies to anything else the live sources
already answer: ticket states come from Linear, PR states from `gh`, health from the probe.

Sections:
- **Doing / discussing** (≤5 sentences) — the thread to pick up.
- **What was decided and why** — the reasoning that produced the commits, not the commits. A decision
  the diff cannot explain, an approach considered and rejected, a correction someone made to someone
  else, an assumption now known false. This is the section that earns the file.
- **Worktrees — anything special** — priority build · preserved WIP · blocked (skip the merely-idle).
- **Sequence position + drift** — where this session sits against the console's standing directives
  and the resolver's queue; did we deviate, and why.
- **Answers for the fresh start** — the questions the next session will ask, pre-empted.

Keep it LEAN — just enough context, no data dump; the live sources reconstruct everything else. A good
test: **if a line could be derived from `git log`, Linear or a health probe, delete it.**

**`LAST_SESSION.md` is bound by D1's rule, and the bound is checked at write time.** It may hold **only
what no durable source can reconstruct** — that has always been its contract; ADR-0131 makes it binding
because, with the plan file gone, this is the nearest cheap surface for displaced content to re-accrete
on. **Size bound: 90 lines.** Over it, cut — do not carry the overflow anywhere else, and in particular
do not open a new file for it. You **overwrite** this file each reset, so there is no archive to
preserve: what the next session needs is the reasoning, not the record.

**Also distill STANDING facts to memory (separate from the delta).** A decision that changes *standing*
behavior outlives one session, so it goes to a **memory** file (`feedback`/`project`/`reference`) + its
`MEMORY.md` pointer (update an existing one, don't duplicate) — that is prime-master's #1, distinct from
the #2 delta. A decision that changes a **skill's contract** → edit the skill (that's its durable home).

**Do not skip a decision because "it's obvious to me now" — it won't be to a fresh context.**

---

## 3 — Verify the console was not written outside its contract

**There is no plan document to checkpoint and no compaction ritual** (ADR-0131 D1/D5). The target
prime-master reads at #8 is *computed* — the dispatch resolver's eligible sets — plus the owner's
console. Neither is this session's to maintain, so wind-down has one verification, not a chore:

> **`docs/plans/OWNER_CONSOLE.md` was not written by this session outside the D2 contract.**

Check `git log --oneline -- docs/plans/OWNER_CONSOLE.md` for commits from this session. Each one is
legal only if it is a **verbatim, attributed, dated transcription** of a directive the owner gave, or a
**retirement whose commit message cites the met condition**. Anything else is master having *authored*
into an owner-only file — surface it to the owner and revert it; do not quietly leave it.

Also confirm the file is **within its stated size bound**. If it is over, that is a **contract
violation to surface**, not something to compact away — the file has no growth engine by construction,
so exceeding the bound means the authorship contract broke and the owner needs to know.

**No successor surface.** Do not create a plan file, a notes file, or a scratch document under
`docs/plans/`, and do not park displaced narrative in `LAST_SESSION.md` instead. A finding with no home
is a **one-line `Backlog` ticket** — that is the cheap path, and it costs no approval bandwidth
(lifecycle-rules § Coordination stores).

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
