"""FRE-1715: Captain's Log DSPy jobs stay on LiteLLM after the DSPy 3.4 engine change.

DSPy 3.4 defaults ``dspy.LM`` to ``engine="auto"``, which calls providers through
its bundled native client. The transport seam, the ``reasoning_effort`` mapping and
the ``response_cost`` that ``dspy_gate.collect_dspy_cost`` settles against all
belong to the LiteLLM route, so ``configure_dspy_lm`` must select that route.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from personal_agent.llm_client import dspy_adapter


def _model_def(*, provider: str, model_id: str, effort: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        provider=provider,
        id=model_id,
        endpoint=None,
        resolve_mode=lambda: SimpleNamespace(resolved_reasoning_effort=effort),
    )


_COMPLETION: dict[str, Any] = {
    "id": "x",
    "object": "chat.completion",
    "created": 1,
    "model": "gpt-4o-mini",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
    ],
    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
}


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[httpx.Request]]:
    """Capture requests at the httpx transport, and refuse every real socket.

    The native DSPy engine does not use httpx, so a regression fails on the socket
    refusal and never reaches a provider.
    """
    seen: list[httpx.Request] = []

    def handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return httpx.Response(200, json=_COMPLETION, request=request)

    def refuse(self: socket.socket, address: object) -> None:
        raise AssertionError(f"real socket connect to {address!r}")

    monkeypatch.setattr(dspy_adapter.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    yield seen


def _run(target: SimpleNamespace, *, is_cloud: bool, prompt: str) -> Any:
    with patch.object(
        dspy_adapter, "resolve_dspy_target", return_value=("deployment", target, is_cloud)
    ):
        lm = dspy_adapter.configure_dspy_lm("captains_log", base_url="http://slm.invalid")
    lm.cache = False
    lm(prompt)
    return lm


def test_cloud_job_uses_litellm_and_records_its_cost(wire: list[httpx.Request]) -> None:
    """The cost that the job reservation settles against comes from LiteLLM's response_cost."""
    target = _model_def(provider="openai", model_id="gpt-4o-mini")

    lm = _run(target, is_cloud=True, prompt="cloud")

    assert [str(r.url) for r in wire] == ["https://api.openai.com/v1/chat/completions"]
    entry = lm.history[-1]
    assert entry["cost"] == pytest.approx(0.000045)
    assert entry["usage"]["prompt_tokens"] == 100
    assert entry["usage"]["completion_tokens"] == 50


def test_cloud_job_forwards_the_declared_reasoning_effort(wire: list[httpx.Request]) -> None:
    """FRE-1007: the deployment's declared effort reaches the wire."""
    target = _model_def(provider="openai", model_id="gpt-5.1", effort="low")

    _run(target, is_cloud=True, prompt="effort")

    assert json.loads(wire[0].content)["reasoning_effort"] == "low"


def test_local_job_uses_litellm_at_the_local_endpoint(wire: list[httpx.Request]) -> None:
    """A local deployment reaches its own endpoint, with the /v1 suffix applied."""
    target = _model_def(provider="slm_local", model_id="local-model")

    _run(target, is_cloud=False, prompt="local")

    assert [str(r.url) for r in wire] == ["http://slm.invalid/v1/chat/completions"]
    assert json.loads(wire[0].content)["model"] == "local-model"
