# Last session — 2026-09-13 18:40 to 2026-09-14 05:40 UTC

## Doing / discussing  (≤5 sentences)
The session started at the planned VPS reboot and ended with the owner's `/doctor` cleanup. At the
reset, adr is in codex round 3 on FRE-1328's ADR, build1 is committing FRE-1372, and build2 is idle
because FRE-1495 is blocked by FRE-1372. Two tickets arrived overnight from a build seat and need the
owner: FRE-1505 (the eval-treatment gateway can misuse real credentials through unsandboxed bash) and
FRE-1506 (IsolatedArmRunner still leaks a Turn node after consolidation settles). No PR is at the gate.

## What was decided and why

**FRE-1328: enforce moves from shape C to shape B (owner, 2026-09-14 ~05:00).** adr measured 91
turns: C (no tool ran) is mostly general knowledge, and 16 of its 30 turns were eval traffic. B (typed
tools ran, then no citation or no match) is the largest group at 38. The owner accepted two limits
that master verified in code: retry only on uncited, not-contained or not-entailed spans, because
`decide()` today blocks on every failure, machine-undecided spans included. After the retry, ship
with the declaration and never `TERMINAL_NO_SOURCE`. Unsettled spans (`_MACHINE_UNDECIDED`) must not
count in FRE-1325's note either. **D5 heavy is withdrawn:** forcing retrieval before generation cannot
skip shape C, and unmeasured means heavy on every turn. No live change: production runs `observe`,
and `_select_enforcement` returns early outside `enforce` (executor.py:2045). Master does not know
whether the owner picked the seat's option 1 as written or with master's two added notes. Check the
ADR PR against both.

**Eval contamination control (owner, 2026-09-13 20:09): the isolated eval stack, with production
behaviour settings passed through.** FRE-1372 was reopened instead of filing a duplicate. Its comment
forbids passing any production substrate address, because IsolatedArmRunner issues an unscoped
DETACH DELETE. The live AC-1 run uses the local model, and the seat must ask master before any role
reaches a paid provider. FRE-1495 is blocked by FRE-1372, and FRE-1487 re-runs after FRE-1495.

**No separate FRE-1498 re-run.** FRE-1495 reuses that harness and scores answer quality and landing.

**Model aliases stay unpinned (owner).** Memory and FRE-1504 record it, and it is not up for a fix.

**FRE-1504 and FRE-1501 closed Done with named follow-throughs, not held open.** FRE-1504's live check
passed at 20:30: the alert fired on the third tick. FRE-1501's runtime proof waits for the first local
fan-out, which FRE-1495's arms will produce. If "Failed to parse tool call arguments as JSON" recurs,
it goes to Verify Failed.

**The red `PreToolUse:Read` messages were hookify, not a Claude Code change** (see memory).

**`/doctor` cleanup (owner-approved).** These plugins are off in `~/.claude/settings.json`:
ralph-loop, the code-review plugin (the built-in `code-review` stays), typescript-lsp and pyright-lsp.
context7 and linear-issue-create are off in `/opt/seshat/.claude/settings.local.json` only. This PR
trims both CLAUDE.md files and moves Model Routing, the merge gotcha, plan naming and the pre-merge
checklist into `.claude/skills/lifecycle-rules.md`. Look for them there now.

**Master errors worth not repeating.**
1. Repeated this file's "FRE-1487 AC-5 waits on the owner" without reading the ticket. It was an
   undecided three-option choice.
2. Quoted the VPS as having ~10 GiB of RAM. It has 22 GiB, with ~16 GiB free. Memory is corrected.
3. Said "my watch did not stop at the first alert". It stopped at 20:30. The 20-minute hold on build1's
   modal came from master reading the notification late while gating #1158.
4. The first launch watch used a regex lookahead that the local grep (ugrep) rejects, so it never
   matched. Test a watch pattern before arming it.

## Worktrees — anything special
- `build` is on FRE-1372's branch, cut before `2ab84c2d`. It still has the old hookify rule, so red
  Read messages continue there until the branch includes that commit.
- At ~21:00 build1 stopped at a `Read(.claude/worktrees/build/.env)` permission prompt. Master
  recommended "No, key names only" and did not answer it. The seat later moved on. The outcome is not
  verified.
- The untracked `*.bak*` files at the root and in `telemetry/` are the owner's. Master removed only its
  own dispatch-state backup.

## Sequence position + drift
Dispatch is running. build1: FRE-1372, then build2: FRE-1495, then the FRE-1487 re-run. adr: FRE-1328,
then FRE-1502. Two alerts were open at the reset. `dispatch_stall:build1`: FRE-1372 still reads
Approved in Linear while its seat commits, so check whether the seat moved it. `dispatch_awaiting_owner:adr`:
probably stale, because the owner answered and the seat is in codex round 3.

## Answers for the fresh start
- **What is at the gate?** Nothing. The next PRs are FRE-1372 (build1) and FRE-1328's ADR (adr).
- **What must be read before gating FRE-1372?** FRE-1505 and FRE-1506. Both touch the eval stack that
  FRE-1372 changes, and FRE-1505 is a credentials exposure that needs the owner.
- **What live checks are owed?** FRE-1501's runtime proof, at the first local fan-out.
- **What needs the owner?** Approval of FRE-1505 and FRE-1506. FRE-1338 (Approved, Urgent, no stream
  label) was updated overnight, so read its thread before labelling it.
