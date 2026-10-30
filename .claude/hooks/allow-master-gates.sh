#!/usr/bin/env bash
# PreToolUse hook: auto-allow master's two routine gate commands (FRE-1706).
#
# Why this exists. Master runs `gh pr merge` and `ENV=cloud make rebuild` hundreds of times per
# week. Both hit an ask-rule in settings.json, and an allow-rule cannot override an ask-rule.
# A PreToolUse `allow` decision can (FRE-867 proves it in production). Measured on 2026-10-30:
# about 190 merge prompts and 98 rebuild prompts across two master sessions. Prompts that the
# owner can no longer approve from the Apple Watch break the dev process.
#
# Why a hook and not an allow-rule. A permission rule prefix-matches the command string, so it
# cannot pin the 40-character head SHA, forbid --admin, or refuse a chained second command.
# Only a hook can read the whole shape.
#
# Contract: emit an `allow` decision ONLY when both hold:
#   1. The git top level of the hook working directory is exactly the main tree /opt/seshat.
#      Worker seats run in .claude/worktrees/*, so they never match. deploy-approval-gate.sh
#      uses the same top-level detection. There is no `|| pwd` fallback here: a fallback that
#      fails open is acceptable for a deny hook and wrong for an allow hook.
#   2. The whole command is exactly one of the forms in allow-master-gates.py.
# Anything else prints nothing and exits 0, which falls through to the normal permission flow
# (the prompt runs exactly as it does today). This hook never emits `deny`. It is registered
# with the `open` dispatch policy so that a missing script degrades to prompting, never to
# allowing.
#
# exec, so the decider inherits this process's stdin: the hook payload arrives there.
set -uo pipefail

# Ambient repository variables (GIT_WORK_TREE, GIT_DIR, ...) make `git rev-parse` report a path
# that the working directory does not have. A worker worktree with GIT_WORK_TREE=/opt/seshat
# would read as the main tree. Clear every GIT_* variable before the lookup.
for name in $(compgen -e | grep '^GIT_'); do unset "$name"; done

# A declined call drains stdin first, so the writer never sees a broken pipe.
decline() { cat >/dev/null; exit 0; }

# The git top level alone is not proof of the master role. A worker worktree is a directory
# inside /opt/seshat, and the only thing that makes git call it a top level is its `.git` file.
# A worker that moves that file aside makes git walk up and report /opt/seshat. Two checks that
# a file operation in the worktree cannot change:
#   - CLAUDE_PROJECT_DIR is set by the harness from the session's project root, so it is the
#     worktree path for a worker seat.
#   - The physical working directory of a worker is under .claude/worktrees/.
[ "${CLAUDE_PROJECT_DIR:-}" = "/opt/seshat" ] || decline
case "$(pwd -P)" in
    /opt/seshat/.claude/worktrees|/opt/seshat/.claude/worktrees/*) decline ;;
esac

root=$(git rev-parse --show-toplevel 2>/dev/null) || decline
[ "$root" = "/opt/seshat" ] || decline

exec python3 "$(dirname "$0")/allow-master-gates.py"
