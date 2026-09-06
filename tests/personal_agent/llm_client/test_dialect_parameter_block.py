"""FRE-1440 / ADR-0145 D3a+D3b — the client builds its parameter block from the dialect.

Every assertion here reads the keyword arguments that actually reach
``litellm.acompletion``. That boundary is the criterion the ticket names, and it
is chosen against two weaker ones the ticket rules out by name: billed tokens are
stochastic, and a logged declaration is exactly the present defect — a client that
logs a value and omits it (``litellm_client.py`` reasoning_declaration_undeliverable).

The one criterion that cannot be met here is AC-2, which asks what the provider
*bills*. That needs a real OVH call and is recorded on the ticket.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config import load_model_config
from personal_agent.llm_client.models import Dialect, ModelDefinition, ModeSpec, Placement
from personal_agent.llm_client.types import DialectParameterRejected, ModelRole
from tests._helpers.litellm_capability import pinned_litellm_capabilities
from tests._helpers.trace import make_test_ctx

#: Every sampler name a dialect can accept. Asserting absence against this whole
#: set is what makes AC-3 a claim about the dialect rather than about one field.
_ALL_SAMPLERS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "frequency_penalty",
    "seed",
)


@pytest.fixture(autouse=True)
def _pin_capabilities() -> Iterator[None]:
    """Litellm's GitHub-fetched capability map must not decide these results."""
    with pinned_litellm_capabilities():
        yield


def _mock_response() -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.total_tokens = 15
    usage.cache_read_input_tokens = None
    usage.cache_creation_input_tokens = None
    usage.prompt_tokens_details = None

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "ok"
    response.choices[0].message.tool_calls = None
    response.usage = usage
    response.id = "resp_fre1440"
    return response


def _dispatch_stack(captured: dict[str, Any]) -> list[Any]:
    """Patches that isolate dispatch from cost, credentials and telemetry."""

    async def _fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _mock_response()

    gate = MagicMock()
    gate.reserve = AsyncMock(return_value="res-fre1440")
    gate.commit = AsyncMock()
    gate.refund = AsyncMock()

    tracker = AsyncMock()
    tracker.connect = AsyncMock()
    tracker.record_api_call = AsyncMock()

    return [
        patch("litellm.acompletion", side_effect=_fake_acompletion),
        patch("litellm.completion_cost", return_value=0.001),
        patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
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
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
            return_value=tracker,
        ),
        patch(
            "personal_agent.config.settings.get_settings",
            return_value=MagicMock(anthropic_api_key="k", openai_api_key="k", ovh_api_key="k"),
        ),
    ]


async def _capture_from_key(model_key: str, **respond_kwargs: Any) -> dict[str, Any]:
    """Dispatch through the factory door and capture litellm's keyword arguments."""
    from personal_agent.llm_client.factory import get_llm_client_for_key

    captured: dict[str, Any] = {}
    client = get_llm_client_for_key(model_key, budget_role="captains_log")
    with _apply(_dispatch_stack(captured)):
        await client.respond(
            role=ModelRole.SESSION_SUMMARY,
            messages=[{"role": "user", "content": "go"}],
            trace_ctx=make_test_ctx("fre1440"),
            **respond_kwargs,
        )
    return captured


async def _capture_from_definition(
    definition: ModelDefinition, *, model_key: str, **respond_kwargs: Any
) -> dict[str, Any]:
    """Same capture, for a definition assembled in the test rather than bound by a role."""
    from personal_agent.llm_client.litellm_client import LiteLLMClient

    captured: dict[str, Any] = {}
    client = LiteLLMClient(
        model_id=definition.id,
        provider=definition.provider or "",
        max_tokens=definition.max_tokens or 1024,
        budget_role="captains_log",
        placement=Placement.CLOUD,
        model_def=definition,
        model_key=model_key,
    )
    with _apply(_dispatch_stack(captured)):
        await client.respond(
            role=ModelRole.SESSION_SUMMARY,
            messages=[{"role": "user", "content": "go"}],
            trace_ctx=make_test_ctx("fre1440"),
            **respond_kwargs,
        )
    return captured


class _apply:
    """Enter a list of patches as one context manager."""

    def __init__(self, patches: list[Any]) -> None:
        self._patches = patches

    def __enter__(self) -> None:
        for item in self._patches:
            item.__enter__()

    def __exit__(self, *exc: Any) -> None:
        for item in reversed(self._patches):
            item.__exit__(*exc)


def _with_mode(model_key: str, mode: ModeSpec) -> ModelDefinition:
    """The real catalog entry, carrying one declared mode instead of its own."""
    definition = load_model_config().models[model_key]
    return definition.model_copy(update={"modes": {"only": mode}, "default_mode": "only"})


class TestDeclaredEffortReachesOvh:
    """AC-1 — the declared effort reaches the wire on a pass-through provider."""

    @pytest.mark.asyncio
    async def test_effort_and_the_forwarding_kwarg_are_both_dispatched(self) -> None:
        """Litellm's ovhcloud map has no reasoning_effort, so the kwarg must carry it (F8/F9)."""
        definition = _with_mode(
            "qwen3.8-27b-ovh", ModeSpec(reasoning_effort="none", temperature=1.0)
        )
        captured = await _capture_from_definition(definition, model_key="qwen3.8-27b-ovh")

        assert captured["reasoning_effort"] == "none"
        assert captured["allowed_openai_params"] == ["reasoning_effort"]

    @pytest.mark.asyncio
    async def test_the_declared_sampler_travels_with_it(self) -> None:
        """ovh_qwen accepts temperature; the mode's value is the dispatched value."""
        definition = _with_mode(
            "qwen3.8-27b-ovh", ModeSpec(reasoning_effort="none", temperature=1.0)
        )
        captured = await _capture_from_definition(definition, model_key="qwen3.8-27b-ovh")

        assert captured["temperature"] == 1.0

    @pytest.mark.asyncio
    async def test_a_dialect_without_the_lever_sends_neither(self) -> None:
        """The forwarding kwarg is not a blanket addition — Anthropic is SDK-mapped (D3b)."""
        captured = await _capture_from_key("claude_sonnet")

        assert captured["reasoning_effort"] == "high"
        assert "allowed_openai_params" not in captured


class TestNoSamplerReachesSonnet:
    """AC-3 — no sampler reaches anthropic_adaptive, on either Sonnet path."""

    @pytest.mark.asyncio
    async def test_the_factory_path_sends_no_sampler(self) -> None:
        """anthropic_adaptive's accepted set is empty; Sonnet 5 400s on any of them (F1/F6)."""
        captured = await _capture_from_key("claude_sonnet")

        assert [field for field in _ALL_SAMPLERS if field in captured] == []

    @pytest.mark.asyncio
    async def test_the_reflection_fallback_sends_no_sampler(self) -> None:
        """The live latent defect: reflection sent temperature=0.3 to a model that rejects it.

        Driven through ``generate_reflection_entry`` itself with DSPy unavailable,
        because the ticket rules out adjudicating this from a completed Sonnet
        turn — the factory path already sends no sampler, so such a turn passes
        while this path still leaks.
        """
        from personal_agent.captains_log import reflection

        captured: dict[str, Any] = {}
        with (
            patch.object(reflection, "DSPY_AVAILABLE", False),
            patch(
                "personal_agent.captains_log.reflection._fetch_trace_events",
                AsyncMock(return_value=[]),
            ),
            patch(
                "personal_agent.captains_log.reflection.load_mean_rating_lookup",
                AsyncMock(return_value={}),
            ),
            patch("personal_agent.config.resolve_role_model_key", return_value="claude_sonnet"),
            _apply(_dispatch_stack(captured)),
        ):
            await reflection.generate_reflection_entry(
                user_message="hi",
                trace_id="trace-fre1440",
                steps_count=1,
                final_state="COMPLETED",
                reply_length=5,
            )

        assert captured, "the fallback never dispatched — the test proves nothing"
        assert [field for field in _ALL_SAMPLERS if field in captured] == []


class TestRejectedCallSiteSamplerRaises:
    """AC-4 — a rejected call-site sampler raises before dispatch, and names its terms."""

    @pytest.mark.asyncio
    async def test_top_p_to_anthropic_adaptive_raises(self) -> None:
        """Not dropped with a log: a masked invalid request is what drop_params: off prevents."""
        from personal_agent.llm_client.factory import get_llm_client_for_key

        captured: dict[str, Any] = {}
        client = get_llm_client_for_key("claude_sonnet", budget_role="captains_log")
        with _apply(_dispatch_stack(captured)), pytest.raises(DialectParameterRejected) as excinfo:
            await client.respond(
                role=ModelRole.SESSION_SUMMARY,
                messages=[{"role": "user", "content": "go"}],
                trace_ctx=make_test_ctx("fre1440"),
                top_p=0.9,
            )

        message = str(excinfo.value)
        assert "top_p" in message
        assert Dialect.ANTHROPIC_ADAPTIVE.value in message
        assert not captured, "the call must not reach the provider"

    @pytest.mark.asyncio
    async def test_temperature_to_anthropic_adaptive_raises(self) -> None:
        """The field reflection used to send, now refused rather than dispatched."""
        from personal_agent.llm_client.factory import get_llm_client_for_key

        captured: dict[str, Any] = {}
        client = get_llm_client_for_key("claude_sonnet", budget_role="captains_log")
        with _apply(_dispatch_stack(captured)), pytest.raises(DialectParameterRejected):
            await client.respond(
                role=ModelRole.SESSION_SUMMARY,
                messages=[{"role": "user", "content": "go"}],
                trace_ctx=make_test_ctx("fre1440"),
                temperature=0.3,
            )
        assert not captured

    @pytest.mark.asyncio
    async def test_reasoning_effort_to_anthropic_budget_raises(self) -> None:
        """anthropic_budget's lever is budget_tokens; litellm's legacy mapping also rewrites
        max_tokens (F5), so routing an effort through it is a silent budget change.
        """
        from personal_agent.llm_client.factory import get_llm_client_for_key

        captured: dict[str, Any] = {}
        client = get_llm_client_for_key("claude_haiku", budget_role="captains_log")
        with _apply(_dispatch_stack(captured)), pytest.raises(DialectParameterRejected):
            await client.respond(
                role=ModelRole.SESSION_SUMMARY,
                messages=[{"role": "user", "content": "go"}],
                trace_ctx=make_test_ctx("fre1440"),
                reasoning_effort="low",
            )
        assert not captured

    @pytest.mark.asyncio
    async def test_no_dialect_means_no_gate(self) -> None:
        """A definition-less construction keeps today's behaviour (ADR-0145 D3b fallback)."""
        from personal_agent.llm_client.litellm_client import LiteLLMClient

        captured: dict[str, Any] = {}
        client = LiteLLMClient(
            model_id="claude-sonnet-5",
            provider="anthropic",
            max_tokens=64,
            budget_role="captains_log",
        )
        with _apply(_dispatch_stack(captured)):
            await client.respond(
                role=ModelRole.SESSION_SUMMARY,
                messages=[{"role": "user", "content": "go"}],
                trace_ctx=make_test_ctx("fre1440"),
                temperature=0.3,
            )
        assert captured["temperature"] == 0.3


class TestAcceptedCallSitesStillWork:
    """AC-5 — the gate must not break a call that is valid today."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("temperature", [0.0, 0.2])
    async def test_openai_gpt5_still_takes_a_call_site_temperature(
        self, temperature: float
    ) -> None:
        """entity_extraction (0.0) and context_compressor (0.2) both bind gpt-5.4-mini."""
        captured = await _capture_from_key("gpt-5.4-mini", temperature=temperature)

        assert captured["temperature"] == temperature

    @pytest.mark.asyncio
    async def test_anthropic_budget_still_takes_a_call_site_temperature(self) -> None:
        """skills.py routes through claude_haiku, whose dialect accepts temperature (F6)."""
        captured = await _capture_from_key("claude_haiku", temperature=0.0)

        assert captured["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_openai_gpt5_still_takes_a_call_site_effort(self) -> None:
        """entity_extraction forwards the resolved effort explicitly."""
        captured = await _capture_from_key("gpt-5.4-mini", reasoning_effort="none")

        assert captured["reasoning_effort"] == "none"


class TestCloudBranchReadsTheDefinition:
    """AC-6 — the cloud branch reads the mode, not only what a caller passes."""

    @pytest.mark.asyncio
    async def test_declared_temperature_is_dispatched_without_a_caller(self) -> None:
        """Today's behaviour is absence: no catalog temperature has reached a cloud model."""
        captured = await _capture_from_key("gpt-5.4-mini")

        assert captured["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_a_caller_value_still_wins_over_the_declaration(self) -> None:
        """An explicit argument is a deliberate override, not a competitor."""
        captured = await _capture_from_key("gpt-5.4-mini", temperature=0.2)

        assert captured["temperature"] == 0.2
