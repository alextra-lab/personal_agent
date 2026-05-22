"""FRE-376 Phase 2 / ADR-0074 §I2 — model client telemetry parity.

Both :class:`LocalLLMClient` and :class:`LiteLLMClient` must emit the
canonical ``model_call_started`` and ``model_call_completed`` events with
the field sets enumerated in
:data:`personal_agent.telemetry.events.CANONICAL_MODEL_CALL_STARTED_FIELDS`
and
:data:`personal_agent.telemetry.events.CANONICAL_MODEL_CALL_COMPLETED_FIELDS`.

A request handler that switches between cloud and local cannot tell the
difference from telemetry alone — the canonical fields are present on
both, with the same names and meanings. This module is the contract
enforcer: adding a required field to those frozensets in ``events.py``
forces both clients (and any future model client) to populate it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.telemetry.events import (
    CANONICAL_MODEL_CALL_COMPLETED_FIELDS,
    CANONICAL_MODEL_CALL_STARTED_FIELDS,
)
from personal_agent.telemetry.trace import SystemTraceContext, TraceContext


def _make_litellm_response() -> MagicMock:
    """Build a minimal litellm ModelResponse mock for parity testing."""
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 50
    usage.total_tokens = 150
    usage.cache_read_input_tokens = None
    usage.cache_creation_input_tokens = None
    usage.prompt_tokens_details = None

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "hello"
    response.choices[0].message.tool_calls = None
    response.usage = usage
    response.id = "resp_parity"
    return response


def _ctx_with_session(session_uuid: str = "11111111-1111-1111-1111-111111111111") -> TraceContext:
    """Build a TraceContext that carries a non-None session_id for parity asserts.

    Parent span is also non-None so ``parent_span_id`` is observable in emits.
    """
    base = SystemTraceContext.new("telemetry_parity_test", session_id=session_uuid)
    return TraceContext(
        trace_id=base.trace_id,
        parent_span_id="22222222-2222-2222-2222-222222222222",
        profile=base.profile,
        user_id=base.user_id,
        session_id=base.session_id,
        kind=base.kind,
    )


async def _drive_litellm(captured: list[tuple[str, dict[str, Any]]]) -> None:
    """Run LiteLLMClient.respond() with all external I/O mocked.

    Captures structured-log calls into ``captured`` as ``(event_name, kwargs)``.
    """
    from personal_agent.llm_client.litellm_client import LiteLLMClient
    from personal_agent.llm_client.types import ModelRole

    mock_response = _make_litellm_response()

    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value="res-parity")
    mock_gate.commit = AsyncMock()

    mock_tracker = AsyncMock()
    mock_tracker.connect = AsyncMock()
    mock_tracker.disconnect = AsyncMock()
    mock_tracker.record_api_call = AsyncMock()

    mock_log = MagicMock()
    mock_log.info = MagicMock(side_effect=lambda event, **kw: captured.append((event, kw)))
    mock_log.warning = MagicMock()
    mock_log.error = MagicMock()

    client = LiteLLMClient(
        model_id="claude-sonnet-4-6",
        provider="anthropic",
        max_tokens=256,
        budget_role="main_inference",
    )

    with (
        patch("personal_agent.llm_client.litellm_client.log", mock_log),
        patch("litellm.acompletion", AsyncMock(return_value=mock_response)),
        patch("litellm.completion_cost", return_value=0.001),
        patch("personal_agent.cost_gate.get_default_gate", return_value=mock_gate),
        patch(
            "personal_agent.cost_gate.load_budget_config",
            return_value=MagicMock(),
        ),
        patch(
            "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
            return_value=Decimal("0.01"),
        ),
        patch(
            "personal_agent.llm_client.history_sanitiser.sanitise_messages",
            side_effect=lambda msgs, trace_id: (msgs, []),
        ),
        patch(
            "personal_agent.llm_client.cost_tracker.CostTrackerService",
            return_value=mock_tracker,
        ),
        patch(
            "personal_agent.config.settings.get_settings",
            return_value=MagicMock(anthropic_api_key="test-key", openai_api_key=None),
        ),
    ):
        await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hello"}],
            trace_ctx=_ctx_with_session(),
        )


def _local_stream_mock(payload: dict[str, Any]) -> MagicMock:
    """Build a streaming-shaped httpx mock that yields one chunk + [DONE]."""
    choice = payload.get("choices", [{}])[0]
    msg = choice.get("message", {})
    delta = {k: v for k, v in msg.items() if v is not None}
    chunk = {
        "id": "chatcmpl-parity",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": choice.get("finish_reason", "stop"),
            }
        ],
        "usage": payload.get("usage"),
    }
    lines = [f"data: {json.dumps(chunk)}", "data: [DONE]"]

    async def aiter_lines() -> Any:
        for line in lines:
            yield line

    response_obj = MagicMock()
    response_obj.raise_for_status = MagicMock()
    response_obj.aiter_lines = aiter_lines
    response_obj.status_code = 200

    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=response_obj)
    stream_cm.__aexit__ = AsyncMock(return_value=None)
    return stream_cm


async def _drive_local(
    captured: list[tuple[str, dict[str, Any]]],
    tmp_path: Path,
) -> None:
    """Run LocalLLMClient.respond() with httpx fully mocked.

    Captures structured-log calls into ``captured``.
    """
    from personal_agent.llm_client.client import LocalLLMClient
    from personal_agent.llm_client.types import ModelRole

    config_file = tmp_path / "models.yaml"
    config_file.write_text(
        """
models:
  primary:
    id: "test-primary"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 2
    default_timeout: 60
"""
    )

    client = LocalLLMClient(
        base_url="http://localhost:1234/v1",
        timeout_seconds=30,
        max_retries=0,
        model_config_path=config_file,
    )

    mock_response = {
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }

    mock_log = MagicMock()
    mock_log.info = MagicMock(side_effect=lambda event, **kw: captured.append((event, kw)))
    mock_log.warning = MagicMock()
    mock_log.error = MagicMock()
    mock_log.debug = MagicMock()

    with (
        patch("personal_agent.llm_client.client.log", mock_log),
        patch("httpx.AsyncClient") as mock_client_class,
    ):
        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=_local_stream_mock(mock_response))
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hi"}],
            trace_ctx=_ctx_with_session(),
        )


def _pick(captured: list[tuple[str, dict[str, Any]]], event: str) -> dict[str, Any]:
    """Return kwargs of the first emit whose event name matches ``event``."""
    for name, kwargs in captured:
        if name == event:
            return kwargs
    raise AssertionError(f"event {event!r} not found in captured emits: {[n for n, _ in captured]}")


@pytest.mark.asyncio
async def test_litellm_emits_canonical_started_fields() -> None:
    """LiteLLMClient model_call_started covers canonical field set (I2)."""
    captured: list[tuple[str, dict[str, Any]]] = []
    await _drive_litellm(captured)
    started = _pick(captured, "model_call_started")
    missing = CANONICAL_MODEL_CALL_STARTED_FIELDS - set(started)
    assert not missing, f"LiteLLMClient model_call_started missing fields: {missing}"
    assert started["session_id"] == "11111111-1111-1111-1111-111111111111"
    assert started["model"] == "anthropic/claude-sonnet-4-6"
    assert started["span_id"] is not None
    assert started["parent_span_id"] == "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
async def test_litellm_emits_canonical_completed_fields() -> None:
    """LiteLLMClient model_call_completed covers canonical field set (I2)."""
    captured: list[tuple[str, dict[str, Any]]] = []
    await _drive_litellm(captured)
    completed = _pick(captured, "model_call_completed")
    missing = CANONICAL_MODEL_CALL_COMPLETED_FIELDS - set(completed)
    assert not missing, f"LiteLLMClient model_call_completed missing fields: {missing}"
    assert completed["input_tokens"] == 100
    assert completed["output_tokens"] == 50
    assert completed["total_tokens"] == 150
    assert isinstance(completed["latency_ms"], int)


@pytest.mark.asyncio
async def test_litellm_preserves_legacy_event_names() -> None:
    """Back-compat: legacy litellm_request_* events still emitted alongside canonical ones."""
    captured: list[tuple[str, dict[str, Any]]] = []
    await _drive_litellm(captured)
    event_names = {n for n, _ in captured}
    assert "litellm_request_start" in event_names
    assert "litellm_request_complete" in event_names
    legacy_complete = _pick(captured, "litellm_request_complete")
    # Back-compat fields that downstream Kibana queries still rely on
    assert "tokens" in legacy_complete
    assert "prompt_tokens" in legacy_complete
    assert "completion_tokens" in legacy_complete


@pytest.mark.asyncio
async def test_local_emits_canonical_started_fields(tmp_path: Path) -> None:
    """LocalLLMClient model_call_started covers canonical field set (I2)."""
    captured: list[tuple[str, dict[str, Any]]] = []
    await _drive_local(captured, tmp_path)
    started = _pick(captured, "model_call_started")
    missing = CANONICAL_MODEL_CALL_STARTED_FIELDS - set(started)
    assert not missing, f"LocalLLMClient model_call_started missing fields: {missing}"
    assert started["session_id"] == "11111111-1111-1111-1111-111111111111"
    assert started["model"] == "test-primary"
    assert started["span_id"] is not None
    assert started["parent_span_id"] == "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
async def test_local_emits_canonical_completed_fields(tmp_path: Path) -> None:
    """LocalLLMClient model_call_completed covers canonical field set (I2)."""
    captured: list[tuple[str, dict[str, Any]]] = []
    await _drive_local(captured, tmp_path)
    completed = _pick(captured, "model_call_completed")
    missing = CANONICAL_MODEL_CALL_COMPLETED_FIELDS - set(completed)
    assert not missing, f"LocalLLMClient model_call_completed missing fields: {missing}"
    assert completed["input_tokens"] == 100
    assert completed["output_tokens"] == 50
    assert completed["total_tokens"] == 150
    assert isinstance(completed["latency_ms"], int)


@pytest.mark.asyncio
async def test_clients_emit_identical_canonical_field_sets(tmp_path: Path) -> None:
    """The whole point of I2: shapes match across clients for canonical events."""
    cloud: list[tuple[str, dict[str, Any]]] = []
    local: list[tuple[str, dict[str, Any]]] = []
    await _drive_litellm(cloud)
    await _drive_local(local, tmp_path)

    cloud_started = set(_pick(cloud, "model_call_started"))
    local_started = set(_pick(local, "model_call_started"))
    canonical_started = CANONICAL_MODEL_CALL_STARTED_FIELDS
    assert canonical_started.issubset(cloud_started)
    assert canonical_started.issubset(local_started)

    cloud_completed = set(_pick(cloud, "model_call_completed"))
    local_completed = set(_pick(local, "model_call_completed"))
    canonical_completed = CANONICAL_MODEL_CALL_COMPLETED_FIELDS
    assert canonical_completed.issubset(cloud_completed)
    assert canonical_completed.issubset(local_completed)
