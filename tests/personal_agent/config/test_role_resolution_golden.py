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
_CATALOG = _REPO_ROOT / "config" / "models.yaml"

_MATRIX_ROLES = (
    "primary",
    "sub_agent",
    "entity_extraction",
    "captains_log",
    "insights",
    "compressor",
    "embedding",
    "reranker",
    "reranker_fallback",
)


@pytest.fixture(autouse=True)
def _clear_matrix_cache():
    yield
    _load_role_matrix.cache_clear()


def _definition_tuple(config_path: Path, model_key: str) -> tuple[object, ...]:
    config = load_model_config(config_path)
    model_def = config.models[model_key]
    return (model_def.id, model_def.provider, model_def.max_tokens, model_def.temperature)


class TestEveryRoleDereferencesToARealDefinition:
    """AC-1 — every matrix role resolves to a key the catalog actually defines.

    This used to assert a forbidden role's ModelDefinition matched across the two
    catalogs, guarding against entity_extraction resolving gpt-5.4-nano locally
    and gpt-5.4-mini in cloud. FRE-916 phase 2 deleted the second catalog, so
    that divergence is unrepresentable and the cross-profile comparison has
    nothing to compare. What still has teeth — and is what actually broke in
    production — is that each role dereferences to a real, fully-formed
    definition rather than a dangling key.
    """

    @pytest.mark.parametrize("role", _MATRIX_ROLES)
    def test_resolves_to_a_defined_model(self, role: str) -> None:
        model_key = resolve_role_model_key(role, config_path=_CATALOG)
        config = load_model_config(_CATALOG)
        assert model_key in config.models

        model_id, provider, _max_tokens, _temperature = _definition_tuple(_CATALOG, model_key)
        assert model_id, f"role {role!r} resolved to a definition with no id"
        assert provider, f"role {role!r} resolved to a definition with no provider"


class TestConsumerRaisesWhenMatrixMissing:
    """AC-2(b), consumer level — the real runtime path raises, not just the helper."""

    @pytest.mark.asyncio
    async def test_extract_entities_raises_without_falling_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real consumer raises ModelRoleError; it never falls back to a default."""
        fixture_root = _FIXTURES / "no_matrix"
        fixture_models_path = fixture_root / "config" / "models.yaml"

        from personal_agent.config import config_guard, model_loader

        monkeypatch.setattr(model_loader, "CATALOG_PATH", fixture_models_path)
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
