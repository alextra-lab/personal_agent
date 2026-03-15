#!/bin/bash
# stop hook: when the agent run completes, send an iMessage notification.
# Does not emit followup_message; only notifies.

input=$(cat)
status=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)

if [[ "$status" != "completed" ]]; then
  echo '{}'
  exit 0
fi

# Optional: include project name in message
project_name=""
if [[ -n "${CURSOR_PROJECT_DIR:-}" ]]; then
  project_name=$(basename "$CURSOR_PROJECT_DIR")
elif [[ -n "$input" ]]; then
  project_name=$(echo "$input" | python3 -c "import sys,json; roots=json.load(sys.stdin).get('workspace_roots',['']); print(roots[0].split('/')[-1] if roots and roots[0] else '')" 2>/dev/null)
fi

if [[ -n "$project_name" ]]; then
  msg="Cursor has completed ($project_name)."
else
  msg="Cursor has completed."
fi

hook_dir="$(cd "$(dirname "$0")" && pwd)"
"$hook_dir/notify-imessage.sh" "$msg"

echo '{}'
exit 0
