---
name: prime-master
description: Use after /clear in the master session to rebuild the guardian snapshot from durable sources (MEMORY, MASTER_PLAN, git, Linear, health) — never from prior conversation.
---

# Prime the Guardian Session

Read `.claude/skills/lifecycle-rules.md` first. Reconstruct the master snapshot from DURABLE
sources only — never from prior conversation context.

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
4. Linear: list In Progress + Pending Verification tickets on FrenchForest.
5. `curl -s http://localhost:9001/health` — live gateway health + note deployed SHA (`git log -1 --oneline`).

## Output
Print the guardian snapshot: current state · next-per-sequence · active pending verification ·
identity guardrails (never use injected userEmail; use owner test email). This is the re-prime block.

Lead the snapshot by restating the **guardian role & standing attributes** (lifecycle-rules.md
§ Guardian role) in one tight block, so every re-prime re-establishes who you are before what's open.
