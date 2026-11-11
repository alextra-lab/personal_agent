# ruff: noqa: D103
"""FRE-1758 (AC-4) — the cloud compose passes the owner ntfy URL to the gateway with ``:-``.

The gateway entry is in ``environment:``, which beats ``env_file``. So the test renders the file
through ``docker compose config`` and checks the resolved value: a set URL must arrive unchanged,
and an unset URL must render as an empty string, not as an error. The URL is a secret, so the
test sets a placeholder only and never reads or prints the real value.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest
import yaml

from personal_agent.config.config_guard import repo_root

_KEY = "AGENT_OWNER_NTFY_URL"
_PLACEHOLDER = "https://ntfy.invalid/placeholder-topic-fre1758"
_RENDER_OVERRIDE = "tests/scripts/fixtures/gateway_render_override.yml"
_BASE_ENV = {
    "POSTGRES_PASSWORD": "test",
    "SESHAT_APP_PASSWORD": "test",
    "NEO4J_PASSWORD": "test",
    "GRAFANA_ADMIN_PASSWORD": "test",
    "GRAFANA_RO_PASSWORD": "test",
    "CF_ACCESS_TEAM_DOMAIN": "team.example.test",
    "GRAFANA_CF_ACCESS_AUD": "0" * 64,
}


def _gateway_environment(url: str | None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != _KEY}
    env.update(_BASE_ENV)
    if url is not None:
        env[_KEY] = url
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.cloud.yml", "-f", _RENDER_OVERRIDE, "config"],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    doc = yaml.safe_load(result.stdout)
    environment = doc["services"]["seshat-gateway"]["environment"]
    assert isinstance(environment, dict)
    return environment


def test_the_compose_entry_is_optional_and_never_required() -> None:
    source = (repo_root() / "docker-compose.cloud.yml").read_text(encoding="utf-8")
    entries = re.findall(rf"^\s*{_KEY}:\s*(.+)$", source, flags=re.MULTILINE)
    assert entries == ["${AGENT_OWNER_NTFY_URL:-}"]
    assert ":?" not in entries[0]


@pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker compose CLI")
def test_a_set_url_reaches_the_gateway_unchanged() -> None:
    assert _gateway_environment(_PLACEHOLDER)[_KEY] == _PLACEHOLDER


@pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker compose CLI")
def test_an_unset_url_renders_as_empty_and_the_render_still_works() -> None:
    assert _gateway_environment(None)[_KEY] == ""
