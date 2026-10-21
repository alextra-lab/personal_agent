"""FRE-376 Phase 3 / FRE-351 (rebased): canonical model_call_completed emit fields.

After FRE-376 Phase 3 removed the ``litellm_request_complete`` event and its
legacy field aliases (``prompt_tokens``, ``completion_tokens``, ``tokens``,
``cache_write_tokens``), these tests assert the canonical
``model_call_completed`` event payload includes ``input_tokens``,
``output_tokens``, ``total_tokens``, ``endpoint``, and
``cache_creation_input_tokens``. ADR-0129 D3 / FRE-1067 additionally retired
``latency_ms`` from this payload — span duration is intrinsic to the
model-call span now, so there is no elapsed-time field left to assert here.

The Phase 2 contract (canonical-field frozenset) is enforced in
``test_telemetry_parity.py``; this file complements it with a runtime
wire-up check on LiteLLMClient.respond().
"""

# ruff: noqa: D103

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# FRE-693: session_id must be UUID-parseable — gate.reserve() now does
# UUID(trace_ctx.session_id) to thread it onto the reservation row (ADR-0074
# §8c), matching production reality (every real session_id is a UUID).
_FAILING_TEST_SESSION_ID = "5b6b3f5a-6f5a-4b1a-9b1a-5b6b3f5a6f5a"


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


async def _call_respond(
    captured_log_calls: list[tuple],
    response: MagicMock | None = None,
    *,
    api_key: str = "test-key",
    cost_rows: list[dict] | None = None,
) -> None:
    """Run LiteLLMClient.respond() with all external I/O mocked.

    The log.info calls are captured into captured_log_calls as (event, kwargs)
    tuples. ``response`` replaces the default mock provider response.
    ``api_key`` is the credential the mocked settings resolve. When
    ``cost_rows`` is given, the kwargs of each ``record_api_call`` land in it.
    """
    from personal_agent.llm_client.litellm_client import LiteLLMClient
    from personal_agent.llm_client.types import ModelRole
    from personal_agent.telemetry.trace import TraceContext
    from tests._helpers.trace import make_test_ctx

    mock_response = response if response is not None else _make_mock_response()

    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value="res-001")
    mock_gate.commit = AsyncMock()

    mock_tracker = AsyncMock()
    mock_tracker.connect = AsyncMock()
    mock_tracker.disconnect = AsyncMock()
    mock_tracker.record_api_call = AsyncMock(
        side_effect=lambda **kw: cost_rows.append(kw) if cost_rows is not None else None
    )

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
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
            return_value=mock_tracker,
        ),
        patch(
            "personal_agent.config.settings.get_settings",
            return_value=MagicMock(anthropic_api_key=api_key, openai_api_key=None),
        ),
    ):
        await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hello"}],
            # A cost row needs a session id; system contexts carry none.
            trace_ctx=(
                make_test_ctx("litellm_emit_payload")
                if cost_rows is None
                else TraceContext.new_trace(session_id=_FAILING_TEST_SESSION_ID)
            ),
        )


def _get_completed_event(calls: list[tuple]) -> dict:
    """Return kwargs from the canonical ``model_call_completed`` log call."""
    for event, kwargs in calls:
        if event == "model_call_completed":
            return kwargs
    raise AssertionError(f"model_call_completed event not found in calls: {[e for e, _ in calls]}")


@pytest.mark.asyncio
async def test_model_call_completed_records_what_the_response_reported() -> None:
    """FRE-1588 AC-1: the engine fields hold the response's own values."""
    response = _make_mock_response()
    response.system_fingerprint = "b9999"
    response.model = "m-test"
    calls: list[tuple] = []
    await _call_respond(calls, response)
    kwargs = _get_completed_event(calls)
    assert kwargs["system_fingerprint"] == "b9999"
    assert kwargs["response_model"] == "m-test"
    # The event's own `model` stays the requested name (cost rows join on it).
    assert kwargs["model"] != "m-test"


@pytest.mark.asyncio
async def test_model_call_completed_records_null_when_response_reports_nothing() -> None:
    """FRE-1588 AC-1: no reported value reads as null, never as configuration."""
    response = _make_mock_response()
    response.system_fingerprint = None
    response.model = None
    calls: list[tuple] = []
    await _call_respond(calls, response)
    kwargs = _get_completed_event(calls)
    assert "system_fingerprint" in kwargs
    assert "response_model" in kwargs
    assert kwargs["system_fingerprint"] is None
    assert kwargs["response_model"] is None


@pytest.mark.asyncio
async def test_model_call_completed_never_records_a_non_string_engine_value() -> None:
    """FRE-1588: an attribute that is not a string (a mock's auto-attribute) is not data."""
    calls: list[tuple] = []
    await _call_respond(calls)  # default mock leaves both attributes as auto-MagicMocks
    kwargs = _get_completed_event(calls)
    assert kwargs["system_fingerprint"] is None
    assert kwargs["response_model"] is None


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
async def test_model_call_completed_excludes_latency_ms() -> None:
    """ADR-0129 D3 / FRE-1067 AC-9: span duration is intrinsic — no second
    elapsed-time field survives on the converted model-call path.
    """
    calls: list[tuple] = []
    await _call_respond(calls)
    kwargs = _get_completed_event(calls)
    assert "latency_ms" not in kwargs
    assert "duration_ms" not in kwargs


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


def _anthropic_usage(*, cache_read: int) -> object:
    """The usage object litellm builds from a recorded Anthropic response.

    Mirrors FRE-1638's trace: 7,069 uncached + 15,972 cache-read input tokens.
    litellm reports the cache read twice here: as ``cache_read_input_tokens``
    and as ``prompt_tokens_details.cached_tokens``.
    """
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    return AnthropicConfig().calculate_usage(
        usage_object={
            "input_tokens": 7069,
            "output_tokens": 507,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": 0,
        },
        reasoning_content=None,
    )


@pytest.mark.asyncio
async def test_anthropic_cache_read_is_recorded_once() -> None:
    """FRE-1638 AC-1: litellm's two names for one Anthropic cache read do not add up."""
    response = _make_mock_response()
    response.usage = _anthropic_usage(cache_read=15972)
    calls: list[tuple] = []
    cost_rows: list[dict] = []
    await _call_respond(calls, response, cost_rows=cost_rows)
    kwargs = _get_completed_event(calls)
    assert kwargs["cache_read_tokens"] == 15972
    assert kwargs["cache_read_tokens"] <= kwargs["input_tokens"]
    # The api_costs row takes the same value from the same source.
    [row] = cost_rows
    assert row["cache_read_input_tokens"] == 15972


@pytest.mark.asyncio
async def test_openai_chat_cache_read_keeps_its_value() -> None:
    """FRE-1638 AC-2: OpenAI Chat Completions reports the cache read only in the details."""
    from litellm.types.utils import PromptTokensDetailsWrapper, Usage

    response = _make_mock_response()
    response.usage = Usage(
        prompt_tokens=71479,
        completion_tokens=20,
        total_tokens=71499,
        prompt_tokens_details=PromptTokensDetailsWrapper(cached_tokens=59312),
    )
    calls: list[tuple] = []
    await _call_respond(calls, response)
    assert _get_completed_event(calls)["cache_read_tokens"] == 59312


@pytest.mark.asyncio
async def test_responses_route_cache_read_keeps_its_value() -> None:
    """FRE-1638 AC-2: the Responses route maps input_tokens_details to the same details field."""
    from litellm.types.llms.openai import ResponsesAPIResponse

    from personal_agent.llm_client.responses_route import to_chat_response

    responses_reply = ResponsesAPIResponse(
        id="resp_1",
        object="response",
        created_at=1760900000,
        status="completed",
        model="gpt-6.1-sol-2026-09-29",
        output=[],
        usage={
            "input_tokens": 50,
            "output_tokens": 20,
            "total_tokens": 70,
            "input_tokens_details": {"cached_tokens": 10},
            "output_tokens_details": {"reasoning_tokens": 8},
        },
    )
    response = _make_mock_response()
    response.usage = to_chat_response(responses_reply).usage
    calls: list[tuple] = []
    await _call_respond(calls, response)
    assert _get_completed_event(calls)["cache_read_tokens"] == 10


@pytest.mark.asyncio
async def test_legacy_litellm_events_are_not_emitted() -> None:
    """ADR-0074 Phase 3: legacy ``litellm_request_*`` events are gone."""
    calls: list[tuple] = []
    await _call_respond(calls)
    event_names = {event for event, _ in calls}
    assert "litellm_request_start" not in event_names
    assert "litellm_request_complete" not in event_names


@pytest.mark.asyncio
async def test_gate_reserve_and_commit_receive_session_id() -> None:
    """FRE-693 (ADR-0074 §8c): the cost-gate reserve/commit calls thread session_id."""
    from uuid import UUID, uuid4

    from personal_agent.llm_client.litellm_client import LiteLLMClient
    from personal_agent.llm_client.types import ModelRole
    from personal_agent.telemetry.trace import TraceContext

    session_id = str(uuid4())
    mock_response = _make_mock_response()

    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value="res-001")
    mock_gate.commit = AsyncMock()

    mock_tracker = AsyncMock()
    mock_tracker.connect = AsyncMock()
    mock_tracker.disconnect = AsyncMock()
    mock_tracker.record_api_call = AsyncMock()

    client = LiteLLMClient(
        model_id="claude-sonnet-4-6",
        provider="anthropic",
        max_tokens=256,
        budget_role="main_inference",
    )

    with (
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
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
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
            trace_ctx=TraceContext.new_trace(session_id=session_id),
        )

    reserve_kwargs = mock_gate.reserve.call_args.kwargs
    assert reserve_kwargs["session_id"] == UUID(session_id)
    assert reserve_kwargs["task_id"] is None

    commit_kwargs = mock_gate.commit.call_args.kwargs
    assert commit_kwargs["session_id"] == session_id


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
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
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
                trace_ctx=TraceContext.new_trace(session_id=_FAILING_TEST_SESSION_ID),
            )


@pytest.mark.asyncio
async def test_litellm_request_failed_includes_session_id() -> None:
    """FRE-552: litellm_request_failed carries session_id from the trace context."""
    calls: list[tuple] = []
    await _call_respond_failing(calls)
    failed = [kwargs for event, kwargs in calls if event == "litellm_request_failed"]
    assert failed, f"litellm_request_failed not found in: {[e for e, _ in calls]}"
    assert failed[0]["session_id"] == _FAILING_TEST_SESSION_ID


# ── FRE-1614: prompt debug capture at the cloud (non-streaming) site ──────────

_SEEDED_API_KEY = "sk-ant-seeded-FRE1614-cloud-site"


@pytest.mark.asyncio
async def test_cloud_site_writes_the_full_request_when_capture_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3 (non-streaming site) and AC-2: one file, the request, no credential."""
    from personal_agent.config import settings
    from personal_agent.telemetry import prompt_debug

    monkeypatch.setattr(settings, "prompt_debug_capture_enabled", True)
    monkeypatch.setattr(prompt_debug, "prompt_debug_root", lambda: tmp_path)

    calls: list[tuple] = []
    await _call_respond(calls, api_key=_SEEDED_API_KEY)

    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1
    raw = files[0].read_text(encoding="utf-8")
    assert _SEEDED_API_KEY not in raw
    record = json.loads(raw)
    assert record["role"] == "primary"
    assert record["placement"] == "cloud"
    assert record["messages"][-1]["role"] == "user"
    assert record["usage"]["prompt_tokens"] == 100
    assert record["params"]["max_tokens"] == 256
    assert "api_key" not in record["params"]
    assert record["trace_id"] == _get_completed_event(calls)["trace_id"]


@pytest.mark.asyncio
async def test_cloud_site_writes_nothing_when_capture_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1 (non-streaming site): the flag off means no file and no capture call."""
    from personal_agent.config import settings
    from personal_agent.telemetry import prompt_debug

    monkeypatch.setattr(settings, "prompt_debug_capture_enabled", False)
    monkeypatch.setattr(prompt_debug, "prompt_debug_root", lambda: tmp_path)
    capture = AsyncMock()
    monkeypatch.setattr(prompt_debug, "capture_model_call", capture)

    await _call_respond([])

    capture.assert_not_called()
    assert list(tmp_path.rglob("*")) == []
