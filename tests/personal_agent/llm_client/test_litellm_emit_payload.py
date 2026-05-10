"""FRE-351: litellm_request_complete emit must include field-parity fields.

Tests verify that litellm_client.py logs completion_tokens, latency_ms,
total_tokens, endpoint, and cache_creation_input_tokens alongside existing
fields when a cloud LLM call succeeds.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch


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
    # Anthropic cache fields — explicitly None unless provided, so getattr(..., None)
    # returns None (MagicMock auto-creates attributes, making getattr never fall back
    # to the default; we set them explicitly to control the value).
    usage.cache_read_input_tokens = cache_read
    usage.cache_creation_input_tokens = cache_write
    usage.prompt_tokens_details = None  # no OpenAI cached_tokens

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "hello"
    response.choices[0].message.tool_calls = None
    response.usage = usage
    response.id = "resp_abc123"
    return response


async def _call_respond(captured_log_calls: list[tuple]) -> None:
    """Run LiteLLMClient.respond() with all external I/O mocked.

    The log.info calls are captured into captured_log_calls as (event, kwargs) tuples.
    """
    from personal_agent.llm_client.litellm_client import LiteLLMClient
    from personal_agent.llm_client.types import ModelRole

    mock_response = _make_mock_response()

    # Gate mock
    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value="res-001")
    mock_gate.commit = AsyncMock()

    # Cost tracker mock
    mock_tracker = AsyncMock()
    mock_tracker.connect = AsyncMock()
    mock_tracker.disconnect = AsyncMock()
    mock_tracker.record_api_call = AsyncMock()

    # Capture log calls
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
        )


def _get_complete_event(calls: list[tuple]) -> dict:
    """Return kwargs from the litellm_request_complete log call."""
    for event, kwargs in calls:
        if event == "litellm_request_complete":
            return kwargs
    raise AssertionError(
        f"litellm_request_complete event not found in calls: {[e for e, _ in calls]}"
    )


import pytest


@pytest.mark.asyncio
async def test_litellm_emit_includes_completion_tokens() -> None:
    """litellm_request_complete must log completion_tokens (FRE-351)."""
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_complete_event(calls)
    assert "completion_tokens" in kwargs, (
        "completion_tokens missing from litellm_request_complete emit. "
        f"Keys present: {sorted(kwargs)}"
    )
    assert kwargs["completion_tokens"] == 50


@pytest.mark.asyncio
async def test_litellm_emit_includes_latency_ms() -> None:
    """litellm_request_complete must log latency_ms in milliseconds (FRE-351)."""
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_complete_event(calls)
    assert "latency_ms" in kwargs, (
        "latency_ms missing from litellm_request_complete emit. "
        f"Keys present: {sorted(kwargs)}"
    )
    assert isinstance(kwargs["latency_ms"], int), (
        f"latency_ms must be int ms, got {type(kwargs['latency_ms'])}"
    )


@pytest.mark.asyncio
async def test_litellm_emit_includes_total_tokens() -> None:
    """litellm_request_complete must log total_tokens (distinct from legacy 'tokens') (FRE-351)."""
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_complete_event(calls)
    assert "total_tokens" in kwargs, (
        "total_tokens missing from litellm_request_complete emit. "
        f"Keys present: {sorted(kwargs)}"
    )
    assert kwargs["total_tokens"] == 150


@pytest.mark.asyncio
async def test_litellm_emit_includes_endpoint() -> None:
    """litellm_request_complete must log endpoint (FRE-351)."""
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_complete_event(calls)
    assert "endpoint" in kwargs, (
        "endpoint missing from litellm_request_complete emit. "
        f"Keys present: {sorted(kwargs)}"
    )
    assert kwargs["endpoint"] == "anthropic"


@pytest.mark.asyncio
async def test_litellm_emit_includes_cache_creation_input_tokens_field() -> None:
    """litellm_request_complete must use cache_creation_input_tokens (not cache_write_tokens) (FRE-351)."""
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_complete_event(calls)
    assert "cache_creation_input_tokens" in kwargs, (
        "cache_creation_input_tokens missing from litellm_request_complete emit. "
        f"Keys present: {sorted(kwargs)}"
    )


@pytest.mark.asyncio
async def test_litellm_emit_backward_compat_tokens_field_still_present() -> None:
    """Legacy 'tokens' field must remain for backward-compat during transition (FRE-351)."""
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_complete_event(calls)
    assert "tokens" in kwargs, (
        "Backward-compat 'tokens' field missing — double-write required during transition. "
        f"Keys present: {sorted(kwargs)}"
    )
