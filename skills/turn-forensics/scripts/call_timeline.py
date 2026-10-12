#!/usr/bin/env python3
"""Print the call and tool timeline of one trace from gateway log text (FRE-1578).

This replaces a ``sed | grep | python3 -c "..."`` chain. Pipe ``docker logs`` into it:

    docker logs cloud-sim-seshat-gateway --since=40m 2>&1 \
      | python3 skills/turn-forensics/scripts/call_timeline.py <trace_id>

Output, one line for each event, in log order:

    HH:MM:SS  +Ns  CALL in=<input tokens> role=<role>
    HH:MM:SS  +Ns  TOOL <name> <path or command>
    HH:MM:SS  +Ns  SUBAGENT done   (or SUBAGENT failed, when the event says success=False)

``+Ns`` is the wall-clock gap since the previous printed event. A big gap before a CALL is
the call (its prefill and decode). A big step in ``in=`` is context growth.

Exit codes: 0 at least one event printed, 1 no input or no event for the trace, 2 bad usage.
Every failure is one ``error:`` line on stderr.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Iterator

_USAGE = "usage: call_timeline.py <trace_id>   (gateway log text on stdin)"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TIME = re.compile(r"(\d\d):(\d\d):(\d\d)\.\d+Z")
_EVENT = re.compile(r"model_call_completed|tool_call_started|artifact_draft_sub_agent_complete")
_TRACE_ID = re.compile(r"[0-9A-Za-z-]{6,64}")


def _seconds(line: str) -> int | None:
    found = _TIME.search(line)
    return int(found[1]) * 3600 + int(found[2]) * 60 + int(found[3]) if found else None


def _describe(line: str) -> str:
    """Return the event text for one log line."""
    if "tool_call_started" in line:
        arg = re.search(r"'path': '([^']+)'|'command': '([^']{0,60})", line)
        name = re.search(r"(?<![\w])tool_name=(\S+)", line)
        shown = (arg[1] or arg[2]) if arg else ""
        return f"TOOL {name[1] if name else '?'} {shown}".rstrip()
    if "sub_agent_complete" in line:
        # The same event is written on a timeout or an error, with success=False.
        outcome = re.search(r"(?<![\w])success=(\w+)", line)
        return "SUBAGENT failed" if outcome and outcome[1] == "False" else "SUBAGENT done"
    # A key is read whole: input_tokens must not match inside cache_creation_input_tokens,
    # and role must not match inside budget_role.
    tokens = re.search(r"(?<![\w])input_tokens=(\d+)", line)
    role = re.search(r"(?<![\w])role=(\w+)", line)
    return f"CALL in={tokens[1] if tokens else '?'} role={role[1] if role else '?'}"


def timeline(lines: Iterable[str], trace_id: str) -> Iterator[str]:
    """Yield the timeline lines for ``trace_id``.

    Args:
        lines: Gateway log lines, with or without colour codes.
        trace_id: The trace to keep.

    Yields:
        One formatted line for each model call, tool call and sub-agent end.
    """
    previous: int | None = None
    for raw in lines:
        line = _ANSI.sub("", raw)
        if trace_id not in line or not _EVENT.search(line):
            continue
        clock = _TIME.search(line)
        now = _seconds(line)
        # The clock has no date: a turn that crosses midnight UTC wraps, so take the gap modulo a day.
        gap = "?" if previous is None or now is None else str((now - previous) % 86400)
        previous = now if now is not None else previous
        stamp = f"{clock[1]}:{clock[2]}:{clock[3]}" if clock else "??:??:??"
        yield f"{stamp}  +{gap}s  {_describe(line)}"


def main(argv: list[str], stdin: Iterable[str]) -> int:
    """Read log text and print the timeline.

    Args:
        argv: The arguments after the program name.
        stdin: The input lines.

    Returns:
        The exit code.
    """
    if argv in (["-h"], ["--help"]):
        print(_USAGE)
        print("Prints the model calls and tool calls of one trace, with the gap between them.")
        return 0
    if len(argv) != 1 or not _TRACE_ID.fullmatch(argv[0]):
        print(f"error: {_USAGE}", file=sys.stderr)
        return 2
    try:
        printed = 0
        for out in timeline(stdin, argv[0]):
            print(out)
            printed += 1
    except UnicodeDecodeError:
        print("error: standard input is not text. Pipe the output of docker logs.", file=sys.stderr)
        return 1
    if printed == 0:
        print(
            f"error: no model call or tool call of trace {argv[0]} in the input. "
            "The window of --since can be too short, or the command before this one failed. "
            "Elasticsearch may hold the events: use query_telemetry with a term on trace_id.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    # sys.stdin passes undecodable bytes through (surrogateescape): decode strictly.
    try:
        _lines = sys.stdin.buffer.read().decode("utf-8").splitlines()
    except UnicodeDecodeError:
        print("error: standard input is not text. Pipe the output of docker logs.", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(main(sys.argv[1:], _lines))
