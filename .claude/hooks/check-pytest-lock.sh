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
    # Match only actual Python test runner processes, not this hook script itself.
    if pgrep -f "python.*-m pytest" > /dev/null 2>&1; then
        pids=$(pgrep -f "python.*-m pytest" | tr '\n' ' ')
        # FRE-777: collision telemetry for the substrate-isolation decision (spec §7 D6).
        # --git-common-dir resolves to the primary repo's .git from any worktree, so all
        # streams append to the one gitignored telemetry/ file without a hardcoded path.
        log_dir="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)")/telemetry"
        mkdir -p "$log_dir" 2>/dev/null && \
            echo "$(date -u +%FT%TZ) worktree=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)") blocked_pids=${pids% }" \
            >> "$log_dir/pytest_lock_blocks.log"
        printf "BLOCKED: pytest already running (PIDs: %s). Wait for it to finish before starting another test run.\n" "${pids% }"
        exit 2
    fi
fi

exit 0
