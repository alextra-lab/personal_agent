# ruff: noqa: D103
"""FRE-1072 / ADR-0129 D6 — the tempo compose service's shape and image pin.

Two layers, same pattern as test_kibana_compose_service.py: a source-only class that parses the
committed YAML directly and always runs, and a render class that exercises `docker compose
config`'s actual resolution (skipped without docker), passing the shared gateway override fixture
for any docker-compose.cloud.yml render (the bug FRE-1187's own Step-8 review caught on the
Kibana test — not to be reintroduced here).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from personal_agent.config.config_guard import repo_root

_RENDER_ENV = {
    **os.environ,
    "POSTGRES_PASSWORD": "test",
    "SESHAT_APP_PASSWORD": "test",
    "NEO4J_PASSWORD": "test",
    "GRAFANA_ADMIN_PASSWORD": "test",
}
_RENDER_OVERRIDE = "tests/scripts/fixtures/gateway_render_override.yml"


def _render_compose(compose_file: str) -> dict[str, object]:
    args = ["docker", "compose", "-f", compose_file]
    if compose_file == "docker-compose.cloud.yml":
        args += ["-f", _RENDER_OVERRIDE]
    args += ["config"]
    result = subprocess.run(
        args, cwd=repo_root(), capture_output=True, text=True, env=_RENDER_ENV, check=True
    )
    doc = yaml.safe_load(result.stdout)
    assert isinstance(doc, dict)
    return doc


class TestTempoComposeServiceSource:
    """Parses the committed YAML directly — no docker dependency, always runs."""

    def _tempo_service(self, compose_file: str) -> dict[str, object]:
        raw = yaml.safe_load((repo_root() / compose_file).read_text())
        return raw["services"]["tempo"]

    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_image_is_pinned_patched_version(self, compose_file: str) -> None:
        """2.10.7, not 2.10.1: CVE-2026-27878 (memory-exhaustion DoS via TraceQL exemplars hint)
        affects up to 2.10.1 and is fixed >=2.10.2 — relevant here because Grafana's anonymous
        Viewer role can trigger arbitrary Tempo queries (ADR-0129 D6's accepted residual).
        """
        assert self._tempo_service(compose_file)["image"] == "grafana/tempo:2.10.7"

    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_no_healthcheck_declared(self, compose_file: str) -> None:
        """The image is distroless (no shell, no wget, no --health flag — verified live against
        the pinned image); a `healthcheck:` block here would be unusable by construction. Guards
        against a future contributor adding one back (e.g. copying the Kibana/Grafana pattern).
        """
        assert "healthcheck" not in self._tempo_service(compose_file)

    def test_dev_config_mount_is_read_only(self) -> None:
        volumes = self._tempo_service("docker-compose.yml")["volumes"]
        config_mount = next(v for v in volumes if "tempo.yaml" in v)
        assert config_mount.endswith(":ro")

    def test_cloud_service_has_resource_limits(self) -> None:
        service = self._tempo_service("docker-compose.cloud.yml")
        assert service["mem_limit"] == "512m"
        assert service["cpus"] == 0.5
        assert service["networks"] == ["cloud-sim"]

    def test_cloud_service_has_no_port_exposed(self) -> None:
        """Tempo is internal-only in the cloud deployment — Grafana is the UI; nothing external
        talks to Tempo directly, and it carries no tunnel host (unlike Kibana/Grafana).
        """
        assert "ports" not in self._tempo_service("docker-compose.cloud.yml")


class TestTempoConfigFile:
    def test_max_duration_exceeds_fourteen_days(self) -> None:
        """AC-1 — the documented Tempo default is 24h and would reject a 14-day TraceQL metrics
        query; must be raised well past 336h (verified live: Tempo pads the query window
        internally, so the override needs real headroom, not an exact 336h match).
        """
        config = yaml.safe_load((repo_root() / "docker/tempo/tempo.yaml").read_text())
        max_duration = config["query_frontend"]["metrics"]["max_duration"]
        hours = int(max_duration.rstrip("h"))
        assert hours >= 336

    def test_otlp_receivers_bind_all_interfaces(self) -> None:
        """0.0.0.0, not localhost — Tempo otherwise defaults to localhost-only receivers, which
        would refuse spans from any other container on the compose network (verified against
        upstream Tempo docs).
        """
        config = yaml.safe_load((repo_root() / "docker/tempo/tempo.yaml").read_text())
        protocols = config["distributor"]["receivers"]["otlp"]["protocols"]
        assert protocols["grpc"]["endpoint"].startswith("0.0.0.0:")
        assert protocols["http"]["endpoint"].startswith("0.0.0.0:")


@pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker compose CLI")
class TestTempoComposeServiceRender:
    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_renders(self, compose_file: str) -> None:
        rendered = _render_compose(compose_file)
        assert "tempo" in rendered["services"]

    def test_dev_compose_declares_volume(self) -> None:
        rendered = _render_compose("docker-compose.yml")
        assert "tempo_data" in rendered["volumes"]

    def test_cloud_compose_declares_volume(self) -> None:
        rendered = _render_compose("docker-compose.cloud.yml")
        assert "tempo_data_cloud" in rendered["volumes"]
