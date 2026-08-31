# ruff: noqa: D103
"""FRE-1342 — eval gateways must never share production's Redis instance.

`AGENT_EVENT_BUS_REDIS_URL: redis://redis:6379/0` on both eval gateways resolved to
production's Redis (`docker-compose.cloud.yml`'s `redis` service, joined to the same
`cloud-sim` network `up -d` puts the eval gateways on). Redis carries no KG data itself,
but it *transports* the Streams events that cause KG writes — an eval turn publishing
`request.captured` on that shared bus is consumed by production's own consolidator, which
writes to the production knowledge graph. That is exactly the cross-session contamination
FRE-1337's harness exists to measure.

Renders the merged three-file config (not just the eval file's source) so the assertion
matches what `docker compose up` actually reads at runtime — same pattern as
`test_eval_compose_depends_on.py` (FRE-1166).
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


class TestEvalComposeRedisIsolation:
    def test_redis_eval_service_exists_isolated(self) -> None:
        compose = _render_compose()
        services = compose["services"]
        assert "redis-eval" in services, "eval stack must define its own Redis service"
        redis_eval = services["redis-eval"]
        # Must not reuse production's container/volume/port.
        assert redis_eval.get("container_name") != "cloud-sim-redis"
        prod_volume_sources = {v["source"] for v in services["redis"]["volumes"]}
        eval_volume_sources = {v["source"] for v in redis_eval.get("volumes", [])}
        assert not (eval_volume_sources & prod_volume_sources)
        prod_ports = {p["published"] for p in services["redis"].get("ports", [])}
        eval_ports = {p["published"] for p in redis_eval.get("ports", [])}
        assert not (prod_ports & eval_ports)

    def test_eval_gateways_point_at_redis_eval_not_production_redis(self) -> None:
        compose = _render_compose()
        for service in ("seshat-gateway-control", "seshat-gateway-treatment"):
            env = compose["services"][service]["environment"]
            redis_url = env["AGENT_EVENT_BUS_REDIS_URL"]
            assert "redis-eval" in redis_url, (
                f"{service} AGENT_EVENT_BUS_REDIS_URL={redis_url!r} must resolve to the "
                "isolated eval Redis, not production's"
            )
            depends_on = compose["services"][service]["depends_on"]
            assert "redis-eval" in depends_on
            assert "redis" not in depends_on

    def test_makefile_eval_infra_up_names_eval_services_explicitly(self) -> None:
        makefile = (repo_root() / "Makefile").read_text()
        lines = makefile.splitlines()
        target_line = next(i for i, line in enumerate(lines) if line.startswith("eval-infra-up:"))
        up_line = next(
            line
            for line in lines[target_line : target_line + 5]
            if "compose" in line and "up" in line
        )
        assert up_line.rstrip().endswith(
            "up -d --build postgres-eval neo4j-eval elasticsearch-eval redis-eval "
            "seshat-gateway-control seshat-gateway-treatment"
        ), (
            "eval-infra-up must name eval services explicitly, not bring up the "
            f"union of both compose files with no service args: {up_line!r}"
        )

    def test_makefile_eval_infra_up_always_rebuilds_with_a_fresh_fingerprint(self) -> None:
        """FRE-1341: a cached seshat-gateway:latest can silently serve months-stale code.

        `--build` forces a rebuild on every bring-up; BUILD_FINGERPRINT is recomputed from
        the current working tree (including uncommitted changes) so the image that gets
        built actually reflects what a rebuild produces, and /health can report it.
        """
        makefile = (repo_root() / "Makefile").read_text()
        lines = makefile.splitlines()
        target_line = next(i for i, line in enumerate(lines) if line.startswith("eval-infra-up:"))
        up_line = next(
            line
            for line in lines[target_line : target_line + 5]
            if "compose" in line and "up" in line
        )
        assert "BUILD_FINGERPRINT=" in up_line
        assert "scripts.eval.gateway_freshness --print-fingerprint" in up_line
        assert " --build " in up_line
