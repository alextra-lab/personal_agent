"""Tests for the frozen-prefix re-establishment on a scheduled reset (ADR-0081
§D2 Decision 5 / §D3, FRE-434).

A reset compacts cold turns into a cumulative **assistant** recap and keeps the
last K turns verbatim, producing ``[first user][assistant recap][K verbatim
turns]`` so the turn after the reset forward-extends again (the sawtooth rising
edge). The recap is an assistant message (not system) because the role-fixer drops
non-leading system messages; the kept tail starts on a user turn so the
recap→tail seam stays alternating.
"""

from __future__ import annotations

import pytest

from personal_agent.orchestrator import within_session_compression as wsc


@pytest.fixture
def _stub_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_summarize_middle(pre_passed, *, trace_id, session_id):  # type: ignore[no-untyped-def]
        return "CUMULATIVE NARRATIVE", 1

    monkeypatch.setattr(wsc, "summarize_middle", _fake_summarize_middle)
    # Pre-pass returns the middle unchanged so summarisation is exercised.
    monkeypatch.setattr(wsc, "_pre_pass_tool_outputs", lambda mid, threshold_tokens: (mid, 0))


def _history() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": "first task"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "current"},
    ]


@pytest.mark.asyncio
async def test_reset_reestablishes_frozen_prefix(_stub_summary: None) -> None:
    result = await wsc.build_frozen_reset(
        _history(),
        trace_id="t",
        session_id="s",
        min_tail_turns=2,
        min_tail_tokens=1,
    )
    msgs = result.messages
    # Head preserved: first user message is the original task, verbatim.
    assert msgs[0] == {"role": "user", "content": "first task"}
    # The recap is an ASSISTANT message carrying the cumulative narrative.
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "CUMULATIVE NARRATIVE"
    # The verbatim tail follows and starts on a user turn (alternation across seam).
    assert msgs[2]["role"] == "user"


@pytest.mark.asyncio
async def test_reset_tail_starts_on_user(_stub_summary: None) -> None:
    # A tail that would otherwise begin on an assistant turn is trimmed to the
    # first user turn so [first user]→[assistant recap]→[user…] alternates.
    result = await wsc.build_frozen_reset(
        _history(),
        trace_id="t",
        session_id="s",
        min_tail_turns=3,  # would grab a3,u3,a2… → must trim to start on user
        min_tail_tokens=1,
    )
    # First message after the recap is a user turn.
    recap_idx = next(i for i, m in enumerate(result.messages) if m["role"] == "assistant")
    assert result.messages[recap_idx + 1]["role"] == "user"


@pytest.mark.asyncio
async def test_salient_highlights_bounded(_stub_summary: None) -> None:
    result = await wsc.build_frozen_reset(
        _history(),
        trace_id="t",
        session_id="s",
        min_tail_turns=2,
        min_tail_tokens=1,
        salient_highlights_max_chars=8,
    )
    assert len(result.salient_highlights) <= 8


@pytest.mark.asyncio
async def test_reset_with_empty_middle_has_no_recap(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pre-pass yields nothing → no summariser call → no recap message.
    monkeypatch.setattr(wsc, "_pre_pass_tool_outputs", lambda mid, threshold_tokens: ([], 0))
    short = [{"role": "user", "content": "only task"}, {"role": "assistant", "content": "a"}]
    result = await wsc.build_frozen_reset(
        short, trace_id="t", session_id="s", min_tail_turns=4, min_tail_tokens=1
    )
    assert all(
        not (m["role"] == "assistant" and m.get("content") == "CUMULATIVE NARRATIVE")
        for m in result.messages
    )
    assert result.narrative == ""
