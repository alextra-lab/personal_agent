#!/usr/bin/env bash
# Test harness for allow-master-gates.sh (FRE-1706).
# Drives the hook with mock PreToolUse payloads from different CWDs and asserts whether it
# emits an `allow` decision. Never merges or rebuilds anything — the hook only decides.
#
# The failure that matters is a FALSE ALLOW: a command that does more than one merge or one
# rebuild, or any command from a worker worktree, slipping through. Most cases below are
# therefore seeded negatives that must print nothing (so the normal prompt runs).
set -uo pipefail

HOOK="$(cd "$(dirname "$0")" && pwd)/allow-master-gates.sh"
PRIMARY="/opt/seshat"
BUILD_WT="/opt/seshat/.claude/worktrees/build"
BUILD2_WT="/opt/seshat/.claude/worktrees/build2"
ADR_WT="/opt/seshat/.claude/worktrees/adrs"
NOT_GIT="/tmp"
SHA="0123456789abcdef0123456789abcdef01234567"
MERGE="gh pr merge 1 --merge --match-head-commit $SHA"
REBUILD="ENV=cloud make rebuild SERVICE=seshat-gateway"
fails=0

decision() { # cwd, command -> "allow" or "" (declined); "no-such-cwd" if the directory is gone
  [ -d "$1" ] || { echo "no-such-cwd"; return; }
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$2" \
    | ( cd "$1" && env CLAUDE_PROJECT_DIR=/opt/seshat bash "$HOOK" ) \
    | python3 -c 'import json,sys; d=sys.stdin.read().strip(); print(json.loads(d)["hookSpecificOutput"]["permissionDecision"] if d else "")'
}

assert_allow() { # desc, command  (cwd = main tree)
  local got; got=$(decision "$PRIMARY" "$2")
  if [ "$got" = "allow" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected allow, got '${got:-decline}')"; fails=$((fails+1)); fi
}

assert_decline() { # desc, command  (cwd = main tree)
  local got; got=$(decision "$PRIMARY" "$2")
  if [ -z "$got" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected decline, got '$got')"; fails=$((fails+1)); fi
}

assert_decline_in() { # desc, cwd, command
  local got; got=$(decision "$2" "$3")
  if [ -z "$got" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected decline, got '$got')"; fails=$((fails+1)); fi
}

assert_decline_env() { # desc, "VAR=value ...", cwd, command
  local got
  [ -d "$3" ] || { echo "FAIL - $1 (cwd $3 is missing)"; fails=$((fails+1)); return; }
  got=$(python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$4" \
    | ( cd "$3" && env CLAUDE_PROJECT_DIR=/opt/seshat $2 bash "$HOOK" ) \
    | python3 -c 'import json,sys; d=sys.stdin.read().strip(); print(json.loads(d)["hookSpecificOutput"]["permissionDecision"] if d else "")')
  if [ -z "$got" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected decline, got '$got')"; fails=$((fails+1)); fi
}

# --- allowed: the exact forms master runs ---------------------------------------------------
assert_allow "merge, required flags only" "$MERGE"
assert_allow "merge with --delete-branch" "$MERGE --delete-branch"
assert_allow "merge with single-quoted --subject and --body" "$MERGE --subject 'FRE-1 title (#1)' --body 'a; b && c | d > e'"
assert_allow "merge with double-quoted --subject and --body" "$MERGE --subject \"FRE-1 title\" --body \"line one\""
assert_allow "merge with a multi-line double-quoted --body" "$MERGE --body \"line one
line two\""
assert_allow "merge with a multi-line single-quoted --body" "$MERGE --body 'line one
line two'"
assert_allow "merge with all optional flags in another order" "$MERGE --body 'b' --delete-branch --subject 's'"
assert_allow "merge with an escaped quote in a double-quoted body" "$MERGE --body \"say \\\"hi\\\"\""
assert_allow "merge with an upper-case hex SHA" "gh pr merge 12 --merge --match-head-commit $(printf %s "$SHA" | tr a-f A-F)"
assert_allow "merge with an exclamation mark in single quotes" "$MERGE --body 'done!'"
assert_allow "merge with an escaped exclamation mark in double quotes" "$MERGE --body \"done\\!\""
assert_allow "merge with quoted shell metacharacters (no dollar, backtick or bang)" "$MERGE --body \"~ * ? [x] {a,b} café\""
assert_allow "merge with a carriage return inside the quotes" "$MERGE --body \"a$(printf '\r')b\""
assert_allow "merge with a backslash-newline inside double quotes" "$MERGE --body \"a\\
b\""
assert_allow "rebuild seshat-gateway" "$REBUILD"
a=$(decision "$PRIMARY/.claude/hooks" "$MERGE")
if [ "$a" = "allow" ]; then echo "ok   - merge from a subdirectory of the main tree"; else echo "FAIL - merge from a subdirectory of the main tree (got '${a:-decline}')"; fails=$((fails+1)); fi
assert_allow "rebuild seshat-pwa" "ENV=cloud make rebuild SERVICE=seshat-pwa"

# --- declined: role (the same allowed commands, from another working directory) -------------
assert_decline_in "merge from the build worktree" "$BUILD_WT" "$MERGE"
assert_decline_in "merge from the build2 worktree" "$BUILD2_WT" "$MERGE"
assert_decline_in "merge from the adrs worktree" "$ADR_WT" "$MERGE"
assert_decline_in "rebuild from the build worktree" "$BUILD_WT" "$REBUILD"
assert_decline_in "rebuild from the build2 worktree" "$BUILD2_WT" "$REBUILD"
assert_decline_in "rebuild from the adrs worktree" "$ADR_WT" "$REBUILD"
assert_decline_in "merge from a directory that is not a git repository" "$NOT_GIT" "$MERGE"

assert_decline_env "GIT_WORK_TREE=/opt/seshat in a worktree (merge)" \
  "GIT_WORK_TREE=/opt/seshat" "$BUILD2_WT" "$MERGE"
assert_decline_env "GIT_WORK_TREE=/opt/seshat in a worktree (rebuild)" \
  "GIT_WORK_TREE=/opt/seshat" "$BUILD2_WT" "$REBUILD"
assert_decline_env "GIT_DIR and GIT_WORK_TREE set to the main tree in a worktree" \
  "GIT_DIR=/opt/seshat/.git GIT_WORK_TREE=/opt/seshat" "$BUILD2_WT" "$REBUILD"
# A worker moves its worktree `.git` file aside: git then reports /opt/seshat as the top level.
# Reproduced with a scratch directory under .claude/worktrees that has no `.git` file.
NOGIT_WT="/opt/seshat/.claude/worktrees/_fre1706_test_nogit"
mkdir -p "$NOGIT_WT"; trap 'rmdir "$NOGIT_WT" 2>/dev/null' EXIT
top=$(cd "$NOGIT_WT" && git rev-parse --show-toplevel)
if [ "$top" = "$PRIMARY" ]; then echo "ok   - setup: a worktree directory without a .git file reads as the main tree"; else echo "FAIL - setup (top level is '$top')"; fails=$((fails+1)); fi
assert_decline_in "worktree directory without a .git file, project dir is the main tree" "$NOGIT_WT" "$REBUILD"
assert_decline_env "worker project dir with a hidden .git file" "CLAUDE_PROJECT_DIR=$BUILD2_WT" "$NOGIT_WT" "$MERGE"
assert_decline_env "main-tree cwd but a worker project dir" "CLAUDE_PROJECT_DIR=$BUILD2_WT" "$PRIMARY" "$MERGE"
unset_out=$(printf '{"tool_name":"Bash","tool_input":{"command":"%s"}}' "$REBUILD" | ( cd "$PRIMARY" && env -u CLAUDE_PROJECT_DIR bash "$HOOK" ) 2>&1)
if [ -z "$unset_out" ]; then echo "ok   - CLAUDE_PROJECT_DIR unset"; else echo "FAIL - CLAUDE_PROJECT_DIR unset (got '$unset_out')"; fails=$((fails+1)); fi
assert_decline_env "CLAUDE_PROJECT_DIR empty" "CLAUDE_PROJECT_DIR=" "$PRIMARY" "$MERGE"
# A git that fails while the cwd is the main tree: an `|| pwd` fallback would read /opt/seshat.
SHIM_DIR=$(mktemp -d); printf '#!/bin/sh\nexit 128\n' > "$SHIM_DIR/git"; chmod +x "$SHIM_DIR/git"
trap 'rmdir "$NOGIT_WT" 2>/dev/null; rm -rf "$SHIM_DIR"' EXIT
assert_decline_env "git fails in the main tree (no pwd fallback)" "PATH=$SHIM_DIR:$PATH" "$PRIMARY" "$MERGE"
assert_decline_in "a subdirectory of a worktree" "$BUILD2_WT/.claude/hooks" "$MERGE"
assert_decline_in "a nested worktree path" "$BUILD2_WT/docs" "$REBUILD"

# --- declined: merge flags ------------------------------------------------------------------
assert_decline "--squash" "gh pr merge 1 --squash --match-head-commit $SHA"
assert_decline "--rebase" "gh pr merge 1 --rebase --match-head-commit $SHA"
assert_decline "--admin added" "$MERGE --admin"
assert_decline "--auto added" "$MERGE --auto"
assert_decline "--auto instead of --merge" "gh pr merge 1 --auto --match-head-commit $SHA"
assert_decline "no --match-head-commit" "gh pr merge 1 --merge"
assert_decline "no --merge" "gh pr merge 1 --match-head-commit $SHA"
assert_decline "39-character SHA" "gh pr merge 1 --merge --match-head-commit ${SHA:0:39}"
assert_decline "41-character SHA" "gh pr merge 1 --merge --match-head-commit ${SHA}a"
assert_decline "short SHA" "gh pr merge 1 --merge --match-head-commit abc1234"
assert_decline "non-hex SHA" "gh pr merge 1 --merge --match-head-commit ${SHA:0:39}g"
assert_decline "non-numeric PR number" "gh pr merge abc --merge --match-head-commit $SHA"
assert_decline "no PR number" "gh pr merge --merge --match-head-commit $SHA"
assert_decline "Unicode digit as the PR number" "gh pr merge ١ --merge --match-head-commit $SHA"
assert_decline "--repo to another repository" "$MERGE --repo other/repo"
assert_decline "--body-file reads a file" "$MERGE --body-file /tmp/x"
assert_decline "repeated --delete-branch" "$MERGE --delete-branch --delete-branch"
assert_decline "repeated --body" "$MERGE --body 'a' --body 'b'"
assert_decline "--delete-branch with a suffix" "$MERGE --delete-branchX"
assert_decline "--delete-branch=true" "$MERGE --delete-branch=true"
assert_decline "--subject with no value" "$MERGE --subject"
assert_decline "repeated --subject in two quote styles" "$MERGE --subject 'a' --subject \"b\""
assert_decline "escaped closing quote leaves the string open" "$MERGE --body \"a\\\""
assert_decline "--match-head-commit=SHA spelling" "gh pr merge 1 --merge --match-head-commit=$SHA"
assert_decline "flag order: --merge after the SHA" "gh pr merge 1 --match-head-commit $SHA --merge"
assert_decline "double space between tokens" "$MERGE  --delete-branch"
assert_decline "tab between tokens" "gh pr merge 1	--merge --match-head-commit $SHA"
assert_decline "leading space" " $MERGE"
assert_decline "trailing newline" "$MERGE
"
assert_decline "trailing space" "$MERGE "

# --- declined: rebuild forms ----------------------------------------------------------------
assert_decline "another SERVICE" "ENV=cloud make rebuild SERVICE=seshat-neo4j"
assert_decline "SERVICE with a suffix" "ENV=cloud make rebuild SERVICE=seshat-gateway-x"
assert_decline "SERVICE with a prefix" "ENV=cloud make rebuild SERVICE=xseshat-gateway"
assert_decline "make rebuild without ENV=cloud" "make rebuild SERVICE=seshat-gateway"
assert_decline "ENV=local" "ENV=local make rebuild SERVICE=seshat-gateway"
assert_decline "no SERVICE" "ENV=cloud make rebuild"
assert_decline "second SERVICE" "ENV=cloud make rebuild SERVICE=seshat-gateway SERVICE=seshat-pwa"
assert_decline "second make target" "ENV=cloud make rebuild deploy SERVICE=seshat-gateway"
assert_decline "make deploy" "ENV=cloud make deploy"
assert_decline "make build-full" "ENV=cloud make build-full"
assert_decline "extra make variable" "ENV=cloud make rebuild SERVICE=seshat-gateway FORCE=1"

# --- declined: chaining and shell syntax ----------------------------------------------------
assert_decline "merge then git push" "$MERGE && git push"
assert_decline "merge then rm" "$MERGE; rm x"
assert_decline "merge or rm" "$MERGE || rm x"
assert_decline "merge piped" "$MERGE | tee /tmp/x"
assert_decline "merge in the background" "$MERGE &"
assert_decline "merge then a background command" "$MERGE & rm x"
assert_decline "merge with a redirect" "$MERGE > /tmp/x"
assert_decline "merge with an stdin redirect" "$MERGE < /tmp/x"
assert_decline "merge then a command on the next line" "$MERGE
rm x"
assert_decline "command before the merge on the previous line" "rm x
$MERGE"
assert_decline "rebuild then git push" "$REBUILD && git push"
assert_decline "rebuild then rm" "$REBUILD; rm x"
assert_decline "rebuild piped" "$REBUILD | cat"
assert_decline "rebuild redirected" "$REBUILD > /tmp/x"
assert_decline "rebuild then a command on the next line" "$REBUILD
rm x"
assert_decline "env prefix before the rebuild" "FOO=1 $REBUILD"
assert_decline "subshell around the merge" "($MERGE)"
assert_decline "brace group around the merge" "{ $MERGE; }"
assert_decline "negated merge" "! $MERGE"
assert_decline "backslash before a flag" "$MERGE \\--admin"
assert_decline "backslash-newline continuation" "$MERGE \\
--admin"

# --- declined: expansion inside and outside quotes ------------------------------------------
assert_decline "command substitution in --body (double quotes)" "$MERGE --body \"\$(rm x)\""
assert_decline "command substitution in --body (single quotes)" "$MERGE --body '\$(rm x)'"
assert_decline "backtick in --body (double quotes)" "$MERGE --body \"\`rm x\`\""
assert_decline "backtick in --body (single quotes)" "$MERGE --body '\`rm x\`'"
assert_decline "command substitution in --subject" "$MERGE --subject \"\$(rm x)\""
assert_decline "variable expansion in --body" "$MERGE --body \"\$HOME\""
assert_decline "brace expansion of a variable in --body" "$MERGE --body \"\${HOME}\""
assert_decline "command substitution outside the quotes" "$MERGE \$(rm x)"
assert_decline "dollar in the rebuild command" "ENV=cloud make rebuild SERVICE=\$(echo seshat-pwa)"
assert_decline "backtick in the rebuild command" "ENV=cloud make rebuild SERVICE=\`echo seshat-pwa\`"
assert_decline "ANSI-C quoting" "$MERGE --body \$'a'"
assert_decline "history expansion: unescaped bang in double quotes" "$MERGE --body \"!42\""
assert_decline "trailing carriage return" "$MERGE$(printf '\r')"
nul=$(printf '{"tool_name":"Bash","tool_input":{"command":"%s --body \\"a\\u0000b\\""}}' "$MERGE" | ( cd "$PRIMARY" && env CLAUDE_PROJECT_DIR=/opt/seshat bash "$HOOK" ))
if [ -z "$nul" ]; then echo "ok   - NUL inside the quotes"; else echo "FAIL - NUL inside the quotes (got '$nul')"; fails=$((fails+1)); fi
assert_decline "unterminated single quote" "$MERGE --body 'abc"
assert_decline "unterminated double quote" "$MERGE --body \"abc"
assert_decline "quote closed early, then a command" "$MERGE --body 'a' ; rm x ; echo 'b'"
assert_decline "adjacent quoted strings" "$MERGE --body 'a''b'"
assert_decline "quoted string glued to the next flag" "$MERGE --body 'a'--delete-branch"
assert_decline "unquoted --body value" "$MERGE --body text"
assert_decline "glob glued to a full SHA" "gh pr merge 1 --merge --match-head-commit ${SHA}*"
assert_decline "glob as a separate argument" "$MERGE *"
assert_decline "tilde in an unquoted position" "$MERGE ~"

# --- declined: out of scope -----------------------------------------------------------------
assert_decline "gh pr close" "gh pr close 1"
assert_decline "gh pr view" "gh pr view 1"
assert_decline "unrelated command" "git status"
assert_decline "empty command" ""

# --- every path exits 0: an exit 2 would block every Bash call of every seat --------------------
check_rc() { # desc, cwd, command
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$3" \
    | ( cd "$2" && env CLAUDE_PROJECT_DIR=/opt/seshat bash "$HOOK" >/dev/null 2>&1 )
  local rc=$?
  if [ "$rc" -eq 0 ]; then echo "ok   - exit 0: $1"; else echo "FAIL - exit $rc: $1"; fails=$((fails+1)); fi
}
check_rc "allowed command" "$PRIMARY" "$MERGE"
check_rc "declined by the decider" "$PRIMARY" "git status"
check_rc "declined by the role check (worktree)" "$BUILD2_WT" "$MERGE"
check_rc "declined by the role check (not a repository)" "$NOT_GIT" "$MERGE"
check_rc "declined by the worktree path check" "$NOGIT_WT" "$MERGE"

# _dispatch.sh fails open and silent when the target is not executable
if [ -x "$HOOK" ]; then echo "ok   - the wrapper is executable"; else echo "FAIL - the wrapper is not executable"; fails=$((fails+1)); fi

# --- the hook never emits deny --------------------------------------------------------------
out=$(printf '{"tool_name":"Bash","tool_input":{"command":"gh pr merge 1 --admin"}}' | ( cd "$PRIMARY" && bash "$HOOK" ); echo "rc=$?")
if [ "$out" = "rc=0" ]; then echo "ok   - a declined command prints nothing and exits 0"; else echo "FAIL - declined command output: '$out'"; fails=$((fails+1)); fi

echo "---"; [ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
