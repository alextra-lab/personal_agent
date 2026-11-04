#!/bin/sh
# Copy the two kinds of write that the real plugin makes: a flag in ~/.claude and a ledger in ~/.config.
mkdir -p "$HOME/.claude" "$HOME/.config/fre1730-fixture"
echo on > "$HOME/.claude/.fre1730-fixture-active"
echo task,cost > "$HOME/.config/fre1730-fixture/ledger.csv"
cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"FRE1730-FIXTURE-MARKER"}}
JSON
