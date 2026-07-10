"""Tests for the ADR-0114 D1 isolation probe (FRE-838, AC-5).

The static compose-file check runs unmarked (fast, no infra). The runtime
DNS/TCP probes are marked `integration` — they require Docker and the
`seshat-study-net` network (`make study-infra-up`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.study.verify_isolation import (
    PROD_DNS_NAMES,
    PROD_ENDPOINTS,
    compose_file_leak_paths,
    dns_resolution_fails,
    tcp_connect_fails,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "docker-compose.study.yml"


def test_study_compose_file_has_no_leak_paths() -> None:
    problems = compose_file_leak_paths(COMPOSE_FILE)
    assert problems == []


def test_leak_path_detection_catches_network_mode_host(tmp_path: Path) -> None:
    bad_compose = tmp_path / "docker-compose.study.yml"
    bad_compose.write_text(
        "services:\n  neo4j-study:\n    image: neo4j:5.26-community\n    network_mode: host\n"
    )
    problems = compose_file_leak_paths(bad_compose)
    assert any("network_mode" in p for p in problems)


def test_leak_path_detection_catches_cloud_sim_membership(tmp_path: Path) -> None:
    bad_compose = tmp_path / "docker-compose.study.yml"
    bad_compose.write_text(
        "services:\n"
        "  neo4j-study:\n"
        "    image: neo4j:5.26-community\n"
        "    networks:\n"
        "      - cloud-sim\n"
    )
    problems = compose_file_leak_paths(bad_compose)
    assert any("cloud-sim" in p for p in problems)


def test_leak_path_detection_catches_extra_hosts(tmp_path: Path) -> None:
    bad_compose = tmp_path / "docker-compose.study.yml"
    bad_compose.write_text(
        "services:\n"
        "  neo4j-study:\n"
        "    image: neo4j:5.26-community\n"
        "    extra_hosts:\n"
        "      - host.docker.internal:host-gateway\n"
    )
    problems = compose_file_leak_paths(bad_compose)
    assert any("extra_hosts" in p for p in problems)


@pytest.mark.integration
class TestRuntimeIsolation:
    """Requires Docker + `make study-infra-up` (creates seshat-study-net)."""

    @pytest.mark.parametrize("hostname", PROD_DNS_NAMES)
    def test_prod_service_dns_names_do_not_resolve_from_study_net(self, hostname: str) -> None:
        assert dns_resolution_fails(hostname) is True

    @pytest.mark.parametrize(("host", "port"), PROD_ENDPOINTS)
    def test_prod_endpoints_are_unreachable_from_study_net(self, host: str, port: int) -> None:
        assert tcp_connect_fails(host, port) is True
