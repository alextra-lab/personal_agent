# tests/personal_agent/memory/test_generate_query_paraphrases.py
"""Tests for the multi-query paraphrase generator (ADR-0104 / FRE-723).

Unit tests — mocks LocalLLMClient.respond, no live LLM server needed. The
generator must fail open (return []) on any error; callers depend on this to
degrade to the dense arm alone rather than hard-failing recall.
"""

from __future__ import annotations

from typing import Any

import pytest

import personal_agent.memory.service as svc


class _FakeLocalClient:
    """Minimal stand-in for LocalLLMClient, matching the codebase's fake-client
    test convention (see tests/personal_agent/second_brain/test_session_summary.py).
    """

    def __init__(self, content: str | None = None, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"content": self.content}


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch, *, content: str | None = None, error: Exception | None = None
) -> _FakeLocalClient:
    client = _FakeLocalClient(content=content, error=error)
    monkeypatch.setattr(svc, "LocalLLMClient", lambda: client)
    return client


class TestGenerateQueryParaphrases:
    """Tests for generate_query_paraphrases."""

    @pytest.mark.asyncio
    async def test_success_parses_newline_separated_paraphrases(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_client(monkeypatch, content="perception\neyesight\nsight")
        result = await svc.generate_query_paraphrases("vision", count=3)
        assert result == ["perception", "eyesight", "sight"]

    @pytest.mark.asyncio
    async def test_result_capped_at_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_client(monkeypatch, content="a\nb\nc\nd\ne")
        result = await svc.generate_query_paraphrases("vision", count=2)
        assert result == ["a", "b"]

    @pytest.mark.asyncio
    async def test_blank_lines_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_client(monkeypatch, content="perception\n\n  \neyesight")
        result = await svc.generate_query_paraphrases("vision", count=5)
        assert result == ["perception", "eyesight"]

    @pytest.mark.asyncio
    async def test_count_below_one_returns_empty_without_calling_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install_fake_client(monkeypatch, content="perception")
        result = await svc.generate_query_paraphrases("vision", count=0)
        assert result == []
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_blank_query_text_returns_empty_without_calling_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install_fake_client(monkeypatch, content="perception")
        result = await svc.generate_query_paraphrases("   ", count=2)
        assert result == []
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_llm_timeout_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.llm_client.types import LLMTimeout

        _install_fake_client(monkeypatch, error=LLMTimeout("timed out"))
        result = await svc.generate_query_paraphrases("vision", count=2)
        assert result == []

    @pytest.mark.asyncio
    async def test_generic_exception_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_client(monkeypatch, error=RuntimeError("boom"))
        result = await svc.generate_query_paraphrases("vision", count=2)
        assert result == []
