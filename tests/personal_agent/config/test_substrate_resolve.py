"""Tests for the substrate backend-selection seam (ADR-0112 D3 / AC-2, FRE-816).

The AC-2 proof: for **every** D3 substrate component, a second config profile
resolves through the *same* ``resolve_substrate`` interface — driven purely by
``config/substrate.yaml`` + ``AppConfig`` — with no code edit. Only local
backends are wired into serving paths today, so this is AC-2's config-test
escape-hatch branch (runtime adoption is the AC-5/AC-9 tickets).
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from personal_agent.config.config_guard import REQUIRED_SUBSTRATE_COMPONENTS
from personal_agent.config.model_loader import load_model_config, resolve_role_model_key
from personal_agent.config.settings import AppConfig
from personal_agent.config.substrate import (
    SubstrateProfileError,
    main,
    resolve_substrate,
)

# Distinct managed targets — deliberately unlike any local/test-stack value so a
# private↔managed difference proves a real swap, not a coincidental collision.
_MANAGED = {
    "managed_database_url": "postgresql+asyncpg://user@managed-pg.example.com:5432/seshat",
    "managed_neo4j_uri": "neo4j+s://managed-neo4j.example.com:7687",
    "managed_elasticsearch_url": "https://managed-es.example.com:9243",
    "managed_embedding_endpoint": "https://oai.endpoints.ovh.net/v1",
    "managed_reranker_endpoint": "https://rerank.example.com/v1",
    "managed_slm_endpoint": "https://slm.example.com/v1",
}


@pytest.fixture
def managed_settings() -> AppConfig:
    """An AppConfig with every ``managed_*`` target set (the managed profile's inputs)."""
    return AppConfig(**_MANAGED)


class TestResolveProfiles:
    def test_private_declares_all_d3_components(self, managed_settings: AppConfig) -> None:
        resolution = resolve_substrate("private", settings=managed_settings)
        assert set(resolution.backends) >= REQUIRED_SUBSTRATE_COMPONENTS

    def test_managed_declares_all_d3_components(self, managed_settings: AppConfig) -> None:
        resolution = resolve_substrate("managed", settings=managed_settings)
        assert set(resolution.backends) >= REQUIRED_SUBSTRATE_COMPONENTS

    def test_unknown_profile_raises(self, managed_settings: AppConfig) -> None:
        with pytest.raises(SubstrateProfileError):
            resolve_substrate("nonexistent", settings=managed_settings)


class TestAc2ComponentSwap:
    """AC-2 core: every D3 component swaps target by profile, one interface, no code edit."""

    @pytest.mark.parametrize("component", sorted(REQUIRED_SUBSTRATE_COMPONENTS))
    def test_component_swaps_by_profile(self, component: str, managed_settings: AppConfig) -> None:
        private = resolve_substrate("private", settings=managed_settings)
        managed = resolve_substrate("managed", settings=managed_settings)

        private_backend = private.backends[component]
        managed_backend = managed.backends[component]

        # Same interface, only the profile arg changed.
        assert private_backend.kind == "local"
        assert managed_backend.kind == "managed"
        # Both targets are configured (no coincidental None==None pass) and differ.
        assert private_backend.target is not None
        assert managed_backend.target is not None
        assert private_backend.target != managed_backend.target

    def test_vector_index_is_covered(self, managed_settings: AppConfig) -> None:
        # ADR-0112 AC-2 explicitly fails a seam that omits the search/vector index.
        assert "vector_index" in REQUIRED_SUBSTRATE_COMPONENTS
        resolution = resolve_substrate("private", settings=managed_settings)
        assert "vector_index" in resolution.backends

    def test_vector_index_rides_neo4j(self, managed_settings: AppConfig) -> None:
        # backed_by:neo4j — its target follows neo4j's per-profile target.
        for profile in ("private", "managed"):
            resolution = resolve_substrate(profile, settings=managed_settings)
            assert resolution.backends["vector_index"].target == resolution.backends["neo4j"].target


class TestPrivateMirrorsReality:
    """The private profile resolves to exactly what the running app reads today."""

    def test_stores_and_slm_mirror_settings(self, managed_settings: AppConfig) -> None:
        resolution = resolve_substrate("private", settings=managed_settings)
        assert resolution.backends["postgres"].target == str(managed_settings.database_url)
        assert resolution.backends["neo4j"].target == str(managed_settings.neo4j_uri)
        assert resolution.backends["elasticsearch"].target == str(
            managed_settings.elasticsearch_url
        )
        assert resolution.backends["slm"].target == str(managed_settings.llm_base_url)

    def test_embedder_reranker_mirror_model_def_endpoint(self, managed_settings: AppConfig) -> None:
        resolution = resolve_substrate("private", settings=managed_settings)
        for component, role in (("embedder", "embedding"), ("reranker", "reranker")):
            key = resolve_role_model_key(role, config_path=managed_settings.model_config_path)
            expected = load_model_config(managed_settings.model_config_path).models[key].endpoint
            assert resolution.backends[component].target == expected


class TestManagedUsesManagedFields:
    def test_managed_targets_are_the_managed_settings(self, managed_settings: AppConfig) -> None:
        resolution = resolve_substrate("managed", settings=managed_settings)
        assert resolution.backends["postgres"].target == _MANAGED["managed_database_url"]
        assert resolution.backends["embedder"].target == _MANAGED["managed_embedding_endpoint"]
        assert resolution.backends["slm"].target == _MANAGED["managed_slm_endpoint"]

    def test_managed_target_is_none_when_field_unset(self) -> None:
        # An operator who selects `managed` without setting the targets gets None
        # (a first-class "unconfigured managed backend" state; AC-1/AC-3 judge it).
        resolution = resolve_substrate("managed", settings=AppConfig())
        assert resolution.backends["postgres"].target is None


class TestManagedEmbedderProfile:
    """ADR-0112 AC-5/AC-6 (FRE-821): storage stays local, only embedder is managed."""

    def test_embedder_resolves_managed(self, managed_settings: AppConfig) -> None:
        resolution = resolve_substrate("managed_embedder", settings=managed_settings)
        assert resolution.backends["embedder"].kind == "managed"
        assert resolution.backends["embedder"].target == _MANAGED["managed_embedding_endpoint"]

    @pytest.mark.parametrize("component", sorted(REQUIRED_SUBSTRATE_COMPONENTS - {"embedder"}))
    def test_every_other_component_matches_private(
        self, component: str, managed_settings: AppConfig
    ) -> None:
        private = resolve_substrate("private", settings=managed_settings)
        managed_embedder = resolve_substrate("managed_embedder", settings=managed_settings)
        assert managed_embedder.backends[component].kind == "local"
        assert managed_embedder.backends[component].target == private.backends[component].target

    def test_declares_all_d3_components(self, managed_settings: AppConfig) -> None:
        resolution = resolve_substrate("managed_embedder", settings=managed_settings)
        assert set(resolution.backends) >= REQUIRED_SUBSTRATE_COMPONENTS


class TestDevTestProfileUnderRealTestEnv:
    """ADR-0112 AC-9 (FRE-820): end-to-end proof under the real pytest test-substrate env.

    ``tests/conftest.py`` (FRE-375) has already rewritten the live settings
    singleton's store URIs to the test stack (:7688/:9201/:5433) and set
    ``AGENT_SUBSTRATE_PROFILE=test`` before this test module even imports. No
    fixture/mocking here — this exercises the actual wiring an operator gets,
    proving the resolved targets ARE the FRE-375 test substrate, not a
    synthetic stand-in for it.
    """

    @pytest.mark.parametrize("profile", ["dev", "test"])
    def test_stores_resolve_to_the_fre_375_test_substrate(self, profile: str) -> None:
        resolution = resolve_substrate(profile)

        postgres = urlparse(resolution.backends["postgres"].target)
        neo4j = urlparse(resolution.backends["neo4j"].target)
        elasticsearch = urlparse(resolution.backends["elasticsearch"].target)

        assert postgres.port == 5433
        assert neo4j.port == 7688
        assert elasticsearch.port == 9201

    @pytest.mark.parametrize("profile", ["dev", "test"])
    def test_no_component_resolves_managed_under_dev_test(self, profile: str) -> None:
        resolution = resolve_substrate(profile)
        for component, backend in resolution.backends.items():
            assert backend.kind == "local", (
                f"{profile} profile component {component!r} resolved kind={backend.kind!r} "
                "— dev/test must never resolve a paid/managed endpoint (ADR-0112 AC-9)"
            )


class TestSubstrateCli:
    def test_cli_prints_table(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--profile", "private"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "profile: private" in captured.out
        assert "postgres" in captured.out
        assert "vector_index" in captured.out

    def test_cli_single_component(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--profile", "private", "--component", "postgres"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out.strip() != ""

    def test_cli_unknown_profile_exits_nonzero(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--profile", "nonexistent"])
        captured = capsys.readouterr()
        assert exit_code != 0
        assert "nonexistent" in captured.err

    def test_cli_unknown_component_exits_nonzero(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--profile", "private", "--component", "bogus"])
        captured = capsys.readouterr()
        assert exit_code != 0
        assert "bogus" in captured.err
