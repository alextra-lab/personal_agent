"""Golden tests for ADR-0099 D1 stage 2 role resolution (FRE-650).

AC-1 (real consumer path, not a standalone helper) + AC-2(b) (no fallback —
the runtime consumer raises, not just the resolver helper in isolation).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.config.model_loader import (
    ModelRoleError,
    _load_role_matrix,
    load_model_config,
    resolve_role_model_key,
)
from personal_agent.second_brain.entity_extraction import extract_entities_and_relationships

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_LOCAL = _REPO_ROOT / "config" / "models.yaml"
_CLOUD = _REPO_ROOT / "config" / "models.cloud.yaml"

_FORBIDDEN_ROLES = (
    "entity_extraction",
    "captains_log",
    "insights",
    "embedding",
    "reranker",
    "reranker_fallback",
    "artifact_builder",
)
_ALLOWED_ROLES = ("compressor",)


@pytest.fixture(autouse=True)
def _clear_matrix_cache():
    yield
    _load_role_matrix.cache_clear()


def _definition_tuple(config_path: Path, model_key: str) -> tuple[object, ...]:
    config = load_model_config(config_path)
    model_def = config.models[model_key]
    return (model_def.id, model_def.provider, model_def.max_tokens, model_def.temperature)


class TestForbiddenRolesResolveIdenticallyAcrossProfiles:
    """AC-1 — a forbidden role's fully-resolved ModelDefinition matches across profiles.

    Must fail if entity_extraction ever again resolves gpt-5.4-nano local vs
    gpt-5.4-mini cloud, or the name matches but the underlying id differs.
    """

    @pytest.mark.parametrize("role", _FORBIDDEN_ROLES)
    def test_same_definition_local_and_cloud(self, role: str) -> None:
        """The role resolves to the same model key and ModelDefinition on both profiles."""
        local_key = resolve_role_model_key(role, config_path=_LOCAL)
        cloud_key = resolve_role_model_key(role, config_path=_CLOUD)
        assert local_key == cloud_key

        local_def = _definition_tuple(_LOCAL, local_key)
        cloud_def = _definition_tuple(_CLOUD, cloud_key)
        assert local_def == cloud_def


class TestAllowedRolesResolveWithoutError:
    """AC-1's lighter half — compressor/reranker resolve via the matrix under each profile.

    No cross-profile equality assertion — they are `allowed`, expected to diverge.
    """

    @pytest.mark.parametrize("role", _ALLOWED_ROLES)
    @pytest.mark.parametrize("config_path", [_LOCAL, _CLOUD])
    def test_resolves_to_the_profiles_own_model_entry(self, role: str, config_path: Path) -> None:
        """The resolved key is a real entry in that profile's own models: mapping."""
        model_key = resolve_role_model_key(role, config_path=config_path)
        config = load_model_config(config_path)
        assert model_key in config.models


class TestConsumerRaisesWhenMatrixMissing:
    """AC-2(b), consumer level — the real runtime path raises, not just the helper."""

    @pytest.mark.asyncio
    async def test_extract_entities_raises_without_falling_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real consumer raises ModelRoleError; it never falls back to a default."""
        fixture_root = _FIXTURES / "no_matrix"
        fixture_models_path = fixture_root / "config" / "models.yaml"

        from personal_agent.config import config_guard, settings

        monkeypatch.setattr(settings, "model_config_path", fixture_models_path)
        monkeypatch.setattr(config_guard, "repo_root", lambda: fixture_root)

        with (
            patch(
                "personal_agent.second_brain.entity_extraction.LocalLLMClient"
            ) as mock_client_cls,
            pytest.raises(ModelRoleError, match="model_roles.yaml"),
        ):
            mock_client_cls.return_value.respond = AsyncMock(
                return_value={"content": "{}", "usage": {}}
            )
            await extract_entities_and_relationships("hello", "world")
