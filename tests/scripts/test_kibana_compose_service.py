# ruff: noqa: D103
"""FRE-1187 / ADR-0134 — the kibana compose service's encryption-key wiring.

Guards that the saved-objects encryption key is sourced from a kibana-only env file
(ADR-0132 D1 precedent) rather than the shared /opt/seshat/.env that seshat-gateway imports
wholesale — the leak codex plan-review flagged for this ticket.

Two layers, because `docker compose config` fully resolves an optional env_file and drops the
directive entirely from its output when the file is absent (verified live, FRE-1187) — it cannot
prove the wiring exists on a host without /opt/seshat/.env.kibana present:

- The static test parses the committed source YAML directly and always runs — it is the
  proof-of-record for what's declared, on any machine, without touching the filesystem.
- The live-merge test additionally exercises `docker compose config`'s actual resolution with a
  throwaway key value, but only where /opt/seshat exists (this VPS) — a fresh clone or CI runner
  has no such path and skips.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from personal_agent.config.config_guard import repo_root

_KIBANA_ENV_FILE = Path("/opt/seshat/.env.kibana")

_RENDER_ENV = {
    **os.environ,
    "POSTGRES_PASSWORD": "test",
    "SESHAT_APP_PASSWORD": "test",
    "NEO4J_PASSWORD": "test",
}


def _render_compose() -> dict[str, object]:
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.cloud.yml", "config"],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        env=_RENDER_ENV,
        check=True,
    )
    doc = yaml.safe_load(result.stdout)
    assert isinstance(doc, dict)
    return doc


class TestKibanaComposeServiceSource:
    """Parses the committed YAML directly — no docker dependency, always runs."""

    def _kibana_service(self) -> dict[str, object]:
        raw = yaml.safe_load((repo_root() / "docker-compose.cloud.yml").read_text())
        return raw["services"]["kibana"]

    def test_encryption_key_sourced_from_kibana_only_env_file(self) -> None:
        env_files = self._kibana_service()["env_file"]
        assert len(env_files) == 1
        assert env_files[0]["path"] == "/opt/seshat/.env.kibana"
        assert env_files[0]["required"] is False

    def test_encryption_key_not_declared_under_environment(self) -> None:
        """The value must come from the scoped env_file, never a plain `environment:` entry."""
        environment = self._kibana_service().get("environment", [])
        assert not any("XPACK_ENCRYPTEDSAVEDOBJECTS_ENCRYPTIONKEY" in str(e) for e in environment)


@pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker compose CLI")
class TestKibanaComposeServiceRender:
    """Exercises `docker compose config`'s actual resolution — proves the runtime behavior."""

    def test_renders_without_env_kibana_present(self) -> None:
        """required: false — a host with no /opt/seshat/.env.kibana still parses (fresh clone, CI)."""
        _render_compose()

    def test_existing_environment_block_unchanged(self) -> None:
        kibana = _render_compose()["services"]["kibana"]
        environment = kibana["environment"]
        assert environment["ELASTICSEARCH_HOSTS"] == "http://elasticsearch:9200"
        assert environment["TELEMETRY_OPTIN"] == "false"

    @pytest.mark.skipif(
        not _KIBANA_ENV_FILE.parent.is_dir(), reason="requires /opt/seshat (this VPS only)"
    )
    def test_env_file_value_merges_into_rendered_environment(self) -> None:
        if _KIBANA_ENV_FILE.exists():
            pytest.skip("/opt/seshat/.env.kibana already present — refusing to overwrite it")
        _KIBANA_ENV_FILE.write_text(
            "XPACK_ENCRYPTEDSAVEDOBJECTS_ENCRYPTIONKEY=pytest-throwaway-value\n"
        )
        try:
            kibana = _render_compose()["services"]["kibana"]
            assert (
                kibana["environment"]["XPACK_ENCRYPTEDSAVEDOBJECTS_ENCRYPTIONKEY"]
                == "pytest-throwaway-value"
            )
        finally:
            _KIBANA_ENV_FILE.unlink()
