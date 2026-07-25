#!/usr/bin/env bash
# Test harness for _dispatch.sh (FRE-984).
# Drives the dispatcher with a synthetic worktree-shaped CLAUDE_PROJECT_DIR and
# asserts exit codes + stderr visibility for resolve/missing/non-executable/
# unset-env cases, under both the closed and open failure policies.
set -uo pipefail

DISPATCH="$(cd "$(dirname "$0")" && pwd)/_dispatch.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/.claude/hooks"
cat > "$TMP/.claude/hooks/real-hook.sh" <<'EOF'
#!/usr/bin/env bash
echo "real-hook ran"
exit 0
EOF
chmod +x "$TMP/.claude/hooks/real-hook.sh"

cat > "$TMP/.claude/hooks/not-executable.sh" <<'EOF'
#!/usr/bin/env bash
echo "should never run"
EOF
chmod -x "$TMP/.claude/hooks/not-executable.sh"

fails=0
assert_exit() { # desc, expected_code, actual_code
  if [ "$2" = "$3" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected $2 got $3)"; fails=$((fails+1)); fi
}
assert_stderr_has() { # desc, needle, haystack
  case "$3" in
    *"$2"*) echo "ok   - $1" ;;
    *) echo "FAIL - $1 (stderr missing '$2': $3)"; fails=$((fails+1)) ;;
  esac
}

# 1. Resolves and execs a real script, from an arbitrary cwd (not the project dir)
out=$(cd /tmp && CLAUDE_PROJECT_DIR="$TMP" "$DISPATCH" real-hook.sh closed 2>&1)
code=$?
assert_exit "resolves+execs real script from arbitrary cwd" 0 "$code"
case "$out" in *"real-hook ran"*) echo "ok   - real script actually ran";; *) echo "FAIL - real script did not run: $out"; fails=$((fails+1));; esac

# 2. Missing script + closed policy → blocked (2), loud stderr
err=$(CLAUDE_PROJECT_DIR="$TMP" "$DISPATCH" missing.sh closed 2>&1 1>/dev/null); code=$?
assert_exit "missing script + closed → exit 2" 2 "$code"
assert_stderr_has "missing script + closed → loud stderr" "SAFETY HOOK DISPATCH ERROR" "$err"

# 3. Missing script + open policy → passes (0), still loud stderr
err=$(CLAUDE_PROJECT_DIR="$TMP" "$DISPATCH" missing.sh open 2>&1 1>/dev/null); code=$?
assert_exit "missing script + open → exit 0" 0 "$code"
assert_stderr_has "missing script + open → still loud stderr" "SAFETY HOOK DISPATCH ERROR" "$err"

# 4. Non-executable target + closed policy → blocked (2), loud stderr
err=$(CLAUDE_PROJECT_DIR="$TMP" "$DISPATCH" not-executable.sh closed 2>&1 1>/dev/null); code=$?
assert_exit "non-executable target + closed → exit 2" 2 "$code"
assert_stderr_has "non-executable target → loud stderr" "not found or not executable" "$err"

# 5. CLAUDE_PROJECT_DIR unset + closed policy → blocked (2), loud stderr
err=$(env -u CLAUDE_PROJECT_DIR "$DISPATCH" real-hook.sh closed 2>&1 1>/dev/null); code=$?
assert_exit "unset CLAUDE_PROJECT_DIR + closed → exit 2" 2 "$code"
assert_stderr_has "unset CLAUDE_PROJECT_DIR → loud stderr" "CLAUDE_PROJECT_DIR is unset" "$err"

# 6. CLAUDE_PROJECT_DIR unset + open policy → passes (0), still loud stderr
err=$(env -u CLAUDE_PROJECT_DIR "$DISPATCH" real-hook.sh open 2>&1 1>/dev/null); code=$?
assert_exit "unset CLAUDE_PROJECT_DIR + open → exit 0" 0 "$code"
assert_stderr_has "unset CLAUDE_PROJECT_DIR + open → still loud stderr" "CLAUDE_PROJECT_DIR is unset" "$err"

# 7. Invalid policy argument → fail closed regardless (config bug must be loud)
err=$(CLAUDE_PROJECT_DIR="$TMP" "$DISPATCH" real-hook.sh bogus 2>&1 1>/dev/null); code=$?
assert_exit "invalid policy → exit 2" 2 "$code"
assert_stderr_has "invalid policy → loud stderr" "invalid policy" "$err"

# 8. Path-traversal / empty script name rejected
err=$(CLAUDE_PROJECT_DIR="$TMP" "$DISPATCH" "../../etc/passwd" closed 2>&1 1>/dev/null); code=$?
assert_exit "path-traversal script name rejected" 2 "$code"
err=$(CLAUDE_PROJECT_DIR="$TMP" "$DISPATCH" "" closed 2>&1 1>/dev/null); code=$?
assert_exit "empty script name rejected" 2 "$code"

# 9. Exit code passthrough — the real hook's own exit code survives dispatch
cat > "$TMP/.claude/hooks/blocking-hook.sh" <<'EOF'
#!/usr/bin/env bash
exit 2
EOF
chmod +x "$TMP/.claude/hooks/blocking-hook.sh"
CLAUDE_PROJECT_DIR="$TMP" "$DISPATCH" blocking-hook.sh closed >/dev/null 2>&1; code=$?
assert_exit "target hook's own exit code passes through" 2 "$code"

echo "---"; [ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
