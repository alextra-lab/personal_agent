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


class TestSanitiserFixedPoint:
    """FRE-942 probe — ADR-0081 §D2's persistence invariant.

    ``build_frozen_reset``'s output is assigned straight to ``ctx.messages`` and later
    persisted into ``session.messages``, but it is never run through
    ``sanitise_messages`` first. ADR-0081 (§D2, "Implementation invariant — persist the
    EXACT wire bytes") requires the persisted form to be either the post-sanitiser wire
    form *or* provably a sanitiser no-op; otherwise the next turn's dispatch mutates the
    frozen region and local KV reuse silently drops to zero.

    These are a **probe**, not a presumed fix: they establish which half of that
    disjunction actually holds today. FRE-942 restructured ``_extract_tail`` (deleting
    the forward tool-pair repair, which only ever handled orphaned *results*), so the
    reverse case — an assistant issuing a call that produced no result — is the one
    worth pinning.
    """

    @pytest.mark.asyncio
    async def test_wellformed_pairs_are_a_sanitiser_no_op(self, _stub_summary: None) -> None:
        """Matched assistant/tool pairs must survive the wire sanitiser untouched."""
        from personal_agent.llm_client.history_sanitiser import sanitise_messages

        history = [
            {"role": "user", "content": "first task"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "run it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "tc-1", "function": {"name": "f", "arguments": "{}"}}],
            },
            {"role": "tool", "content": "result", "tool_call_id": "tc-1"},
            {"role": "user", "content": "current"},
        ]
        result = await wsc.build_frozen_reset(
            history, trace_id="t", session_id="s", min_tail_turns=2, min_tail_tokens=1
        )

        sanitised, report = sanitise_messages(list(result.messages))

        assert not report.was_dirty
        assert sanitised == result.messages

    def _reverse_orphan_history(self) -> list[dict[str, Any]]:
        """A history whose most recent turn is a reverse orphan.

        Reachable in production: the executor records every returned call on the
        assistant message, but skips *dispatching* one that arrives without a tool
        name (``executor.py`` gate), so no matching ``role="tool"`` reply is ever
        appended for ``tc-missing``.
        """
        return [
            {"role": "user", "content": "first task"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "run both"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc-ok", "function": {"name": "f", "arguments": "{}"}},
                    {"id": "tc-missing", "function": {"name": "", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "content": "result", "tool_call_id": "tc-ok"},
        ]

    @pytest.mark.asyncio
    async def test_reverse_orphan_in_the_middle_is_summarised_away(
        self, _stub_summary: None
    ) -> None:
        """Under the production tail shape the defect does not manifest.

        With the default ``min_tail_turns`` the walk stops before the user turn that
        opens the reverse-orphan pair, so the whole pair falls to the middle band and
        is compacted into the recap — it never reaches the verbatim tail, and the
        persisted output is a clean sanitiser fixed point. This is why the gap is
        latent, not live (and the frozen-reset action itself does not fire in
        production — ADR-0092 open item #7).
        """
        from personal_agent.llm_client.history_sanitiser import sanitise_messages

        result = await wsc.build_frozen_reset(
            self._reverse_orphan_history(),
            trace_id="t",
            session_id="s",
            min_tail_turns=2,
            min_tail_tokens=1,
        )
        # The reverse-orphan assistant did not survive into the verbatim tail.
        assert not any(
            m.get("role") == "assistant" and len(m.get("tool_calls") or []) == 2
            for m in result.messages
        )
        sanitised, report = sanitise_messages(list(result.messages))
        assert not report.was_dirty
        assert sanitised == result.messages

    @pytest.mark.xfail(
        strict=True,
        reason="FRE-954: build_frozen_reset does not sanitise-then-persist, so a "
        "reverse orphan resident in the verbatim tail violates ADR-0081 §D2 "
        "byte-identity. Pre-existing (the deleted repair only handled forward "
        "orphans); latent behind the never-firing reset action (ADR-0092 #7). "
        "Deliberately not fixed here — sanitise-then-persist changes behaviour on a "
        "path with no traffic to validate against. Flip to a passing fixed-point "
        "assertion when FRE-954 lands.",
    )
    @pytest.mark.asyncio
    async def test_reverse_orphan_in_tail_is_not_yet_a_fixed_point(
        self, _stub_summary: None
    ) -> None:
        """Documents the known defect: a tail-resident reverse orphan is rewritten.

        When the walk reaches the user turn that opens the pair (larger tail), the
        reverse-orphan assistant is preserved verbatim in the tail — and the wire
        sanitiser then strips ``tc-missing`` from its ``tool_calls`` on the next
        dispatch, so the persisted bytes differ from the wire bytes.
        """
        from personal_agent.llm_client.history_sanitiser import sanitise_messages

        result = await wsc.build_frozen_reset(
            self._reverse_orphan_history(),
            trace_id="t",
            session_id="s",
            min_tail_turns=3,
            min_tail_tokens=1,
        )
        # Precondition: the orphan really is in the persisted tail (else this test
        # would pass vacuously and the xfail would be meaningless).
        assert any(
            m.get("role") == "assistant" and len(m.get("tool_calls") or []) == 2
            for m in result.messages
        )
        sanitised, report = sanitise_messages(list(result.messages))
        # The xfail expectation: today this assertion FAILS (report.was_dirty is True).
        assert not report.was_dirty
        assert sanitised == result.messages
