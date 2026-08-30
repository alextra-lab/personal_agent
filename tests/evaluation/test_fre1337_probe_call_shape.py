"""AC-2 structural proof + the model-identity fix, both via a mocked client.

Two things a live run cannot cheaply prove on every call, so a unit test proves them once,
structurally: (1) the probe truly passes no tools and no history — the contamination-free
claim; (2) the deployment is pinned to the exact requested key via
``set_current_selection`` before ``respond()`` is called, closing the local-model bug the
codex plan-review found (``get_llm_client_for_key`` alone silently ignores the key for
local placements).
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts.eval.fre1337_intent_probe import probe
from scripts.eval.fre1337_intent_probe.taxonomy import PROBE_SYSTEM_PROMPT, build_probe_prompt

from personal_agent.config.selection import get_current_selection
from personal_agent.llm_client.types import ModelRole

pytestmark = pytest.mark.asyncio


class _FakeClient:
    """Captures the exact respond() call shape and the selection pinned at call time."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.selection_at_call_time: str | None = None

    async def respond(self, **kwargs: Any) -> dict[str, Any]:
        self.selection_at_call_time = get_current_selection(ModelRole.STUDY.value)
        self.calls.append(kwargs)
        return self.response


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    client = _FakeClient(
        response={
            "content": '{"task_type": "conversational", "reason": "it is a greeting"}',
            "raw": {"model": "unsloth/qwen3.6-35-A3B"},
        }
    )
    monkeypatch.setattr(probe, "get_llm_client_for_key", lambda *a, **k: client)
    return client


async def test_probe_passes_no_tools(fake_client: _FakeClient) -> None:
    await probe.classify_with_model("qwen3.6-35b-thinking", "How is your day going?")
    call = fake_client.calls[0]
    assert call.get("tools") is None
    assert "tool_choice" not in call or call["tool_choice"] is None


async def test_probe_passes_no_history_only_one_user_message(fake_client: _FakeClient) -> None:
    await probe.classify_with_model("qwen3.6-35b-thinking", "How is your day going?")
    call = fake_client.calls[0]
    assert call["messages"] == [
        {"role": "user", "content": build_probe_prompt("How is your day going?")}
    ]
    assert call["system_prompt"] == PROBE_SYSTEM_PROMPT
    assert call.get("previous_response_id") is None


async def test_probe_pins_the_exact_requested_deployment(fake_client: _FakeClient) -> None:
    await probe.classify_with_model("qwen3.6-27b-ovh", "How is your day going?")
    assert fake_client.selection_at_call_time == "qwen3.6-27b-ovh"


async def test_probe_selection_is_reset_after_the_call(fake_client: _FakeClient) -> None:
    await probe.classify_with_model("qwen3.6-27b-ovh", "How is your day going?")
    assert get_current_selection(ModelRole.STUDY.value) is None


async def test_probe_records_prompt_verbatim(fake_client: _FakeClient) -> None:
    result = await probe.classify_with_model("qwen3.6-35b-thinking", "How is your day going?")
    assert (
        result.prompt == f"{PROBE_SYSTEM_PROMPT}\n\n{build_probe_prompt('How is your day going?')}"
    )


async def test_probe_captures_resolved_model_id_for_identity_check(
    fake_client: _FakeClient,
) -> None:
    result = await probe.classify_with_model("qwen3.6-35b-thinking", "How is your day going?")
    assert result.resolved_model_id == "unsloth/qwen3.6-35-A3B"
    assert result.requested_model_id == "unsloth/qwen3.6-35-A3B"


async def test_invalid_json_response_yields_invalid_response_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(response={"content": "not json", "raw": {}})
    monkeypatch.setattr(probe, "get_llm_client_for_key", lambda *a, **k: client)
    result = await probe.classify_with_model("qwen3.6-35b-thinking", "anything")
    assert result.task_type == "invalid_response"


async def test_task_type_outside_taxonomy_yields_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        response={"content": '{"task_type": "research", "reason": "x"}', "raw": {}}
    )
    monkeypatch.setattr(probe, "get_llm_client_for_key", lambda *a, **k: client)
    result = await probe.classify_with_model("qwen3.6-35b-thinking", "anything")
    assert result.task_type == "invalid_response"
