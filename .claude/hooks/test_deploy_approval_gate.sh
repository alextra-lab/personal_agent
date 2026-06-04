#!/usr/bin/env bash
# Test harness for deploy-approval-gate.sh.
# Drives the hook with mock PreToolUse payloads from different CWDs and asserts
# exit codes. Never runs an actual deploy — the hook only gates.
set -uo pipefail

HOOK="$(cd "$(dirname "$0")" && pwd)/deploy-approval-gate.sh"
PRIMARY="/opt/seshat"
BUILD_WT="/opt/seshat/.claude/worktrees/build"
ADR_WT="/opt/seshat/.claude/worktrees/adrs"
SENTINEL="$PRIMARY/.claude/.deploy-approved"
fails=0

payload() { printf '{"tool_name":"Bash","tool_input":{"command":"%s"}}' "$1"; }

assert_exit() { # desc, expected_code, actual_code
  if [ "$2" = "$3" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected $2 got $3)"; fails=$((fails+1)); fi
}

# 1. Deploy command in build worktree → DENY (2)
( cd "$BUILD_WT" && payload "ENV=cloud make rebuild SERVICE=seshat-gateway" | bash "$HOOK" ); assert_exit "build worktree denies deploy" 2 $?
# 2. Deploy command in adr worktree → DENY (2)
( cd "$ADR_WT" && payload "make deploy" | bash "$HOOK" ); assert_exit "adr worktree denies deploy" 2 $?
# 3. Non-deploy command in build worktree → ALLOW (0)
( cd "$BUILD_WT" && payload "make test" | bash "$HOOK" ); assert_exit "build worktree allows non-deploy" 0 $?
# 4. Deploy in master, NO sentinel → DENY (2)
rm -f "$SENTINEL"
( cd "$PRIMARY" && payload "ENV=cloud make rebuild SERVICE=seshat-gateway" | bash "$HOOK" ); assert_exit "master denies deploy without sentinel" 2 $?
# 5. Deploy in master WITH fresh sentinel → ALLOW (0) and sentinel consumed
touch "$SENTINEL"
( cd "$PRIMARY" && payload "ENV=cloud make rebuild SERVICE=seshat-gateway" | bash "$HOOK" ); assert_exit "master allows deploy with sentinel" 0 $?
[ ! -f "$SENTINEL" ]; assert_exit "sentinel consumed after use" 0 $?
# 6. Non-deploy command in master → ALLOW (0)
( cd "$PRIMARY" && payload "git status" | bash "$HOOK" ); assert_exit "master allows non-deploy" 0 $?

echo "---"; [ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
