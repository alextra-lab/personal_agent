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

# --- declined: shell-level escapes ----------------------------------------------------------
assert_decline "chained destructive command" \
  "$PROD -c \"SELECT 1;\" ; rm -rf /tmp/x"
assert_decline "redirection to a file" \
  "$PROD -c \"SELECT 1;\" > /tmp/out.txt"
assert_decline "command substitution" \
  "$PROD -c \"SELECT 1;\" \$(rm -rf /tmp/x)"
assert_decline "piped into a writer" \
  "$PROD -c \"SELECT 1;\" | tee /tmp/out.txt"
assert_decline "second docker exec that is not read-only" \
  "$PROD -c \"SELECT 1;\"; docker exec cloud-sim-postgres psql -U agent -c \"DROP TABLE t;\""

# --- declined: out of scope -----------------------------------------------------------------
assert_decline "container not in the allowlist" \
  "docker exec cloud-sim-neo4j psql -U agent -c \"SELECT 1;\""
assert_decline "psql on the host, not via docker exec" \
  "psql -U agent -d personal_agent -c \"SELECT 1;\""
assert_decline "unrelated command" "git status"

echo "---"; [ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
