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

    @staticmethod
    def _eval_infra_up_commands() -> list[str]:
        """The recipe's `docker compose ... up` commands, as `make -n` expands them.

        FRE-1542 split the single `up` into a substrate phase and a gateway phase, so these
        tests read the expanded recipe instead of one Makefile line.
        """
        recipe = subprocess.run(
            ["make", "-n", "eval-infra-up"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        commands = [
            part.strip().removesuffix("|| exit 1").strip()
            for line in recipe.replace("\\\n", " ").splitlines()
            for part in line.split(";")
            if " up -d " in part and "--dry-run" not in part
        ]
        assert len(commands) == 2, commands
        return commands

    def test_makefile_eval_infra_up_names_eval_services_explicitly(self) -> None:
        substrate, gateways = self._eval_infra_up_commands()
        assert substrate.rstrip().endswith(
            "up -d --no-deps --wait postgres-eval neo4j-eval elasticsearch-eval redis-eval"
        ), (
            "eval-infra-up must name eval services explicitly, not bring up the "
            f"union of both compose files with no service args: {substrate!r}"
        )
        assert gateways.rstrip().endswith(
            "up -d --no-deps --build seshat-gateway-control seshat-gateway-treatment"
        ), f"eval-infra-up must name the eval gateways explicitly: {gateways!r}"

    def test_makefile_eval_infra_up_always_rebuilds_with_a_fresh_fingerprint(self) -> None:
        """FRE-1341: a cached seshat-gateway:latest can silently serve months-stale code.

        `--build` forces a rebuild on every bring-up; BUILD_FINGERPRINT is recomputed from
        the current working tree (including uncommitted changes) so the image that gets
        built actually reflects what a rebuild produces, and /health can report it.
        """
        _, gateways = self._eval_infra_up_commands()
        assert "BUILD_FINGERPRINT=" in gateways
        assert "scripts.eval.gateway_freshness --print-fingerprint" in gateways
        assert " --build " in gateways
