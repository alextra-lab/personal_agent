"""FRE-1517 stage 2: the smoke checks' verdicts."""

from __future__ import annotations

import json

from scripts.eval.fre1517.stage2_smoke import judge


def _call(name: str, arguments: str) -> dict[str, object]:
    return {"function": {"name": name, "arguments": arguments}}


def test_a_synthesis_check_fails_on_markup_on_a_call_and_on_empty_content() -> None:
    assert judge("synth_primary", {"content": "Sóller has a concert on 11 October."})[0]
    assert not judge("synth_primary", {"content": "<tool_call>\n<function=web_search>"})[0]
    assert not judge("synth_worker", {"content": "x", "tool_calls": [_call("web_search", "{}")]})[0]
    assert not judge("synth_worker", {"content": "  "})[0]


def test_the_worker_check_needs_a_parseable_web_search_with_a_query() -> None:
    good = _call("web_search", json.dumps({"query": "Sóller events"}))
    assert judge("worker_tool_call", {"tool_calls": [good]})[0]
    assert not judge("worker_tool_call", {"content": "I will search."})[0]
    assert not judge("worker_tool_call", {"tool_calls": [_call("web_search", "{bad")]})[0]
    assert not judge("worker_tool_call", {"tool_calls": [_call("web_search", '{"query": ""}')]})[0]
