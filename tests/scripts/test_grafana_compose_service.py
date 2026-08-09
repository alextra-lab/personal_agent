# ruff: noqa: D103
"""FRE-1072 / ADR-0129 D6 — the grafana compose service's shape, security config, and image pin.

Two layers, same pattern as test_kibana_compose_service.py: a source-only class that parses the
committed YAML directly and always runs, and a render class exercising `docker compose config`'s
actual resolution (skipped without docker), passing the shared gateway override fixture for any
docker-compose.cloud.yml render.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
import yaml

from personal_agent.config.config_guard import repo_root

_RENDER_ENV = {
    **os.environ,
    "POSTGRES_PASSWORD": "test",
    "SESHAT_APP_PASSWORD": "test",
    "NEO4J_PASSWORD": "test",
    "GRAFANA_ADMIN_PASSWORD": "test",
    "GRAFANA_RO_PASSWORD": "test",
}
_RENDER_OVERRIDE = "tests/scripts/fixtures/gateway_render_override.yml"


def _render_compose(compose_file: str) -> dict[str, object]:
    args = ["docker", "compose", "-f", compose_file]
    if compose_file == "docker-compose.cloud.yml":
        args += ["-f", _RENDER_OVERRIDE]
    args += ["config"]
    result = subprocess.run(
        args, cwd=repo_root(), capture_output=True, text=True, env=_RENDER_ENV, check=True
    )
    doc = yaml.safe_load(result.stdout)
    assert isinstance(doc, dict)
    return doc


class TestGrafanaComposeServiceSource:
    """Parses the committed YAML directly — no docker dependency, always runs."""

    def _grafana_service(self, compose_file: str) -> dict[str, object]:
        raw = yaml.safe_load((repo_root() / compose_file).read_text())
        return raw["services"]["grafana"]

    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_image_is_pinned_patched_version(self, compose_file: str) -> None:
        """13.1.3, not 13.1.1: the bundled Tempo datasource plugin in 13.1.1 reports version
        13.1.2, one patch behind the fix for GL-Vuln VUL-2026-0062 (path traversal) — verified
        live by inspecting the actual pinned image's plugin.json.
        """
        assert self._grafana_service(compose_file)["image"] == "grafana/grafana:13.1.3"

    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_anonymous_viewer_access_configured(self, compose_file: str) -> None:
        env = self._grafana_service(compose_file)["environment"]
        assert any("GF_AUTH_ANONYMOUS_ENABLED=true" in str(e) for e in env)
        assert any("GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer" in str(e) for e in env)

    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_login_form_disabled_but_basic_auth_stays_on(self, compose_file: str) -> None:
        """disable_login_form only hides the /login UI form — it does not affect basic auth,
        which the admin API still needs (verified against current Grafana docs, not assumed).
        """
        env = self._grafana_service(compose_file)["environment"]
        assert any("GF_AUTH_DISABLE_LOGIN_FORM=true" in str(e) for e in env)
        assert any("GF_AUTH_BASIC_ENABLED=true" in str(e) for e in env)

    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_router_logging_enabled(self, compose_file: str) -> None:
        """AC-10's positive control depends on it — Grafana does not log successful requests
        otherwise (ADR-0129 D6).
        """
        env = self._grafana_service(compose_file)["environment"]
        assert any("GF_SERVER_ROUTER_LOGGING=true" in str(e) for e in env)

    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_admin_password_not_hardcoded(self, compose_file: str) -> None:
        env = self._grafana_service(compose_file)["environment"]
        password_line = next(e for e in env if "GF_SECURITY_ADMIN_PASSWORD" in str(e))
        assert "${GRAFANA_ADMIN_PASSWORD" in password_line

    def test_cloud_admin_password_is_required_not_silently_blank(self) -> None:
        """Every sibling secret in this file (POSTGRES_PASSWORD, NEO4J_PASSWORD,
        SESHAT_APP_PASSWORD) uses the `${VAR:?required}` guard so compose fails fast on an
        unset var, rather than substituting an empty string and letting Grafana boot with a
        blank admin password — silently undercutting the admin-API auth ADR-0129 D6 treats as
        load-bearing (Viewer-role anonymous access already reaches every datasource).
        """
        env = self._grafana_service("docker-compose.cloud.yml")["environment"]
        password_line = next(e for e in env if "GF_SECURITY_ADMIN_PASSWORD" in str(e))
        assert ":?" in password_line, f"expected a required-var guard, got: {password_line}"

    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_ro_password_not_hardcoded(self, compose_file: str) -> None:
        """FRE-1203: GRAFANA_RO_PASSWORD backs the pg-ledger datasource's grafana_ro credential
        (config/grafana/provisioning/datasources/datasources.yaml) — must come from the
        environment, matching GRAFANA_ADMIN_PASSWORD's pattern above.
        """
        env = self._grafana_service(compose_file)["environment"]
        password_line = next(e for e in env if "GRAFANA_RO_PASSWORD" in str(e))
        assert "${GRAFANA_RO_PASSWORD" in password_line

    def test_cloud_ro_password_is_required_not_silently_blank(self) -> None:
        """Same reasoning as test_cloud_admin_password_is_required_not_silently_blank: a silently
        blank GRAFANA_RO_PASSWORD would mismatch the grafana_ro Postgres role's real password and
        fail the datasource at query time rather than at boot, obscuring the actual cause.
        """
        env = self._grafana_service("docker-compose.cloud.yml")["environment"]
        password_line = next(e for e in env if "GRAFANA_RO_PASSWORD" in str(e))
        assert ":?" in password_line, f"expected a required-var guard, got: {password_line}"

    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_depends_on_postgres_healthy(self, compose_file: str) -> None:
        """FRE-1203: the pg-ledger datasource needs Postgres reachable at boot; unlike Tempo
        (distroless, no healthcheck), Postgres has a real healthcheck so this dependency can wait
        on it rather than merely on process start.
        """
        depends_on = self._grafana_service(compose_file)["depends_on"]
        assert depends_on["postgres"]["condition"] == "service_healthy"

    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_dashboards_provisioned_from_files_not_ui(self, compose_file: str) -> None:
        """Load-bearing per ADR-0129 D6: dashboards are files, reviewable in a diff — not
        UI-assembled.
        """
        volumes = self._grafana_service(compose_file)["volumes"]
        assert any("config/grafana/provisioning" in v and v.endswith(":ro") for v in volumes)
        assert any("config/grafana/dashboards" in v and v.endswith(":ro") for v in volumes)

    def test_depends_on_tempo_started_not_healthy(self) -> None:
        """Tempo has no Docker healthcheck (distroless image, verified live) — a
        condition: service_healthy dependency would never resolve.
        """
        depends_on = self._grafana_service("docker-compose.yml")["depends_on"]
        assert depends_on["tempo"]["condition"] == "service_started"

    def test_cloud_service_binds_loopback_only(self) -> None:
        """No static host port: two consecutive live incidents (3000 collided with seshat-pwa,
        3001 collided with an orphaned next-server process — FRE-1072, 2026-08-07) established
        that no port chosen inside this repo can be known-safe on an arbitrary host. The host
        picks via GRAFANA_HOST_PORT (default 3001); container port stays 3000.
        """
        ports = self._grafana_service("docker-compose.cloud.yml")["ports"]
        assert any(p.startswith("127.0.0.1:${GRAFANA_HOST_PORT:-3001}:3000") for p in ports)

    def test_cloud_service_has_resource_limits(self) -> None:
        service = self._grafana_service("docker-compose.cloud.yml")
        assert service["mem_limit"] == "512m"
        assert service["cpus"] == 0.5

    def test_no_literal_deployment_domain_in_comments(self) -> None:
        """ADR-0129 D6 names `observe` as the placeholder host — never a literal domain (a
        pre-commit hook enforces this repo-wide; this test asserts the convention directly on
        the new block).
        """
        raw = (repo_root() / "docker-compose.cloud.yml").read_text()
        grafana_block_start = raw.index("grafana:\n    image: grafana/grafana")
        comment_start = raw.rindex("# Grafana", 0, grafana_block_start)
        comment_block = raw[comment_start:grafana_block_start]
        assert "observe" in comment_block


@pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker compose CLI")
class TestGrafanaComposeServiceRender:
    @pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.cloud.yml"])
    def test_renders(self, compose_file: str) -> None:
        rendered = _render_compose(compose_file)
        assert "grafana" in rendered["services"]

    def test_dev_compose_declares_volume(self) -> None:
        rendered = _render_compose("docker-compose.yml")
        assert "grafana_data" in rendered["volumes"]

    def test_cloud_compose_declares_volume(self) -> None:
        rendered = _render_compose("docker-compose.cloud.yml")
        assert "grafana_data_cloud" in rendered["volumes"]
