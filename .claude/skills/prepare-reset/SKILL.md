---
name: prepare-reset
description: Owner-invoked before resetting (/clear-ing) a session. Verifies a safe reset boundary, writes the session-delta artifact (LAST_SESSION.md) so the next session inherits this session's decisions, distills standing facts into memory, and emits a go/no-go verdict. The safe wind-down bookend to prime-master.
---

# Prepare for reset — safe wind-down before `/clear`

Invoked by the owner in the session about to be reset. The bookend to `prime-master`:
prime-master READS from durable sources after a reset; this skill WRITES the one thing those
sources cannot hold — the conversational layer (the *why*, the *was-doing*, the drift-and-why) —
before it is lost to `/clear`.

Detect the role from the RC/tmux name: `cc-master` → master; `cc-build*`/`cc-adrs` → worker.

## 1 — Safety gate (refuse a mid-flight reset)

**Master.** ALL must hold, or the reset is BLOCKED:
- Nothing merged-but-unverified in `Awaiting Deploy` that you own; no PR mid-merge, no ticket
  half-closed.
- Working tree clean on `main` (`git -C /opt/seshat status`).

**Worker.** Its PR is pushed AND its handoff comment is posted. (A worker resumes its own
context with `claude -c`; the only hard gate is no unpushed in-flight work.)

If anything fails: name exactly what's blocking and finish/record it first — do NOT bless the reset.

## 2 — Write the session-delta artifact

**Master only.** Write **`docs/plans/LAST_SESSION.md`** (overwrite — it is always "the LAST
session", a bridge, not an archive; template in `docs/plans/templates/`). Write **only what no
durable source can reconstruct** — do NOT narrate commits (prime-master reads `git log` itself),
ticket states (Linear), or health (the probe). Sections:
- **Doing / discussing** (≤5 sentences) — the thread to pick up.
- **What was decided and why** — the reasoning behind the commits: approaches rejected,
  corrections made, assumptions now known false. This section earns the file.
- **Worktrees — anything special** (skip the merely-idle).
- **Sequence position + drift** — where this sits vs the console's directives; deviations and why.
- **Answers for the fresh start** — the next session's questions, pre-empted.

**Size bound: 90 lines.** Over it, cut — never carry overflow to another file. Test for every
line: if it could be derived from `git log`, Linear or a probe, delete it.

**Also distill STANDING facts to memory** — a decision that changes standing behaviour outlives
the session: memory file + `MEMORY.md` pointer (update, don't duplicate). A decision that changes
a skill's contract → edit the skill. Don't skip a decision because "it's obvious now" — it won't
be to a fresh context.

## 3 — Emit the verdict

- **`SAFE TO /clear`** — plus the re-prime pointer (`/prime-master` for master; a worker just
  wakes with `claude -c`) and a 3–5 line "where we are / what's next", OR
- **`BLOCKED`** — the exact unmet condition(s) and what clears them.

Never assert SAFE on an unverified Step 1.
