"""Isolation probe for the ADR-0114 study substrate (FRE-838, AC-5).

Verifies the study-net network cannot reach prod's Bolt/Postgres/
Elasticsearch endpoints — the mechanism behind AC-5(1): "prod credentials
are absent from the study environment and a deliberate connection attempt
from the study env to the prod bolt/ES/PG endpoints fails."

Two layers:
  - ``compose_file_leak_paths`` — a static check over ``docker-compose.study.yml``
    (no Docker required) catching the leak paths a runtime probe alone would
    miss: ``network_mode: host``, ``network_mode: service:...``/``container:...``,
    ``extra_hosts`` mapping ``host.docker.internal``, or membership in the
    prod ``cloud-sim`` network.
  - ``dns_resolution_fails`` / ``tcp_connect_fails`` — runtime probes that
    launch a throwaway container on ``seshat-study-net`` and attempt to
    reach prod by its compose service DNS name on its container-internal
    port (``neo4j:7687``, not the host's loopback-published ``127.0.0.1:7687``
    — code-review finding, FRE-838: probing the container's own loopback is
    unreachable from *any* network topology and proves nothing about
    whether study-net is actually isolated from prod).

Usage:
    uv run python scripts/study/verify_isolation.py
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

STUDY_NETWORK = "seshat-study-net"
STUDY_SERVICE_NAME = "neo4j-study"
PROD_PROBE_IMAGE = "neo4j:5.26-community"

# Prod compose service DNS names, paired with the CONTAINER-internal port
# each listens on (not the host's loopback-published port — see module
# docstring). If study-net were ever accidentally joined to prod's
# network, this is the exact name+port a real connection would use —
# Docker's embedded DNS would resolve the name and the container port
# would be reachable directly, which is what makes this probe meaningful
# rather than tautological.
PROD_ENDPOINTS: tuple[tuple[str, int], ...] = (
    ("neo4j", 7687),  # Bolt
    ("postgres", 5432),
    ("elasticsearch", 9200),
)
PROD_DNS_NAMES: tuple[str, ...] = tuple(name for name, _ in PROD_ENDPOINTS)


def compose_file_leak_paths(compose_path: Path) -> list[str]:
    """Return isolation-leak problems found in the study compose file.

    Empty list means clean.
    """
    data = yaml.safe_load(compose_path.read_text())
    service = data["services"][STUDY_SERVICE_NAME]

    problems: list[str] = []
    if "network_mode" in service:
        problems.append(
            f"network_mode is set ({service['network_mode']!r}) — bypasses network isolation"
        )
    if "extra_hosts" in service:
        problems.append(
            f"extra_hosts is set ({service['extra_hosts']!r}) — may map a route to the host"
        )

    networks = service.get("networks", [])
    if "cloud-sim" in networks:
        problems.append("service is joined to cloud-sim (the prod/eval network)")

    return problems


def _run_in_study_network(
    shell_command: str, timeout_s: int = 10
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            STUDY_NETWORK,
            "--entrypoint",
            "bash",
            PROD_PROBE_IMAGE,
            "-c",
            shell_command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def dns_resolution_fails(hostname: str, resolve_timeout_s: int = 3) -> bool:
    """True when *hostname* does not resolve from a study-net container.

    *hostname* is a prod compose service name. A non-joined bridge
    network's embedded DNS server returns NXDOMAIN
    quickly for most names, but some environments fall through to a slower
    external resolver with no route — wrap ``getent`` in an in-container
    ``timeout`` so that case still counts as "fails" (correctly — the name
    did not resolve) instead of hanging past the outer subprocess timeout.
    """
    result = _run_in_study_network(
        f"timeout {resolve_timeout_s} getent hosts {hostname}",
        timeout_s=resolve_timeout_s + 5,
    )
    return result.returncode != 0


def tcp_connect_fails(host: str, port: int, timeout_s: int = 2) -> bool:
    """True when a TCP connect to *host*:*port* fails from a study-net container.

    *host* should be a prod compose service DNS name (see
    ``PROD_ENDPOINTS``), not a loopback address — a container's own
    ``127.0.0.1`` can never reach the Docker host's published ports under
    any network topology, which would make this check pass regardless of
    whether real isolation holds. Uses Bash's ``/dev/tcp`` trick — no
    extra tooling required in the probe image.
    """
    result = _run_in_study_network(f"timeout {timeout_s} bash -c 'exec 3<>/dev/tcp/{host}/{port}'")
    return result.returncode != 0


def verify_isolation() -> dict[str, Any]:
    """Run the full isolation proof: static leak-path check + runtime probes."""
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.study.yml"
    results: dict[str, Any] = {
        "compose_file_leak_paths": compose_file_leak_paths(compose_path),
        "dns_resolution_fails": {name: dns_resolution_fails(name) for name in PROD_DNS_NAMES},
        "tcp_connect_fails": {
            f"{host}:{port}": tcp_connect_fails(host, port) for host, port in PROD_ENDPOINTS
        },
    }
    return results


def main() -> None:
    """CLI entrypoint."""
    print(json.dumps(verify_isolation(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
