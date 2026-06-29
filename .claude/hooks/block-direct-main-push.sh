#!/usr/bin/env bash
# PreToolUse hook: block a direct push to origin main from a build/worktree session (FRE-680).
#   - build / build2 / adr worktrees: block any git push whose destination is main,
#     and fail closed on a bare/unresolvable push (you must push your feature branch
#     explicitly; master merges via PR).
#   - master (primary tree): allow — this is the docs/MASTER_PLAN direct-to-main path.
# Role is determined by the worktree root of the hook's CWD; push-target analysis is
# done in embedded python3 (argv tokenization, not substring matching).
# Exit 2 = block and surface the message (matches repo hook contract).
set -uo pipefail

input=$(cat)

# Classify the command: NONPUSH | TARGETS_MAIN | FEATURE | BARE
verdict=$(printf '%s' "$input" | python3 -c '
import sys, json, shlex

try:
    cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "") or ""
except Exception:
    print("NONPUSH"); sys.exit()

try:
    argv = shlex.split(cmd)
except ValueError:
    argv = cmd.split()

push_idx = None
for i in range(len(argv) - 1):
    if argv[i] == "git" and argv[i + 1] == "push":
        push_idx = i + 1
        break
if push_idx is None:
    print("NONPUSH"); sys.exit()

separators = {"&&", "||", ";", "|"}
rest = []
for tok in argv[push_idx + 1:]:
    if tok in separators:
        break
    rest.append(tok)

positional = [t for t in rest if not t.startswith("-")]

def hits_main(spec):
    dst = spec.split(":")[-1] if ":" in spec else spec
    if dst.startswith("refs/"):
        dst = dst.rsplit("/", 1)[-1]
    return dst == "main"

refspecs = positional[1:] if len(positional) >= 2 else []
if any(hits_main(s) for s in refspecs):
    print("TARGETS_MAIN")
elif refspecs:
    print("FEATURE")
else:
    print("BARE")
' 2>/dev/null)

[ "$verdict" = "NONPUSH" ] && exit 0

root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

case "$root" in
    */.claude/worktrees/build|*/.claude/worktrees/build2|*/.claude/worktrees/adrs)
        case "$verdict" in
            TARGETS_MAIN|BARE)
                printf 'BLOCKED: direct push to main is forbidden from the build/worktree session (role boundary). Open a PR; master merges. (FRE-680 guard)\n'
                exit 2
                ;;
        esac
        ;;
esac

# primary tree (master's domain) or a feature-branch push from a worktree: allow.
exit 0
