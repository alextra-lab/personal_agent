"""FRE-1341 — eval gateway build must receive BUILD_FINGERPRINT so the running container
can report what it's actually built from (`/health`'s `build_fingerprint` field).

Renders the merged three-file config, same pattern as `test_eval_compose_redis_isolation.py`
(FRE-1342) — the assertion must match what `docker compose build` actually reads at runtime,
not just the eval file's source text.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
import yaml

from personal_agent.config.config_guard import repo_root

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="requires the docker compose CLI"
)

_RENDER_ENV = {
    **os.environ,
    "POSTGRES_PASSWORD": "test",
    "SESHAT_APP_PASSWORD": "test",
    "NEO4J_PASSWORD": "test",
    "AGENT_OWNER_EMAIL": "test@example.com",
    "GRAFANA_ADMIN_PASSWORD": "test",
    "GRAFANA_RO_PASSWORD": "test",
    "BUILD_FINGERPRINT": "test-fingerprint-abc123",
}

_RENDER_OVERRIDE = "tests/scripts/fixtures/gateway_render_override.yml"


def _render_compose() -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.cloud.yml",
            "-f",
            "docker-compose.eval.yml",
            "-f",
            _RENDER_OVERRIDE,
            "config",
        ],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        env=_RENDER_ENV,
        check=True,
    )
    doc = yaml.safe_load(result.stdout)
    assert isinstance(doc, dict)
    return doc


class TestEvalComposeBuildArgs:
    def test_gateway_services_receive_build_fingerprint_arg(self) -> None:
        compose = _render_compose()
        for service in ("seshat-gateway-control", "seshat-gateway-treatment"):
            build = compose["services"][service]["build"]
            args = build.get("args") or {}
            assert args.get("BUILD_FINGERPRINT") == "test-fingerprint-abc123", (
                f"{service}'s build.args.BUILD_FINGERPRINT must resolve from the shell "
                f"env, got: {args!r}"
            )

    def test_build_fingerprint_defaults_to_unknown_when_unset(self) -> None:
        env = {k: v for k, v in _RENDER_ENV.items() if k != "BUILD_FINGERPRINT"}
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.cloud.yml",
                "-f",
                "docker-compose.eval.yml",
                "-f",
                _RENDER_OVERRIDE,
                "config",
            ],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        doc = yaml.safe_load(result.stdout)
        args = doc["services"]["seshat-gateway-control"]["build"].get("args") or {}
        assert args.get("BUILD_FINGERPRINT") == "unknown"
