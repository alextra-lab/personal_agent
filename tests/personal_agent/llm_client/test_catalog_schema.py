"""Three-layer catalog schema validation (ADR-0121 Layers 1-3, FRE-916).

AC-2 is the headline here: a role cannot bind to a wrong-kind deployment, in
either direction, and the failure names the role and the key. Before ADR-0121
nothing typed a model, so ``embedding`` binding to a chat model was prevented
only by convention.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_agent.llm_client.models import (
    INHERIT_DEPLOYMENT,
    ModelConfig,
    ModelKind,
    Placement,
    RoleBinding,
)

_PROVIDERS = {
    "slm_local": {
        "placement": "local",
        "max_concurrency": 2,
        "base_url": "https://slm/v1",
        "dialect": "llamacpp_qwen",
    },
    "openai": {"placement": "cloud", "max_concurrency": 10, "auth_env": "openai_api_key"},
}

_DEPLOYMENTS = {
    "qwen-chat": {
        "id": "unsloth/qwen",
        "provider": "slm_local",
        "kind": "llm",
        "context_length": 65536,
        "max_concurrency": 1,
        "default_timeout": 90,
        # ADR-0145 D3a: trivial mode, since this suite tests kind/role binding,
        # not dialect vocabulary.
        "default_mode": "default",
        "modes": {"default": {}},
    },
    "qwen-embed": {
        "id": "Qwen/Qwen3-Embedding-8B",
        "provider": "openai",
        "kind": "embedding",
        "dimensions": 1024,
        "context_length": 32768,
        "max_concurrency": 1,
        "default_timeout": 60,
    },
}


def _build(roles: dict[str, object]) -> ModelConfig:
    """Validate a catalog with the given role bindings."""
    return ModelConfig.model_validate(
        {"providers": _PROVIDERS, "models": _DEPLOYMENTS, "roles": roles}
    )


class TestKindCompatibility:
    """AC-2 — a role cannot bind to a wrong-kind deployment."""

    def test_writer_role_bound_to_embedding_model_fails(self) -> None:
        """entity_extraction (an LLM role) may not bind to an embedding deployment."""
        with pytest.raises(ValidationError) as exc:
            _build({"entity_extraction": {"deployment": "qwen-embed"}})

        message = str(exc.value)
        assert "entity_extraction" in message, "error must name the role"
        assert "qwen-embed" in message, "error must name the deployment key"
        assert "llm" in message and "embedding" in message

    def test_embedding_role_bound_to_chat_model_fails(self) -> None:
        """The other direction: embedding may not bind to a chat deployment.

        This is the direction the pre-ADR-0121 config could not prevent at all.
        """
        with pytest.raises(ValidationError) as exc:
            _build({"embedding": {"deployment": "qwen-chat"}})

        message = str(exc.value)
        assert "embedding" in message, "error must name the role"
        assert "qwen-chat" in message, "error must name the deployment key"

    def test_reranker_roles_require_reranker_kind(self) -> None:
        """Both reranker roles are kind-constrained, not just the primary one."""
        for role in ("reranker", "reranker_fallback"):
            with pytest.raises(ValidationError, match=role):
                _build({role: {"deployment": "qwen-chat"}})

    def test_compatible_bindings_load(self) -> None:
        """The correct pairings validate cleanly."""
        config = _build(
            {
                "entity_extraction": {"deployment": "qwen-chat"},
                "embedding": {"deployment": "qwen-embed"},
            }
        )
        assert config.roles["embedding"].deployment == "qwen-embed"

    def test_unlisted_role_defaults_to_llm_fail_closed(self) -> None:
        """A role with no declared requirement accepts LLM only.

        Fail-closed by omission: a role added later cannot silently accept an
        arbitrary kind just because nobody remembered to constrain it.
        """
        with pytest.raises(ValidationError, match="some_future_role"):
            _build({"some_future_role": {"deployment": "qwen-embed"}})


class TestProviderReferences:
    """Every deployment must reference a real provider (ADR-0121 §8)."""

    def test_unknown_provider_fails(self) -> None:
        """A deployment pointing at a provider that does not exist fails load."""
        with pytest.raises(ValidationError, match="nonexistent"):
            ModelConfig.model_validate(
                {
                    "providers": _PROVIDERS,
                    "models": {"orphan": {**_DEPLOYMENTS["qwen-chat"], "provider": "nonexistent"}},
                    "roles": {},
                }
            )

    def test_missing_provider_fails_when_providers_declared(self) -> None:
        """A deployment with no provider fails load (FRE-916 phase 2).

        Phase 1 allowed a providerless "legacy" entry while the three layers were
        introduced additively. Every entry now declares a provider, so the laxity
        became a live fail-open: a providerless deployment has no placement, and
        placement_of would default it to LOCAL — routing a would-be cloud model
        local and skipping the paid-attachment cost gate (code review). The
        provider is now required; the error names the deployment.
        """
        deployment = {k: v for k, v in _DEPLOYMENTS["qwen-chat"].items() if k != "provider"}
        with pytest.raises(ValidationError, match="orphan"):
            ModelConfig.model_validate(
                {"providers": _PROVIDERS, "models": {"orphan": deployment}, "roles": {}}
            )

    def test_missing_provider_tolerated_when_no_providers_block(self) -> None:
        """With no `providers:` block (a bare fixture catalog) the check is inert."""
        deployment = {k: v for k, v in _DEPLOYMENTS["qwen-chat"].items() if k != "provider"}
        config = ModelConfig.model_validate({"models": {"bare": deployment}, "roles": {}})
        assert config.models["bare"].provider is None


class TestBindingReferences:
    """Every binding must reference a real deployment (retained from ADR-0099)."""

    def test_dangling_deployment_reference_fails(self) -> None:
        """A binding naming a non-existent deployment fails, naming both."""
        with pytest.raises(ValidationError) as exc:
            _build({"primary": {"deployment": "no-such-model"}})
        assert "primary" in str(exc.value) and "no-such-model" in str(exc.value)


class TestPlacement:
    """Placement is a provider fact, read once — not parsed from a URL."""

    def test_placement_derives_from_provider(self) -> None:
        """Placement is read from the provider, not parsed from the endpoint URL."""
        config = _build({})
        assert config.placement_of("qwen-chat") is Placement.LOCAL
        assert config.placement_of("qwen-embed") is Placement.CLOUD

    def test_unknown_deployment_defaults_local(self) -> None:
        """Matches the pre-ADR-0121 fallback: an unresolved provider_type meant local."""
        assert _build({}).placement_of("no-such-model") is Placement.LOCAL


class TestRoleBindingDefaults:
    """Bindings are pinned unless explicitly opened (ADR-0121 §6)."""

    def test_roles_are_pinned_by_default(self) -> None:
        """A binding with no explicit `open` is pinned."""
        config = _build({"entity_extraction": {"deployment": "qwen-chat"}})
        assert config.roles["entity_extraction"].open is False

    def test_open_is_explicit(self) -> None:
        """Opening a role to user selection requires saying so."""
        config = _build({"primary": {"deployment": "qwen-chat", "open": True}})
        assert config.roles["primary"].open is True

    def test_kind_defaults_to_llm(self) -> None:
        """A deployment with no declared kind is an LLM."""
        assert _build({}).models["qwen-chat"].kind is ModelKind.LLM


class TestInheritDeploymentSentinel:
    """ADR-0145 D1 (FRE-1443 AC-5) — `inherit` is accepted only when it resolves.

    The catalog validator (path 3 of the ticket's seven) is the load-time half
    of the sentinel: a binding may name INHERIT_DEPLOYMENT instead of a
    literal key, but only when the `primary` binding it defers to itself
    resolves to a real, kind-compatible deployment.
    """

    def test_inherit_binding_resolves_against_primary_and_loads(self) -> None:
        """The happy path: sub_agent inherits primary's llm deployment."""
        config = _build(
            {
                "primary": {"deployment": "qwen-chat", "open": True},
                "sub_agent": {"deployment": INHERIT_DEPLOYMENT},
            }
        )
        assert config.roles["sub_agent"].deployment == INHERIT_DEPLOYMENT

    def test_inherit_binding_with_no_primary_binding_fails(self) -> None:
        """AC-5 seeded negative: nothing for `inherit` to resolve against."""
        with pytest.raises(ValidationError) as exc:
            _build({"sub_agent": {"deployment": INHERIT_DEPLOYMENT}})

        message = str(exc.value)
        assert "sub_agent" in message
        assert "no 'primary' role binding" in message

    def test_inherit_binding_when_primary_itself_is_inherit_fails(self) -> None:
        """AC-5 seeded negative: inherit cannot resolve recursively."""
        with pytest.raises(ValidationError) as exc:
            _build(
                {
                    "primary": {"deployment": INHERIT_DEPLOYMENT},
                    "sub_agent": {"deployment": INHERIT_DEPLOYMENT},
                }
            )

        assert "cannot resolve recursively" in str(exc.value)

    def test_inherit_binding_whose_primary_is_dangling_fails(self) -> None:
        """AC-5 seeded negative: primary itself names no real deployment."""
        with pytest.raises(ValidationError) as exc:
            _build(
                {
                    "primary": {"deployment": "no-such-model"},
                    "sub_agent": {"deployment": INHERIT_DEPLOYMENT},
                }
            )

        message = str(exc.value)
        # The primary binding's own dangling-reference failure fires first —
        # inherit cannot resolve to a deployment that does not exist either way.
        assert "primary" in message and "no-such-model" in message

    def test_inherit_binding_still_enforces_kind_compatibility(self) -> None:
        """AC-5 + AC-2 together: inherit does not bypass the kind check.

        `embedding` requires an embedding deployment; primary here resolves to
        an llm one, so the mismatch must still fail after inherit resolves.
        """
        with pytest.raises(ValidationError) as exc:
            _build(
                {
                    "primary": {"deployment": "qwen-chat"},
                    "embedding": {"deployment": INHERIT_DEPLOYMENT},
                }
            )

        message = str(exc.value)
        assert "embedding" in message
        assert "llm" in message and "embedding" in message


class TestDefaultsByPrimary:
    """``defaults_by_primary`` schema shape (ADR-0121 Addendum A step 1, FRE-965).

    Step 1 only adds the field and its shape; the fail-closed must-define /
    dangling-value guard is step 2 (FRE-966), so these tests cover Pydantic's
    structural validation only.
    """

    def test_absent_defaults_to_none(self) -> None:
        """A binding with no `defaults_by_primary` block leaves it unset."""
        config = _build({"sub_agent": {"deployment": "qwen-chat"}})
        assert config.roles["sub_agent"].defaults_by_primary is None

    def test_accepts_a_str_to_str_map(self) -> None:
        binding = RoleBinding(
            deployment="qwen-chat",
            defaults_by_primary={"qwen-chat": "qwen-chat"},
        )
        assert binding.defaults_by_primary == {"qwen-chat": "qwen-chat"}

    def test_rejects_non_string_values(self) -> None:
        """A malformed map (non-string value) fails Pydantic validation."""
        with pytest.raises(ValidationError):
            RoleBinding(deployment="qwen-chat", defaults_by_primary={"qwen-chat": 1})

    def test_rejects_non_string_keys(self) -> None:
        with pytest.raises(ValidationError):
            RoleBinding(deployment="qwen-chat", defaults_by_primary={1: "qwen-chat"})
