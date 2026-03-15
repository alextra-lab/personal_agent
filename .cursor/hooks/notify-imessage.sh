#!/bin/bash
# Send a one-line iMessage to the configured buddy (for Cursor notifications).
#
# Usage: notify-imessage.sh "Message text"
#
# Requires CURSOR_IMESSAGE_BUDDY to be set (e.g. ) in your
# environment (~/.zshenv, ~/.zshrc, or Cursor env). If unset, tries ~/.zshenv
# so Cursor hooks (which often run without your shell env) still see it.
# If still unset, does nothing and exits 0 so hooks do not fail.

set -e

msg="${1:-}"
buddy="${CURSOR_IMESSAGE_BUDDY:-}"

if [[ -z "$buddy" ]] && [[ -f ~/.zshenv ]]; then
  buddy=$(zsh -c 'source ~/.zshenv 2>/dev/null; echo "$CURSOR_IMESSAGE_BUDDY"' 2>/dev/null) || true
  buddy="${buddy%$'\n'}"
fi

if [[ -z "$buddy" ]]; then
  echo "CURSOR_IMESSAGE_BUDDY not set; skipping iMessage notification." >&2
  exit 0
fi

if [[ -z "$msg" ]]; then
  exit 0
fi

# Pass message and buddy via argv so we don't need to escape quotes in the message.
osascript -e 'on run argv' \
  -e 'tell application "Messages" to send (item 1 of argv) to buddy (item 2 of argv) of (service 1 whose service type is iMessage)' \
  -e 'end run' -- "$msg" "$buddy"

exit 0
