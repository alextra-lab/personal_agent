# ruff: noqa: D103
"""FRE-1663 — Grafana verifies the Cloudflare Access JWT instead of serving anonymous Viewer.

Starts a throwaway Grafana (the pinned image) on a loopback port, configured with the grafana
`environment` that `docker-compose.cloud.yml` commits. Only the key source differs: Grafana takes
`jwk_set_url` over https only, so the test serves a throwaway key set from a file
(`GF_AUTH_JWT_JWK_SET_FILE`). The container shares no network or volume with the real stack.

The file covers key parsing, signatures, claims, sign-up and roles. It does not cover TLS, DNS,
the Cloudflare endpoint, the key cache or key rotation. The owner's live check (FRE-1663 AC-4)
covers the real URL.

Skips without the docker CLI or when the pinned image is not already present (it never pulls).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import jwt
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.scripts.test_grafana_compose_service import _render_compose

_IMAGE = "grafana/grafana:13.1.3"
_AUD = "0" * 64  # the value tests/scripts/test_grafana_compose_service.py renders the tag as
_OTHER_AUD = "f" * 64  # stands in for the agent app's tag
_HEADER = "Cf-Access-Jwt-Assertion"
_ADMIN_PASSWORD = "fre1663-test-admin"  # fre-375-allow: throwaway container, not production


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "image", "inspect", _IMAGE], capture_output=True, check=False)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_ready(), reason=f"requires the docker CLI and a local {_IMAGE} image"
)


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key: rsa.RSAPrivateKey, *, aud: str | list[str], exp_in: int = 300) -> str:
    now = int(time.time())
    claims = {
        "aud": aud,
        "email": "owner@example.test",
        "sub": "00000000-0000-0000-0000-000000000001",
        "iat": now,
        "exp": now + exp_in,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return _new_key()


@pytest.fixture(scope="module")
def grafana_url(
    tmp_path_factory: pytest.TempPathFactory, signing_key: rsa.RSAPrivateKey
) -> Iterator[str]:
    service = _render_compose("docker-compose.cloud.yml")["services"]["grafana"]  # type: ignore[index]
    env: dict[str, str] = dict(service["environment"])
    env.pop("GF_AUTH_JWT_JWK_SET_URL")
    env["GF_AUTH_JWT_JWK_SET_FILE"] = "/jwks.json"
    env["GF_SECURITY_ADMIN_PASSWORD"] = _ADMIN_PASSWORD
    env.pop("GRAFANA_RO_PASSWORD", None)

    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk.update({"kid": "test-key", "alg": "RS256", "use": "sig"})
    jwks = tmp_path_factory.mktemp("jwks") / "jwks.json"
    jwks.write_text(json.dumps({"keys": [jwk]}))
    jwks.chmod(0o644)

    name = f"fre1663-grafana-{uuid.uuid4().hex[:8]}"
    args = ["docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::3000"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args += ["-v", f"{jwks}:/jwks.json:ro", _IMAGE]
    subprocess.run(args, capture_output=True, text=True, check=True)
    try:
        mapping = subprocess.run(
            ["docker", "port", name, "3000/tcp"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
        url = f"http://127.0.0.1:{mapping.rsplit(':', 1)[1]}"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if requests.get(f"{url}/api/health", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            pytest.fail(
                f"grafana did not become healthy in 90s:\n{logs.stdout[-2000:]}{logs.stderr[-2000:]}"
            )
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _home(url: str, token: str | None = None) -> requests.Response:
    headers = {_HEADER: token} if token else {}
    return requests.get(f"{url}/api/dashboards/home", headers=headers, timeout=10)


class TestGrafanaRejectsRequestsWithoutAVerifiedAccessToken:
    def test_no_credentials_is_401(self, grafana_url: str) -> None:
        """AC-1."""
        response = _home(grafana_url)
        assert response.status_code == 401, response.text

    def test_token_signed_by_a_foreign_key_is_401(self, grafana_url: str) -> None:
        """AC-2: right audience, wrong signer."""
        response = _home(grafana_url, _token(_new_key(), aud=[_AUD]))
        assert response.status_code == 401, response.text

    def test_token_for_another_access_app_is_401(
        self, grafana_url: str, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """AC-2: right signer (the same team), the agent app's audience."""
        response = _home(grafana_url, _token(signing_key, aud=[_OTHER_AUD]))
        assert response.status_code == 401, response.text

    def test_expired_token_is_401(self, grafana_url: str, signing_key: rsa.RSAPrivateKey) -> None:
        response = _home(grafana_url, _token(signing_key, aud=[_AUD], exp_in=-3600))
        assert response.status_code == 401, response.text

    def test_garbage_token_is_401(self, grafana_url: str) -> None:
        response = _home(grafana_url, "not-a-jwt")
        assert response.status_code == 401, response.text


class TestGrafanaAdmitsAVerifiedAccessToken:
    def test_valid_token_with_audience_array_is_200(
        self, grafana_url: str, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """Positive control for the 401s above. Cloudflare sends `aud` as an array."""
        response = _home(grafana_url, _token(signing_key, aud=[_AUD]))
        assert response.status_code == 200, response.text

    def test_valid_token_with_audience_string_is_200(
        self, grafana_url: str, signing_key: rsa.RSAPrivateKey
    ) -> None:
        response = _home(grafana_url, _token(signing_key, aud=_AUD))
        assert response.status_code == 200, response.text

    def test_audience_array_holding_the_tag_among_others_is_200(
        self, grafana_url: str, signing_key: rsa.RSAPrivateKey
    ) -> None:
        response = _home(grafana_url, _token(signing_key, aud=[_OTHER_AUD, _AUD]))
        assert response.status_code == 200, response.text

    def test_verified_user_is_viewer_and_cannot_write(
        self, grafana_url: str, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """Same reach as the old anonymous Viewer: it reads, it cannot create a dashboard."""
        token = _token(signing_key, aud=[_AUD])
        orgs = requests.get(f"{grafana_url}/api/user/orgs", headers={_HEADER: token}, timeout=10)
        assert orgs.status_code == 200, orgs.text
        assert [o["role"] for o in orgs.json()] == ["Viewer"]
        create = requests.post(
            f"{grafana_url}/api/dashboards/db",
            headers={_HEADER: token},
            json={"dashboard": {"title": "fre1663-probe", "panels": []}, "overwrite": False},
            timeout=10,
        )
        assert create.status_code == 403, create.text


class TestGrafanaKeepsBasicAuthForInternalTools:
    def test_admin_basic_auth_is_200(self, grafana_url: str) -> None:
        """AC-3: provisioning and the visualization tooling use basic auth on the loopback port."""
        response = requests.get(
            f"{grafana_url}/api/dashboards/home", auth=("admin", _ADMIN_PASSWORD), timeout=10
        )
        assert response.status_code == 200, response.text

    def test_wrong_basic_auth_password_is_401(self, grafana_url: str) -> None:
        response = requests.get(
            f"{grafana_url}/api/dashboards/home", auth=("admin", "wrong"), timeout=10
        )
        assert response.status_code == 401, response.text

    def test_basic_auth_still_admits_when_the_jwt_header_is_invalid(self, grafana_url: str) -> None:
        """Accepted exception, pinned so it stays a decision: Grafana tries each auth client in
        turn, so a valid admin Basic header succeeds even beside a bad JWT. Basic needs the admin
        password, and the tunnel route reaches it only after Access (ticket item 4 keeps it).
        """
        response = requests.get(
            f"{grafana_url}/api/dashboards/home",
            headers={_HEADER: "not-a-jwt"},
            auth=("admin", _ADMIN_PASSWORD),
            timeout=10,
        )
        assert response.status_code == 200, response.text


class TestGrafanaJwtLoginLeavesNoSession:
    def test_verified_token_sets_no_session_cookie(
        self, grafana_url: str, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """Each request must carry its own token. A cookie would let a later request skip the
        Access check, so the JWT login must not issue one (`enable_login_token` stays off).
        """
        response = _home(grafana_url, _token(signing_key, aud=[_AUD]))
        assert response.status_code == 200, response.text
        assert "grafana_session" not in response.cookies
        assert "grafana_session" not in response.headers.get("Set-Cookie", "")
        bare = requests.get(f"{grafana_url}/api/dashboards/home", timeout=10)
        assert bare.status_code == 401, bare.text
