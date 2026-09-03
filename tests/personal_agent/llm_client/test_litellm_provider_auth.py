"""FRE-1155: LiteLLMClient resolves credential + base_url from the catalog.

Resolves via the catalog's declared ``ProviderDefinition`` (``auth_env`` /
``base_url``) instead of a hardcoded provider-name branch. Each test asserts
against the actual ``litellm.acompletion`` dispatch arguments — not that a
resolution function was called — per the ticket's own acceptance criteria.
"""

# ruff: noqa: D103

from __future__ import annotations

from contextlib import ExitStack
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.models import ModelConfig, Placement, ProviderDefinition
from personal_agent.llm_client.types import LLMClientError, ModelRole
from tests._helpers.trace import make_test_ctx


def _make_mock_response() -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.total_tokens = 15
    usage.cache_read_input_tokens = None
    usage.cache_creation_input_tokens = None
    usage.prompt_tokens_details = None

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "hello"
    response.choices[0].message.tool_calls = None
    response.usage = usage
    response.id = "resp_test"
    return response


def _fixture_config(provider_name: str, provider_def: ProviderDefinition) -> ModelConfig:
    """A standalone catalog holding exactly one provider — no dependency on config/models.yaml."""
    return ModelConfig(providers={provider_name: provider_def}, models={})


async def _call_respond(
    *,
    provider: str,
    catalog: ModelConfig | None,
    settings_mock: MagicMock,
    capture: dict[str, object] | None = None,
) -> AsyncMock:
    """Run LiteLLMClient.respond() with every external touchpoint mocked.

    Returns the ``litellm.acompletion`` mock so callers can inspect the actual
    dispatch arguments it was called with. When ``capture`` is passed, the
    ``acompletion``/``gate`` mocks are stashed into it *before* ``respond()``
    runs, so a caller expecting ``respond()`` to raise can still inspect them
    afterwards (this function's own return value is unreachable on a raise).
    """
    mock_response = _make_mock_response()
    mock_acompletion = AsyncMock(return_value=mock_response)

    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value="res-001")
    mock_gate.commit = AsyncMock()
    mock_gate.refund = AsyncMock()

    if capture is not None:
        capture["acompletion"] = mock_acompletion
        capture["gate"] = mock_gate

    mock_tracker = AsyncMock()
    mock_tracker.connect = AsyncMock()
    mock_tracker.disconnect = AsyncMock()
    mock_tracker.record_api_call = AsyncMock()

    client = LiteLLMClient(
        model_id="test-model",
        provider=provider,
        max_tokens=256,
        budget_role="main_inference",
    )

    patches = [
        patch("litellm.acompletion", mock_acompletion),
        patch("litellm.completion_cost", return_value=0.001),
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
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
            return_value=mock_tracker,
        ),
        patch("personal_agent.config.settings.get_settings", return_value=settings_mock),
    ]
    if catalog is not None:
        patches.append(patch("personal_agent.config.load_model_config", return_value=catalog))

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hi"}],
            trace_ctx=make_test_ctx("litellm_provider_auth"),
        )

    return mock_acompletion


@pytest.mark.asyncio
async def test_declared_auth_env_credential_resolves_in_dispatch_args() -> None:
    """AC1: a provider neither anthropic nor openai resolves its credential.

    The declared credential is resolved and placed on the outbound call —
    asserted from the actual dispatch arguments litellm.acompletion receives.

    Uses ``ovhcloud`` (real, pre-classified for the ADR-0141 D2 egress-guard
    route — see ``test_litellm_client_egress_guard.py``) rather than a wholly
    invented provider name: since that ticket, credential resolution staying
    catalog-driven (asserted here) is necessary but no longer sufficient for
    a provider to dispatch — it must also be classified into an egress-guard
    route mechanism, which a fixture-only ``ProviderDefinition`` cannot grant
    (see ``test_new_provider_unnamed_in_client_source_resolves_credential``
    below for the now-unclassified case).
    """
    provider_name = "ovhcloud"
    catalog = _fixture_config(
        provider_name,
        ProviderDefinition(
            auth_env="acme_cloud_key",
            placement=Placement.CLOUD,
            max_concurrency=10,
        ),
    )
    settings_mock = MagicMock(
        acme_cloud_key="sk-acme-999", anthropic_api_key=None, openai_api_key=None
    )

    mock_acompletion = await _call_respond(
        provider=provider_name, catalog=catalog, settings_mock=settings_mock
    )

    dispatch_kwargs = mock_acompletion.call_args.kwargs
    assert dispatch_kwargs["api_key"] == "sk-acme-999"


@pytest.mark.asyncio
async def test_new_provider_unnamed_in_client_source_resolves_credential() -> None:
    """AC2: adding a provider to the catalog requires no client edit — for auth.

    ``brand_new_vendor`` is a name this test invents — it does not, and must
    not, appear anywhere in litellm_client.py. The credential still resolves
    purely from the catalog lookup, proving the client has no per-provider
    special-casing for auth to defeat.

    Amended by ADR-0141 D2 (FRE-1364): auth/base_url resolution staying
    catalog-driven is necessary but no longer sufficient for dispatch — a
    provider must also be classified into an egress-guard route mechanism
    (a small, explicit, security-reviewed allowlist; fail-closed otherwise,
    "that is a security regression and is not accepted" per the ADR). A
    wholly novel provider like this one is therefore expected to fail at the
    *guard-classification* step specifically, proving auth resolved
    correctly and it is the newer, deliberate check that refuses dispatch —
    not a credential-resolution regression.
    """
    import inspect

    from personal_agent.llm_client import litellm_client as litellm_client_module
    from personal_agent.llm_client.types import LLMClientError

    provider_name = "brand_new_vendor"
    assert provider_name not in inspect.getsource(litellm_client_module), (
        "the client source names the new provider — defeats the point of this test"
    )

    catalog = _fixture_config(
        provider_name,
        ProviderDefinition(
            auth_env="brand_new_vendor_key",
            placement=Placement.CLOUD,
            max_concurrency=5,
        ),
    )
    settings_mock = MagicMock(
        brand_new_vendor_key="sk-bnv-123", anthropic_api_key=None, openai_api_key=None
    )

    capture: dict[str, object] = {}
    # The message itself is the proof: "egress-guard route mechanism" only
    # appears in _build_guarded_client's failure, never in the auth_env /
    # catalog-declaration failures raised earlier in respond() — so a match
    # here means auth resolution ran and succeeded before this later,
    # deliberate check refused the (still-unclassified) provider.
    with pytest.raises(LLMClientError, match="egress-guard route mechanism"):
        await _call_respond(
            provider=provider_name, catalog=catalog, settings_mock=settings_mock, capture=capture
        )
    capture["acompletion"].assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_declared_credential_fails_closed() -> None:
    """AC3: a declared auth_env resolving empty/missing fails closed.

    Refuses to dispatch unauthenticated rather than proceeding by omission
    (the decided behaviour — mirrors is_provider_available()'s existing
    fail-closed treatment of a missing credential as unselectable).
    """
    provider_name = "acme_cloud"
    catalog = _fixture_config(
        provider_name,
        ProviderDefinition(
            auth_env="acme_cloud_key",
            placement=Placement.CLOUD,
            max_concurrency=10,
        ),
    )
    settings_mock = MagicMock(acme_cloud_key=None, anthropic_api_key=None, openai_api_key=None)

    capture: dict[str, object] = {}
    with pytest.raises(LLMClientError, match="acme_cloud_key"):
        await _call_respond(
            provider=provider_name, catalog=catalog, settings_mock=settings_mock, capture=capture
        )

    capture["acompletion"].assert_not_awaited()
    capture["gate"].reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_absent_from_catalog_fails_closed() -> None:
    """A provider name the catalog doesn't declare at all also fails closed.

    Distinct from AC3 (declared auth_env, empty value): here there is no
    catalog entry whatsoever, so there is no way to know the provider's auth
    policy. Reachable via ``ExtractionModelOverride.provider``
    (second_brain/entity_extraction.py), which doesn't validate its provider
    string against the catalog — treating "unknown" the same as "no auth
    needed" would silently reproduce the original bug for that path.
    """
    catalog = ModelConfig(providers={}, models={})
    settings_mock = MagicMock(anthropic_api_key=None, openai_api_key=None)

    capture: dict[str, object] = {}
    with pytest.raises(LLMClientError, match="not declared in the model catalog"):
        await _call_respond(
            provider="totally_uncatalogued",
            catalog=catalog,
            settings_mock=settings_mock,
            capture=capture,
        )

    capture["acompletion"].assert_not_awaited()
    capture["gate"].reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_declared_base_url_used_as_outbound_dispatch_base() -> None:
    """AC4: a provider's declared base_url is used as the outbound base.

    Used for chat dispatch — asserted from the actual dispatch arguments.
    Uses ``ovhcloud`` (see the AC1 test above for why a wholly novel provider
    name no longer reaches a full dispatch, post ADR-0141 D2).
    """
    provider_name = "ovhcloud"
    custom_base = "https://acme-cloud.example.net/v1"
    catalog = _fixture_config(
        provider_name,
        ProviderDefinition(
            base_url=custom_base,
            auth_env="acme_cloud_key",
            placement=Placement.CLOUD,
            max_concurrency=10,
        ),
    )
    settings_mock = MagicMock(
        acme_cloud_key="sk-acme-999", anthropic_api_key=None, openai_api_key=None
    )

    mock_acompletion = await _call_respond(
        provider=provider_name, catalog=catalog, settings_mock=settings_mock
    )

    dispatch_kwargs = mock_acompletion.call_args.kwargs
    assert dispatch_kwargs["api_base"] == custom_base


@pytest.mark.asyncio
async def test_anthropic_and_openai_dispatch_unchanged() -> None:
    """AC5: existing anthropic and openai dispatch is unchanged.

    Their credentials still resolve through the new catalog-lookup path
    (against the real config/models.yaml, not a fixture), with no
    special-casing left behind for either.
    """
    settings_mock = MagicMock(anthropic_api_key="sk-anthropic-1", openai_api_key="sk-openai-1")

    anthropic_call = await _call_respond(
        provider="anthropic", catalog=None, settings_mock=settings_mock
    )
    assert anthropic_call.call_args.kwargs["api_key"] == "sk-anthropic-1"
    assert "api_base" not in anthropic_call.call_args.kwargs

    openai_call = await _call_respond(provider="openai", catalog=None, settings_mock=settings_mock)
    assert openai_call.call_args.kwargs["api_key"] == "sk-openai-1"
    assert "api_base" not in openai_call.call_args.kwargs
