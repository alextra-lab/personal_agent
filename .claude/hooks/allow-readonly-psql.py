#!/usr/bin/env python3
# PreToolUse decider for allow-readonly-psql.sh (FRE-867). See that file for the rationale.
#
# Why this is a file rather than a heredoc inside the shell wrapper. It used to be carried as
# `decider=$(cat <<'PY' ... PY)` and passed to `python3 -c`. Under bash 5 that parses fine, but
# bash 3.2 -- which is what /bin/bash still is on macOS -- scans the command-substitution body
# before honouring the quoted heredoc delimiter, so every backtick, apostrophe and quote in this
# program became shell syntax. The lone backtick in the command-injection check below is
# unbalanced by construction, so on any bash that scans it the whole hook became a syntax error
# and the dispatcher reported it on EVERY Bash call, not just psql ones. Keeping the program in
# its own file removes the failure mode outright instead of balancing quotes by hand forever.
#
# The program below is unchanged from the heredoc version; only its packaging moved.

import json
import re
import shlex
import sys

# Segment separators and the operator tokens that disqualify a command outright.
SEPARATORS = {";", "&&", "||", "|"}
FORBIDDEN_TOKENS = {"&", ">", ">>", "<", "<<", ">&", "&>", "(", ")"}

# Commands that may appear alongside the query without making it non-read-only.
# Seats habitually label their output ("echo === foo ===; docker exec ... psql ...") and pipe
# through a pager, so refusing to model those would leave the common shape still prompting.
SAFE_COMMANDS = {
    "echo", "printf", "true", "cat", "head", "tail", "wc", "sort", "uniq",
    "cut", "tr", "nl", "grep", "column", "jq", "date",
}

# Containers whose psql may be reached this way. Test-stack containers are omitted on purpose:
# they already carry blanket allow-rules in settings.json and need no shape check.
ALLOWED_CONTAINERS = {"cloud-sim-postgres"}

# `docker exec` flags that cannot change what the statement does.
SAFE_DOCKER_FLAGS = {"-i", "-t", "-it", "-ti", "--interactive", "--tty"}

# psql flags are ALLOWLISTED, not denylisted. A denylist cannot be complete here: -o and -L write
# query output and a session log to any path, and -v/--set expands a variable into the statement
# AFTER this hook has vetted it (`-v x='1; DROP TABLE t' -c 'SELECT :x'`), which defeats the whole
# check. Anything unlisted falls through to a prompt, which is the safe direction.
PSQL_CONNECTION_FLAGS = {  # take a value, either attached or as the next token
    "-U", "-d", "-h", "-p", "-F", "-P", "-R",
    "--username", "--dbname", "--host", "--port",
    "--field-separator", "--pset", "--record-separator",
}
PSQL_BOOLEAN_FLAGS = {
    "-t", "-A", "-x", "-q", "-w", "-W", "-n", "-X", "-H", "-E", "-e", "-s", "-S", "-0", "-z",
    "--tuples-only", "--no-align", "--expanded", "--quiet", "--no-password", "--password",
    "--no-readline", "--no-psqlrc", "--csv", "--html", "--echo-queries", "--single-step",
    "--single-line",
}
# Single-character short forms only (excludes the "--foo" long spellings, which getopt never
# bundles) -- eligible for GNU-style bundling, e.g. `-tAc` == `-t -A -c` (FRE-867, 2026-07-30
# comment: a real stall bundled exactly these three and the parser declined it because it only
# recognized them written separately).
PSQL_BUNDLABLE_BOOLEAN_FLAGS = {f for f in PSQL_BOOLEAN_FLAGS if len(f) == 2}

STATEMENT_START = re.compile(r"^\s*(select|with|show|explain|table|values)\b", re.IGNORECASE)

# Whole-word write/side-effect keywords. Two shapes make this list, not the opening keyword, the
# thing that decides: a data-modifying CTE ("WITH x AS (DELETE ...)") reads as a SELECT at the
# front, and SELECT ... INTO creates a table while looking like an ordinary projection. `into`
# does not collide with `in` — the word boundary separates them.
WRITE_KEYWORDS = re.compile(
    r"\b(insert|into|merge|update|delete|drop|create|alter|truncate|grant|revoke|copy|vacuum|"
    r"reindex|cluster|lock|call|do|set|reset|begin|commit|rollback|savepoint|refresh|"
    r"import|prepare|execute|deallocate|listen|notify|unlisten|discard|analyze|"
    r"nextval|setval|pg_terminate_backend|pg_cancel_backend|pg_advisory_lock|"
    r"pg_logical_emit_message|pg_read_file|pg_read_binary_file|pg_ls_dir|"
    r"lo_import|lo_export|dblink)\b",
    re.IGNORECASE,
)


def decline() -> None:
    """Say nothing, so the normal permission flow decides."""
    raise SystemExit(0)


def read_command() -> str:
    try:
        return json.load(sys.stdin).get("tool_input", {}).get("command", "") or ""
    except Exception:
        decline()


def tokenize(cmd: str) -> list[str]:
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        decline()


def sql_is_read_only(sql: str) -> bool:
    if "\\" in sql:  # psql meta-commands: \copy writes, \! shells out
        return False
    body = sql.strip().rstrip(";")
    if ";" in body:  # a second statement hiding behind the first
        return False
    if not STATEMENT_START.match(body):
        return False
    return not WRITE_KEYWORDS.search(body)


def expand_bundled_flags(arg: str) -> list[str] | None:
    """Split a GNU-style bundled short-flag argument (e.g. `-tAc`) into its parts.

    Returns the expanded flag list (e.g. `["-t", "-A", "-c"]`) when `arg` is a pure run of
    known bundlable boolean flags, optionally ending in `c` for the statement flag. Returns
    `None` for anything else (an unknown character anywhere, or `c` appearing before the end --
    it only takes a value, so it can only be the last flag in a bundle). Only ever called on an
    arg that starts with a single `-` and has more than one character after it -- the exact
    shapes `-t`, `-c`, `--tuples-only`, `-cSELECT...` etc. are handled by their own branches
    before this is ever reached.
    """
    body = arg[1:]
    flags: list[str] = []
    for index, char in enumerate(body):
        if f"-{char}" in PSQL_BUNDLABLE_BOOLEAN_FLAGS:
            flags.append(f"-{char}")
            continue
        if char == "c" and index == len(body) - 1:
            flags.append("-c")
            continue
        return None
    return flags


def psql_segment_is_read_only(tokens: list[str]) -> bool:
    """True when `tokens` is a docker-exec psql invocation running one read-only statement."""
    if tokens[:2] != ["docker", "exec"]:
        return False

    rest = tokens[2:]
    while rest and rest[0].startswith("-"):
        if rest[0] not in SAFE_DOCKER_FLAGS:
            return False
        rest = rest[1:]

    if len(rest) < 2 or rest[0] not in ALLOWED_CONTAINERS or rest[1] != "psql":
        return False

    args, sql = rest[2:], None
    index = 0
    while index < len(args):
        arg = args[index]

        # The statement itself, in each of the three spellings psql accepts.
        if arg in ("-c", "--command"):
            if sql is not None or index + 1 >= len(args):
                return False  # a second statement, or -c with nothing after it
            sql, index = args[index + 1], index + 2
            continue
        if arg.startswith("--command="):
            if sql is not None:
                return False
            sql, index = arg[len("--command="):], index + 1
            continue
        if arg.startswith("-c") and len(arg) > 2:
            if sql is not None:
                return False
            sql, index = arg[2:], index + 1
            continue

        if not arg.startswith("-"):
            return False  # a bare positional is the dbname; we require it via -d

        if arg in PSQL_BOOLEAN_FLAGS:
            index += 1
            continue
        if arg in PSQL_CONNECTION_FLAGS:
            if index + 1 >= len(args):
                return False
            index += 2  # consumes its value
            continue
        if not arg.startswith("--") and arg[:2] in PSQL_CONNECTION_FLAGS and len(arg) > 2:
            index += 1  # attached short value, e.g. -Uagent or -F'|'
            continue
        if arg.startswith("--") and "=" in arg and arg.split("=", 1)[0] in PSQL_CONNECTION_FLAGS:
            index += 1
            continue

        if not arg.startswith("--") and len(arg) > 2:
            expanded = expand_bundled_flags(arg)
            if expanded is not None:
                if expanded[-1] == "-c":
                    if sql is not None or index + 1 >= len(args):
                        return False  # a second statement, or -c with nothing after it
                    sql, index = args[index + 1], index + 2
                else:
                    index += 1
                continue

        return False  # unlisted flag — fall through to a prompt

    return sql is not None and sql_is_read_only(sql)


command = read_command()
if "psql" not in command or "docker" not in command:
    decline()
if "`" in command or "$(" in command or "${" in command:
    decline()
# A newline separates commands in the shell, but shlex treats it as ordinary whitespace, so a
# second command on its own line lands INSIDE the preceding segment and only that segment's first
# token is ever checked — "psql -c 'SELECT 1;'; echo hi\nrm -rf x" would be allowed, bypassing the
# `rm` ask-rule. Rather than teach the tokenizer about line structure, refuse multi-line commands
# outright: a multi-line query then prompts, which is exactly today's behaviour, so nothing regresses.
if "\n" in command or "\r" in command:
    decline()

tokens = tokenize(command)
if any(token in FORBIDDEN_TOKENS for token in tokens):
    decline()

segments: list[list[str]] = [[]]
for token in tokens:
    if token in SEPARATORS:
        segments.append([])
    else:
        segments[-1].append(token)

saw_query = False
for segment in segments:
    if not segment:
        decline()  # empty segment means we mis-parsed; do not guess
    if segment[0] == "docker":
        if not psql_segment_is_read_only(segment):
            decline()
        saw_query = True
    elif segment[0] not in SAFE_COMMANDS:
        decline()

if not saw_query:
    decline()

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "read-only psql query, auto-allowed per FRE-867",
    }
}))
