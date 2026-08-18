# ruff: noqa: D103
"""Guards against a container claiming a fixed address on the cloud-sim network.

Live-incident regression (FRE-1244): docker-compose.cloud.yml pinned seshat-pwa (172.25.0.11) and
caddy (172.25.0.12) to static addresses while every other service free-floats to the lowest free
address in the same subnet. Recreating a free-floating service while a static-holder is stopped lets
the free-floater squat the vacated static address; the static-holder then cannot start at all
("Address already in use"), and no retry fixes it because the squatter never yields. This already took
external ingress down for the duration of manual recovery.

Separately, a hardcoded IP address in the Caddyfile (`http://172.25.0.10`, a leftover from before
commit 4bc0ee0c moved Caddy's static address to .12 without updating the Caddyfile) is the same class
of bug one layer over: an address baked into config that the actual container may not hold.

This is the general form, not specific to Caddy or the PWA: no service in any compose file should
declare a fixed network address, and no Caddyfile site block should hardcode one. Static, no
docker/live-stack dependency — runs under plain `make test`.

Deliberately narrow in scope, matching FRE-1244's own AC-1 "how checked" column (grep `config/` and
`docker-compose*.yml`) rather than an unbounded repo-wide literal-IP scan: a CIDR used as an ACL
matcher (`remote_ip 172.25.0.0/16`) or the network's own `ipam.config.subnet` declaration are legitimate
and must not be flagged — those describe the network's address space or a matcher, not a container's
claimed address.
"""

from __future__ import annotations

import re

import yaml

from personal_agent.config.config_guard import repo_root

_COMPOSE_FILES = [
    "docker-compose.yml",
    "docker-compose.cloud.yml",
    "docker-compose.eval.yml",
    "docker-compose.study.yml",
    "docker-compose.test.yml",
]

# A Caddy site-block header naming a raw IPv4 literal as the address, e.g. `http://172.25.0.10 {`
# or `172.25.0.10:80 {`. Anchored to line start (a block header), not any line mentioning an IP —
# an ACL matcher like `remote_ip 172.25.0.0/16` must not match.
_CADDY_IP_SITE_BLOCK = re.compile(r"^(https?://)?(\d{1,3}\.){3}\d{1,3}(:\d+)?\s*\{")


def _pinned_addresses(compose_file: str) -> dict[str, list[str]]:
    """Maps service name -> list of fixed-address keys it declares (ipv4_address/ipv6_address)."""
    path = repo_root() / compose_file
    doc = yaml.safe_load(path.read_text())
    pinned: dict[str, list[str]] = {}
    for service_name, service in doc.get("services", {}).items():
        networks = service.get("networks")
        if not isinstance(networks, dict):
            continue
        for net_config in networks.values():
            if not isinstance(net_config, dict):
                continue
            claimed = [key for key in ("ipv4_address", "ipv6_address") if key in net_config]
            if claimed:
                pinned.setdefault(service_name, []).extend(claimed)
    return pinned


def test_no_service_declares_a_fixed_network_address() -> None:
    offenders = {}
    for compose_file in _COMPOSE_FILES:
        path = repo_root() / compose_file
        if not path.exists():
            continue
        pinned = _pinned_addresses(compose_file)
        if pinned:
            offenders[compose_file] = pinned
    assert not offenders, (
        f"service(s) declare a fixed network address — this is exactly the FRE-1244 squat vector: "
        f"{offenders}"
    )


def test_caddyfile_has_no_hardcoded_ip_site_block() -> None:
    path = repo_root() / "config" / "cloud-sim" / "Caddyfile"
    offenders = [
        line.strip()
        for line in path.read_text().splitlines()
        if _CADDY_IP_SITE_BLOCK.match(line.strip())
    ]
    assert not offenders, (
        f"Caddyfile site block addressed by raw IP instead of hostname/service routing: {offenders}"
    )
