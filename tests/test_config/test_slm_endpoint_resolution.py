"""Per-profile SLM endpoint resolution (ADR-0132 D4, FRE-1144).

The trigger defect for ADR-0132 was a dead ``llm_base_url`` default
(``http://127.0.0.1:1234/v1``) that no deployment could reach and nothing
noticed. D4 closes that class structurally: there is no default at all, each
deployment declares its own SLM endpoint, and an unset value fails at
construction rather than resolving to something unreachable.

These tests are AC-c's instrument.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from personal_agent.config.settings import AppConfig, enforce_slm_endpoint_declared

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The D4 matrix. Each profile declares its endpoint; none inherits a default.
_PROFILE_ENDPOINTS = (
    ("cloud", "http://caddy:8600"),
    ("local", "http://127.0.0.1:8000"),
    ("eval", "http://slm-eval:8000"),
)


class TestSlmEndpointResolution:
    """`AGENT_SLM_BASE_URL` resolves per deployment profile, by exact value."""

    @pytest.mark.parametrize(("profile", "endpoint"), _PROFILE_ENDPOINTS)
    def test_profile_resolves_declared_endpoint_exactly(
        self, monkeypatch: pytest.MonkeyPatch, profile: str, endpoint: str
    ) -> None:
        """Each profile resolves to the endpoint its deployment declares."""
        monkeypatch.setenv("AGENT_DEPLOYMENT_PROFILE", profile)
        monkeypatch.setenv("AGENT_SLM_BASE_URL", endpoint)

        config = AppConfig()

        assert config.deployment_profile == profile
        assert config.slm_base_url == endpoint

    @pytest.mark.parametrize("profile", ["cloud", "eval"])
    def test_unset_endpoint_is_refused_at_boot_for_deployed_profiles(
        self, monkeypatch: pytest.MonkeyPatch, profile: str
    ) -> None:
        """An unset SLM endpoint is refused rather than silently defaulting.

        This is the assertion that closes the dead-default class: there is no
        value to fall back to. Enforcement sits at boot rather than in a model
        validator because ``get_settings()`` runs at import scope across the
        codebase — see :func:`enforce_slm_endpoint_declared`.
        """
        monkeypatch.setenv("AGENT_DEPLOYMENT_PROFILE", profile)
        monkeypatch.delenv("AGENT_SLM_BASE_URL", raising=False)

        config = AppConfig(slm_base_url=None)

        with pytest.raises(ValueError, match="AGENT_SLM_BASE_URL"):
            enforce_slm_endpoint_declared(config)

    def test_tooling_profile_is_not_refused_at_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The `local` default must not brick CI or host tooling.

        `scripts/check_config.py` runs on CI runners with no .env; refusing here
        would turn every CI run red. The endpoint still cannot silently default —
        `resolved_slm_base_url` raises at the point of use.
        """
        monkeypatch.setenv("AGENT_DEPLOYMENT_PROFILE", "local")
        config = AppConfig(slm_base_url=None)

        enforce_slm_endpoint_declared(config)  # does not raise

    def test_unset_endpoint_raises_on_access_not_silently_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reading the endpoint with none declared raises; it never returns a default."""
        monkeypatch.delenv("AGENT_SLM_BASE_URL", raising=False)
        config = AppConfig(slm_base_url=None)

        with pytest.raises(ValueError, match="AGENT_SLM_BASE_URL"):
            _ = config.resolved_slm_base_url

    def test_test_axis_uses_fixture_endpoint_never_the_tunnel(self) -> None:
        """Under ``APP_ENV=test`` the endpoint is the FRE-375 fixture value.

        ``tests/conftest.py`` pins it before any import triggers settings
        resolution, so the suite can never address the real tunnel.
        """
        config = AppConfig()

        assert config.slm_base_url is not None
        assert "slm." not in config.slm_base_url, "test runs must not address the SLM tunnel host"
        assert config.slm_base_url.startswith("http://localhost")


class TestDeadDefaultIsGone:
    """The loopback literal survives nowhere in config-bearing files (AC-c)."""

    def test_no_loopback_1234_literal_in_source_or_config(self) -> None:
        """`grep -rn "127.0.0.1:1234|localhost:1234" src/ config/ .env.example` is empty."""
        result = subprocess.run(
            [
                "grep",
                "-rnI",  # -I: skip binary files (compiled .pyc under __pycache__)
                "--exclude-dir=__pycache__",
                r"127\.0\.0\.1:1234\|localhost:1234",
                "src/",
                "config/",
                ".env.example",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 1, (
            "dead loopback default survives in config-bearing files:\n" + result.stdout
        )

    def test_outbound_cf_fields_are_gone_from_settings(self) -> None:
        """The outbound CF service-token concept no longer exists in app config.

        Inbound JWT verification (``cf_access_team_domain`` / ``cf_access_aud``)
        is deliberately retained — it authenticates arriving requests.
        """
        config = AppConfig()

        assert not hasattr(config, "cf_access_client_id")
        assert not hasattr(config, "cf_access_client_secret")
        assert not hasattr(config, "slm_tunnel_base_url")
        assert not hasattr(config, "llm_base_url")
        # Inbound surface retained.
        assert hasattr(config, "cf_access_team_domain")
        assert hasattr(config, "cf_access_aud")


class TestInferenceRoutesThroughTheProxy:
    """The cloud profile's inference path addresses Caddy, not the tunnel.

    The absence of CF headers is only half the criterion. A build that had lost
    its route to the SLM entirely would also send no headers — what distinguishes
    a working cutover is that model endpoints resolve onto the egress block.
    """

    def test_model_endpoints_resolve_onto_the_configured_slm_base(self) -> None:
        """`models.yaml` placeholder endpoints are rewritten to the SLM base.

        On the cloud profile that base is the internal Caddy egress URL, so this
        rewrite is the mechanism that routes inference through the proxy holding
        the Cloudflare credential.
        """
        from personal_agent.config.model_loader import load_model_config

        config = load_model_config(settings=AppConfig(slm_base_url="http://caddy:8600"))

        # placement is declared once on the PROVIDER (ADR-0121), not per model.
        local_endpoints = [
            definition.endpoint
            for definition in config.models.values()
            if definition.endpoint and "slm" in (definition.provider or "")
        ]
        assert local_endpoints, "expected at least one local model endpoint to check"
        for endpoint in local_endpoints:
            assert endpoint.startswith("http://caddy:8600"), (
                f"local endpoint {endpoint!r} does not route through the egress block"
            )
            assert "slm.example.com" not in endpoint

    def test_health_probe_traverses_the_same_path_as_inference(self) -> None:
        """AC-d: the health probe goes through the egress block, not around it."""
        config = AppConfig(slm_base_url="http://caddy:8600")

        assert config.resolved_slm_health_url == "http://caddy:8600/health"

    def test_health_url_strips_the_openai_v1_suffix(self) -> None:
        """`/health` sits at the server root, not under the OpenAI path."""
        config = AppConfig(slm_base_url="http://localhost:8000/v1")

        assert config.resolved_slm_health_url == "http://localhost:8000/health"
