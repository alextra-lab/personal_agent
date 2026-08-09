# ruff: noqa: D103
"""FRE-1224 — host port 4318 on this Mac belongs to the Mac-local Collector, not to a container.

The general form of a collision class the existing guard structurally cannot see.
test_compose_port_collisions.py compares compose services *against each other* within one file; it
has no notion of a host-native process competing for the same port. That is exactly the case here:
the Mac Collector runs under launchd, not in compose, and it must own 4318 because that is
slm_server's default OTLP endpoint (slm_server telemetry.py at ea2b0b8).

Found live while planning FRE-1224: docker-compose.yml bound tempo to "4318:4318" with no interface
prefix, therefore 0.0.0.0, which covers loopback. The two orderings fail differently and one is
silent:

  * Collector binds first (launchd RunAtLoad) -> `docker compose up` fails "port is already
    allocated". Loud, and the same shape as the FRE-1072 Verify Failed incident.
  * Dev stack binds first -> the Collector cannot bind, launchd KeepAlive respawns it forever, and
    slm_server's spans hit connection-refused. Telemetry goes dark with nothing announcing it —
    precisely the outcome FRE-1230's restart gate exists to prevent.

Static, no docker dependency — runs under plain `make test`.
"""

from __future__ import annotations

import pytest
import yaml

from personal_agent.config.config_guard import repo_root

# Reserved for the Mac-local Collector's OTLP/HTTP receiver. Not a free choice: it is
# slm_server's compiled-in default, and keeping it is what makes "no producer change" true.
MAC_COLLECTOR_HOST_PORT = 4318

_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.cloud.yml")


def _host_port_claims(compose_file: str) -> dict[int, list[str]]:
    """Maps host port -> ["service (port spec)"] for every fixed host port a service publishes.

    Collapses by port number alone rather than by (interface, port): a 127.0.0.1 bind and a
    0.0.0.0 bind on the same number still collide at the OS level, so treating them as
    independent would miss the case this module exists for.
    """
    doc = yaml.safe_load((repo_root() / compose_file).read_text())
    claims: dict[int, list[str]] = {}
    for service_name, service in (doc.get("services") or {}).items():
        for port_spec in service.get("ports", []) or []:
            spec = str(port_spec)
            parts = spec.split(":")
            host_port_str = (parts[-2] if len(parts) >= 2 else parts[0]).split("-")[0]
            try:
                host_port = int(host_port_str)
            except ValueError:
                continue  # env-var placeholder or similar — no fixed port to collide on
            claims.setdefault(host_port, []).append(f"{service_name} ({spec})")
    return claims


@pytest.mark.parametrize("compose_file", _COMPOSE_FILES)
def test_no_compose_service_claims_the_mac_collector_port(compose_file: str) -> None:
    claimants = _host_port_claims(compose_file).get(MAC_COLLECTOR_HOST_PORT, [])
    assert not claimants, (
        f"{compose_file}: host port {MAC_COLLECTOR_HOST_PORT} is reserved for the Mac-local "
        f"OTel Collector (slm_server's default OTLP endpoint) but is claimed by: {claimants}. "
        f"A container winning this port sends slm_server's telemetry dark silently."
    )
