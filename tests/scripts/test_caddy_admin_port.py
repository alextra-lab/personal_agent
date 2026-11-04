"""The Caddy admin port answers only inside the Caddy container (FRE-1732 AC-4).

Each container on the Docker network could reach ``admin 0.0.0.0:2019``. The compose health check
calls the port on loopback, so both files must agree. The live check is in the ticket's handoff.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_CADDYFILE = REPO_ROOT / "config" / "cloud-sim" / "Caddyfile"
_COMPOSE = REPO_ROOT / "docker-compose.cloud.yml"


def test_the_admin_port_listens_on_loopback_only() -> None:
    """Every ``admin`` line in the global block names localhost."""
    admin = re.findall(r"^\s*admin\s+(\S+)\s*$", _CADDYFILE.read_text(), flags=re.MULTILINE)

    assert admin == ["localhost:2019"]


def test_the_health_check_uses_the_loopback_address() -> None:
    """The health check keeps working after the change."""
    assert "http://127.0.0.1:2019/config/" in _COMPOSE.read_text()
