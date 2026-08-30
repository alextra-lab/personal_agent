# ruff: noqa: D103
"""FRE-1166 — eval gateways must not depend_on the retired embeddings/reranker containers.

Neither eval gateway calls the local containers at the application level (both inherit the
`private` substrate profile, whose embedder/reranker sources resolve to the OVH/Voyage-managed
endpoints in `config/models.yaml`, not `localhost:8503`/`:8504`). But `docker-compose.eval.yml`'s
shared `depends_on` anchor forced `embeddings`/`reranker` to `service_healthy` before either
eval gateway would start — a real functional defect independent of retiring the service
definitions entirely. Renders the merged two-file config (not just the eval file's source) so
the assertion matches what `docker compose up` actually reads at runtime.
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
    "AGENT_OWNER_EMAIL": "test@example.com",
    "GRAFANA_ADMIN_PASSWORD": "test",  # FRE-1072 — now a required var in docker-compose.cloud.yml
    "GRAFANA_RO_PASSWORD": "test",  # FRE-1203 — now a required var in docker-compose.cloud.yml
}

_RENDER_OVERRIDE = "tests/scripts/fixtures/gateway_render_override.yml"


def _render_compose() -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.cloud.yml",
            "-f",
            "docker-compose.eval.yml",
            "-f",
            _RENDER_OVERRIDE,
            "config",
        ],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        env=_RENDER_ENV,
        check=True,
    )
    doc = yaml.safe_load(result.stdout)
    assert isinstance(doc, dict)
    return doc


class TestEvalComposeDependsOn:
    def test_eval_gateways_do_not_depend_on_embeddings_or_reranker(self) -> None:
        compose = _render_compose()
        for service in ("seshat-gateway-control", "seshat-gateway-treatment"):
            depends_on = compose["services"][service]["depends_on"]
            assert "embeddings" not in depends_on
            assert "reranker" not in depends_on

    def test_eval_gateways_other_dependencies_unchanged(self) -> None:
        compose = _render_compose()
        for service in ("seshat-gateway-control", "seshat-gateway-treatment"):
            depends_on = compose["services"][service]["depends_on"]
            assert set(depends_on) == {
                "postgres-eval",
                "neo4j-eval",
                "elasticsearch-eval",
                "redis-eval",  # FRE-1342 — isolated from production's redis
                "searxng",
            }
