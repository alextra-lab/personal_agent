"""The digest's structured-output contract and truncation attribution (FRE-996).

Two things are proven here, and they are the pilot's whole point:

* the contract reaches the provider as a **forced tool**, not a ``response_format`` —
  see :mod:`personal_agent.memory.session_digest_wire` for why the distinction is
  load-bearing rather than stylistic;
* a reply cut off at the output ceiling is attributed to **truncation**, not to a schema
  violation. Conflating them is what let FRE-995 read a sizing fault as a format fault.
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import orjson
import pytest

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.memory.session_digest import (
    TERMINAL_ELIGIBLE_REASONS,
    SessionSummaryStatus,
    SummaryFailureReason,
)
from personal_agent.memory.session_digest_wire import DIGEST_TOOL_NAME
from personal_agent.second_brain import session_summary as ss

_USER_ID = uuid4()
_T0 = datetime(2026, 7, 23, 10, 0, 0, tzinfo=timezone.utc)


def _capture(n: int) -> TaskCapture:
    return TaskCapture(
        trace_id=f"cap-{n}",
        session_id="sess-1",
        timestamp=_T0 + timedelta(minutes=n),
        user_message="check the cluster",
        assistant_response="The cluster is green and all shards are assigned.",
        outcome="completed",
        user_id=_USER_ID,
        tool_results=[],
        tools_used=[],
    )


def _session() -> list[TaskCapture]:
    return [_capture(1), _capture(2)]


def _valid_output() -> str:
    return orjson.dumps(
        {
            "label": "cluster health check",
            "digest": {
                "established": [{"text": "The cluster is green.", "basis": "mixed"}],
                "decisions": [],
                "unresolved": [],
                "corrections": [],
            },
        }
    ).decode()


# ── Truncation is attributed to truncation ────────────────────────────────────


@pytest.mark.asyncio
async def test_truncated_reply_is_not_reported_as_a_schema_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction FRE-995 could not make: a sizing fault is not a format fault."""

    async def fake_call(_prompt: str, **_: Any) -> str:
        raise ss.OutputTruncated("stopped at the 2048-token ceiling")

    monkeypatch.setattr(ss, "_call_model", fake_call)

    outcome = await ss.generate_session_digest(_session(), session_id="sess-1", ended_at=_T0)

    assert outcome.status is SessionSummaryStatus.FAILED
    assert outcome.failure_reason is SummaryFailureReason.OUTPUT_TRUNCATED


def test_truncation_stays_terminal_eligible() -> None:
    """Truncation lands in SCHEMA_INVALID today, which is terminal-eligible.

    Re-labelling it must not quietly make the session retryable across sweeps forever —
    that is the FRE-987 defect in a new place.
    """
    assert SummaryFailureReason.OUTPUT_TRUNCATED in TERMINAL_ELIGIBLE_REASONS


@pytest.mark.asyncio
async def test_truncation_costs_exactly_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Superseded by FRE-993 — this pilot deferred the bound, and it is now decided.

    FRE-996 left truncation on the retry it had inherited from ``SCHEMA_INVALID``,
    saying in terms that changing that bound was a sizing decision belonging to
    FRE-993. It does: the retry re-issues a byte-identical request against the same
    output ceiling, so a reply cut off *by that ceiling* cannot be argued into fitting
    on a second try.
    """
    attempts = 0

    async def fake_call(_prompt: str, **_: Any) -> str:
        nonlocal attempts
        attempts += 1
        raise ss.OutputTruncated("ceiling")

    monkeypatch.setattr(ss, "_call_model", fake_call)
    await ss.generate_session_digest(_session(), session_id="sess-1", ended_at=_T0)

    assert attempts == 1


@pytest.mark.asyncio
async def test_the_reply_a_truncation_retry_would_have_won_is_never_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inverse of the test this replaces, and the point of the change.

    A second call *could* be sampled into a valid reply — that is true of any resample
    and is why the fail-safe is scoped to truncation alone rather than applied to the
    stochastic validation failures. What it cannot do is change the ceiling that cut the
    first reply off, so the producer stops rather than paying to find out.
    """
    replies = iter([ss.OutputTruncated("ceiling"), _valid_output()])

    async def fake_call(_prompt: str, **_: Any) -> str:
        nxt = next(replies)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(ss, "_call_model", fake_call)
    outcome = await ss.generate_session_digest(_session(), session_id="sess-1", ended_at=_T0)

    assert outcome.status is SessionSummaryStatus.FAILED
    assert outcome.failure_reason is SummaryFailureReason.OUTPUT_TRUNCATED
    assert next(replies, None) is not None, "the second reply was never asked for"


# ── Detection inside _call_model ──────────────────────────────────────────────


class _FakeClient:
    """Records the kwargs the producer dispatched, and replays one canned response."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    async def respond(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return self.response


def _install(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    import personal_agent.llm_client.factory as factory

    monkeypatch.setattr(factory, "get_llm_client_for_key", lambda *_a, **_k: client)


@pytest.mark.asyncio
async def test_contract_is_sent_as_a_forced_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient({"content": _valid_output(), "tool_calls": [], "finish_reason": "stop"})
    _install(monkeypatch, client)

    await ss._call_model("prompt", role_name="claude_sonnet", provider="anthropic", session_id="s")

    tools = client.kwargs["tools"]
    assert [t["function"]["name"] for t in tools] == [DIGEST_TOOL_NAME]
    assert client.kwargs["tool_choice"]["function"]["name"] == DIGEST_TOOL_NAME


@pytest.mark.asyncio
async def test_contract_is_omitted_when_the_setting_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reversible by configuration — the pilot must be switchable off without a deploy."""
    from personal_agent.config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "session_digest_structured_output", False)

    client = _FakeClient({"content": _valid_output(), "tool_calls": [], "finish_reason": "stop"})
    _install(monkeypatch, client)

    await ss._call_model("prompt", role_name="claude_sonnet", provider="anthropic", session_id="s")

    assert client.kwargs.get("tools") is None
    assert client.kwargs.get("tool_choice") is None


@pytest.mark.asyncio
async def test_payload_is_read_from_the_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(
        {
            "content": "",
            "tool_calls": [{"id": "1", "name": DIGEST_TOOL_NAME, "arguments": _valid_output()}],
            "finish_reason": "tool_calls",
        }
    )
    _install(monkeypatch, client)

    content = await ss._call_model(
        "prompt", role_name="claude_sonnet", provider="anthropic", session_id="s"
    )

    assert orjson.loads(content)["label"] == "cluster health check"


@pytest.mark.asyncio
async def test_text_content_is_still_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that answers in prose anyway must not be treated as an empty reply."""
    client = _FakeClient({"content": _valid_output(), "tool_calls": [], "finish_reason": "stop"})
    _install(monkeypatch, client)

    content = await ss._call_model(
        "prompt", role_name="claude_sonnet", provider="anthropic", session_id="s"
    )

    assert orjson.loads(content)["label"] == "cluster health check"


@pytest.mark.asyncio
async def test_finish_reason_length_raises_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(
        {
            "content": '{"label": "cut off mid-str',
            "tool_calls": [],
            "finish_reason": "length",
        }
    )
    _install(monkeypatch, client)

    with pytest.raises(ss.OutputTruncated):
        await ss._call_model(
            "prompt", role_name="claude_sonnet", provider="anthropic", session_id="s"
        )


@pytest.mark.asyncio
async def test_a_reply_truncated_to_nothing_is_truncation_not_emptiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generation that exhausted its budget before emitting anything is truncated.

    Scoring it as ``empty`` would understate the truncation rate and point the
    remediation at the wrong thing — the model did not decline to answer, it ran out of
    room. The pilot harness made exactly this mistake on its first pass.
    """
    client = _FakeClient(
        {
            "content": "",
            "tool_calls": [],
            "finish_reason": "length",
            "usage": {"completion_tokens": ss._MAX_OUTPUT_TOKENS},
        }
    )
    _install(monkeypatch, client)

    with pytest.raises(ss.OutputTruncated):
        await ss._call_model(
            "prompt", role_name="claude_sonnet", provider="anthropic", session_id="s"
        )


@pytest.mark.asyncio
async def test_hitting_the_ceiling_raises_even_when_the_stop_reason_lies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second, independent truncation signal.

    litellm's ``response_format`` path overwrites the provider's stop reason with
    ``"stop"``. This pilot avoids that path, but the corroborating token check means a
    library change that reintroduces it cannot silently reclassify a truncated digest as
    a clean one — which is the cheapest way this measurement could produce a false success.
    """
    client = _FakeClient(
        {
            "content": _valid_output(),
            "tool_calls": [],
            "finish_reason": "stop",
            "usage": {"completion_tokens": ss._MAX_OUTPUT_TOKENS},
        }
    )
    _install(monkeypatch, client)

    with pytest.raises(ss.OutputTruncated):
        await ss._call_model(
            "prompt", role_name="claude_sonnet", provider="anthropic", session_id="s"
        )
