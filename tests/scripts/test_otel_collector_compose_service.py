# ruff: noqa: D103
"""FRE-1070 / ADR-0129 D5 — the otel-collector compose service's shape, image pin, and config.

Same two-layer pattern as test_tempo_compose_service.py: a source-only class that parses the
committed YAML directly and always runs, and a render class that exercises `docker compose
config`'s actual resolution (skipped without docker).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from urllib.parse import urlsplit

import pytest
import yaml

from personal_agent.config.config_guard import repo_root

_RENDER_ENV = {
    **os.environ,
    "POSTGRES_PASSWORD": "test",
    "SESHAT_APP_PASSWORD": "test",
    "NEO4J_PASSWORD": "test",
    "GRAFANA_ADMIN_PASSWORD": "test",
    "GRAFANA_RO_PASSWORD": "test",
}
_RENDER_OVERRIDE = "tests/scripts/fixtures/gateway_render_override.yml"

# Hostnames a Collector exporter may legitimately target — compose-internal service names or
# loopback. Anything else is off-box (AC-4).
_ALLOWED_EXPORTER_HOSTS = {"tempo", "localhost", "127.0.0.1"}


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


def _exporter_hostname(endpoint: str) -> str:
    """Extract the hostname from either a bare ``host:port`` or a URL-form endpoint.

    Fails loudly (raises) on a shape it cannot parse, rather than silently skipping it —
    an unrecognized exporter endpoint shape must not pass AC-4 by accident.
    """
    if "://" in endpoint:
        parsed = urlsplit(endpoint)
        if not parsed.hostname:
            raise ValueError(f"could not parse hostname from URL-form endpoint: {endpoint!r}")
        return parsed.hostname
    host, _, port = endpoint.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"could not parse host:port from endpoint: {endpoint!r}")
    return host


class TestOtelCollectorComposeServiceSource:
    """Parses the committed YAML directly — no docker dependency, always runs."""

    def _service(self, compose_file: str) -> dict[str, object]:
        raw = yaml.safe_load((repo_root() / compose_file).read_text())
        return raw["services"]["otel-collector"]

    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_image_is_pinned_core_distribution(self, compose_file: str) -> None:
        """The plain core `otel/opentelemetry-collector` image, not `-contrib` and not a vendor
        distro (Alloy/EDOT/Splunk/Datadog) — ADR-0129 D5's "vanilla upstream Collector".
        """
        assert self._service(compose_file)["image"] == "otel/opentelemetry-collector:0.158.0"

    def test_dev_config_mount_is_read_only(self) -> None:
        volumes = self._service("docker-compose.yml")["volumes"]
        config_mount = next(v for v in volumes if "collector-config.yaml" in v)
        assert config_mount.endswith(":ro")

    def test_dev_ports_bind_loopback_only(self) -> None:
        ports = self._service("docker-compose.yml")["ports"]
        assert ports, "expected host-mapped OTLP ports for the acceptance fixture"
        for spec in ports:
            assert str(spec).startswith("127.0.0.1:"), f"expected loopback-only bind, got {spec!r}"

    def test_dev_ports_do_not_collide_with_tempo(self) -> None:
        """Tempo owns 4327/4328 on the host for FRE-1072's direct-inject tests; the Collector must
        not claim the same host ports.

        Tempo moved off host 4317/4318 in FRE-1224: host 4318 belongs to the Mac-local OTel
        Collector, since it is slm_server's compiled-in default OTLP endpoint.
        """
        ports = self._service("docker-compose.yml")["ports"]
        host_ports = {str(spec).split(":")[1] for spec in ports}
        assert host_ports.isdisjoint({"4327", "4328"})

    def test_cloud_service_has_resource_limits(self) -> None:
        service = self._service("docker-compose.cloud.yml")
        assert service["mem_limit"] == "512m"
        assert service["cpus"] == 0.5
        assert service["networks"] == ["cloud-sim"]

    def test_cloud_service_has_no_port_exposed(self) -> None:
        """Internal-only in the cloud deployment — nothing outside the compose network needs
        to reach the Collector directly.
        """
        assert "ports" not in self._service("docker-compose.cloud.yml")

    def test_cloud_gateway_points_at_collector(self) -> None:
        raw = yaml.safe_load((repo_root() / "docker-compose.cloud.yml").read_text())
        gateway_env = raw["services"]["seshat-gateway"]["environment"]
        assert gateway_env["AGENT_OTEL_EXPORTER_ENDPOINT"] == "otel-collector:4317"


class TestOtelCollectorConfigFile:
    def _config(self) -> dict[str, object]:
        return yaml.safe_load((repo_root() / "config/otel/collector-config.yaml").read_text())

    def test_receivers_bind_all_interfaces(self) -> None:
        protocols = self._config()["receivers"]["otlp"]["protocols"]
        assert protocols["grpc"]["endpoint"].startswith("0.0.0.0:")
        assert protocols["http"]["endpoint"].startswith("0.0.0.0:")

    def test_redaction_processor_targets_the_declared_fixture_key(self) -> None:
        actions = self._config()["processors"]["attributes/redaction"]["actions"]
        assert actions == [{"key": "fre1070.fixture.blocked", "action": "delete"}]

    def test_redaction_processor_does_not_touch_real_span_attributes(self) -> None:
        """A blocklist rule naming a real attribute key would silently strip production trace
        data — this must never happen (the original contrib allow-list draft this ticket
        replaced would have deleted three of these; see the plan's revision note).
        """
        real_attrs = {
            "personal_agent.step.iteration",
            "personal_agent.step.tool_count",
            "personal_agent.tool.name",
            "service.name",
            "gen_ai.operation.name",
        }
        actions = self._config()["processors"]["attributes/redaction"]["actions"]
        blocked_keys = {a["key"] for a in actions}
        assert blocked_keys.isdisjoint(real_attrs)

    def test_no_exporter_addresses_anything_off_box(self) -> None:
        """AC-4 — every exporter endpoint resolves to a compose-internal service name or
        localhost. Fails (does not skip) on an endpoint shape it cannot parse.
        """
        exporters = self._config()["exporters"]
        checked = 0
        for name, spec in exporters.items():
            endpoint = spec.get("endpoint") if isinstance(spec, dict) else None
            if endpoint is None:
                continue  # e.g. `debug` — stdout, not a network exporter
            host = _exporter_hostname(endpoint)
            assert host in _ALLOWED_EXPORTER_HOSTS, (
                f"exporter {name!r} addresses off-box host {host!r} (endpoint={endpoint!r})"
            )
            checked += 1
        assert checked > 0, "expected at least one network exporter to check"

    def test_pipeline_wires_redaction_before_export(self) -> None:
        traces = self._config()["service"]["pipelines"]["traces"]
        assert "attributes/redaction" in traces["processors"]
        assert set(traces["exporters"]) == {"otlp_grpc/tempo", "debug"}


@pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker compose CLI")
class TestOtelCollectorComposeServiceRender:
    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_renders(self, compose_file: str) -> None:
        rendered = _render_compose(compose_file)
        assert "otel-collector" in rendered["services"]

    def test_dev_service_depends_on_tempo(self) -> None:
        rendered = _render_compose("docker-compose.yml")
        assert "tempo" in rendered["services"]["otel-collector"]["depends_on"]
