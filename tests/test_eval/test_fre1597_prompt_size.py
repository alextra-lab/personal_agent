"""FRE-1597 (ADR-0156 A5, T-D1): the prompt-size drop of AC-3 (ADR AC-F1).

No test calls a model, a gateway or a store.
"""

from __future__ import annotations

import copy

import pytest
from scripts.eval.fre1594 import transport
from scripts.eval.fre1594.reveal import REMOVED_TOOL
from scripts.eval.fre1597 import prompt_size

PROMPT = "What is the capital of France?"


def _tool(name: str, description: str = "") -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or name,
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _user(time: str) -> str:
    return (
        "<turn_context>\n## Current Date & Time\nCurrent date: 2026-10-13\n"
        f"Current time: {time}\n</turn_context>\n\n{PROMPT}"
    )


def _body(
    system: str, tools: list[dict[str, object]], time: str, call_id: str
) -> dict[str, object]:
    return {
        "model": "m",
        "max_tokens": 12288,
        "cache_prompt": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": _user(time)},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "memory_recall", "arguments": "{}"},
                    }
                ],
            },
            {"tool_call_id": call_id, "role": "tool", "name": "memory_recall", "content": "none"},
        ],
        "tools": tools,
    }


_B_SYSTEM = (
    f"You are the agent.\n- read_only: {REMOVED_TOOL}, read, read_skill\n"
    "- query-elasticsearch: Reads logs.\n- system-metrics: A snapshot.\n"
)
_A_SYSTEM = (
    "You are the agent.\n- read_only: read, read_skill\n"
    "- log-search: Finds log events. Not for latency: model-latency.\n"
    "- system-metrics: A snapshot.\n"
)


def _b() -> dict[str, object]:
    tools = [
        _tool("fetch_url"),
        _tool(REMOVED_TOOL, "x" * 3000),
        _tool("read_skill", "query-elasticsearch"),
    ]
    return _body(_B_SYSTEM, tools, "10:00:01", "call_mem_aaa111")


def _a() -> dict[str, object]:
    tools = [_tool("fetch_url"), _tool("read_skill", "log-search")]
    return _body(_A_SYSTEM, tools, "10:07:45", "call_mem_bbb222")


def test_b_minus_removes_the_one_tool_definition_only() -> None:
    b = _b()
    minus = prompt_size.b_minus(b)
    assert [t["function"]["name"] for t in minus["tools"]] == ["fetch_url", "read_skill"]  # type: ignore[index, union-attr]
    assert {k: v for k, v in minus.items() if k != "tools"} == {
        k: v for k, v in b.items() if k != "tools"
    }
    with pytest.raises(prompt_size.SizeError, match=REMOVED_TOOL):
        prompt_size.b_minus(minus)


def test_a_is_aligned_to_b_when_only_the_time_and_the_call_id_differ() -> None:
    aligned = prompt_size.align(_a(), _b())
    assert aligned["messages"][1:] == _b()["messages"][1:]  # type: ignore[index]
    assert aligned["messages"][0] == _a()["messages"][0]  # type: ignore[index]
    assert aligned["tools"] == _a()["tools"]


def test_the_alignment_refuses_another_prompt_or_another_message() -> None:
    other = _a()
    other["messages"][1]["content"] = _user("10:07:45").replace(PROMPT, "Hello")  # type: ignore[index]
    with pytest.raises(prompt_size.SizeError, match="differ"):
        prompt_size.align(other, _b())
    extra = _a()
    extra["messages"][3]["content"] = "a memory"  # type: ignore[index]
    with pytest.raises(prompt_size.SizeError, match="differ"):
        prompt_size.align(extra, _b())


def test_the_tool_count_line_may_drop_by_the_removed_tools_only() -> None:
    b, a = _b(), prompt_size.align(_a(), _b())
    b["messages"][0]["content"] += "Available tools (3 total):\n"  # type: ignore[index]
    a["messages"][0]["content"] += "Available tools (2 total):\n"  # type: ignore[index]
    assert prompt_size.body_diff(b, a)["unexpected"] == []
    a["messages"][0]["content"] = a["messages"][0]["content"].replace("(2 total)", "(1 total)")  # type: ignore[index]
    assert prompt_size.body_diff(b, a)["unexpected"] != []


def test_the_diff_allows_only_t_c2_changes() -> None:
    allowed = prompt_size.body_diff(_b(), prompt_size.align(_a(), _b()))
    assert allowed["tools_removed"] == [REMOVED_TOOL]
    assert allowed["unexpected"] == []

    a = prompt_size.align(_a(), _b())
    a["messages"][0]["content"] += "- a new rule about cats\n"  # type: ignore[index]
    a["temperature"] = 0.5
    a["tools"].append(_tool("web_search"))  # type: ignore[union-attr]
    unexpected = prompt_size.body_diff(_b(), a)["unexpected"]
    assert any("cats" in u for u in unexpected)  # type: ignore[union-attr]
    assert any("temperature" in u for u in unexpected)  # type: ignore[union-attr]
    assert any("web_search" in u for u in unexpected)  # type: ignore[union-attr]


def _reply(cache_n: int, prompt_n: int) -> transport.Reply:
    return transport.Reply(
        ok=True,
        status=200,
        error=None,
        content="",
        reasoning=None,
        tool_calls=(),
        structured=False,
        finish_reason="length",
        timings={"cache_n": cache_n, "prompt_n": prompt_n, "predicted_n": 1},
        usage={},
        secs=1.0,
        system_fingerprint="b1",
        cost_usd=0.0,
        request_sha256="sha",
    )


class _Scripted:
    def __init__(self, *replies: transport.Reply) -> None:
        self.replies = list(replies)
        self.sent: list[dict[str, object]] = []

    def send(self, body: dict[str, object]) -> transport.Reply:
        self.sent.append(copy.deepcopy(body))
        return self.replies.pop(0)


def test_each_body_is_sent_cold_with_one_output_token() -> None:
    sender = _Scripted(_reply(0, 11000), _reply(0, 10130), _reply(0, 10200))
    counts = prompt_size.count_tokens(
        sender, {"B": _b(), "B-minus": prompt_size.b_minus(_b()), "A": _a()}
    )
    assert counts == {"B": 11000, "B-minus": 10130, "A": 10200}
    assert all(b["cache_prompt"] is False and b["max_tokens"] == 1 for b in sender.sent)


def test_a_reply_that_reused_the_cache_is_refused() -> None:
    sender = _Scripted(_reply(512, 10488))
    with pytest.raises(prompt_size.SizeError, match="cache_n"):
        prompt_size.count_tokens(sender, {"B": _b()})


@pytest.mark.parametrize(
    ("a_tokens", "verdict"),
    [(10304, "pass"), (10305, "fail"), (10000, "pass")],
)
def test_the_drop_must_be_at_least_eighty_percent_of_the_definition(
    a_tokens: int, verdict: str
) -> None:
    # tokens(B) - tokens(B-minus) = 870; 80% is 696, so A must be at most 10304.
    counts = {"B": 11000, "B-minus": 10130, "A": a_tokens}
    assert prompt_size.drop_verdict(counts)["verdict"] == verdict


def test_no_drop_from_the_definition_fails() -> None:
    assert prompt_size.drop_verdict({"B": 100, "B-minus": 100, "A": 90})["verdict"] == "fail"
