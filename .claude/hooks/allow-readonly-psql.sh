#!/usr/bin/env bash
# PreToolUse hook: auto-allow READ-ONLY psql queries against substrate containers (FRE-867).
#
# Why this exists. Worker seats do substrate diagnosis constantly, and every one of those
# queries hits the `Bash(docker exec:*)` ask-rule and stops the seat dead on a permission
# dialog nobody is watching. Measured cost so far: three stalls, ~17.5 seat-hours, every one
# of them a SELECT.
#
# Why a hook and not an allow-rule. Permission rules prefix-match the command string, so the
# narrowest expressible rule is per-CONTAINER ("docker exec cloud-sim-postgres psql:*") — which
# would also authorize DROP/UPDATE against production Postgres with no prompt. The boundary
# that matters here is the SHAPE of the statement, not the container, and only a hook can read
# it. Test-stack containers keep their existing blanket allow-rules; this covers the rest.
#
# Contract: emit an `allow` decision ONLY when every segment of the command is provably
# read-only. Anything else prints nothing and exits 0, which falls through to the normal
# permission flow (i.e. the seat prompts exactly as it does today). Registered with the `open`
# dispatch policy for the same reason: a missing script must degrade to prompting, never to
# allowing.
#
# The decider lives in allow-readonly-psql.py, NOT in a heredoc here. It used to be carried as
# `decider=$(cat <<'PY' ... PY)` and handed to `python3 -c`; bash 3.2 (still /bin/bash on macOS)
# scans a command-substitution body before honouring the quoted heredoc delimiter, so the
# program's own backticks and apostrophes became shell syntax and the hook failed to parse
# entirely — which the dispatcher then reported on EVERY Bash call. See that file's header.
#
# exec, so the decider inherits this process's stdin: the hook payload arrives there, and a
# heredoc would have consumed it (leaving a guard that declines everything while looking fine).
set -uo pipefail

exec python3 "$(dirname "$0")/allow-readonly-psql.py"
