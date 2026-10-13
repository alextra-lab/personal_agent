"""FRE-1597 (ADR-0156 A5, T-D1): a capture of two primary calls, and the call 2 of a replay.

No test calls a model, a gateway or a store.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from scripts.eval.fre1594 import transport
from scripts.eval.fre1594.transport import wire_messages
from scripts.eval.fre1597 import calls

PROMPT = "Check the logs for errors in the last hour."


def _tool(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _first() -> dict[str, object]:
    return {
        "model": "unsloth/qwen3.8-flash-next",
        "max_tokens": 12288,
        "temperature": 1.0,
        "stream": True,
        "messages": [
            {"role": "system", "content": "You are the agent."},
            {"role": "user", "content": f"<turn_context>\nt\n</turn_context>\n\n{PROMPT}"},
        ],
        "tools": [_tool("fetch_url"), _tool("create_linear_issue"), _tool("read_skill")],
    }


_SKILL_CONTENT = '{"ok": true, "skill_name": "log-search", "body": "b", "_citation": "[S2@ab]"}'


def _second(first: dict[str, object], *, turn: int = 0) -> dict[str, object]:
    second = copy.deepcopy(first)
    call_id = f"call_t{turn}_0_stub_7_0"
    stub_call = {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_skill", "arguments": '{"name": "log-search"}'},
    }
    messages = second["messages"]
    assert isinstance(messages, list)
    messages += wire_messages(
        [
            {"role": "assistant", "content": "", "tool_calls": [stub_call]},
            {
                "tool_call_id": call_id,
                "role": "tool",
                "name": "read_skill",
                "content": _SKILL_CONTENT,
            },
        ]
    )
    second["tools"] = [
        _tool("fetch_url"),
        _tool("search_logs"),
        _tool("create_linear_issue"),
        _tool("read_skill"),
    ]
    return second


def _reply(*tool_calls: tuple[str, str], content: str = "") -> transport.Reply:
    return transport.Reply(
        ok=True,
        status=200,
        error=None,
        content=content,
        reasoning="thinking",
        tool_calls=tuple(
            {"id": f"m{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(tool_calls)
        ),
        structured=True,
        finish_reason="tool_calls",
        timings={},
        usage={},
        secs=1.0,
        system_fingerprint="b1",
        cost_usd=0.0,
        request_sha256="x",
    )


# --------------------------------------------------------------------------- primary_calls


def _write(directory: Path, n: int, body: dict[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{n:04d}.json"
    path.write_text(json.dumps({"seq": n, "body": body}))
    return path


def test_primary_calls_keeps_the_requests_with_tools_that_end_on_the_prompt(
    tmp_path: Path,
) -> None:
    first = _first()
    files = [
        _write(tmp_path, 1, first),
        _write(tmp_path, 2, {"messages": [{"role": "user", "content": PROMPT}]}),  # no tools
        _write(tmp_path, 3, _second(first)),
        _write(tmp_path, 4, {"messages": [{"role": "user", "content": "other"}], "tools": [1]}),
    ]
    assert calls.primary_calls(files, PROMPT) == [first, _second(first)]


# --------------------------------------------------------------------------- from_capture


def test_a_capture_of_one_read_skill_splits_into_two_calls() -> None:
    first = _first()
    two = calls.TwoCalls.from_capture([first, _second(first, turn=2)], "log-search")
    assert two.added_tools == ("search_logs",)
    assert two.turn_id == 2
    assert two.skill_message is not None
    assert two.skill_message["content"] == _SKILL_CONTENT


def test_a_no_skill_capture_has_one_call_only() -> None:
    two = calls.TwoCalls.from_capture([_first()], None)
    assert two.second is None and two.added_tools == ()
    with pytest.raises(calls.FidelityError, match="2 primary"):
        calls.TwoCalls.from_capture([_first(), _first()], None)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda b: b["messages"].insert(2, {"role": "user", "content": "nudge"}), "messages"),
        (lambda b: b.__setitem__("temperature", 0.5), "temperature"),
        (lambda b: b["tools"].append(_tool("measure_model_latency")), "tools"),
        (
            lambda b: b["messages"][-2]["tool_calls"].append(b["messages"][-2]["tool_calls"][0]),
            "one",
        ),
        (lambda b: b["messages"][0].__setitem__("content", "rebuilt"), "messages"),
    ],
)
def test_a_capture_that_production_did_not_build_as_planned_is_refused(
    mutate: object, match: str
) -> None:
    first = _first()
    second = _second(first)
    assert callable(mutate)
    mutate(second)
    with pytest.raises(calls.FidelityError, match=match):
        calls.TwoCalls.from_capture([first, second], "log-search")


def test_a_capture_that_loaded_another_skill_is_refused() -> None:
    first = _first()
    with pytest.raises(calls.FidelityError, match="model-latency"):
        calls.TwoCalls.from_capture([first, _second(first)], "model-latency")


# --------------------------------------------------------------------------- second_request


def test_the_second_request_carries_the_models_reply_and_the_captured_result() -> None:
    first = _first()
    second = _second(first, turn=0)
    two = calls.TwoCalls.from_capture([first, second], "log-search")

    body = two.second_request(_reply(("read_skill", '{"name": "log-search"}'), content="Loading."))

    messages = body["messages"]
    assert isinstance(messages, list)
    assert messages[:2] == first["messages"]
    assistant, tool = messages[2], messages[3]
    assert assistant["content"] == "Loading."
    assert assistant["reasoning_content"] == "thinking"
    assert assistant["tool_calls"][0]["id"] == "call_t0_0_m0"
    assert tool == {
        "tool_call_id": "call_t0_0_m0",
        "role": "tool",
        "name": "read_skill",
        "content": _SKILL_CONTENT,
    }
    assert body["tools"] == second["tools"]
    assert {k: v for k, v in body.items() if k != "messages"} == {
        k: v for k, v in second.items() if k != "messages"
    }


@pytest.mark.parametrize(
    "reply_calls",
    [
        (("read_skill", '{"name": "model-latency"}'),),
        (("read_skill", '{"name": "log-search"}'), ("bash", '{"command": "ls"}')),
        (("bash", '{"command": "ls"}'),),
        (),
    ],
)
def test_the_second_request_needs_exactly_the_scripted_read_skill(
    reply_calls: tuple[tuple[str, str], ...],
) -> None:
    first = _first()
    two = calls.TwoCalls.from_capture([first, _second(first)], "log-search")
    assert calls.skip_reason(two, _reply(*reply_calls)) is not None
    with pytest.raises(ValueError):
        two.second_request(_reply(*reply_calls))


def test_a_reply_with_only_the_scripted_read_skill_is_not_skipped() -> None:
    first = _first()
    two = calls.TwoCalls.from_capture([first, _second(first)], "log-search")
    assert calls.skip_reason(two, _reply(("read_skill", '{"name": "log-search"}'))) is None
