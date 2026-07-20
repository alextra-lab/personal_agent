"""FRE-376 Phase 2 / ADR-0074 §I2 — model client telemetry parity.

Two layers of test:

1. **Helper contract** (``TestCanonicalEmitContract``) — calls
   :func:`emit_model_call_started` / :func:`emit_model_call_completed`
   directly against a mock logger and asserts the kwargs cover the
   canonical field sets defined in
   :data:`personal_agent.telemetry.events.CANONICAL_MODEL_CALL_STARTED_FIELDS`
   /
   :data:`personal_agent.telemetry.events.CANONICAL_MODEL_CALL_COMPLETED_FIELDS`.
   Adding a required field to those frozensets forces this test to fail
   until the helper is updated.

2. **Client wiring** (``TestClientWiring``) — patches the helpers at each
   client's import site and verifies the clients invoke them with the
   right arguments (``trace_ctx``, ``span_id``, ``model``, ``role``,
   ``endpoint``). The clients' transport / cost-gate / cost-tracker
   surface is mocked only to the minimum needed to reach the emit; the
   test does not assert on transport behavior.

Splitting these concerns keeps the contract assertion close to the
helper (where the contract lives) and the wiring assertion close to the
client (where the wiring lives).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.prompt_identity import derive_prompt_identity
from personal_agent.llm_client.telemetry import (
    emit_model_call_completed,
    emit_model_call_started,
)


def _identity() -> Any:
    """Minimal PromptIdentity for direct emit-helper contract tests."""
    return derive_prompt_identity(
        "orchestrator.primary",
        static_prefix="static",
        full_prompt="static\ndynamic",
        component_ids=("operator_stanza",),
    )


from personal_agent.telemetry.events import (
    CANONICAL_MODEL_CALL_COMPLETED_FIELDS,
    CANONICAL_MODEL_CALL_STARTED_FIELDS,
)
from personal_agent.telemetry.trace import SystemTraceContext, TraceContext


def _ctx_with_session() -> TraceContext:
    """TraceContext with non-None session_id and parent_span_id for parity asserts."""
    base = SystemTraceContext.new(
        "telemetry_parity_test", session_id="11111111-1111-1111-1111-111111111111"
    )
    return TraceContext(
        trace_id=base.trace_id,
        parent_span_id="22222222-2222-2222-2222-222222222222",
        user_id=base.user_id,
        session_id=base.session_id,
        kind=base.kind,
    )


# ---------------------------------------------------------------------------
# Layer 1: helper contract
# ---------------------------------------------------------------------------


class TestCanonicalEmitContract:
    """The helpers must cover the canonical field set on every call."""

    def test_started_helper_emits_canonical_fields(self) -> None:
        """``model_call_started`` kwargs cover ``CANONICAL_MODEL_CALL_STARTED_FIELDS``."""
        log = MagicMock()
        ctx = _ctx_with_session()

        emit_model_call_started(
            log=log,
            role="primary",
            model="anthropic/claude-sonnet-4-6",
            endpoint="anthropic",
            provider="anthropic",
            trace_ctx=ctx,
            span_id="33333333-3333-3333-3333-333333333333",
        )

        log.info.assert_called_once()
        event_name, kwargs = log.info.call_args.args[0], log.info.call_args.kwargs
        assert event_name == "model_call_started"
        missing = CANONICAL_MODEL_CALL_STARTED_FIELDS - set(kwargs)
        assert not missing, f"started helper missing canonical fields: {missing}"
        assert kwargs["trace_id"] == ctx.trace_id
        assert kwargs["session_id"] == "11111111-1111-1111-1111-111111111111"
        assert kwargs["span_id"] == "33333333-3333-3333-3333-333333333333"
        assert kwargs["parent_span_id"] == "22222222-2222-2222-2222-222222222222"

    def test_completed_helper_emits_canonical_fields(self) -> None:
        """``model_call_completed`` kwargs cover ``CANONICAL_MODEL_CALL_COMPLETED_FIELDS``."""
        log = MagicMock()
        ctx = _ctx_with_session()

        emit_model_call_completed(
            log=log,
            role="primary",
            model="anthropic/claude-sonnet-4-6",
            endpoint="anthropic",
            provider="anthropic",
            trace_ctx=ctx,
            span_id="33333333-3333-3333-3333-333333333333",
            latency_ms=125,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            prompt_identity=_identity(),
        )

        log.info.assert_called_once()
        event_name, kwargs = log.info.call_args.args[0], log.info.call_args.kwargs
        assert event_name == "model_call_completed"
        missing = CANONICAL_MODEL_CALL_COMPLETED_FIELDS - set(kwargs)
        assert not missing, f"completed helper missing canonical fields: {missing}"
        assert kwargs["input_tokens"] == 100
        assert kwargs["output_tokens"] == 50
        assert kwargs["latency_ms"] == 125
        assert kwargs["prompt_callsite"] == "orchestrator.primary"
        assert kwargs["prompt_component_ids"] == ["operator_stanza"]
        assert len(kwargs["prompt_static_prefix_hash"]) == 16

    def test_completed_helper_drops_legacy_token_aliases(self) -> None:
        """Phase 3 (ADR-0074): ``prompt_tokens`` / ``completion_tokens`` / ``model_id`` aliases removed."""
        log = MagicMock()
        emit_model_call_completed(
            log=log,
            role="primary",
            model="anthropic/claude-sonnet-4-6",
            endpoint="anthropic",
            provider="anthropic",
            trace_ctx=_ctx_with_session(),
            span_id="33333333-3333-3333-3333-333333333333",
            latency_ms=125,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            prompt_identity=_identity(),
        )
        kwargs = log.info.call_args.kwargs
        for legacy in ("prompt_tokens", "completion_tokens", "model_id"):
            assert legacy not in kwargs, f"legacy alias {legacy!r} must not be emitted"
        # Canonical names still present.
        assert kwargs["input_tokens"] == 100
        assert kwargs["output_tokens"] == 50
        assert kwargs["model"] == "anthropic/claude-sonnet-4-6"

    def test_started_helper_drops_model_id_alias(self) -> None:
        """Phase 3: started emit does not co-emit the ``model_id`` alias."""
        log = MagicMock()
        emit_model_call_started(
            log=log,
            role="primary",
            model="anthropic/claude-sonnet-4-6",
            endpoint="anthropic",
            provider="anthropic",
            trace_ctx=_ctx_with_session(),
            span_id="33333333-3333-3333-3333-333333333333",
        )
        kwargs = log.info.call_args.kwargs
        assert "model_id" not in kwargs
        assert kwargs["model"] == "anthropic/claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Layer 2: client wiring
# ---------------------------------------------------------------------------


class TestClientWiring:
    """Each client invokes the helpers with the right ``trace_ctx`` + ``span_id``."""

    @pytest.mark.asyncio
    async def test_litellm_client_calls_both_helpers_with_matched_span(self) -> None:
        """LiteLLMClient calls started + completed helpers with the same span_id."""
        from personal_agent.llm_client.litellm_client import LiteLLMClient
        from personal_agent.llm_client.types import ModelRole

        # Minimal response — content only; usage fields are irrelevant here.
        usage = MagicMock()
        usage.prompt_tokens = 1
        usage.completion_tokens = 1
        usage.total_tokens = 2
        usage.cache_read_input_tokens = None
        usage.cache_creation_input_tokens = None
        usage.prompt_tokens_details = None
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "ok"
        response.choices[0].message.tool_calls = None
        response.usage = usage
        response.id = "resp_wiring"

        mock_gate = MagicMock()
        mock_gate.reserve = AsyncMock(return_value="res-wire")
        mock_gate.commit = AsyncMock()
        mock_tracker = AsyncMock()

        ctx = _ctx_with_session()

        with (
            patch("personal_agent.llm_client.litellm_client.emit_model_call_started") as started,
            patch(
                "personal_agent.llm_client.litellm_client.emit_model_call_completed"
            ) as completed,
            patch("litellm.acompletion", AsyncMock(return_value=response)),
            patch("litellm.completion_cost", return_value=0.0),
            patch("personal_agent.cost_gate.get_default_gate", return_value=mock_gate),
            patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock()),
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
                return_value=MagicMock(anthropic_api_key="k", openai_api_key=None),
            ),
        ):
            client = LiteLLMClient(
                model_id="claude-sonnet-4-6",
                provider="anthropic",
                max_tokens=16,
                budget_role="main_inference",
            )
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=ctx,
            )

        # Both helpers called exactly once.
        started.assert_called_once()
        completed.assert_called_once()

        # The client routed the right canonical args through.
        s_kwargs = started.call_args.kwargs
        c_kwargs = completed.call_args.kwargs
        assert s_kwargs["trace_ctx"] is ctx
        assert c_kwargs["trace_ctx"] is ctx
        assert s_kwargs["model"] == "anthropic/claude-sonnet-4-6"
        assert s_kwargs["role"] == "primary"
        assert s_kwargs["endpoint"] == "anthropic"
        assert s_kwargs["provider"] == "anthropic"
        assert c_kwargs["provider"] == "anthropic"
        # Started and completed must share the span_id so they join.
        assert s_kwargs["span_id"] == c_kwargs["span_id"]
        assert s_kwargs["span_id"]  # non-empty

    @pytest.mark.asyncio
    async def test_litellm_cost_row_model_matches_telemetry_model(self) -> None:
        """ADR-0121 T4 / AC-8 regression guard (codex plan-review finding).

        ``api_costs.model`` must name the same model as the ``model_call_completed``
        event for the same call — codex's plan review caught that ``record_api_call``
        was writing the bare ``model_id`` while telemetry wrote ``provider/model_id``,
        which AC-8's fail clause ("if the recorded model differs from the
        model-call-completed model") explicitly forbids.
        """
        from personal_agent.llm_client.litellm_client import LiteLLMClient
        from personal_agent.llm_client.types import ModelRole

        usage = MagicMock()
        usage.prompt_tokens = 100
        usage.completion_tokens = 50
        usage.total_tokens = 150
        usage.cache_read_input_tokens = None
        usage.cache_creation_input_tokens = None
        usage.prompt_tokens_details = None
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "ok"
        response.choices[0].message.tool_calls = None
        response.usage = usage
        response.id = "resp_cost_match"

        mock_gate = MagicMock()
        mock_gate.reserve = AsyncMock(return_value="res-cost-match")
        mock_gate.commit = AsyncMock()
        mock_tracker = AsyncMock()

        ctx = _ctx_with_session()

        with (
            patch(
                "personal_agent.llm_client.litellm_client.emit_model_call_completed"
            ) as completed,
            patch("litellm.acompletion", AsyncMock(return_value=response)),
            # A definite non-zero cost so record_api_call is guaranteed to fire
            # (the code path only records when cost > 0).
            patch("litellm.completion_cost", return_value=0.05),
            patch("personal_agent.cost_gate.get_default_gate", return_value=mock_gate),
            patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock()),
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
                return_value=MagicMock(anthropic_api_key="k", openai_api_key=None),
            ),
        ):
            client = LiteLLMClient(
                model_id="claude-sonnet-4-6",
                provider="anthropic",
                max_tokens=16,
                budget_role="main_inference",
            )
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=ctx,
            )

        completed.assert_called_once()
        mock_tracker.record_api_call.assert_awaited_once()

        telemetry_model = completed.call_args.kwargs["model"]
        cost_row_model = mock_tracker.record_api_call.call_args.kwargs["model"]
        assert telemetry_model == cost_row_model == "anthropic/claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_local_client_calls_started_with_correct_args(self, tmp_path: Path) -> None:
        """LocalLLMClient invokes ``emit_model_call_started`` correctly.

        We assert only on the started emit so we can short-circuit the call
        by raising from ``httpx`` right after — keeping the test off the
        streaming aggregator and response parser.
        """
        import httpx

        from personal_agent.llm_client.client import LocalLLMClient
        from personal_agent.llm_client.types import LLMTimeout, ModelRole

        config = tmp_path / "models.yaml"
        config.write_text(
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

        ctx = _ctx_with_session()
        client = LocalLLMClient(
            base_url="http://mock-slm.test/v1",
            timeout_seconds=30,
            max_retries=0,
            model_config_path=config,
        )

        with (
            patch("personal_agent.llm_client.client.emit_model_call_started") as started,
            patch("personal_agent.llm_client.client.emit_model_call_completed") as completed,
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            # Force the call to terminate right after the started emit so we
            # don't have to fake the streaming response shape.
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(side_effect=httpx.TimeoutException("stop"))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with pytest.raises(LLMTimeout):
                await client.respond(
                    role=ModelRole.PRIMARY,
                    messages=[{"role": "user", "content": "hi"}],
                    trace_ctx=ctx,
                )

        started.assert_called_once()
        completed.assert_not_called()

        s_kwargs = started.call_args.kwargs
        assert s_kwargs["trace_ctx"] is ctx
        assert s_kwargs["model"] == "test-primary"
        assert s_kwargs["role"] == "primary"
        assert s_kwargs["endpoint"] == "http://mock-slm.test/v1/chat/completions"
        # This fixture's models.yaml declares no `providers:` block, so the
        # ADR-0121 provider-required validator never runs and
        # ModelDefinition.provider is None — the emit helper must fall back
        # to "unknown" rather than crash the chat turn.
        assert s_kwargs["provider"] == "unknown"
        assert s_kwargs["span_id"]  # non-empty


# ---------------------------------------------------------------------------
# Layer 3: Phase 3 cleanup — confirm legacy event names are no longer emitted.
# ---------------------------------------------------------------------------


class TestNoLegacyEvents:
    """ADR-0074 Phase 3: deprecated event names removed from LiteLLMClient."""

    @pytest.mark.asyncio
    async def test_litellm_does_not_emit_legacy_event_names(self) -> None:
        """No ``litellm_request_start`` / ``litellm_request_complete`` in respond()."""
        from personal_agent.llm_client.litellm_client import LiteLLMClient
        from personal_agent.llm_client.types import ModelRole

        usage = MagicMock()
        usage.prompt_tokens = 1
        usage.completion_tokens = 1
        usage.total_tokens = 2
        usage.cache_read_input_tokens = None
        usage.cache_creation_input_tokens = None
        usage.prompt_tokens_details = None
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "ok"
        response.choices[0].message.tool_calls = None
        response.usage = usage
        response.id = "resp_canonical_only"

        mock_gate = MagicMock()
        mock_gate.reserve = AsyncMock(return_value="res-canonical")
        mock_gate.commit = AsyncMock()
        mock_tracker = AsyncMock()

        captured: list[tuple[str, dict[str, Any]]] = []
        mock_log = MagicMock()
        mock_log.info = MagicMock(side_effect=lambda e, **kw: captured.append((e, kw)))
        mock_log.warning = MagicMock()
        mock_log.error = MagicMock()

        with (
            patch("personal_agent.llm_client.litellm_client.log", mock_log),
            patch("litellm.acompletion", AsyncMock(return_value=response)),
            patch("litellm.completion_cost", return_value=0.0),
            patch("personal_agent.cost_gate.get_default_gate", return_value=mock_gate),
            patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock()),
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
                return_value=MagicMock(anthropic_api_key="k", openai_api_key=None),
            ),
        ):
            client = LiteLLMClient(
                model_id="claude-sonnet-4-6",
                provider="anthropic",
                max_tokens=16,
                budget_role="main_inference",
            )
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=_ctx_with_session(),
            )

        names = {e for e, _ in captured}
        assert "litellm_request_start" not in names
        assert "litellm_request_complete" not in names
        # Canonical events still present.
        assert "model_call_started" in names
        assert "model_call_completed" in names
