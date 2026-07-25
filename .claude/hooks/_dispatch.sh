#!/usr/bin/env bash
# Shared entry point for every settings.json hook registration (FRE-984).
#
# A bare relative command (".claude/hooks/foo.sh") resolves against the
# session's current working directory, not the project root — the instant a
# session `cd`s into a subdirectory, the path breaks and the shell's own
# "command not found" (127) surfaces to Claude Code as a non-blocking hook
# error, so a safety hook can go dark with nothing loud to show for it.
#
# This wrapper resolves the target against $CLAUDE_PROJECT_DIR (the env var
# Claude Code injects into every hook subprocess, confirmed anchored to the
# worktree root regardless of session cwd) and execs it. If the target can't
# be resolved, it fails LOUD (a clearly-flagged stderr message) always, and
# fails CLOSED (exit 2, blocking) or OPEN (exit 0) per the declared policy —
# a hook that blocks by contract (deploy gate, main-push guard, forbidden-
# pattern check) must not silently pass when its script goes missing; a hook
# that is explicitly non-blocking by design (format-python, nudge-discussion)
# must not start blocking just because its script vanished.
#
# Usage: _dispatch.sh <script-name> <closed|open> [args passed through]
#
# Known limitation: this can only run at all if the invoking shell manages to
# resolve "$CLAUDE_PROJECT_DIR"/.claude/hooks/_dispatch.sh in the first place.
# If CLAUDE_PROJECT_DIR were totally unset, that top-level resolution itself
# would 127 before this script ever starts — the same silent-127 failure mode
# this ticket closes. Live-verified (session diagnostic, both at a worktree
# root and from a subdirectory) that Claude Code always sets the var for hook
# subprocesses; the unset-check below covers direct/manual invocation and is
# exercised by test_hook_dispatch.sh, not the settings.json bootstrap step.
set -uo pipefail

name="${1:-}"
policy="${2:-}"
shift 2 2>/dev/null || true

loud() {
    printf 'SAFETY HOOK DISPATCH ERROR: %s\n' "$1" >&2
}

fail() {
    loud "$1"
    if [ "$policy" = "closed" ]; then
        exit 2
    fi
    exit 0
}

case "$policy" in
    closed|open) ;;
    *) loud "invalid policy '$policy' for hook '$name' — must be 'closed' or 'open'"; exit 2 ;;
esac

case "$name" in
    ""|*/*|.*) fail "invalid or missing hook script name: '$name'" ;;
esac

if [ -z "${CLAUDE_PROJECT_DIR:-}" ]; then
    fail "CLAUDE_PROJECT_DIR is unset — cannot resolve '$name'; treat as unguarded"
fi

target="$CLAUDE_PROJECT_DIR/.claude/hooks/$name"

if [ ! -x "$target" ]; then
    fail "$target not found or not executable — treat as unguarded, investigate immediately"
fi

exec "$target" "$@"
