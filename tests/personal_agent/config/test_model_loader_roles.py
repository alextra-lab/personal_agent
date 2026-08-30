"""Unit tests for resolve_role_model_key (ADR-0099 D1 stage 2, FRE-650).

Cache-bleed note: the matrix loader is cached by resolved root path
(mirrors ``_load_model_config_at_path``'s pattern), so every test that
points ``root`` at a fixture calls ``.cache_clear()`` in an autouse
fixture to avoid one test's cached matrix leaking into another.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.config.model_loader import (
    ModelRoleError,
    _load_role_matrix,
    resolve_role_model_key,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_CATALOG = _REPO_ROOT / "config" / "models.yaml"


@pytest.fixture(autouse=True)
def _clear_matrix_cache():
    yield
    _load_role_matrix.cache_clear()


class TestRolesResolveToTheAllValue:
    """AC-1's mechanism half: every role resolves to its single `all:` value.

    FRE-916 phase 2 collapsed the divergence axis, so there is no longer a
    per-profile variant to compare against — the assertion is simply that each
    role dereferences to its declared key.
    """

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            # `sub_agent` is deliberately absent: ADR-0121 T5 (FRE-920, master
            # gate 2026-07-20) removed its matrix entry as a stale duplicate of
            # the Layer-3 binding — it was never actually resolved through this
            # matrix (see TestUndeclaredRole below for the now-undeclared case).
            ("primary", "qwen3.6-35b-thinking"),
            ("entity_extraction", "gpt-5.4-mini"),
            ("captains_log", "claude_sonnet"),
            ("insights", "claude_sonnet"),
            ("compressor", "gpt-5.4-mini"),
            ("embedding", "embedding"),
            ("reranker", "reranker"),
            ("reranker_fallback", "reranker_fallback"),
        ],
    )
    def test_resolves_to_declared_key(self, role: str, expected: str) -> None:
        assert resolve_role_model_key(role, config_path=_CATALOG) == expected


class TestCompressorNoLongerDiverges:
    """`compressor` resolves to one model (ADR-0121, FRE-916).

    It used to resolve to its own role name as a catalog key — gpt-5.4-nano
    locally, gpt-5.4-mini in cloud. That split was the clearest case of a role
    name masquerading as a model: `compressor` was never a model, and the two
    files disagreed about which one it meant. With nano retired it binds to
    gpt-5.4-mini, and phase 2 removed the second file entirely, so there is no
    per-profile value left to diverge.
    """

    def test_resolves_to_gpt_5_4_mini(self) -> None:
        assert resolve_role_model_key("compressor", config_path=_CATALOG) == "gpt-5.4-mini"


class TestConfigPathDefaultsToTheSingleCatalog:
    def test_config_path_none_uses_the_catalog_constant(self) -> None:
        """No settings monkeypatch: the catalog is a module constant since
        FRE-916 phase 2 deleted settings.model_config_path.
        """
        assert resolve_role_model_key("entity_extraction") == "gpt-5.4-mini"


class TestUndeclaredRole:
    def test_raises_for_unknown_role(self) -> None:
        with pytest.raises(ModelRoleError, match="not declared"):
            resolve_role_model_key("totally_made_up_role", config_path=_CATALOG)

    def test_artifact_builder_is_not_matrix_resolved(self) -> None:
        """ADR-0119 §2/AC-8 (FRE-879): artifact_builder is off the matrix.

        It is an "open" role resolved via the Layer-3 binding
        (config/model_roles.yaml's `bindings:` section, ADR-0121), never the matrix.
        The parked FRE-879 WIP's first cut made it a matrix row — the exact regression
        this ticket corrects — so this asserts it stays undeclared here.
        """
        with pytest.raises(ModelRoleError, match="not declared"):
            resolve_role_model_key("artifact_builder", config_path=_CATALOG)

    def test_sub_agent_is_not_matrix_resolved(self) -> None:
        """ADR-0121 T5 (FRE-920, master gate 2026-07-20): sub_agent is off the matrix.

        Its entry here was a stale duplicate of the Layer-3 binding (a
        two-places-for-one-role drift trap master caught at the gate) and was
        removed; sub_agent resolves only via config/model_roles.yaml's
        `bindings:` section, same as artifact_builder above.
        """
        with pytest.raises(ModelRoleError, match="not declared"):
            resolve_role_model_key("sub_agent", config_path=_CATALOG)


class TestMatrixMissing:
    """AC-2(b), helper level — the consumer-level case lives in the golden test."""

    def test_raises_when_matrix_missing(self) -> None:
        fixture_root = _FIXTURES / "no_matrix"
        with pytest.raises(ModelRoleError, match="model_roles.yaml"):
            resolve_role_model_key(
                "entity_extraction",
                config_path=fixture_root / "config" / "models.yaml",
                root=fixture_root,
            )


class TestDanglingRoleReference:
    """Mirrors AC-9, but at the runtime-consumer resolution layer."""

    def test_raises_when_resolved_key_absent_from_models(self) -> None:
        fixture_root = _FIXTURES / "role_dangling_key"
        with pytest.raises(ModelRoleError, match="gpt-9-ghost"):
            resolve_role_model_key(
                "entity_extraction",
                config_path=fixture_root / "config" / "models.yaml",
                root=fixture_root,
            )
