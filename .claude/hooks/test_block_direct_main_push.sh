#!/usr/bin/env bash
# Test harness for block-direct-main-push.sh (FRE-680).
# Drives the hook with mock PreToolUse payloads from synthetic worktree-shaped
# CWDs and asserts exit codes. Hermetic: builds temp dirs with the right path
# suffixes (no real repo, no hardcoded machine paths). Never runs a real push.
set -uo pipefail

HOOK="$(cd "$(dirname "$0")" && pwd)/block-direct-main-push.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

BUILD_WT="$TMP/.claude/worktrees/build"
ADR_WT="$TMP/.claude/worktrees/adrs"
PRIMARY="$TMP/primary"
mkdir -p "$BUILD_WT" "$ADR_WT" "$PRIMARY"
fails=0

payload() { printf '{"tool_name":"Bash","tool_input":{"command":"%s"}}' "$1"; }

assert_exit() { # desc, expected_code, actual_code
  if [ "$2" = "$3" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected $2 got $3)"; fails=$((fails+1)); fi
}

# 1. Push to main from build worktree → BLOCK (2)
( cd "$BUILD_WT" && payload "git push origin main" | bash "$HOOK" ); assert_exit "build worktree blocks push to main" 2 $?
# 2. Push HEAD:main from adr worktree → BLOCK (2)
( cd "$ADR_WT" && payload "git push origin HEAD:main" | bash "$HOOK" ); assert_exit "adr worktree blocks HEAD:main" 2 $?
# 3. Fully-qualified refs/heads/main from build worktree → BLOCK (2)
( cd "$BUILD_WT" && payload "git push origin refs/heads/main" | bash "$HOOK" ); assert_exit "build worktree blocks refs/heads/main" 2 $?
# 4. main:main refspec from build worktree → BLOCK (2)
( cd "$BUILD_WT" && payload "git push origin main:main" | bash "$HOOK" ); assert_exit "build worktree blocks main:main" 2 $?
# 5. Feature-branch push from build worktree → ALLOW (0)
( cd "$BUILD_WT" && payload "git push --force-with-lease origin fre-680-x" | bash "$HOOK" ); assert_exit "build worktree allows feature push" 0 $?
# 6. Feature branch whose NAME contains 'main' → ALLOW (0) — must not false-block
( cd "$BUILD_WT" && payload "git push origin feature-mainline" | bash "$HOOK" ); assert_exit "build worktree allows feature-mainline" 0 $?
# 7. Bare push from build worktree (unresolvable destination) → BLOCK (2), fail-closed
( cd "$BUILD_WT" && payload "git push" | bash "$HOOK" ); assert_exit "build worktree fails closed on bare push" 2 $?
# 8. Push to main from primary tree → ALLOW (0) — the docs/MASTER_PLAN allow path
( cd "$PRIMARY" && payload "git push origin main" | bash "$HOOK" ); assert_exit "primary allows push to main (docs path)" 0 $?
# 9. Non-push command in build worktree → ALLOW (0)
( cd "$BUILD_WT" && payload "git status" | bash "$HOOK" ); assert_exit "build worktree allows non-push" 0 $?

echo "---"; [ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
