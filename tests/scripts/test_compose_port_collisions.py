# ruff: noqa: D103
"""Guards against two compose services claiming the same host port.

Live-incident regression (2026-08-07): docker-compose.cloud.yml bound Grafana to host port
127.0.0.1:3000, colliding with seshat-pwa's own pre-existing 127.0.0.1:3000 binding on the same
host. `docker compose config` validates the compose *file* only — it has no notion of runtime
port availability against a sibling service — so this was invisible to CI and only surfaced when
Grafana's container actually tried to start on a real host, after merge. Docker refused with
"port is already allocated" and the deploy failed (FRE-1072 Verify Failed).

This is the general form of that bug, not specific to Grafana: any two services in the same
compose file that publish the same host port will collide at container-start time regardless of
which services they are. Static, no docker/live-stack dependency — runs under plain `make test`.
"""

from __future__ import annotations

import yaml

from personal_agent.config.config_guard import repo_root


def _host_ports(compose_file: str) -> dict[int, list[str]]:
    """Maps host port -> list of "service (full port spec)" claiming it.

    Collapses by port number alone, not by (interface, port): a 127.0.0.1 bind and a 0.0.0.0
    bind on the same port number can still collide at the OS level (0.0.0.0 covers every
    interface, including loopback), so treating them as independent would miss exactly this
    class of incident.
    """
    doc = yaml.safe_load((repo_root() / compose_file).read_text())
    claims: dict[int, list[str]] = {}
    for service_name, service in doc.get("services", {}).items():
        for port_spec in service.get("ports", []):
            spec = str(port_spec)
            parts = spec.split(":")
            host_port_str = parts[-2] if len(parts) >= 2 else parts[0]
            host_port_str = host_port_str.split("-")[
                0
            ]  # a-b ranges: only the range start matters here
            try:
                host_port = int(host_port_str)
            except ValueError:
                continue  # not a fixed host port (e.g. an env-var placeholder) — nothing to collide on
            claims.setdefault(host_port, []).append(f"{service_name} ({spec})")
    return claims


def _assert_no_collisions(compose_file: str) -> None:
    claims = _host_ports(compose_file)
    collisions = {port: services for port, services in claims.items() if len(services) > 1}
    assert not collisions, (
        f"{compose_file}: host port(s) claimed by more than one service: {collisions}"
    )


def test_no_host_port_collisions_in_dev_compose() -> None:
    _assert_no_collisions("docker-compose.yml")


def test_no_host_port_collisions_in_cloud_compose() -> None:
    _assert_no_collisions("docker-compose.cloud.yml")
