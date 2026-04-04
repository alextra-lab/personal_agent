#!/bin/bash
# PreToolUse hook: block concurrent pytest runs.
# Multiple simultaneous test suites saturate CPU/memory on Apple Silicon.
#
# Exit 2 = Claude Code blocks the tool call and shows the message to the model.

input=$(cat)

cmd=$(echo "$input" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('tool_input', {}).get('command', ''))
except Exception:
    pass
" 2>/dev/null)

if echo "$cmd" | grep -qE "pytest"; then
    if pgrep -f "pytest" > /dev/null 2>&1; then
        pids=$(pgrep -f "pytest" | tr '\n' ' ')
        printf "BLOCKED: pytest already running (PIDs: %s). Wait for it to finish before starting another test run.\n" "${pids% }"
        exit 2
    fi
fi

exit 0
