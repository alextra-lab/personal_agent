"""FRE-1600 AC-1: the arms of the history replay are the histories they claim to be.

No test calls a model.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from scripts.eval.fre1600 import history
from scripts.eval.fre1600.history import (
    ARMS,
    first_messages,
    load_session,
    reply_message,
    session_calls,
    tool_messages,
)

from personal_agent.orchestrator.untrusted_channel import MEMORY_RECALL_TOOL


@pytest.fixture(scope="module")
def turns() -> list[history.Turn]:
    """The scripted session."""
    return load_session()


def test_session_has_six_follow_up_turns_and_tool_turns_alternate(
    turns: list[history.Turn],
) -> None:
    """AC-1 needs 6 turns or more. Turns 1, 3 and 5 use a tool, so both kinds of follow-up are measured."""
    assert len(turns) == 7
    assert [t.index for t in turns if t.tool is not None] == [1, 3, 5]


def test_first_call_is_the_production_shape(turns: list[history.Turn]) -> None:
    """A first call adds the <turn_context> user message and the memory_recall exchange."""
    user, call, result = first_messages(turns[0])
    assert user["role"] == "user"
    assert str(user["content"]).startswith("<turn_context>\n")
    assert str(user["content"]).endswith(turns[0].user)
    assert call["tool_calls"][0]["function"]["name"] == MEMORY_RECALL_TOOL  # type: ignore[index]
    assert result["role"] == "tool"
    assert result["content"] == turns[0].memory


def test_today_stores_raw_user_text_and_reply_only(turns: list[history.Turn]) -> None:
    """The today arm keeps what service/app.py stores: the raw user text and the final reply."""
    calls = session_calls("today", turns)
    turn2_first = next(m for t, c, m in calls if t == 2 and c == 1)
    assert turn2_first[:2] == [
        {"role": "user", "content": turns[0].user},
        reply_message(turns[0]),
    ]


@pytest.mark.parametrize("arm", ["control", "preserve"])
def test_control_is_previous_last_request_plus_reply_plus_new(
    turns: list[history.Turn], arm: str
) -> None:
    """The control and preserve arms send the previous last request, the reply, then the new content."""
    calls = session_calls(arm, turns)
    for turn in turns[1:]:
        prev_last = [m for t, _, m in calls if t == turn.index - 1][-1]
        first = next(m for t, c, m in calls if t == turn.index and c == 1)
        assert first == [*prev_last, reply_message(turns[turn.index - 2]), *first_messages(turn)]


def test_no_tools_drops_only_the_tool_exchanges(turns: list[history.Turn]) -> None:
    """no_tools keeps the turn as sent without its web_search exchange."""
    calls = session_calls("no_tools", turns)
    turn2_first = next(m for t, c, m in calls if t == 2 and c == 1)
    assert turn2_first == [
        *first_messages(turns[0]),
        reply_message(turns[0]),
        *first_messages(turns[1]),
    ]
    assert all(m.get("name") != "web_search" for m in turn2_first)


def test_tool_turn_second_call_extends_the_first(turns: list[history.Turn]) -> None:
    """A tool turn's second call extends its first call with the web_search exchange."""
    for arm in ARMS:
        calls = session_calls(arm, turns)
        first = next(m for t, c, m in calls if t == 3 and c == 1)
        second = next(m for t, c, m in calls if t == 3 and c == 2)
        assert second == [*first, *tool_messages(turns[2])]


def test_tool_result_carries_its_citation_marker(turns: list[history.Turn]) -> None:
    """The tool exchange carries the stored reasoning and the citation marker."""
    call, result = tool_messages(turns[0])
    assert call["reasoning_content"] == turns[0].tool.reasoning  # type: ignore[union-attr]
    body = json.loads(str(result["content"]))
    assert body["_citation"] == f"[{turns[0].tool.marker}]"  # type: ignore[union-attr]


def test_every_arm_sends_the_same_calls(turns: list[history.Turn]) -> None:
    """Every arm sends the same 10 calls, so the arms differ only in the history."""
    shapes = {arm: [(t, c) for t, c, _ in session_calls(arm, turns)] for arm in ARMS}
    assert len({tuple(s) for s in shapes.values()}) == 1
    assert len(shapes["today"]) == 10


def test_report_derives_boundary_from_control(tmp_path: Path) -> None:
    """The boundary is control's prompt_n minus the new tokens, and the bound is their sum."""
    (tmp_path / "counts.json").write_text(
        json.dumps([{"turn": 2, "user_fenced": 100, "memory": 200}])
    )
    rows = []
    for arm, prompt_n in {"today": 3000, "control": 900, "no_tools": 500, "preserve": 320}.items():
        rows.append(
            {"arm": arm, "draw": 0, "turn": 2, "call": 1, "timings": {"prompt_n": prompt_n}}
        )
    (tmp_path / "rows.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    args = argparse.Namespace(run_dir=tmp_path)
    out = history.report(args)
    entry = out["turns"][0]  # type: ignore[index]
    assert entry["new_tokens"] == 300
    assert entry["boundary"] == 600
    assert entry["bound"] == 900
