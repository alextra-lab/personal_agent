#!/bin/bash
# Create a Reminders item so you get notified on all iCloud devices (Mac, iPhone, iPad, Watch).
#
# Usage: notify-reminder.sh "Message text"
#
# Guard: if CURSOR_IMESSAGE_BUDDY is unset, does nothing (same env as notify-imessage.sh).
# If unset, exits 0 so hooks do not fail.

set -e

msg="${1:-}"
buddy="${CURSOR_IMESSAGE_BUDDY:-}"

if [[ -z "$buddy" ]]; then
  echo "CURSOR_IMESSAGE_BUDDY not set; skipping reminder." >&2
  exit 0
fi

if [[ -z "$msg" ]]; then
  exit 0
fi

# Escape double quotes for AppleScript string ( \" inside "..." )
msg_escaped="${msg//\"/\\\"}"

osascript <<EOF
tell application "Reminders"
  set d to current date
  set minutes of d to (minutes of d) + 1
  set r to make new reminder with properties {name:"$msg_escaped", due date:d, priority:1}
end tell
EOF

exit 0
