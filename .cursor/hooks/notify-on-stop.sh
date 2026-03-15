#!/bin/bash
# stop hook: when the agent run completes, create a Reminder so you see it on all devices.
# Does not emit followup_message; only notifies.

input=$(cat)
status=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)

if [[ "$status" != "completed" ]]; then
  echo '{}'
  exit 0
fi

# Include project (work context) in the reminder message
project_name=""
if [[ -n "${CURSOR_PROJECT_DIR:-}" ]]; then
  project_name=$(basename "$CURSOR_PROJECT_DIR")
elif [[ -n "$input" ]]; then
  project_name=$(echo "$input" | python3 -c "import sys,json; roots=json.load(sys.stdin).get('workspace_roots',['']); print(roots[0].split('/')[-1] if roots and roots[0] else '')" 2>/dev/null)
fi

if [[ -n "$project_name" ]]; then
  msg="Cursor run completed — $project_name. Review when back."
else
  msg="Cursor run completed. Review when back."
fi

hook_dir="$(cd "$(dirname "$0")" && pwd)"
"$hook_dir/notify-reminder.sh" "$msg"

echo '{}'
exit 0
