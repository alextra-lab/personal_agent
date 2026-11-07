# ruff: noqa: D103
"""Guards for the dev-agent network and its egress proxy (ADR-0159 D4.1, D4.2, D4.5; FRE-1682).

The dev-agent network has no route out. Its only exit is one proxy that forwards to one host.
These checks read the compose file and the Squid config as text and fail when a change opens a
second exit. The live proof is `make dev-agent-verify`. Static, no Docker needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from personal_agent.config.config_guard import repo_root

_ROOT = repo_root()
_COMPOSE = _ROOT / "docker-compose.cloud.yml"
_SQUID = _ROOT / "docker" / "dev-agent" / "squid.conf"
_PROXY = "dev-agent-proxy"


@pytest.fixture(scope="module")
def compose() -> dict[str, object]:
    data = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _networks_of(service: dict[str, object]) -> set[str]:
    networks = service.get("networks", {})
    if isinstance(networks, dict):
        return set(networks)
    assert isinstance(networks, list)
    return {str(n) for n in networks}


def test_dev_agent_network_is_internal(compose: dict[str, object]) -> None:
    networks = compose["networks"]
    assert isinstance(networks, dict)
    assert networks["dev-agent"]["internal"] is True


def test_the_egress_network_is_not_internal(compose: dict[str, object]) -> None:
    networks = compose["networks"]
    assert isinstance(networks, dict)
    assert not networks["dev-agent-egress"].get("internal", False)


def test_only_the_proxy_joins_dev_agent(compose: dict[str, object]) -> None:
    services = compose["services"]
    assert isinstance(services, dict)
    members = {name for name, svc in services.items() if "dev-agent" in _networks_of(svc)}
    assert members == {_PROXY}


def test_only_the_proxy_joins_the_egress_network(compose: dict[str, object]) -> None:
    services = compose["services"]
    assert isinstance(services, dict)
    members = {name for name, svc in services.items() if "dev-agent-egress" in _networks_of(svc)}
    assert members == {_PROXY}


def test_the_proxy_is_on_those_two_networks_only(compose: dict[str, object]) -> None:
    services = compose["services"]
    assert isinstance(services, dict)
    assert _networks_of(services[_PROXY]) == {"dev-agent", "dev-agent-egress"}


def test_the_proxy_has_the_dev_agent_alias(compose: dict[str, object]) -> None:
    services = compose["services"]
    assert isinstance(services, dict)
    assert _PROXY in services[_PROXY]["networks"]["dev-agent"]["aliases"]


def test_the_proxy_is_hardened_and_holds_nothing(compose: dict[str, object]) -> None:
    services = compose["services"]
    assert isinstance(services, dict)
    proxy = services[_PROXY]
    assert proxy["read_only"] is True
    assert proxy["cap_drop"] == ["ALL"]
    assert any(str(o).startswith("no-new-privileges") for o in proxy["security_opt"])
    for forbidden in ("ports", "volumes", "env_file", "privileged", "cap_add", "depends_on"):
        assert forbidden not in proxy, forbidden
    assert "docker.sock" not in yaml.safe_dump(proxy)
    assert not proxy.get("environment")


def test_the_proxy_sets_no_static_address_and_no_container_name(
    compose: dict[str, object],
) -> None:
    services = compose["services"]
    assert isinstance(services, dict)
    proxy_text = yaml.safe_dump(services[_PROXY])
    assert "ipv4_address" not in proxy_text
    assert "container_name" not in services[_PROXY]


def _squid_lines() -> list[str]:
    lines = [ln.strip() for ln in _SQUID.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def test_squid_access_rules_in_order() -> None:
    rules = [ln for ln in _squid_lines() if ln.startswith("http_access")]
    assert rules == [
        "http_access deny !CONNECT",
        "http_access deny numeric_authority",
        "http_access deny !tls_port",
        "http_access allow npm_registry",
        "http_access deny all",
    ]


def test_squid_allows_exactly_one_host_without_a_reverse_lookup() -> None:
    acls = [ln for ln in _squid_lines() if ln.startswith("acl ")]
    registry = [ln for ln in acls if ln.startswith("acl npm_registry ")]
    assert registry == ["acl npm_registry dstdomain -n registry.npmjs.org"]
    numeric = [ln for ln in acls if ln.startswith("acl numeric_authority ")]
    assert len(numeric) == 1 and " -n " in numeric[0]
    assert [ln for ln in acls if ln.startswith("acl tls_port ")] == ["acl tls_port port 443"]


def test_squid_keeps_no_cache_and_no_second_listener() -> None:
    lines = _squid_lines()
    assert lines.count("cache deny all") == 1
    assert [ln for ln in lines if ln.startswith("http_port")] == ["http_port 3128"]
    assert not [ln for ln in lines if ln.startswith(("https_port", "tcp_outgoing", "cache_peer"))]
