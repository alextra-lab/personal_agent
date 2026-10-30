#!/usr/bin/env python3
# PreToolUse decider for allow-master-gates.sh (FRE-1706). See that file for the rationale.
#
# The decider is a closed grammar, not a parser. The whole raw command must fullmatch one of two
# regexes. A command outside the grammar cannot match, so a second segment (`&& git push`,
# `; rm x`, a pipe, a redirect, a newline) has nowhere to hide. shlex is not used: it would
# tokenize the shapes below and then need a second pass to prove no operator slipped through.

import json
import re
import sys

# Quoted text for --subject and --body. Inside single quotes bash applies no rule at all. Inside
# double quotes bash expands `$` and the backtick, and both are rejected for the whole command
# below. Bash also expands `!` inside double quotes when history expansion is on, so an unescaped
# `!` is not matched there. The remaining content is inert. A backslash escapes one character;
# DOTALL lets the text span lines, which a PR body does.
SINGLE_QUOTED = r"'[^']*'"
DOUBLE_QUOTED = r'"(?:[^"\\!]|\\.)*"'
QUOTED = rf"(?:{SINGLE_QUOTED}|{DOUBLE_QUOTED})"

MERGE_HEAD = re.compile(
    r"gh pr merge [0-9]+ --merge --match-head-commit [0-9a-fA-F]{40}((?: .*)?)", re.DOTALL
)
# One optional flag, preceded by the single space that separates tokens.
MERGE_OPTIONAL = re.compile(rf" (?:--delete-branch|--subject {QUOTED}|--body {QUOTED})", re.DOTALL)
REBUILD = re.compile(r"ENV=cloud make rebuild SERVICE=(?:seshat-gateway|seshat-pwa)")


def decline() -> None:
    """Say nothing, so the normal permission flow decides."""
    raise SystemExit(0)


def read_command() -> str:
    try:
        return json.load(sys.stdin).get("tool_input", {}).get("command", "") or ""
    except Exception:
        decline()


def merge_options_are_valid(tail: str) -> bool:
    """True when `tail` is zero or more optional merge flags, each used at most once."""
    seen: set[str] = set()
    position = 0
    while position < len(tail):
        match = MERGE_OPTIONAL.match(tail, position)
        if match is None:
            return False
        flag = match.group(0).split(" ")[1]
        if flag in seen:
            return False
        seen.add(flag)
        position = match.end()
    return True


def command_is_allowed(command: str) -> bool:
    # bash expands both inside double quotes, so they are refused everywhere, quoted or not.
    if "$" in command or "`" in command:
        return False
    # A NUL cannot be part of a bash command string, so the input is not a command at all.
    if "\x00" in command:
        return False
    if REBUILD.fullmatch(command):
        return True
    head = MERGE_HEAD.fullmatch(command)
    return head is not None and merge_options_are_valid(head.group(1))


if not command_is_allowed(read_command()):
    decline()

print(
    json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "master merge or rebuild gate, auto-allowed per FRE-1706",
            }
        }
    )
)
