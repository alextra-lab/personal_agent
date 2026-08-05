# ruff: noqa: D103
"""FRE-1146 / ADR-0132 D3 — the filebeat compose service.

Renders the actual docker-compose.cloud.yml through `docker compose config` (not just parses
the source), matching test_gateway_depends_on.py's pattern, so the assertion matches what
`docker compose up` reads at runtime. Guards the codex plan-review requirement that no
/var/run/docker.sock mount ever reappears on this service.
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
}


def _render_compose() -> dict[str, object]:
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.cloud.yml", "config"],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        env=_RENDER_ENV,
        check=True,
    )
    doc = yaml.safe_load(result.stdout)
    assert isinstance(doc, dict)
    return doc


class TestFilebeatComposeService:
    def test_service_exists_and_builds_from_dedicated_dockerfile(self) -> None:
        compose = _render_compose()
        filebeat = compose["services"]["filebeat"]
        assert filebeat["build"]["dockerfile"] == "Dockerfile.filebeat"

    def test_depends_on_caddy_and_elasticsearch_healthy(self) -> None:
        compose = _render_compose()
        depends_on = compose["services"]["filebeat"]["depends_on"]
        assert depends_on["caddy"]["condition"] == "service_healthy"
        assert depends_on["elasticsearch"]["condition"] == "service_healthy"

    def test_restart_policy_matches_caddy(self) -> None:
        compose = _render_compose()
        assert compose["services"]["filebeat"]["restart"] == "unless-stopped"

    def test_registry_volume_is_mounted_and_declared(self) -> None:
        compose = _render_compose()
        volumes = compose["services"]["filebeat"]["volumes"]
        registry_mounts = [v for v in volumes if v.get("source") == "filebeat_registry_cloud"]
        assert len(registry_mounts) == 1
        assert registry_mounts[0]["target"] == "/usr/share/filebeat/data"
        assert "filebeat_registry_cloud" in compose["volumes"]

    def test_containers_dir_mounted_read_only(self) -> None:
        compose = _render_compose()
        volumes = compose["services"]["filebeat"]["volumes"]
        containers_mounts = [v for v in volumes if v.get("source") == "/var/lib/docker/containers"]
        assert len(containers_mounts) == 1
        assert containers_mounts[0]["read_only"] is True

    def test_no_docker_socket_mount_anywhere(self) -> None:
        """The blocking finding from the first codex plan-review round: no docker.sock, ever."""
        compose = _render_compose()
        filebeat = compose["services"]["filebeat"]
        volumes = filebeat.get("volumes", [])
        assert not any("docker.sock" in str(v.get("source", "")) for v in volumes)
