"""ADR-0145 D1 (FRE-1443 AC-1/AC-2) — the factory dispatches an inherit-bound
sub_agent onto whatever the session's primary resolves to.

Before this ticket the factory asked ``get_current_selection("sub_agent")``,
always got ``None``, and fell through to the binding's own static default —
the owner's live-turn complaint (2026-09-06, session ``6a4b1d46``): selecting
``qwen3.8-27b-ovh`` as primary still ran every sub-agent on the local default.

The real catalog does not bind ``sub_agent`` to ``inherit`` yet — that is the
catalog collapse (FRE-1445), which this ticket's own D1 sentinel must exist
before landing (ADR-0145's migration order). So this exercises the factory
end-to-end (path 4 of the ticket's seven) against a fixture catalog with an
inherit-bound sub_agent, monkeypatching only the catalog load — the selection
context, the resolvers, and the client construction are all real.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from personal_agent.config.selection import _current_selection, set_current_selection
from personal_agent.llm_client import factory as factory_module
from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.models import (
    INHERIT_DEPLOYMENT,
    ModelConfig,
    ModelDefinition,
    ModeSpec,
    ProviderDefinition,
    RoleBinding,
)

_LOCAL_DEFAULT = "local_default"
_CLOUD_OVH = "cloud_ovh"
_CLOUD_ANTHROPIC = "cloud_anthropic"

_TRIVIAL_MODES = {"default": ModeSpec()}


def _config() -> ModelConfig:
    return ModelConfig(
        providers={
            "slm_local": ProviderDefinition(
                placement="local", max_concurrency=3, dialect="llamacpp_qwen"
            ),
            "ovhcloud": ProviderDefinition(
                placement="cloud", max_concurrency=50, dialect="ovh_qwen"
            ),
            "anthropic": ProviderDefinition(
                placement="cloud", max_concurrency=50, dialect="anthropic_adaptive"
            ),
        },
        models={
            _LOCAL_DEFAULT: ModelDefinition(
                id="local-default-model",
                provider="slm_local",
                context_length=131072,
                max_concurrency=3,
                default_timeout=90,
                modes=_TRIVIAL_MODES,
                default_mode="default",
            ),
            _CLOUD_OVH: ModelDefinition(
                id="qwen3.8-27b-ovh-wire-id",
                provider="ovhcloud",
                context_length=131072,
                max_concurrency=50,
                default_timeout=180,
                modes=_TRIVIAL_MODES,
                default_mode="default",
            ),
            _CLOUD_ANTHROPIC: ModelDefinition(
                id="claude-sonnet-5",
                provider="anthropic",
                context_length=200000,
                max_concurrency=50,
                default_timeout=180,
                modes=_TRIVIAL_MODES,
                default_mode="default",
            ),
        },
        roles={
            "primary": RoleBinding(deployment=_LOCAL_DEFAULT, open=True),
            "sub_agent": RoleBinding(deployment=INHERIT_DEPLOYMENT),
        },
    )


@pytest.fixture(autouse=True)
def _fixture_catalog(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Swap the factory's catalog load for the fixture, and reset selection."""
    cfg = _config()
    monkeypatch.setattr(factory_module, "load_model_config", lambda: cfg)
    token = _current_selection.set({})
    try:
        yield
    finally:
        _current_selection.reset(token)


class TestSubAgentInheritsThePrimarySelection:
    """AC-1/AC-2 — a selected primary reaches the sub-agent dispatch."""

    def test_ac1_cloud_primary_produces_a_matching_cloud_sub_agent(self) -> None:
        """AC-1: selecting the OVH primary dispatches the sub-agent onto OVH too."""
        set_current_selection({"primary": _CLOUD_OVH})

        primary_client = factory_module.get_llm_client(role_name="primary")
        sub_agent_client = factory_module.get_llm_client(role_name="sub_agent")

        assert isinstance(sub_agent_client, LiteLLMClient)
        assert sub_agent_client.model_id == primary_client.model_id
        assert sub_agent_client.model_id == "qwen3.8-27b-ovh-wire-id"

    def test_ac2_a_second_unrelated_primary_also_reaches_the_sub_agent(self) -> None:
        """AC-2: not special-cased to one primary — repeated with claude_sonnet."""
        set_current_selection({"primary": _CLOUD_ANTHROPIC})

        primary_client = factory_module.get_llm_client(role_name="primary")
        sub_agent_client = factory_module.get_llm_client(role_name="sub_agent")

        assert isinstance(sub_agent_client, LiteLLMClient)
        assert sub_agent_client.model_id == primary_client.model_id
        assert sub_agent_client.model_id == "claude-sonnet-5"

    def test_no_selection_sub_agent_gets_primarys_configured_default(self) -> None:
        """Baseline: no selection at all still resolves through inherit, not the literal."""
        sub_agent_client = factory_module.get_llm_client(role_name="sub_agent")

        assert isinstance(sub_agent_client, LiteLLMClient)
        assert sub_agent_client.model_id == "local-default-model"

    def test_sub_agent_never_dispatches_against_the_literal_sentinel(self) -> None:
        set_current_selection({"primary": _CLOUD_OVH})
        sub_agent_client = factory_module.get_llm_client(role_name="sub_agent")
        assert sub_agent_client.model_key != INHERIT_DEPLOYMENT
