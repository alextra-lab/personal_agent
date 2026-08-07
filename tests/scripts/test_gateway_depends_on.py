# ruff: noqa: D103
"""FRE-1123 — gateway must not depend_on the unreachable local embeddings/reranker containers.

FRE-1166 — the embeddings/reranker service definitions themselves are retired (the local
0.6B llama.cpp provisioning path is dead residue; embedding/reranking are OVH/Voyage-managed).

Renders the actual docker-compose.cloud.yml through `docker compose config` (not just parses
the source) so the assertion matches what `docker compose up` reads at runtime, per the
ticket's own failure condition: "the change is asserted from the compose source rather than
from the rendered configuration."
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
import yaml

from personal_agent.config.config_guard import repo_root

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="requires the docker compose CLI"
)

_RENDER_ENV = {
    **os.environ,
    "POSTGRES_PASSWORD": "test",
    "SESHAT_APP_PASSWORD": "test",
    "NEO4J_PASSWORD": "test",
    "GRAFANA_ADMIN_PASSWORD": "test",  # FRE-1072 — now a required var in docker-compose.cloud.yml
}


_RENDER_OVERRIDE = "tests/scripts/fixtures/gateway_render_override.yml"


def _render_compose() -> dict[str, object]:
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.cloud.yml", "-f", _RENDER_OVERRIDE, "config"],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        env=_RENDER_ENV,
        check=True,
    )
    doc = yaml.safe_load(result.stdout)
    assert isinstance(doc, dict)
    return doc


class TestGatewayDependsOn:
    def test_gateway_does_not_depend_on_embeddings_or_reranker(self) -> None:
        compose = _render_compose()
        depends_on = compose["services"]["seshat-gateway"]["depends_on"]
        assert "embeddings" not in depends_on
        assert "reranker" not in depends_on

    def test_gateway_other_dependencies_unchanged(self) -> None:
        compose = _render_compose()
        depends_on = compose["services"]["seshat-gateway"]["depends_on"]
        assert set(depends_on) == {"postgres", "neo4j", "elasticsearch", "redis", "searxng"}

    def test_embeddings_and_reranker_service_definitions_removed(self) -> None:
        compose = _render_compose()
        services = compose["services"]
        assert "embeddings" not in services
        assert "reranker" not in services
