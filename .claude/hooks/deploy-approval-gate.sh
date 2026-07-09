#!/usr/bin/env bash
# PreToolUse hook: gate deploy commands by session role.
#   - build / adr worktrees: hard-deny any deploy command (those sessions never deploy).
#   - master (primary tree): allow. Master is the deploy authority; deploys are gated
#     by the owner's explicit approval + master's judgment, not a sentinel file.
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

case "$root" in
    */.claude/worktrees/build|*/.claude/worktrees/adrs)
        printf 'BLOCKED: deploy commands are forbidden in the build/adr session (role boundary). master deploys.\n'
        exit 2
        ;;
esac

# master / primary tree: allow — master is the deploy authority.
exit 0
