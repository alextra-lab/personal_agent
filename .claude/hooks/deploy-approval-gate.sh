#!/usr/bin/env bash
# PreToolUse hook: gate deploy commands by session role.
#   - build / adr worktrees: hard-deny any deploy command (those sessions never deploy).
#   - master (primary tree): deny unless a fresh approval sentinel exists; consume it on use.
# Role is determined by the worktree root of the hook's CWD.
# Exit 2 = block and surface the message (matches repo hook contract).
set -uo pipefail

input=$(cat)
cmd=$(printf '%s' "$input" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('tool_input', {}).get('command', ''))
except Exception:
    pass
" 2>/dev/null)

# Only inspect deploy-class commands.
if ! printf '%s' "$cmd" | grep -qE '(ENV=cloud[[:space:]]+make|make[[:space:]]+(rebuild|deploy|build|build-full|tunnel-up))'; then
    exit 0
fi

root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
sentinel="$root/.claude/.deploy-approved"

case "$root" in
    */.claude/worktrees/build|*/.claude/worktrees/adrs)
        printf 'BLOCKED: deploy commands are forbidden in the build/adr session (role boundary). master deploys.\n'
        exit 2
        ;;
esac

# master / primary tree: require a fresh sentinel (< 5 min old), then consume it.
if [ -f "$sentinel" ]; then
    if [ -n "$(find "$sentinel" -mmin -5 2>/dev/null)" ]; then
        rm -f "$sentinel"
        exit 0
    fi
    rm -f "$sentinel"  # stale — drop it and fall through to deny
fi

printf 'BLOCKED: deploy requires explicit owner approval. The /master skill writes .claude/.deploy-approved only after you answer "deploy now? yes".\n'
exit 2
