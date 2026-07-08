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
_LOCAL = _REPO_ROOT / "config" / "models.yaml"
_CLOUD = _REPO_ROOT / "config" / "models.cloud.yaml"


@pytest.fixture(autouse=True)
def _clear_matrix_cache():
    yield
    _load_role_matrix.cache_clear()


class TestForbiddenRolesResolveToTheAllValue:
    """AC-1's mechanism half: one `all:` value, regardless of active profile."""

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            ("entity_extraction", "gpt-5.4-mini"),
            ("captains_log", "claude_sonnet"),
            ("insights", "claude_sonnet"),
            ("embedding", "embedding"),
            ("reranker", "reranker"),
            ("reranker_fallback", "reranker_fallback"),
        ],
    )
    def test_resolves_same_key_under_both_profiles(self, role: str, expected: str) -> None:
        assert resolve_role_model_key(role, config_path=_LOCAL) == expected
        assert resolve_role_model_key(role, config_path=_CLOUD) == expected


class TestAllowedRolesResolvePerProfile:
    """`compressor` resolves via the matrix too, even though `allowed`."""

    @pytest.mark.parametrize("role", ["compressor"])
    def test_resolves_under_each_profile(self, role: str) -> None:
        assert resolve_role_model_key(role, config_path=_LOCAL) == role
        assert resolve_role_model_key(role, config_path=_CLOUD) == role


class TestConfigPathDefaultsToActiveSettings:
    def test_config_path_none_uses_settings_model_config_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "model_config_path", _LOCAL)
        assert resolve_role_model_key("entity_extraction") == "gpt-5.4-mini"


class TestUndeclaredRole:
    def test_raises_for_unknown_role(self) -> None:
        with pytest.raises(ModelRoleError, match="not declared"):
            resolve_role_model_key("totally_made_up_role", config_path=_LOCAL)


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
