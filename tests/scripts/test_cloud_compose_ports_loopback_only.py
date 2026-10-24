# ruff: noqa: D103
"""FRE-1664 — every host port that docker-compose.cloud.yml publishes binds loopback only.

Live finding (2026-10-24): Caddy published "80:80" and "443:443". Docker binds that form on
0.0.0.0 and on [::], so the Caddyfile "localhost" site (SSH-tunnel dev access, no tunnel-only
filter) answered the internet with the PWA page and the gateway API. The Cloudflare tunnel
reaches Caddy over the Docker network (http://caddy:80), so no service here needs a public
host binding. Static, no docker dependency — runs under plain `make test`.
"""

from __future__ import annotations

import yaml

from personal_agent.config.config_guard import repo_root

_LOOPBACK_PREFIX = "127.0.0.1:"


def _published_port_specs() -> list[tuple[str, str]]:
    """Returns (service, port spec) for every published port in the cloud compose file."""
    doc = yaml.safe_load((repo_root() / "docker-compose.cloud.yml").read_text())
    return [
        (name, str(spec))
        for name, service in doc.get("services", {}).items()
        for spec in service.get("ports", [])
    ]


def test_every_published_port_binds_loopback_only() -> None:
    public = [
        f"{service} ({spec})"
        for service, spec in _published_port_specs()
        if not spec.startswith(_LOOPBACK_PREFIX)
    ]
    assert not public, f"docker-compose.cloud.yml publishes ports on every interface: {public}"


def test_caddy_publishes_http_and_https_on_loopback() -> None:
    caddy_specs = {spec for service, spec in _published_port_specs() if service == "caddy"}
    assert {"127.0.0.1:80:80", "127.0.0.1:443:443"} <= caddy_specs
