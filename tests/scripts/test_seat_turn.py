# ruff: noqa: D103
"""Unit tests for the seat-transcript turn classifier (FRE-1499).

The row shapes below are copied from real seat transcripts read during the
FRE-1499 investigation (``~/.claude/projects/<worktree>/<session>.jsonl``):

- the build2 red-CI incident: a poke answered by one ``(No response.)`` row;
- the adr FRE-1328 turn that ended with three numbered questions to the owner,
  whose LAST line carries no question mark.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.dispatch.seat_turn import parse_transcript, read_transcript


def _row(**fields: object) -> str:
    return json.dumps(fields)


def _user_text(text: str, *, sidechain: bool = False) -> str:
    return _row(
        type="user",
        isSidechain=sidechain,
        message={"role": "user", "content": text},
    )


def _tool_result() -> str:
    return _row(
        type="user",
        message={"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    )


def _assistant(
    *blocks: dict[str, object], stop: str | None = "end_turn", sidechain: bool = False
) -> str:
    return _row(
        type="assistant",
        isSidechain=sidechain,
        message={"role": "assistant", "stop_reason": stop, "content": list(blocks)},
    )


def _text(text: str) -> dict[str, object]:
    return {"type": "text", "text": text}


def _tool_use(name: str) -> dict[str, object]:
    return {"type": "tool_use", "name": name, "input": {}}


_SYSTEM_TAIL = [
    _row(type="attachment"),
    _row(type="system", subtype="stop_hook_summary"),
    _row(type="system", subtype="turn_duration"),
]

# The real adr FRE-1328 ending (2026-09-12 06:49 UTC), trimmed: the questions
# sit in a numbered list; the closing line has no "?".
_ADR_QUESTIONS = (
    "Three questions, in order of weight:\n\n"
    "1. **Is the distinction real to you** — obligation-satisfiability now, "
    "misreport-detection still parked?\n"
    "2. **Do you still want `enforce` at all?**\n"
    "3. **Is the exempt-but-checked finding worth an ADR on its own?**\n\n"
    "I have written nothing and opened nothing. Tell me where you land, and push "
    "back on the framing if it is wrong."
)


def test_turn_ending_with_numbered_questions_is_awaiting_owner() -> None:
    lines = [
        _user_text("<command-name>/adr</command-name>\n<command-args>FRE-1328</command-args>"),
        _assistant(_tool_use("Bash"), stop="tool_use"),
        _tool_result(),
        _assistant({"type": "thinking", "thinking": ""}),
        _assistant(_text(_ADR_QUESTIONS)),
        *_SYSTEM_TAIL,
    ]
    turn = parse_transcript("a9e2d22e", lines)
    assert turn.state == "awaiting-owner"
    assert turn.question is not None
    assert "exempt-but-checked" in turn.question
    assert turn.session_id == "a9e2d22e"


def test_no_response_turn_is_empty_response() -> None:
    lines = [
        _user_text("PR #1144 failed CI checks - correct them"),
        _assistant({"type": "thinking", "thinking": ""}),
        _assistant(_text("(No response.)")),
        *_SYSTEM_TAIL,
    ]
    assert parse_transcript("0b7e28a2", lines).state == "empty-response"


def test_turn_ending_with_plain_prose_is_ended() -> None:
    lines = [_user_text("go"), _assistant(_text("Done. Pushed the fix and opened PR #12."))]
    turn = parse_transcript("s", lines)
    assert turn.state == "ended"
    assert turn.question is None


def test_question_far_above_the_ending_does_not_count() -> None:
    long_tail = "Done.\n" + ("x" * 2000)
    lines = [_user_text("go"), _assistant(_text("Should I?\n" + long_tail))]
    assert parse_transcript("s", lines).state == "ended"


def test_pending_ask_user_question_is_awaiting_owner() -> None:
    lines = [_user_text("go"), _assistant(_tool_use("AskUserQuestion"), stop="tool_use")]
    assert parse_transcript("s", lines).state == "awaiting-owner"


def test_unanswered_tool_call_is_mid_turn() -> None:
    lines = [_user_text("go"), _assistant(_tool_use("Bash"), stop="tool_use")]
    assert parse_transcript("s", lines).state == "mid-turn"


def test_tool_result_without_reply_is_mid_turn() -> None:
    lines = [_user_text("go"), _assistant(_tool_use("Bash"), stop="tool_use"), _tool_result()]
    assert parse_transcript("s", lines).state == "mid-turn"


def test_prompt_without_reply_is_mid_turn() -> None:
    lines = [_assistant(_text("Done.")), _user_text("PR #1 failed CI checks - correct them")]
    assert parse_transcript("s", lines).state == "mid-turn"


def test_sidechain_rows_are_ignored() -> None:
    lines = [
        _user_text("go"),
        _assistant(_text("Which option do you want?")),
        _assistant(_tool_use("Bash"), stop="tool_use", sidechain=True),
    ]
    assert parse_transcript("s", lines).state == "awaiting-owner"


def test_no_message_rows_is_unknown() -> None:
    assert parse_transcript("s", [*_SYSTEM_TAIL, "not json"]).state == "unknown"


def test_dispatched_tickets_come_from_skill_command_args() -> None:
    lines = [
        _user_text("<command-name>/build</command-name>\n<command-args>FRE-1499</command-args>"),
        _user_text("please look at FRE-1 later"),
        _assistant(_text("On it.")),
    ]
    assert parse_transcript("s", lines).dispatched_tickets == frozenset({"FRE-1499"})


def test_command_args_in_list_content_are_read() -> None:
    row = _row(
        type="user",
        message={
            "role": "user",
            "content": [{"type": "text", "text": "<command-args>FRE-7</command-args>"}],
        },
    )
    assert parse_transcript("s", [row]).dispatched_tickets == frozenset({"FRE-7"})


def test_read_transcript_uses_file_stem_as_session_id(tmp_path: Path) -> None:
    path = tmp_path / "4de75861-207e.jsonl"
    path.write_text("\n".join([_user_text("go"), _assistant(_text("Done."))]) + "\n")
    turn = read_transcript(str(path))
    assert turn is not None
    assert turn.session_id == "4de75861-207e"
    assert turn.state == "ended"


def test_read_transcript_missing_file_is_none(tmp_path: Path) -> None:
    assert read_transcript(str(tmp_path / "absent.jsonl")) is None
