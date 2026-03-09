#!/bin/bash
# stop hook: run pytest when the agent completes.
# If tests fail and loop_count is low, send the agent back to fix them.

input=$(cat)
status=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
loop_count=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('loop_count',0))" 2>/dev/null)

if [[ "$status" != "completed" ]]; then
  echo '{}'
  exit 0
fi

cd "$CURSOR_PROJECT_DIR" 2>/dev/null || cd "$(echo "$input" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('workspace_roots',[''])[0])" 2>/dev/null)" || exit 0

test_output=$(python3 -m pytest --tb=line -q --no-header 2>&1 | tail -20)

if echo "$test_output" | grep -qE "[0-9]+ failed"; then
  failed_count=$(echo "$test_output" | grep -oE "[0-9]+ failed" | head -1)
  cat <<EOF
{"followup_message": "Tests have $failed_count. Fix the failing tests before completing. Summary:\n$test_output"}
EOF
else
  echo '{}'
fi

exit 0
