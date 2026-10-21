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
from unittest.mock import AsyncMock
from uuid import uuid4

import orjson
import pytest

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.llm_client.types import ModelRole
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
    # FRE-1037: the cloud call must report its true role, not primary.
    assert client.kwargs["role"] is ModelRole.SESSION_SUMMARY


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


@pytest.mark.asyncio
async def test_local_path_passes_session_summary_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRE-1037: local path (provider=None) labels its call role=SESSION_SUMMARY, not sub_agent."""
    fake_client = _FakeClient(
        {"content": _valid_output(), "tool_calls": [], "finish_reason": "stop"}
    )
    fake_client.respond = AsyncMock(wraps=fake_client.respond)
    # ADR-0141 D1: both branches acquire through the same factory door now.
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client_for_key",
        lambda model_key, *, budget_role: fake_client,
    )

    await ss._call_model("prompt", role_name="qwen3.6-35b-thinking", provider=None, session_id="s")

    assert fake_client.kwargs["role"] is ModelRole.SESSION_SUMMARY


# ── a deployment that cannot force a tool (FRE-1647) ──────────────────────────


class _UnforcedClient(_FakeClient):
    """A client whose deployment cannot force a tool (litellm's map flags Sonnet 5.5)."""

    supports_forced_tool_use = False


class _ForcedClient(_FakeClient):
    """A client whose deployment says, explicitly, that it can force a tool."""

    supports_forced_tool_use = True


async def _request_for(client: _FakeClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The kwargs one cloud generation dispatches, minus the per-call trace context."""
    _install(monkeypatch, client)
    await ss._call_model("prompt", role_name="claude_sonnet", provider="anthropic", session_id="s")
    return {k: v for k, v in client.kwargs.items() if k != "trace_ctx"}


def _reply() -> dict[str, Any]:
    return {"content": _valid_output(), "tool_calls": [], "finish_reason": "stop"}


@pytest.mark.asyncio
async def test_an_unforceable_deployment_gets_auto_with_the_single_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1: Sonnet 5.5 refuses a forced choice, so the producer asks for ``auto``."""
    from personal_agent.memory.session_digest_wire import digest_tool

    request = await _request_for(_UnforcedClient(_reply()), monkeypatch)
    assert request["tool_choice"] == "auto"
    assert request["tools"] == [digest_tool()]


@pytest.mark.asyncio
async def test_a_forceable_deployment_keeps_todays_exact_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1: *fails if* any forced-capable request changes.

    The control is a client that does not state the capability at all, which is every
    client before FRE-1647. The whole request is compared except the trace context,
    which is minted per call and never reaches the provider.
    """
    control = await _request_for(_FakeClient(_reply()), monkeypatch)
    forced = await _request_for(_ForcedClient(_reply()), monkeypatch)
    assert forced == control
    assert forced["tool_choice"] == {"type": "function", "function": {"name": DIGEST_TOOL_NAME}}


@pytest.mark.asyncio
async def test_the_contract_off_sends_no_choice_on_an_unforceable_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch still wins: no contract means no tools and no choice, whatever the model."""
    from personal_agent.config.settings import get_settings

    monkeypatch.setattr(get_settings(), "session_digest_structured_output", False)
    request = await _request_for(_UnforcedClient(_reply()), monkeypatch)
    assert request.get("tools") is None
    assert request.get("tool_choice") is None


@pytest.mark.asyncio
async def test_a_prose_reply_under_auto_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2: with ``auto`` the model may answer in prose; that reply stores nothing.

    The existing path applies: the text fallback hands the prose to the parser, which
    rejects it, so the session fails as ``schema_invalid`` and stays eligible for retry.
    """
    client = _UnforcedClient(
        {
            "content": "The session covered a cluster check.",
            "tool_calls": [],
            "finish_reason": "stop",
        }
    )
    _install(monkeypatch, client)

    outcome = await ss.generate_session_digest(_session(), session_id="sess-1", ended_at=_T0)

    assert client.kwargs["tool_choice"] == "auto"
    assert outcome.status is SessionSummaryStatus.FAILED
    assert outcome.failure_reason is SummaryFailureReason.SCHEMA_INVALID


@pytest.mark.asyncio
async def test_a_schema_valid_json_text_reply_under_auto_is_accepted_as_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing path is kept on purpose (FRE-1647, Codex plan review item 1).

    ``_reply_payload`` reads text when no tool call came back, on every deployment
    (``test_text_content_is_still_accepted``). Under ``auto`` the same parser and checks
    apply, so a valid digest in text is a real answer, not a silent success. Requiring the
    tool call would change reply handling for the forced-capable deployments too.
    """
    client = _UnforcedClient(_reply())
    _install(monkeypatch, client)

    outcome = await ss.generate_session_digest(_session(), session_id="sess-1", ended_at=_T0)

    assert client.kwargs["tool_choice"] == "auto"
    assert outcome.status is SessionSummaryStatus.GENERATED
