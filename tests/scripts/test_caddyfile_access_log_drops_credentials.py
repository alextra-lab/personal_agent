# ruff: noqa: D103
"""FRE-1786 — no inbound access logger writes a credential header.

Caddy logs every request header. It redacts `Cookie` and `Authorization` by itself, but not the
Cloudflare Access JWT headers or the Worker's `X-Internal-Token`. Those values went to
`/var/log/caddy/access.log` and, through Filebeat, to `caddy-access-*`. Every inbound logger must
delete them. The egress loggers log the gateway's own outbound requests, which carry none of them.

Adapts the Caddyfile with the real `caddy` binary, as `test_caddyfile_logs_to_fixed_path.py` does.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from personal_agent.config.config_guard import repo_root

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker CLI")

CADDYFILE = repo_root() / "config" / "cloud-sim" / "Caddyfile"
CREDENTIAL_HEADERS = (
    "Cf-Access-Jwt-Assertion",
    "X-Cf-Access-Jwt-Assertion",
    "X-Internal-Token",
)
EGRESS_LOGGERS = {"egress_slm", "egress_artifacts"}


def _loggers() -> dict[str, dict]:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{CADDYFILE}:/etc/caddy/Caddyfile:ro",
            "caddy:2-alpine",
            "caddy",
            "adapt",
            "--config",
            "/etc/caddy/Caddyfile",
            "--adapter",
            "caddyfile",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    logs = json.loads(result.stdout)["logging"]["logs"]
    return {name: cfg for name, cfg in logs.items() if name != "default"}


def _inbound() -> dict[str, dict]:
    return {name: cfg for name, cfg in _loggers().items() if name not in EGRESS_LOGGERS}


def test_the_inbound_loggers_are_the_six_tunnel_and_local_blocks() -> None:
    """Routing x2 (localhost and the agent host), graph, es, otlp and api."""
    assert len(_inbound()) == 6


@pytest.mark.parametrize("header", CREDENTIAL_HEADERS)
def test_every_inbound_logger_deletes_the_credential_header(header: str) -> None:
    missing = []
    for name, cfg in _inbound().items():
        encoder = cfg.get("encoder", {})
        field = encoder.get("fields", {}).get(f"request>headers>{header}", {})
        if encoder.get("format") != "filter" or field.get("filter") != "delete":
            missing.append(name)
    assert missing == [], f"{header} is logged by: {missing}"


def test_every_filtered_logger_still_writes_json() -> None:
    for name, cfg in _inbound().items():
        assert cfg["encoder"]["wrap"]["format"] == "json", name
