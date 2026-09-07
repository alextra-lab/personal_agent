"""ADR-0145 D1 (FRE-1443) — `inherit` resolves against the session's primary.

Before this ticket the factory asked `get_current_selection("sub_agent")`,
always got `None`, and never consulted the primary's selection: choosing a
cloud primary still produced local sub-agent calls (the owner's live-turn
complaint, FRE-1421 F6). `inherit` on a binding's `deployment` field fixes
that — resolved via :func:`resolve_inherited_deployment`, and by every one of
the seven readers of a binding's deployment field.

This file covers the resolver layer: :func:`resolve_inherited_deployment`
directly, and the two general-purpose resolvers that must resolve the
sentinel rather than return it literally (paths 1 and 2). Path 7
(``resolve_role_model_key``) is covered separately at the end — it is not a
live path until ADR-0145 D4 folds the two role tables, so its own fixture
says so.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from personal_agent.config.model_loader import (
    _load_role_matrix,
    resolve_inherited_deployment,
    resolve_role_model_key,
    resolve_role_target,
    resolve_selected_deployment,
)
from personal_agent.config.selection import _current_selection, set_current_selection
from personal_agent.llm_client.models import (
    INHERIT_DEPLOYMENT,
    ModelConfig,
    ModelDefinition,
    ModeSpec,
    ProviderDefinition,
    RoleBinding,
)

_LOCAL_DEFAULT = "local_default"
_CLOUD_A = "cloud_a"
_CLOUD_B = "cloud_b"

_TRIVIAL_MODES = {"default": ModeSpec()}


def _config() -> ModelConfig:
    """Primary is open, defaults local; sub_agent inherits primary's deployment."""
    return ModelConfig(
        providers={
            "slm_local": ProviderDefinition(
                placement="local", max_concurrency=3, dialect="llamacpp_qwen"
            ),
            "provider_a": ProviderDefinition(
                placement="cloud", max_concurrency=50, dialect="ovh_qwen"
            ),
            "provider_b": ProviderDefinition(
                placement="cloud", max_concurrency=50, dialect="ovh_qwen"
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
            _CLOUD_A: ModelDefinition(
                id="cloud-model-a",
                provider="provider_a",
                context_length=200000,
                max_concurrency=50,
                default_timeout=180,
                modes=_TRIVIAL_MODES,
                default_mode="default",
            ),
            _CLOUD_B: ModelDefinition(
                id="cloud-model-b",
                provider="provider_b",
                context_length=128000,
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
def _reset_selection() -> Iterator[None]:
    token = _current_selection.set({})
    try:
        yield
    finally:
        _current_selection.reset(token)


class TestResolveInheritedDeployment:
    """The shared helper every reader of the sentinel calls."""

    def test_no_selection_falls_back_to_primarys_binding_default(self) -> None:
        cfg = _config()
        assert resolve_inherited_deployment(cfg) == _LOCAL_DEFAULT

    def test_honours_the_sessions_primary_selection(self) -> None:
        cfg = _config()
        set_current_selection({"primary": _CLOUD_A})
        assert resolve_inherited_deployment(cfg) == _CLOUD_A

    def test_a_second_unrelated_primary_selection_also_resolves(self) -> None:
        """AC-2's mechanism: not special-cased to one primary."""
        cfg = _config()
        set_current_selection({"primary": _CLOUD_B})
        assert resolve_inherited_deployment(cfg) == _CLOUD_B

    def test_an_invalid_selection_falls_back_through_the_same_guardrail(self) -> None:
        """Fail-closed selection guardrail still applies through inherit."""
        cfg = _config()
        set_current_selection({"primary": "no_such_model_xyz"})
        assert resolve_inherited_deployment(cfg) == _LOCAL_DEFAULT


class TestResolveRoleTargetResolvesInherit:
    """Path 1 — AC-1/AC-2/AC-3: resolve_role_target never returns the literal sentinel."""

    def test_no_selection_sub_agent_gets_primarys_default(self) -> None:
        cfg = _config()
        key, definition = resolve_role_target("sub_agent", config=cfg)
        assert key == _LOCAL_DEFAULT
        assert definition is not None
        assert definition.id == "local-default-model"

    def test_cloud_primary_a_produces_a_matching_sub_agent(self) -> None:
        """AC-1 — a cloud primary selection reaches the sub-agent binding."""
        cfg = _config()
        set_current_selection({"primary": _CLOUD_A})

        key, definition = resolve_role_target("sub_agent", config=cfg)

        assert key == _CLOUD_A
        assert definition is not None
        assert definition.id == "cloud-model-a"

    def test_cloud_primary_b_also_produces_a_matching_sub_agent(self) -> None:
        """AC-2 — a second, unrelated primary is not special-cased."""
        cfg = _config()
        set_current_selection({"primary": _CLOUD_B})

        key, definition = resolve_role_target("sub_agent", config=cfg)

        assert key == _CLOUD_B
        assert definition is not None
        assert definition.id == "cloud-model-b"

    def test_explicit_inherit_model_key_also_resolves(self) -> None:
        """An explicit model_key of `inherit` (not just the binding default) resolves too."""
        cfg = _config()
        set_current_selection({"primary": _CLOUD_A})

        key, definition = resolve_role_target("sub_agent", model_key=INHERIT_DEPLOYMENT, config=cfg)

        assert key == _CLOUD_A
        assert definition is not None

    def test_never_returns_the_literal_sentinel(self) -> None:
        cfg = _config()
        set_current_selection({"primary": _CLOUD_A})
        key, _ = resolve_role_target("sub_agent", config=cfg)
        assert key != INHERIT_DEPLOYMENT


class TestResolveSelectedDeploymentResolvesInherit:
    """Path 2 — the same guarantee for resolve_selected_deployment."""

    def test_no_selection_default_key_is_not_the_literal_sentinel(self) -> None:
        cfg = _config()
        assert resolve_selected_deployment("sub_agent", None, cfg) == _LOCAL_DEFAULT

    def test_primary_selection_propagates_through_default_resolution(self) -> None:
        cfg = _config()
        set_current_selection({"primary": _CLOUD_B})
        # sub_agent itself carries no selection (pinned) — None is what the
        # factory actually passes for it (config/selection.py: only "primary"
        # is ever placed in the map).
        assert resolve_selected_deployment("sub_agent", None, cfg) == _CLOUD_B

    def test_primary_role_itself_is_unaffected(self) -> None:
        """Sanity: resolving `primary` itself never touches the inherit machinery."""
        cfg = _config()
        assert resolve_selected_deployment("primary", None, cfg) == _LOCAL_DEFAULT
        assert resolve_selected_deployment("primary", _CLOUD_A, cfg) == _CLOUD_A


class TestResolveRoleModelKeyInheritSentinel:
    """Path 7 — not live until ADR-0145 D4 folds the two role tables.

    ``sub_agent`` (the only inherit-eligible role today) is deliberately off
    this matrix (ADR-0121 T5, FRE-920) and stays that way here — see
    ``tests/personal_agent/config/test_model_loader_roles.py::TestUndeclaredRole``.
    This fixture instead seeds a contrived matrix role whose `all:` value is
    the sentinel, purely to prove the function resolves it rather than
    returning the literal string, ahead of D4 making the path live.
    """

    _FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "role_model_key_inherit"

    @pytest.fixture(autouse=True)
    def _clear_matrix_cache(self) -> Iterator[None]:
        yield
        _load_role_matrix.cache_clear()

    def test_inherit_all_value_resolves_to_primarys_default(self) -> None:
        resolved = resolve_role_model_key(
            "some_matrix_role",
            config_path=self._FIXTURE_ROOT / "config" / "models.yaml",
            root=self._FIXTURE_ROOT,
        )
        assert resolved == "claude_sonnet"
        assert resolved != INHERIT_DEPLOYMENT
