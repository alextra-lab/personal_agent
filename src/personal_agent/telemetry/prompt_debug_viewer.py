"""Print captured model-call prompts in a readable form (FRE-1614).

Run inside the gateway, where the capture volume is mounted::

    make prompt-show ARGS="--last"
    docker exec cloud-sim-seshat-gateway /app/.venv/bin/python -m <this module> --trace <id>

Each call gets a header (sequence, role, model, placement, start time, duration,
the usage the provider reported), then every message in full with its role and an
approximate token count, then the tool block with each tool's count. ``--full``
adds the complete tool JSON. Counts use cl100k (as ``estimate_tokens`` does); they do not
include the chat template's own tokens, so they read a little under the reported
``prompt_tokens``.

The capture files come from :mod:`personal_agent.telemetry.prompt_debug`.
"""

from __future__ import annotations

import argparse
import functools
import json
import pathlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import tiktoken

from personal_agent.telemetry.prompt_debug import prompt_debug_root, safe_path_component

_CAPTURE_GLOB = "[0-9]*.json"

Record = dict[str, object]


@functools.cache
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Count cl100k tokens, as ``estimate_tokens`` does, but never raise on captured text.

    ``estimate_tokens`` refuses text holding a special-token literal such as
    ``<|endoftext|>``. Captured web pages and files can hold one, so here a special
    token is counted as ordinary text.

    Args:
        text: Text to count.

    Returns:
        The token count; 0 for empty or whitespace-only text.
    """
    if not text or not text.strip():
        return 0
    return len(_encoding().encode(text, disallowed_special=()))


@dataclass(frozen=True)
class TokenEstimate:
    """Approximate prompt size of one captured call."""

    messages: int
    tools: int

    @property
    def total(self) -> int:
        """Messages plus tools."""
        return self.messages + self.tools


def _load(path: pathlib.Path) -> Record | None:
    """Read one capture file, or ``None`` when it is not a JSON object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast(Record, data) if isinstance(data, dict) else None


def _sort_key(record: Record) -> tuple[str, int]:
    seq = record.get("seq")
    return (str(record.get("started_at", "")), seq if isinstance(seq, int) else 0)


def select_records(
    root: pathlib.Path,
    *,
    trace: str | None = None,
    session: str | None = None,
    last: bool = False,
) -> list[Record]:
    """Load the capture records for one trace, one session, or the newest trace.

    Args:
        root: Capture root directory.
        trace: Trace id to show.
        session: Session id to show (all its traces).
        last: Show the trace of the most recently written file.

    Returns:
        The records, in call order (start time, then sequence number). Empty when
        nothing matches.
    """
    if not root.is_dir():
        return []
    if trace is not None:
        paths = list(root.glob(f"*/{safe_path_component(trace, 'no-trace')}/{_CAPTURE_GLOB}"))
    elif session is not None:
        paths = list(root.glob(f"{safe_path_component(session, 'no-session')}/*/{_CAPTURE_GLOB}"))
    elif last:
        every = list(root.glob(f"*/*/{_CAPTURE_GLOB}"))
        if not every:
            return []
        newest = max(every, key=lambda p: p.stat().st_mtime)
        paths = list(newest.parent.glob(_CAPTURE_GLOB))
    else:
        return []
    records = [record for record in (_load(path) for path in paths) if record is not None]
    return sorted(records, key=_sort_key)


def _message_text(message: dict[str, object]) -> str:
    """Return the text a message carries: its content plus any tool calls as JSON."""
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        ]
        text = "\n".join(parts)
    else:
        text = ""
    tool_calls = message.get("tool_calls")
    if tool_calls:
        text = f"{text}\n[tool_calls] {json.dumps(tool_calls, ensure_ascii=False)}".lstrip("\n")
    return text


def _messages(record: Record) -> list[dict[str, object]]:
    messages = record.get("messages")
    if not isinstance(messages, list):
        return []
    return [cast(dict[str, object], m) for m in messages if isinstance(m, dict)]


def _tools(record: Record) -> list[dict[str, object]]:
    tools = record.get("tools")
    if not isinstance(tools, list):
        return []
    return [cast(dict[str, object], t) for t in tools if isinstance(t, dict)]


def _tool_name(tool: dict[str, object]) -> str:
    function = tool.get("function")
    if isinstance(function, dict) and "name" in function:
        return str(function["name"])
    return str(tool.get("name", "?"))


def _tool_tokens(tool: dict[str, object]) -> int:
    return count_tokens(json.dumps(tool, ensure_ascii=False))


def estimate_record_tokens(record: Record) -> TokenEstimate:
    """Estimate the prompt size of one call, split into messages and tools.

    Args:
        record: A capture record.

    Returns:
        The cl100k estimate of the message text and of the tool definitions.
    """
    return TokenEstimate(
        messages=sum(count_tokens(_message_text(m)) for m in _messages(record)),
        tools=sum(_tool_tokens(t) for t in _tools(record)),
    )


def _usage_line(record: Record) -> str:
    usage = record.get("usage")
    if not isinstance(usage, dict) or not usage:
        return "usage: not reported"
    return "usage: " + " · ".join(f"{key} {value}" for key, value in usage.items())


def render_record(record: Record, *, full: bool) -> str:
    """Render one captured call as readable text.

    Args:
        record: A capture record.
        full: Include the complete tool JSON.

    Returns:
        The rendering, ending with a newline.
    """
    seq = record.get("seq")
    seq_text = f"{seq:03d}" if isinstance(seq, int) else "???"
    lines = [
        "=" * 100,
        f"#{seq_text} {record.get('role')} · {record.get('model')} · {record.get('placement')}"
        f" · {record.get('started_at')} · {record.get('duration_ms')} ms",
        f"trace {record.get('trace_id')} · session {record.get('session_id')}",
        _usage_line(record),
        f"params: {json.dumps(record.get('params', {}), ensure_ascii=False)}",
    ]
    for index, message in enumerate(_messages(record), start=1):
        text = _message_text(message)
        lines.append(f"--- [{index}] {message.get('role')} · ~{count_tokens(text)} tok")
        lines.append(text)
    tools = _tools(record)
    estimate = estimate_record_tokens(record)
    lines.append(f"--- tools ({len(tools)}) · ~{estimate.tools} tok")
    for tool in tools:
        lines.append(f"  {_tool_name(tool):<40} ~{_tool_tokens(tool)} tok")
    if full and tools:
        lines.append(json.dumps(tools, ensure_ascii=False, indent=1))
    usage = record.get("usage")
    actual = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    footer = (
        f"estimate: messages ~{estimate.messages} + tools ~{estimate.tools} = ~{estimate.total} tok"
    )
    if isinstance(actual, int) and actual > 0:
        footer += f" · reported prompt_tokens {actual} ({(estimate.total - actual) / actual:+.1%})"
    lines.append(footer)
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        0 when at least one call was shown, 1 when nothing matched.
    """
    parser = argparse.ArgumentParser(
        prog="prompt_debug_viewer", description="Show captured model-call prompts (FRE-1614)."
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--trace", help="trace id to show")
    selector.add_argument("--session", help="session id to show (every trace)")
    selector.add_argument("--last", action="store_true", help="the most recent trace")
    parser.add_argument("--full", action="store_true", help="include the complete tool JSON")
    parser.add_argument("--root", type=pathlib.Path, default=None, help="capture root")
    args = parser.parse_args(argv)

    root: pathlib.Path = args.root if args.root is not None else prompt_debug_root()
    records = select_records(root, trace=args.trace, session=args.session, last=args.last)
    if not records:
        sys.stderr.write(
            f"no capture found under {root} — is AGENT_PROMPT_DEBUG_CAPTURE_ENABLED on?\n"
        )
        return 1
    for record in records:
        sys.stdout.write(render_record(record, full=args.full))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
