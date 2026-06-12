"""FRE-376 Phase 3 / FRE-351 (rebased): canonical model_call_completed emit fields.

After FRE-376 Phase 3 removed the ``litellm_request_complete`` event and its
legacy field aliases (``prompt_tokens``, ``completion_tokens``, ``tokens``,
``cache_write_tokens``), these tests assert the canonical
``model_call_completed`` event payload includes ``latency_ms``,
``input_tokens``, ``output_tokens``, ``total_tokens``, ``endpoint``, and
``cache_creation_input_tokens``.

The Phase 2 contract (canonical-field frozenset) is enforced in
``test_telemetry_parity.py``; this file complements it with a runtime
wire-up check on LiteLLMClient.respond().
"""

# ruff: noqa: D103

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_response(
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    total_tokens: int = 150,
    cache_read: int | None = None,
    cache_write: int | None = None,
) -> MagicMock:
    """Build a minimal litellm ModelResponse mock with realistic usage."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = total_tokens
    usage.cache_read_input_tokens = cache_read
    usage.cache_creation_input_tokens = cache_write
    usage.prompt_tokens_details = None

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "hello"
    response.choices[0].message.tool_calls = None
    response.usage = usage
    response.id = "resp_abc123"
    return response


async def _call_respond(captured_log_calls: list[tuple]) -> None:
    """Run LiteLLMClient.respond() with all external I/O mocked.

    The log.info calls are captured into captured_log_calls as (event, kwargs)
    tuples.
    """
    from personal_agent.llm_client.litellm_client import LiteLLMClient
    from personal_agent.llm_client.types import ModelRole
    from tests._helpers.trace import make_test_ctx

    mock_response = _make_mock_response()

    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value="res-001")
    mock_gate.commit = AsyncMock()

    mock_tracker = AsyncMock()
    mock_tracker.connect = AsyncMock()
    mock_tracker.disconnect = AsyncMock()
    mock_tracker.record_api_call = AsyncMock()

    mock_log = MagicMock()

    def _capture_info(event: str, **kwargs: object) -> None:
        captured_log_calls.append((event, kwargs))

    mock_log.info = MagicMock(side_effect=_capture_info)
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
            trace_ctx=make_test_ctx("litellm_emit_payload"),
        )


def _get_completed_event(calls: list[tuple]) -> dict:
    """Return kwargs from the canonical ``model_call_completed`` log call."""
    for event, kwargs in calls:
        if event == "model_call_completed":
            return kwargs
    raise AssertionError(f"model_call_completed event not found in calls: {[e for e, _ in calls]}")


@pytest.mark.asyncio
async def test_model_call_completed_includes_output_tokens() -> None:
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_completed_event(calls)
    assert "output_tokens" in kwargs, f"output_tokens missing. Keys present: {sorted(kwargs)}"
    assert kwargs["output_tokens"] == 50


@pytest.mark.asyncio
async def test_model_call_completed_includes_input_tokens() -> None:
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_completed_event(calls)
    assert "input_tokens" in kwargs, f"input_tokens missing. Keys present: {sorted(kwargs)}"
    assert kwargs["input_tokens"] == 100


@pytest.mark.asyncio
async def test_model_call_completed_includes_latency_ms() -> None:
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_completed_event(calls)
    assert "latency_ms" in kwargs
    assert isinstance(kwargs["latency_ms"], int)


@pytest.mark.asyncio
async def test_model_call_completed_includes_total_tokens() -> None:
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_completed_event(calls)
    assert kwargs["total_tokens"] == 150


@pytest.mark.asyncio
async def test_model_call_completed_includes_endpoint() -> None:
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_completed_event(calls)
    assert kwargs["endpoint"] == "anthropic"


@pytest.mark.asyncio
async def test_model_call_completed_uses_cache_creation_input_tokens() -> None:
    """Canonical name (not the dropped legacy ``cache_write_tokens`` alias)."""
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_completed_event(calls)
    assert "cache_creation_input_tokens" in kwargs
    assert "cache_write_tokens" not in kwargs


@pytest.mark.asyncio
async def test_legacy_litellm_events_are_not_emitted() -> None:
    """ADR-0074 Phase 3: legacy ``litellm_request_*`` events are gone."""
    calls: list[tuple] = []
    await _call_respond(calls)
    event_names = {event for event, _ in calls}
    assert "litellm_request_start" not in event_names
    assert "litellm_request_complete" not in event_names


async def _call_respond_failing(captured_error_calls: list[tuple]) -> None:
    """Run LiteLLMClient.respond() where ``litellm.acompletion`` raises.

    The ``log.error`` calls are captured into ``captured_error_calls`` as
    ``(event, kwargs)`` tuples so error-path field threading can be asserted.
    """
    from personal_agent.llm_client.litellm_client import LiteLLMClient
    from personal_agent.llm_client.types import LLMClientError, ModelRole
    from personal_agent.telemetry.trace import TraceContext

    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value="res-001")
    mock_gate.refund = AsyncMock()

    mock_tracker = AsyncMock()
    mock_tracker.connect = AsyncMock()
    mock_tracker.disconnect = AsyncMock()

    mock_log = MagicMock()

    def _capture_error(event: str, **kwargs: object) -> None:
        captured_error_calls.append((event, kwargs))

    mock_log.info = MagicMock()
    mock_log.warning = MagicMock()
    mock_log.error = MagicMock(side_effect=_capture_error)

    client = LiteLLMClient(
        model_id="claude-sonnet-4-6",
        provider="anthropic",
        max_tokens=256,
        budget_role="main_inference",
    )

    with (
        patch("personal_agent.llm_client.litellm_client.log", mock_log),
        patch("litellm.acompletion", AsyncMock(side_effect=RuntimeError("upstream 500"))),
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
        with pytest.raises(LLMClientError):
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hello"}],
                trace_ctx=TraceContext.new_trace(session_id="sess-552"),
            )


@pytest.mark.asyncio
async def test_litellm_request_failed_includes_session_id() -> None:
    """FRE-552: litellm_request_failed carries session_id from the trace context."""
    calls: list[tuple] = []
    await _call_respond_failing(calls)
    failed = [kwargs for event, kwargs in calls if event == "litellm_request_failed"]
    assert failed, f"litellm_request_failed not found in: {[e for e, _ in calls]}"
    assert failed[0]["session_id"] == "sess-552"
