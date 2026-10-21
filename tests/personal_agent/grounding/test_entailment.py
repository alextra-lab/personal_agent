"""The D3(d) entailment judge (ADR-0138, FRE-1286) — its pure parts.

Everything here runs without a model: excerpt selection, prompt construction, payload
parsing, and the judge's behaviour when the call fails. What the judge *decides* is a
property of a model and is measured by ``scripts/eval/fre1286_entailment`` instead — the
same split FRE-1281 uses for span extraction.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from personal_agent.grounding.entailment import (
    ENTAILMENT_TOOL_NAME,
    MAX_OUTPUT_TOKENS,
    EntailmentVerdict,
    ModelEntailmentJudge,
    build_prompt,
    entailment_tool,
    entailment_tool_choice,
    parse_judgement,
    select_excerpt,
)
from personal_agent.telemetry.trace import SystemTraceContext


class _StubClient:
    """A client returning one canned tool call, recording what it was asked."""

    def __init__(self, arguments: str | None, *, raises: Exception | None = None) -> None:
        self.arguments = arguments
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        if self.arguments is None:
            return {"tool_calls": []}
        return {"tool_calls": [{"name": ENTAILMENT_TOOL_NAME, "arguments": self.arguments}]}


# ── Excerpt selection (D-3) ─────────────────────────────────────────────────────────


def test_short_source_is_passed_through_whole() -> None:
    """Nothing to select from: the judge should see the source as it is."""
    source = "Testing found this fish is high in mercury."
    assert select_excerpt("this fish is high in mercury", source, max_chars=6000) == source


def test_excerpt_selects_the_window_carrying_the_claim_tokens() -> None:
    """The passage that made containment pass is the passage the judge must read.

    A long page with the supporting sentence at the very end would otherwise be judged on
    its first 6,000 characters, which say nothing about the claim — a false rejection
    manufactured by our own truncation rather than by the source.
    """
    filler = "Unrelated navigation boilerplate about shipping and returns. " * 200
    supporting = "Laboratory testing found this fish is high in mercury."
    source = filler + supporting

    excerpt = select_excerpt("this fish is high in mercury", source, max_chars=400)

    assert supporting in excerpt
    assert len(excerpt) <= 400


def test_excerpt_selection_is_deterministic() -> None:
    """Two calls on the same input return the same window — no model, no randomness."""
    source = "alpha beta gamma " * 500 + "the reactor is safe to restart"
    first = select_excerpt("the reactor is safe to restart", source, max_chars=300)
    second = select_excerpt("the reactor is safe to restart", source, max_chars=300)
    assert first == second


def test_excerpt_ties_break_to_the_earliest_window() -> None:
    """A source stating the claim twice yields the first occurrence, not an arbitrary one."""
    sentence = "the reactor is safe to restart. "
    source = sentence + "filler " * 200 + sentence
    excerpt = select_excerpt("the reactor is safe to restart", source, max_chars=200)
    assert excerpt.startswith("the reactor is safe to restart")


# ── Prompt construction ─────────────────────────────────────────────────────────────


def test_prompt_fences_claim_and_passage_with_the_nonce() -> None:
    """Both untrusted inputs are delimited by an unguessable token.

    The passage is a fetched web page. With a fixed delimiter it could close its own
    region and open one of its own, steering the judge over text it was never given.
    """
    prompt = build_prompt("a claim", "a passage", nonce="deadbeef")

    assert "<<<CLAIM deadbeef>>>" in prompt
    assert "<<<END CLAIM deadbeef>>>" in prompt
    assert "<<<PASSAGE deadbeef>>>" in prompt
    assert "<<<END PASSAGE deadbeef>>>" in prompt


def test_tool_definition_forces_a_verdict_from_the_closed_set() -> None:
    """The judge answers through an enum, so a prose escape is not available to it."""
    tool = entailment_tool()
    verdict = tool["function"]["parameters"]["properties"]["verdict"]

    assert set(verdict["enum"]) == {v.value for v in EntailmentVerdict}
    assert entailment_tool_choice()["function"]["name"] == ENTAILMENT_TOOL_NAME


# ── Payload parsing ─────────────────────────────────────────────────────────────────


def test_parse_reads_verdict_and_reason() -> None:
    """The happy path."""
    judgement = parse_judgement('{"verdict": "contradicted", "reason": "states the negation"}')
    assert judgement.verdict is EntailmentVerdict.CONTRADICTED
    assert judgement.reason == "states the negation"


def test_parse_of_an_unknown_verdict_is_undecided_not_supported() -> None:
    """An unreadable answer must never become a pass.

    ``supported`` is the only verdict that delivers a claim, so every parse failure has to
    land somewhere else — otherwise a malformed reply is a way through the gate.
    """
    for payload in ('{"verdict": "probably"}', "not json at all", "[]", '{"reason": "x"}'):
        assert parse_judgement(payload).verdict is EntailmentVerdict.UNDECIDED


# ── The model judge's failure behaviour ─────────────────────────────────────────────


def test_judge_returns_undecided_when_the_call_raises() -> None:
    """A provider error is undecided, never supported, and never an exception upward.

    The caller runs on the turn path; a raised error there would cost the user their
    answer over our own outage.
    """
    judge = ModelEntailmentJudge(
        _StubClient(None, raises=RuntimeError("provider down")), timeout_s=1.0
    )
    judgement = asyncio.run(judge.judge("a claim", "a passage"))

    assert judgement.verdict is EntailmentVerdict.UNDECIDED
    assert "RuntimeError" in judgement.reason


def test_judge_returns_undecided_when_no_tool_call_comes_back() -> None:
    """A reply that ignored the forced tool decides nothing."""
    judge = ModelEntailmentJudge(_StubClient(None), timeout_s=1.0)
    assert asyncio.run(judge.judge("a claim", "a passage")).verdict is EntailmentVerdict.UNDECIDED


def test_judge_call_carries_the_timeout_and_the_output_ceiling() -> None:
    """AC-5's structural half.

    A judge call with no timeout can sit on the critical path indefinitely, and
    ``asyncio.gather`` is only as fast as its slowest member — so the bound on added
    latency is this argument, not the measurement taken afterwards. The token ceiling is
    declared for the reason the extractor declares one: the cost gate reserves against it.
    """
    client = _StubClient('{"verdict": "supported"}')
    judge = ModelEntailmentJudge(client, timeout_s=2.5)

    asyncio.run(judge.judge("a claim", "a passage"))

    assert client.calls[0]["timeout_s"] == 2.5
    assert client.calls[0]["max_tokens"] == MAX_OUTPUT_TOKENS
    assert client.calls[0]["tool_choice"] == entailment_tool_choice()


def test_judge_sends_only_the_selected_excerpt() -> None:
    """A 200KB page is not sent to the judge; the window that matters is."""
    client = _StubClient('{"verdict": "supported"}')
    judge = ModelEntailmentJudge(client, timeout_s=1.0, max_excerpt_chars=200)

    asyncio.run(judge.judge("the reactor is safe", "padding " * 5000 + "the reactor is safe"))

    body = client.calls[0]["messages"][0]["content"]
    assert "the reactor is safe" in body
    assert len(body) < 1000


# ── a deployment that cannot force a tool (FRE-1647) ────────────────────────────


_PINNED_TRACE = SystemTraceContext.new("entailment")
"""One trace for every request a FRE-1647 test compares."""


class _UnforcedStub(_StubClient):
    """A client whose deployment cannot force a tool (litellm's map flags Sonnet 5.5)."""

    supports_forced_tool_use = False


class _ForcedStub(_StubClient):
    """A client whose deployment says, explicitly, that it can force a tool."""

    supports_forced_tool_use = True


def _request_for(client: _StubClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The kwargs one judgement dispatches, with the per-call nonce and trace pinned."""
    import personal_agent.grounding.entailment as entailment

    monkeypatch.setattr(entailment, "new_delimiter_nonce", lambda: "0000aaaa")
    asyncio.run(
        ModelEntailmentJudge(client, timeout_s=1.0).judge(
            "a claim", "a passage", trace_ctx=_PINNED_TRACE
        )
    )
    (call,) = client.calls
    return call


def test_an_unforceable_deployment_gets_auto_with_the_single_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1: Sonnet 5.5 refuses a forced choice, so the judge asks for ``auto``."""
    call = _request_for(_UnforcedStub('{"verdict": "supported"}'), monkeypatch)
    assert call["tool_choice"] == "auto"
    assert call["tools"] == [entailment_tool()]


def test_a_forceable_deployment_keeps_todays_exact_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1: *fails if* any forced-capable request changes.

    The control is a client that does not state the capability at all, which is every
    client before FRE-1647; the whole request is compared, not one key.
    """
    control = _request_for(_StubClient('{"verdict": "supported"}'), monkeypatch)
    forced = _request_for(_ForcedStub('{"verdict": "supported"}'), monkeypatch)
    assert forced == control
    assert forced["tool_choice"] == {
        "type": "function",
        "function": {"name": ENTAILMENT_TOOL_NAME},
    }
    assert forced["tools"] == [entailment_tool()]


def test_a_reply_with_no_tool_call_under_auto_is_undecided() -> None:
    """AC-2: with ``auto`` the judge may answer in prose; that reply decides nothing."""

    class _ProseClient(_UnforcedStub):
        async def respond(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"content": "The passage supports the claim.", "tool_calls": []}

    client = _ProseClient(None)
    judgement = asyncio.run(
        ModelEntailmentJudge(client, timeout_s=1.0).judge("a claim", "a passage")
    )
    assert client.calls[0]["tool_choice"] == "auto"
    assert judgement.verdict is EntailmentVerdict.UNDECIDED
