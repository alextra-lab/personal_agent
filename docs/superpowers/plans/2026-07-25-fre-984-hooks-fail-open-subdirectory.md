# FRE-984 — Safety hooks fail open from any subdirectory

**Ticket:** FRE-984 (Approved, High, stream:build2, Tier-2:Sonnet)
**Related:** FRE-935, FRE-936 (ADR-0123 T2/T3 — the PWA work that first hit this)

## Root cause (empirically confirmed live in this session)

All five hooks in `.claude/settings.json` are registered with paths relative to cwd
(`.claude/hooks/<script>.sh`). Verified via a temporary diagnostic hook:

- The hook subprocess's own `pwd` tracks the session's *actual* current working directory
  (confirmed it changes to `seshat-pwa` when the session `cd`s there) — so a relative
  `.claude/hooks/...` command breaks the instant the session leaves the worktree root.
- `$CLAUDE_PROJECT_DIR` (env var Claude Code injects into every hook subprocess) stayed
  correctly anchored to the worktree root (`/opt/seshat/.claude/worktrees/build2`) in both
  probes — at root and from the `seshat-pwa` subdirectory. This is the anchor the ticket
  asks for, and it demonstrably survives the worktree case.

A missing hook script currently exits via the shell's own "command not found" (127), which
Claude Code reports as a **non-blocking** hook error — silent scroll-by, tool call proceeds
unguarded. That's the "fail open" half of the bug.

## Fix

1. **New `.claude/hooks/_dispatch.sh`** — shared entry point. Takes the target script's
   filename as `$1`, resolves it as `"$CLAUDE_PROJECT_DIR"/.claude/hooks/$1`, execs it if
   present+executable. If `CLAUDE_PROJECT_DIR` is unset or the target is missing/non-executable,
   prints a clearly-flagged message to stderr and **exits 2** (fail closed, loud) instead of the
   previous silent non-blocking 127. One shared script avoids duplicating the resolve+guard
   logic five times across `settings.json` command strings.

2. **`.claude/settings.json`** — all five hook commands become
   `"$CLAUDE_PROJECT_DIR"/.claude/hooks/_dispatch.sh <script>.sh` instead of the bare relative
   path.

3. **Folded-in supporting fix** — `.claude/hooks/deploy-approval-gate.sh`'s worktree-matching
   case pattern only lists `build` and `adrs`, missing `build2` (block-direct-main-push.sh's
   pattern already includes all three). Confirmed live: a deploy command run from this very
   build2 worktree root is currently **allowed** through the gate — a second, independent
   fail-open gap in the exact file this ticket is about. This blocks proving AC #2 (the deploy
   gate must engage from a subdirectory of *this* worktree), so it's folded into this PR rather
   than filed separately (build SKILL Step 5).
   - **Not folding in:** `test_deploy_approval_gate.sh` has 2 pre-existing failures (sentinel-file
     consumption logic the test asserts but the current script doesn't implement — drift from the
     original design doc, unrelated to path resolution). Confirmed pre-existing via baseline run
     before any edits. Out of scope for this ticket; flagged in the handoff, not fixed here.

## Tests

- New `test_hook_dispatch.sh`: dispatch resolves+runs a real script from an arbitrary cwd
  (proves cwd-independence given `CLAUDE_PROJECT_DIR`); loud+closed exit 2 on missing script;
  loud+closed exit 2 on unset `CLAUDE_PROJECT_DIR`.
- `test_deploy_approval_gate.sh`: add a build2-worktree case (mirrors existing BUILD_WT/ADR_WT
  structure) proving the folded-in fix.
- Live verification in this session (documented with actual output, not scripted — depends on
  real hook wiring): from `seshat-pwa/` inside this worktree, a Bash call fires hooks with no
  not-found errors; a direct-main-push attempt and a deploy-command attempt from that
  subdirectory are both blocked; the same commands still behave correctly from the worktree
  root (no regression).

## Files

- `.claude/hooks/_dispatch.sh` (new)
- `.claude/hooks/test_hook_dispatch.sh` (new)
- `.claude/settings.json` (edit — 5 hook command registrations)
- `.claude/hooks/deploy-approval-gate.sh` (edit — add build2 to worktree pattern)
- `.claude/hooks/test_deploy_approval_gate.sh` (edit — add build2 case)
