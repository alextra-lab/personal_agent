#!/bin/bash
# PostToolUse hook: auto-format Python files after Edit/Write.
# Runs `ruff format` then `ruff check --fix` on the touched file.
# Exit 0 = silent success; non-blocking by design.

input=$(cat)

file=$(printf '%s' "$input" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('tool_input', {}).get('file_path', ''))
except Exception:
    pass
" 2>/dev/null)

# Only act on .py files inside the project
case "$file" in
    *.py) ;;
    *) exit 0 ;;
esac

# Resolve to repo-relative if absolute path under /opt/seshat
case "$file" in
    /opt/seshat/*) rel="${file#/opt/seshat/}" ;;
    *) rel="$file" ;;
esac

cd /opt/seshat || exit 0

# Skip if file vanished between edit and hook
[ -f "$rel" ] || exit 0

# Run formatter and linter quietly; never block the edit.
# --unfixable F401: never auto-remove unused imports mid-edit — an import added
# in one edit before its usage in a later edit must survive (FRE-798). CI's
# `ruff check` (no --fix) still fails on a genuinely-unused import, so nothing slips.
uv run ruff format "$rel" >/dev/null 2>&1
uv run ruff check --fix --unfixable F401 "$rel" >/dev/null 2>&1

exit 0
