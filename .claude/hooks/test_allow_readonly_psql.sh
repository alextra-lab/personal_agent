#!/usr/bin/env bash
# Test harness for allow-readonly-psql.sh.
# Drives the hook with mock PreToolUse payloads and asserts whether it emits an `allow`
# decision. Never touches a database — the hook only decides.
#
# The failure that matters is a FALSE ALLOW: a command that mutates production substrate
# slipping through as read-only. Most cases below are therefore write/escape shapes that
# must decline.
set -uo pipefail

HOOK="$(cd "$(dirname "$0")" && pwd)/allow-readonly-psql.sh"
PROD="docker exec cloud-sim-postgres psql -U agent -d personal_agent"
fails=0

decision() { # command -> "allow" or "" (declined)
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$1" \
    | bash "$HOOK" \
    | python3 -c 'import json,sys; d=sys.stdin.read().strip(); print(json.loads(d)["hookSpecificOutput"]["permissionDecision"] if d else "")'
}

assert_allow() { # desc, command
  local got; got=$(decision "$2")
  if [ "$got" = "allow" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected allow, got '${got:-decline}')"; fails=$((fails+1)); fi
}

assert_decline() { # desc, command
  local got; got=$(decision "$2")
  if [ -z "$got" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected decline, got '$got')"; fails=$((fails+1)); fi
}

# --- allowed: the shapes the seats actually stalled on -------------------------------------
assert_allow "plain SELECT" \
  "$PROD -c \"SELECT count(*) FROM api_costs;\""
assert_allow "SELECT with the seats' formatting flags" \
  "$PROD -t -A -F'|' -c \"SELECT trace_id::text, session_id::text FROM api_costs LIMIT 4;\""
assert_allow "labelled query (echo, then the query)" \
  "echo '=== uuid versions ==='; $PROD -c \"SELECT substring(session_id::text,15,1), count(*) FROM api_costs GROUP BY 1;\""
assert_allow "query piped to a pager" \
  "$PROD -c \"SELECT * FROM api_costs;\" | head -20"
assert_allow "CTE" \
  "$PROD -c \"WITH d AS (SELECT date_trunc('hour', timestamp) h FROM api_costs) SELECT h, count(*) FROM d GROUP BY 1;\""
assert_allow "attached -c form" \
  "$PROD -c\"SELECT 1;\""
assert_allow "docker exec -i" \
  "docker exec -i cloud-sim-postgres psql -U agent -d personal_agent -c \"SELECT 1;\""
# FRE-867 (2026-07-30 06:03 comment): a real 06:01 UTC stall bundled these as one
# GNU-style short-flag argument; the parser only recognized them written separately.
assert_allow "bundled boolean flags + command flag (-tAc)" \
  "$PROD -tAc \"SELECT count(*) FROM api_costs;\""
assert_allow "bundled boolean flags only, no command bundled (-tA)" \
  "$PROD -tA -c \"SELECT 1;\""

# --- declined: bundled-flag parser's own documented decline paths ---------------------------
# FRE-867 correctness review: expand_bundled_flags's docstring calls out these two decline
# shapes explicitly; pin them so a future refactor can't silently turn either into a false allow.
assert_decline "c not last in the bundle" \
  "$PROD -ct \"SELECT 1;\""
assert_decline "c not last, buried mid-bundle" \
  "$PROD -tcA \"SELECT 1;\""
assert_decline "unbundlable write-risk flag (-o) mixed into a bundle" \
  "$PROD -to /tmp/out.txt -c \"SELECT 1;\""
assert_decline "unbundlable variable-expansion flag (-v) mixed into a bundle" \
  "$PROD -tv x=1 -c \"SELECT 1;\""

# --- declined: writes and side effects ------------------------------------------------------
assert_decline "DROP"       "$PROD -c \"DROP TABLE api_costs;\""
assert_decline "UPDATE"     "$PROD -c \"UPDATE api_costs SET cost = 0;\""
assert_decline "DELETE"     "$PROD -c \"DELETE FROM api_costs;\""
assert_decline "TRUNCATE"   "$PROD -c \"TRUNCATE api_costs;\""
assert_decline "data-modifying CTE reading as a SELECT" \
  "$PROD -c \"WITH d AS (DELETE FROM api_costs RETURNING *) SELECT count(*) FROM d;\""
assert_decline "second statement after a SELECT" \
  "$PROD -c \"SELECT 1; DROP TABLE api_costs;\""
assert_decline "SET is not read-only" "$PROD -c \"SET work_mem = '1GB';\""
assert_decline "COPY writes to disk" \
  "$PROD -c \"COPY api_costs TO '/tmp/x.csv';\""
assert_decline "psql meta-command escape" "$PROD -c \"\\\\! rm -rf /tmp/x\""
assert_decline "server-side file read" \
  "$PROD -c \"SELECT pg_read_file('/etc/passwd');\""
assert_decline "-f runs a script off disk" "$PROD -f /tmp/whatever.sql"
assert_decline "no -c at all (would sit interactive)" "$PROD"
# SELECT ... INTO creates a table while reading as an ordinary projection. Caught live by the
# push security review after the first version of this hook allowed it.
assert_decline "SELECT INTO creates a table" \
  "$PROD -c \"SELECT * INTO evil FROM api_costs;\""
assert_decline "MERGE inside a CTE" \
  "$PROD -c \"WITH x AS (MERGE INTO t USING s ON true WHEN MATCHED THEN DO NOTHING RETURNING 1) SELECT 1;\""
assert_decline "sequence side effect" "$PROD -c \"SELECT nextval('s');\""
assert_decline "backend termination" "$PROD -c \"SELECT pg_terminate_backend(1);\""

# --- declined: psql flag smuggling ----------------------------------------------------------
# The flag set is an allowlist because a denylist cannot be complete: -o and -L write files, and
# -v expands into the statement AFTER this hook has vetted it.
assert_decline "-o writes query output to a file" \
  "$PROD -o /tmp/out.txt -c \"SELECT 1;\""
assert_decline "--output= writes query output to a file" \
  "$PROD --output=/tmp/out.txt -c \"SELECT 1;\""
assert_decline "-L writes a session log" \
  "$PROD -L /tmp/session.log -c \"SELECT 1;\""
assert_decline "-v expands a variable into the vetted statement" \
  "$PROD -v x=\"1; DROP TABLE api_costs\" -c \"SELECT :x;\""
assert_decline "unrecognised long flag" "$PROD --nonesuch -c \"SELECT 1;\""

# --- declined: shell-level escapes ----------------------------------------------------------
assert_decline "chained destructive command" \
  "$PROD -c \"SELECT 1;\" ; rm -rf /tmp/x"
assert_decline "redirection to a file" \
  "$PROD -c \"SELECT 1;\" > /tmp/out.txt"
assert_decline "command substitution" \
  "$PROD -c \"SELECT 1;\" \$(rm -rf /tmp/x)"
assert_decline "piped into a writer" \
  "$PROD -c \"SELECT 1;\" | tee /tmp/out.txt"
# Found by master re-attacking the hook at the gate, after two automated review rounds missed it.
# shlex treats a newline as whitespace, so the second command used to land inside the `echo`
# segment and only that segment's first token was checked. `rm` is in the project's ask-list, so
# this bypassed a real guard, not a theoretical one.
assert_decline "newline-separated second command" \
  "$PROD -c \"SELECT 1;\"; echo hi
rm -rf /tmp/x"
assert_decline "newline before the query" \
  "rm -rf /tmp/x
$PROD -c \"SELECT 1;\""
assert_decline "multi-line SQL (falls through to a prompt, as before)" \
  "$PROD -c \"SELECT 1,
2;\""
assert_decline "second docker exec that is not read-only" \
  "$PROD -c \"SELECT 1;\"; docker exec cloud-sim-postgres psql -U agent -c \"DROP TABLE t;\""

# --- declined: out of scope -----------------------------------------------------------------
assert_decline "container not in the allowlist" \
  "docker exec cloud-sim-neo4j psql -U agent -c \"SELECT 1;\""
assert_decline "psql on the host, not via docker exec" \
  "psql -U agent -d personal_agent -c \"SELECT 1;\""
assert_decline "unrelated command" "git status"

echo "---"; [ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
