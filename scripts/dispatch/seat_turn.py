"""Classify how a dispatch seat's current conversation ended its last turn (FRE-1499).

The pane scrape (``pane_state``) tells whether a seat is idle. It cannot tell
WHY: a seat that ended its turn with questions to the owner and a seat that
answers every poke with an empty turn both render the same idle input prompt.
The transcript can tell them apart, because every turn is a JSON row.

The current transcript is the newest ``*.jsonl`` in the seat's project dir
(``context_probe.resolve_jsonl``). ``/clear`` starts a new file, so the file
stem identifies ONE conversation. The Remote Control bridge id does not: it
stays the same across ``/clear`` (FRE-1499 investigation).

Pure text analysis plus one file read, like ``pane_state``; no tmux, no network.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Literal

from scripts.dispatch import context_probe

__all__ = ["SeatTurn", "SeatTurnState", "parse_transcript", "read_seat_turn", "read_transcript"]

SeatTurnState = Literal["awaiting-owner", "empty-response", "ended", "mid-turn", "unknown"]

# A skill seed row carries the ticket as ``<command-args>FRE-123</command-args>``
# (``/build FRE-123`` / ``/adr FRE-123``, see ``launcher.seed_command``).
_COMMAND_ARGS_RE: re.Pattern[str] = re.compile(r"<command-args>\s*(FRE-[0-9]+)\s*</command-args>")

# A question mark that ends a sentence: followed by whitespace, a markdown or
# quote closer, or the end of the text. Excludes a URL query (``x?y=1``).
_QUESTION_RE: re.Pattern[str] = re.compile(r"\?(?=[\s*_`\"')\]]|$)")

# Only the ending of the final message counts. The real FRE-1328 adr turn put
# its three questions in a numbered list about 900 characters before the end,
# under a closing line with no question mark, so the last line alone is not
# enough. A question much further up is part of the work, not the hand-off.
_QUESTION_WINDOW_CHARS = 1500
_QUESTION_MAX_CHARS = 200

# What an empty turn looks like. ``(No response.)`` is the literal text the
# build2 seat returned to 42 red-CI pokes on 2026-09-12.
_EMPTY_TEXTS: frozenset[str] = frozenset({"", "(No response.)"})


@dataclasses.dataclass(frozen=True)
class SeatTurn:
    """How a seat's current conversation ended its last turn.

    Attributes:
        session_id: The transcript file stem — one conversation.
        state: ``awaiting-owner`` (the turn ended with a question, or an
            unanswered ``AskUserQuestion``), ``empty-response`` (the turn ended
            with no text), ``ended`` (the turn ended with prose and no
            question), ``mid-turn`` (the last message is a tool call, a tool
            result, or a prompt with no reply), or ``unknown`` (no messages).
        question: For ``awaiting-owner`` with text, the last question line.
        dispatched_tickets: Ticket ids named by skill seed rows in this conversation.
    """

    session_id: str
    state: SeatTurnState
    question: str | None
    dispatched_tickets: frozenset[str]


def _blocks(message: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Return a message's content blocks (a plain-string content is one text block)."""
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _text_of(message: Mapping[str, object]) -> str:
    """Join the text blocks of one message."""
    return "\n".join(
        str(block.get("text", "")) for block in _blocks(message) if block.get("type") == "text"
    )


def _classify(
    messages: Sequence[tuple[str, Mapping[str, object]]],
) -> tuple[SeatTurnState, str | None]:
    """Classify the main-chain message sequence by its trailing assistant rows.

    Args:
        messages: ``(type, message)`` pairs, main chain only, in file order.

    Returns:
        The turn state and, for a text question, the question line.
    """
    if not messages:
        return "unknown", None
    trailing: list[Mapping[str, object]] = []
    for kind, message in reversed(messages):
        if kind == "user":
            break
        trailing.append(message)
    if not trailing:
        return "mid-turn", None
    trailing.reverse()
    last = trailing[-1]
    tool_names = [str(b.get("name", "")) for b in _blocks(last) if b.get("type") == "tool_use"]
    if tool_names:
        return ("awaiting-owner" if "AskUserQuestion" in tool_names else "mid-turn"), None
    if last.get("stop_reason") != "end_turn":
        return "mid-turn", None
    text = "\n".join(_text_of(message) for message in trailing).strip()
    if text in _EMPTY_TEXTS:
        return "empty-response", None
    window = text[-_QUESTION_WINDOW_CHARS:]
    question_lines = [line.strip() for line in window.splitlines() if _QUESTION_RE.search(line)]
    if question_lines:
        return "awaiting-owner", question_lines[-1][:_QUESTION_MAX_CHARS]
    return "ended", None


def parse_transcript(session_id: str, lines: Iterable[str]) -> SeatTurn:
    """Classify a transcript's JSONL lines.

    Sidechain rows (sub-agents) and non-message rows (system, attachment) are
    skipped. An unparseable line is skipped, never fatal.

    Args:
        session_id: The transcript file stem.
        lines: The transcript's JSONL lines.

    Returns:
        The classified ``SeatTurn``.
    """
    messages: list[tuple[str, Mapping[str, object]]] = []
    tickets: set[str] = set()
    for line in lines:
        try:
            row: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("isSidechain"):
            continue
        kind = row.get("type")
        message = row.get("message")
        if kind not in ("user", "assistant") or not isinstance(message, dict):
            continue
        if kind == "user":
            tickets.update(_COMMAND_ARGS_RE.findall(_text_of(message)))
        messages.append((str(kind), message))
    state, question = _classify(messages)
    return SeatTurn(session_id, state, question, frozenset(tickets))


def read_transcript(path: str) -> SeatTurn | None:
    """Read and classify one transcript file.

    The whole file is read: a skill seed row sits at the start of a
    conversation, so a tail read would lose ``dispatched_tickets``.

    Args:
        path: The transcript ``*.jsonl`` path.

    Returns:
        The classified ``SeatTurn``, or ``None`` when the file cannot be read.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return parse_transcript(Path(path).stem, handle)
    except OSError:
        return None


def read_seat_turn(tmux_session: str) -> SeatTurn | None:
    """Read and classify a tmux seat's current transcript.

    Args:
        tmux_session: The seat's tmux session name (e.g. ``cc-2build``).

    Returns:
        The classified ``SeatTurn``, or ``None`` when no transcript resolves.
    """
    jsonl = context_probe.resolve_jsonl(tmux_session)
    return read_transcript(jsonl) if jsonl else None
