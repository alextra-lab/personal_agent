"""Unit tests for the session-level summariser (FRE-347 / FRE-346 G1).

All tests mock the LLM client; no live model calls. The summariser must
never raise — every error path returns ``None`` so the consolidator can
proceed with ``session_summary=None``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.cost_gate import BudgetDenied
from personal_agent.llm_client import LLMTimeout
from personal_agent.second_brain import session_summary as ss


def _make_capture(
    *,
    user: str = "What is the status of the deploy?",
    assistant: str = "It is green; latest commit a1b2c3 deployed at 14:02 UTC.",
    minutes_offset: int = 0,
    session_id: str = "session-x",
) -> TaskCapture:
    return TaskCapture(
        trace_id=str(uuid4()),
        session_id=session_id,
        timestamp=datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc)
        + timedelta(minutes=minutes_offset),
        user_message=user,
        assistant_response=assistant,
        outcome="completed",
        user_id=uuid4(),
    )


class _FakeCloudClient:
    """Minimal stand-in for the cloud LLM client returned by ``get_llm_client``."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"content": self.content}


@pytest.fixture(autouse=True)
def _force_cloud_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the captains_log role to a cloud model so tests exercise the cloud path.

    The repo default (gpt-5.4-nano) is already cloud, but pinning here makes
    the test independent of config drift.
    """

    class _ModelDef:
        provider = "openai"
        id = "gpt-5.4-nano"

    class _ModelConfig:
        models = {"gpt-5.4-nano": _ModelDef()}

    monkeypatch.setattr(ss, "load_model_config", lambda: _ModelConfig())
    monkeypatch.setattr(ss, "resolve_role_model_key", lambda role: "gpt-5.4-nano")


@pytest.fixture
def fake_cloud_client(monkeypatch: pytest.MonkeyPatch) -> _FakeCloudClient:
    """Install a fake cloud client and return it for inspection."""
    client = _FakeCloudClient(
        content=(
            "Deploy status checked at 14:02 UTC; commit a1b2c3 confirmed green. "
            "User wanted reassurance before merging the next change."
        )
    )

    # The factory import is inside generate_session_summary; patch the underlying module.
    import personal_agent.llm_client.factory as factory

    monkeypatch.setattr(factory, "get_llm_client", lambda **kwargs: client)
    return client


@pytest.mark.asyncio
async def test_returns_summary_on_happy_path(fake_cloud_client: _FakeCloudClient) -> None:
    """Cloud-path happy case: the summariser returns prose and calls the LLM once."""
    captures = [_make_capture(minutes_offset=i) for i in range(3)]
    summary = await ss.generate_session_summary(captures, session_id="s1")

    assert summary is not None
    assert "Deploy status" in summary
    assert ss._MIN_SUMMARY_CHARS <= len(summary) <= ss._MAX_SUMMARY_CHARS

    # The cloud client was called once with the system prompt + user prompt.
    assert len(fake_cloud_client.calls) == 1
    call = fake_cloud_client.calls[0]
    assert call["system_prompt"] == ss._SUMMARY_SYSTEM_PROMPT
    user_msg = call["messages"][0]["content"]
    assert "3 turn(s)" in user_msg
    assert "Conversation excerpts" in user_msg


@pytest.mark.asyncio
async def test_empty_captures_returns_none() -> None:
    """An empty capture list short-circuits to None without calling the LLM."""
    summary = await ss.generate_session_summary([], session_id="s1")
    assert summary is None


@pytest.mark.asyncio
async def test_budget_denied_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """BudgetDenied is swallowed — the summariser never blocks consolidation."""

    def _denying_client(**_: Any) -> Any:
        class _C:
            async def respond(self, **kwargs: Any) -> Any:
                raise BudgetDenied(
                    role="captains_log",
                    time_window="daily",
                    current_spend=Decimal("2.50"),
                    cap=Decimal("2.50"),
                    window_resets_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
                )

        return _C()

    import personal_agent.llm_client.factory as factory

    monkeypatch.setattr(factory, "get_llm_client", _denying_client)

    captures = [_make_capture()]
    summary = await ss.generate_session_summary(captures, session_id="s1")
    assert summary is None  # budget denial never raises out of the summariser


@pytest.mark.asyncio
async def test_generic_exception_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any unexpected exception from the LLM client returns None without raising."""

    def _raising_client(**_: Any) -> Any:
        class _C:
            async def respond(self, **kwargs: Any) -> Any:
                raise RuntimeError("model down")

        return _C()

    import personal_agent.llm_client.factory as factory

    monkeypatch.setattr(factory, "get_llm_client", _raising_client)

    captures = [_make_capture()]
    summary = await ss.generate_session_summary(captures, session_id="s1")
    assert summary is None


@pytest.mark.asyncio
async def test_too_short_response_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pathologically short LLM output is rejected (returns None)."""
    short = _FakeCloudClient(content="ok")
    import personal_agent.llm_client.factory as factory

    monkeypatch.setattr(factory, "get_llm_client", lambda **kwargs: short)

    summary = await ss.generate_session_summary([_make_capture()], session_id="s1")
    assert summary is None


@pytest.mark.asyncio
async def test_too_long_response_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """An over-long LLM output is truncated to the documented cap."""
    huge = _FakeCloudClient(content="x" * (ss._MAX_SUMMARY_CHARS + 500))
    import personal_agent.llm_client.factory as factory

    monkeypatch.setattr(factory, "get_llm_client", lambda **kwargs: huge)

    summary = await ss.generate_session_summary([_make_capture()], session_id="s1")
    assert summary is not None
    assert len(summary) <= ss._MAX_SUMMARY_CHARS


@pytest.mark.asyncio
async def test_quote_wrapped_response_is_unwrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Surrounding quotes (a common LLM artefact) are stripped from the summary."""
    inner = "Reviewed the deploy status; commit a1b2c3 is green and merged into main."
    quoted = _FakeCloudClient(content=f'"{inner}"')
    import personal_agent.llm_client.factory as factory

    monkeypatch.setattr(factory, "get_llm_client", lambda **kwargs: quoted)

    summary = await ss.generate_session_summary([_make_capture()], session_id="s1")
    assert summary == inner


@pytest.mark.asyncio
async def test_disabled_via_settings_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``AGENT_SESSION_SUMMARY_ENABLED=false`` kill-switch short-circuits to None."""
    from personal_agent.config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "session_summary_enabled", False, raising=False)

    summary = await ss.generate_session_summary([_make_capture()], session_id="s1")
    assert summary is None


@pytest.mark.asyncio
async def test_prompt_contains_turn_excerpts_and_caps_at_20(
    fake_cloud_client: _FakeCloudClient,
) -> None:
    """Long sessions are capped at 20 inlined turns and signal the omitted remainder."""
    captures = [_make_capture(minutes_offset=i, user=f"q{i}", assistant=f"a{i}") for i in range(25)]
    await ss.generate_session_summary(captures, session_id="s1")

    user_msg = fake_cloud_client.calls[0]["messages"][0]["content"]
    assert "25 turn(s)" in user_msg
    # Only the first 20 are inlined; the rest are signalled as omitted.
    assert "5 more turn(s) omitted" in user_msg
    assert "User: q0" in user_msg
    assert "User: q19" in user_msg
    assert "User: q20" not in user_msg


@pytest.mark.asyncio
async def test_local_path_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the role resolves to a local model, a timeout returns None cleanly."""

    class _LocalModelDef:
        provider = None  # forces the local path
        id = "qwen-local"

    class _ModelConfig:
        models = {"qwen-local": _LocalModelDef()}

    monkeypatch.setattr(ss, "load_model_config", lambda: _ModelConfig())
    monkeypatch.setattr(ss, "resolve_role_model_key", lambda role: "qwen-local")

    class _LocalClient:
        async def respond(self, **kwargs: Any) -> Any:
            raise LLMTimeout("timed out")

    monkeypatch.setattr(ss, "LocalLLMClient", lambda: _LocalClient())

    summary = await ss.generate_session_summary([_make_capture()], session_id="s1")
    assert summary is None
