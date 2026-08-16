#!/usr/bin/env bash
# Test harness for deploy-approval-gate.sh.
# Drives the hook with mock PreToolUse payloads from different CWDs and asserts
# exit codes. Never runs an actual deploy — the hook only gates.
set -uo pipefail

HOOK="$(cd "$(dirname "$0")" && pwd)/deploy-approval-gate.sh"
PRIMARY="/opt/seshat"
BUILD_WT="/opt/seshat/.claude/worktrees/build"
BUILD2_WT="/opt/seshat/.claude/worktrees/build2"
ADR_WT="/opt/seshat/.claude/worktrees/adrs"
fails=0

payload() { printf '{"tool_name":"Bash","tool_input":{"command":"%s"}}' "$1"; }

assert_exit() { # desc, expected_code, actual_code
  if [ "$2" = "$3" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected $2 got $3)"; fails=$((fails+1)); fi
}

# 1. Deploy command in build worktree → DENY (2)
( cd "$BUILD_WT" && payload "ENV=cloud make rebuild SERVICE=seshat-gateway" | bash "$HOOK" ); assert_exit "build worktree denies deploy" 2 $?
# 1b. Deploy command in build2 worktree → DENY (2) — FRE-984: pattern previously omitted build2
( cd "$BUILD2_WT" && payload "ENV=cloud make rebuild SERVICE=seshat-gateway" | bash "$HOOK" ); assert_exit "build2 worktree denies deploy" 2 $?
# 2. Deploy command in adr worktree → DENY (2)
( cd "$ADR_WT" && payload "make deploy" | bash "$HOOK" ); assert_exit "adr worktree denies deploy" 2 $?
# 3. Non-deploy command in build worktree → ALLOW (0)
( cd "$BUILD_WT" && payload "make test" | bash "$HOOK" ); assert_exit "build worktree allows non-deploy" 0 $?
# 4. Deploy-class command in master → ALLOW (0)
( cd "$PRIMARY" && payload "ENV=cloud make rebuild SERVICE=seshat-gateway" | bash "$HOOK" ); assert_exit "master allows deploy" 0 $?
# 5. Non-deploy command in master → ALLOW (0)
( cd "$PRIMARY" && payload "git status" | bash "$HOOK" ); assert_exit "master allows non-deploy" 0 $?

echo "---"; [ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
