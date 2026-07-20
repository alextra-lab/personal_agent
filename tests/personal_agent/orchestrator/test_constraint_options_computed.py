"""Tests for the catalog-backed computed-options decision type (ADR-0122 §3 / FRE-881).

Proves AC-6: the artifact_builder option set equals exactly the availability-filtered
set of catalog deployments of ``kind: llm`` — asserted in both directions — and the
settings-validation surface consults the catalog rather than the static registry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from personal_agent.llm_client.models import (
    ModelConfig,
    ModelDefinition,
    ModelKind,
    Placement,
    ProviderDefinition,
    RoleBinding,
)
from personal_agent.orchestrator import constraint_options as co


def _provider(
    *, auth_env: str | None, placement: Placement = Placement.CLOUD
) -> ProviderDefinition:
    return ProviderDefinition(auth_env=auth_env, placement=placement, max_concurrency=10)


def _model(
    *,
    provider: str,
    kind: ModelKind = ModelKind.LLM,
    summary: str = "",
    input_cost: float | None = None,
    output_cost: float | None = None,
    context_length: int = 8192,
    max_tokens: int | None = None,
    dimensions: int | None = None,
) -> ModelDefinition:
    return ModelDefinition(
        id=f"vendor/{provider}",
        provider=provider,
        kind=kind,
        summary=summary,
        input_cost_per_token=input_cost,
        output_cost_per_token=output_cost,
        context_length=context_length,
        max_tokens=max_tokens,
        dimensions=dimensions,
        max_concurrency=4,
        default_timeout=30,
    )


def _catalog() -> ModelConfig:
    """A 3-llm (2 providers) + 1-embedding catalog with artifact_builder bound local."""
    return ModelConfig(
        providers={
            "local_p": _provider(auth_env=None, placement=Placement.LOCAL),
            "cloud_up": _provider(auth_env="cloud_up_key"),
            "cloud_down": _provider(auth_env="cloud_down_key"),
        },
        models={
            "m_local": _model(provider="local_p"),
            "m_cloud_up": _model(
                provider="cloud_up",
                summary="fast cloud builder",
                input_cost=0.000003,
                output_cost=0.000015,
                context_length=200000,
                max_tokens=32768,
            ),
            "m_cloud_down": _model(provider="cloud_down"),
            "m_embed": _model(provider="cloud_up", kind=ModelKind.EMBEDDING, dimensions=1024),
        },
        roles={"artifact_builder": RoleBinding(deployment="m_local", open=True)},
    )


# ── AC-6 core: option set == availability-filtered kind:llm deployments, both ways ──


def test_computed_options_equal_available_llm_deployments_both_directions() -> None:
    config = _catalog()
    # cloud_down provider is unavailable; local_p + cloud_up are up.
    available = {"local_p", "cloud_up"}
    options = co.compute_artifact_builder_options(
        config, is_provider_available=lambda p: p in available
    )
    keys = {o.action_id for o in options}

    # forward: only available llm deployments are present
    assert keys == {"m_local", "m_cloud_up"}
    # reverse, three failure modes each individually asserted:
    assert "m_cloud_down" not in keys  # a deployment whose provider is down is absent
    assert "m_embed" not in keys  # a non-llm deployment never leaks in
    assert "m_cloud_up" in keys  # an available one is present
    # no non-catalog key can leak in
    assert keys <= set(config.models)


def test_computed_options_empty_when_role_pinned() -> None:
    """A pinned role offers nothing — matches resolve_artifact_builder_key's own
    open-role requirement (is_selectable_binding), so a pinned config never offers
    a card selection that would silently be overridden to the default.
    """
    pinned = _catalog()
    pinned.roles["artifact_builder"] = RoleBinding(deployment="m_local", open=False)
    options = co.compute_artifact_builder_options(pinned, is_provider_available=lambda _p: True)
    assert options == []


def test_computed_option_carries_catalog_display_detail() -> None:
    """The card detail (cost, context, max output, summary) is projected from the catalog."""
    config = _catalog()
    options = co.compute_artifact_builder_options(config, is_provider_available=lambda _p: True)
    by_key = {o.action_id: o for o in options}

    up = by_key["m_cloud_up"]
    assert up.label == "m_cloud_up"
    assert up.summary == "fast cloud builder"
    assert up.input_cost_per_token == pytest.approx(0.000003)
    assert up.output_cost_per_token == pytest.approx(0.000015)
    assert up.context_length == 200000
    assert up.max_output_tokens == 32768


# ── availability predicate ──


def test_build_provider_availability_predicate() -> None:
    config = _catalog()
    settings = SimpleNamespace(cloud_up_key="sk-present", cloud_down_key="")
    available = co.build_provider_availability(config, settings)  # type: ignore[arg-type]

    assert available("local_p") is True  # no-auth (local tunnel) provider
    assert available("cloud_up") is True  # credential present
    assert available("cloud_down") is False  # credential empty
    assert available("nonexistent") is False  # dangling → fail closed


def test_artifact_builder_default_key_is_the_binding_deployment() -> None:
    assert co.artifact_builder_default_key(_catalog()) == "m_local"


def test_artifact_builder_default_key_raises_when_unbound() -> None:
    """No artifact_builder binding → raise, never return a non-deployment role name."""
    from personal_agent.config.model_loader import ModelConfigError

    unbound = ModelConfig(
        providers={"local_p": _provider(auth_env=None, placement=Placement.LOCAL)},
        models={"m_local": _model(provider="local_p")},
        roles={},
    )
    with pytest.raises(ModelConfigError):
        co.artifact_builder_default_key(unbound)


# ── resolve_artifact_builder_key — fail-closed catalog check (ADR-0122 §4, AC-4) ──


def test_resolve_artifact_builder_key_accepts_valid_available_key() -> None:
    config = _catalog()
    resolved = co.resolve_artifact_builder_key(
        "m_cloud_up", config, is_provider_available=lambda p: p in {"local_p", "cloud_up"}
    )
    assert resolved == "m_cloud_up"


def test_resolve_artifact_builder_key_falls_back_on_unknown_key() -> None:
    """AC-4(a): a key absent from the catalog substitutes the configured default."""
    config = _catalog()
    resolved = co.resolve_artifact_builder_key(
        "not-a-real-model", config, is_provider_available=lambda _p: True
    )
    assert resolved == "m_local"  # the binding's own default, never an arbitrary model


def test_resolve_artifact_builder_key_falls_back_on_embedding_kind() -> None:
    """AC-4(b): a catalog key of kind embedding substitutes the configured default."""
    config = _catalog()
    resolved = co.resolve_artifact_builder_key(
        "m_embed", config, is_provider_available=lambda _p: True
    )
    assert resolved == "m_local"


def test_resolve_artifact_builder_key_falls_back_on_unavailable_provider() -> None:
    """AC-4(c): a valid, kind-compatible key whose provider is down substitutes the default."""
    config = _catalog()
    resolved = co.resolve_artifact_builder_key(
        "m_cloud_down", config, is_provider_available=lambda p: p != "cloud_down"
    )
    assert resolved == "m_local"


def test_resolve_artifact_builder_key_never_revalidates_the_fallback_default() -> None:
    """The substituted default is unconditional.

    It is the trusted binding, not itself subject to the availability check
    applied to the requested key.
    """
    config = _catalog()
    resolved = co.resolve_artifact_builder_key(
        "m_embed", config, is_provider_available=lambda _p: False
    )
    assert resolved == "m_local"


# ── settings-validation surface (consults catalog, unfiltered by availability) ──


def test_valid_preference_actions_for_artifact_builder_uses_catalog_llm_keys() -> None:
    config = _catalog()
    actions = co._valid_preference_actions_for_config("artifact_builder", config)
    # every kind:llm catalog key is valid regardless of availability (a saved
    # preference survives a transient outage); non-llm keys are not.
    assert actions == {"always_pause", "m_local", "m_cloud_up", "m_cloud_down"}
    assert "m_embed" not in actions


def test_is_known_constraint_admits_static_and_computed() -> None:
    assert co.is_known_constraint("tool_iteration_limit") is True
    assert co.is_known_constraint("attachment_cost") is True
    assert co.is_known_constraint("artifact_builder") is True
    assert co.is_known_constraint("nope") is False


def test_static_constraints_unchanged() -> None:
    assert co.option_ids("tool_iteration_limit") == ["continue_10", "finish_now"]
    assert co.default_action_id("context_compression") == "stop_here"
    opts, default = co.resolve_options_and_default("tool_iteration_limit")
    assert opts == ["continue_10", "finish_now"]
    assert default == "finish_now"


# ── live wiring against the real catalog (env-independent assertions) ──


def test_resolve_options_and_default_artifact_builder_against_real_catalog() -> None:
    """The computed path dispatches to the real catalog; local llm keys always present."""
    opts, default = co.resolve_options_and_default("artifact_builder")
    # slm_local has no auth_env → always available regardless of cloud credentials
    assert "qwen3.6-35b-thinking" in opts
    assert "qwen3.6-35b-instruct" in opts
    # non-llm deployments are never options
    assert "embedding" not in opts
    assert "reranker" not in opts
    # the configured default is the artifact_builder binding's deployment —
    # claude_sonnet, owner-directed 2026-07-20 (ADR-0121 T5, FRE-920 master
    # gate): there is only one binding now that Path/ExecutionProfile is
    # gone, and the owner picked sonnet directly for both sub_agent and
    # artifact_builder.
    assert default == "claude_sonnet"


def test_valid_preference_actions_artifact_builder_against_real_catalog() -> None:
    actions = co.valid_preference_actions("artifact_builder")
    assert "always_pause" in actions
    assert {"qwen3.6-35b-thinking", "qwen3.6-35b-instruct"} <= actions
    assert "claude_sonnet" in actions  # llm, unfiltered by availability
    assert "embedding" not in actions
    assert "reranker" not in actions
