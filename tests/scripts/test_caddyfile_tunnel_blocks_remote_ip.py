# ruff: noqa: D103
"""FRE-1240 — every inbound Cloudflare-Tunnel site block refuses a peer outside the compose network.

Caddy publishes 80/443 on all host interfaces, so a request to the VPS IP with a forged Host
header reaches a tunnel block without passing Cloudflare Access. `remote_ip` tests the TCP peer
address, which a header cannot forge. This test reads the committed Caddyfile and fails if a
block that serves a tunnel host name has no `remote_ip` matcher, after `import <snippet>` lines
are expanded.

The behavioral proof (the 403 answer on every route) is in test_fre1240_tunnel_only_smoke.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CADDYFILE_PATH = REPO_ROOT / "config" / "cloud-sim" / "Caddyfile"
COMPOSE_PATH = REPO_ROOT / "docker-compose.cloud.yml"

_SUBNET = "172.25.0.0/16"
_TUNNEL_ADDRESS = re.compile(r"\{\$[A-Z_]+_HOST[:}]")
_IMPORT = re.compile(r"^\s*import\s+(\S+)\s*$")
_EXPECTED_TUNNEL_HOSTS = {"AGENT_HOST", "GRAPH_HOST", "ES_HOST", "API_HOST", "OTLP_HOST"}


def _top_level_blocks(text: str) -> dict[str, str]:
    """Map each top-level block address (or snippet name) to its body text."""
    blocks: dict[str, str] = {}
    address: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if address is None:
            if line and not line[0].isspace() and not line.startswith("#") and line.endswith("{"):
                address = line[:-1].strip()
                body = []
        elif line == "}":
            blocks[address] = "\n".join(body)
            address = None
        else:
            body.append(line)
    return blocks


def _expand_imports(body: str, snippets: dict[str, str]) -> str:
    out: list[str] = []
    for line in body.splitlines():
        match = _IMPORT.match(line)
        if match and f"({match.group(1)})" in snippets:
            out.append(_expand_imports(snippets[f"({match.group(1)})"], snippets))
        else:
            out.append(line)
    return "\n".join(out)


def _tunnel_blocks(text: str) -> dict[str, str]:
    """Return the expanded body of each block whose address is a tunnel host name."""
    blocks = _top_level_blocks(text)
    snippets = {name: body for name, body in blocks.items() if name.startswith("(")}
    return {
        address: _expand_imports(body, snippets)
        for address, body in blocks.items()
        if _TUNNEL_ADDRESS.search(address)
    }


def _blocks_without_remote_ip(text: str) -> list[str]:
    return [
        address
        for address, body in _tunnel_blocks(text).items()
        if not re.search(rf"\bremote_ip\s+{re.escape(_SUBNET)}", body)
    ]


def test_the_five_tunnel_blocks_are_found() -> None:
    # Guards the parser: a parser that finds no block would make the checks below pass for nothing.
    addresses = _tunnel_blocks(CADDYFILE_PATH.read_text())
    found = {re.search(r"\{\$([A-Z_]+_HOST)", address).group(1) for address in addresses}  # type: ignore[union-attr]
    assert found == _EXPECTED_TUNNEL_HOSTS


def test_every_tunnel_block_carries_the_remote_ip_restriction() -> None:
    missing = _blocks_without_remote_ip(CADDYFILE_PATH.read_text())
    assert missing == [], f"tunnel block(s) with no remote_ip {_SUBNET} matcher: {missing}"


def test_every_tunnel_block_answers_403_to_the_refused_peer() -> None:
    for address, body in _tunnel_blocks(CADDYFILE_PATH.read_text()).items():
        assert 'respond "Forbidden" 403' in body, f"{address} has no 403 answer"


def test_seeded_negative_a_block_without_the_matcher_is_reported() -> None:
    fixture = (
        "(tunnel_only) {\n"
        f"\t@not_tunnel not remote_ip {_SUBNET}\n"
        "\thandle @not_tunnel {\n"
        '\t\trespond "Forbidden" 403\n'
        "\t}\n"
        "}\n"
        "{$AGENT_HOST:agent.example.com}:80 {\n"
        "\timport tunnel_only\n"
        "\treverse_proxy seshat-pwa:3000\n"
        "}\n"
        "{$GRAPH_HOST:graph.example.com}:80 {\n"
        "\treverse_proxy neo4j:7474\n"
        "}\n"
    )
    assert _blocks_without_remote_ip(fixture) == ["{$GRAPH_HOST:graph.example.com}:80"]


def test_seeded_negative_an_import_of_a_snippet_without_the_matcher_is_reported() -> None:
    fixture = (
        "(tunnel_only) {\n"
        '\trespond "Forbidden" 403\n'
        "}\n"
        "{$API_HOST:api.example.com}:80 {\n"
        "\timport tunnel_only\n"
        "}\n"
    )
    assert _blocks_without_remote_ip(fixture) == ["{$API_HOST:api.example.com}:80"]


def test_caddyfile_subnet_equals_the_compose_cloud_sim_subnet() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    subnets = [c["subnet"] for c in compose["networks"]["cloud-sim"]["ipam"]["config"]]
    assert subnets == [_SUBNET]
