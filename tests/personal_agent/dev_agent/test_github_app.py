"""The dev-agent GitHub App client (ADR-0159 D8.1–D8.3; FRE-1685).

No network: every call goes to an ``httpx.MockTransport`` that records it.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from uuid import UUID

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from personal_agent.dev_agent.github_app import (
    GitHubAppConfig,
    GitHubAppHost,
    app_jwt,
    repo_name,
)

from personal_agent.exceptions import DevAgentRepoError

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
_TOKEN = "ghs_secret_installation_token_value"
_JOB = UUID("1234abcd-0000-4000-8000-000000000000")


def _config() -> GitHubAppConfig:
    return GitHubAppConfig(
        app_id=42,
        installation_id=7,
        org="example-org",
        private_key_pem=_PEM,
        api_url="https://api.github.test",
    )


Handler = Callable[[httpx.Request], httpx.Response]


def _host(handler: Handler, calls: list[httpx.Request]) -> GitHubAppHost:
    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/app/installations/7/access_tokens":
            return httpx.Response(201, json={"token": _TOKEN, "expires_at": "2026-11-07T14:00:00Z"})
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return GitHubAppHost(_config(), client)


def _repo_body(name: str, *, private: bool = True, owner: str = "example-org") -> dict[str, object]:
    return {"name": name, "private": private, "owner": {"login": owner}}


class TestRepoName:
    def test_slug_and_short_job_id(self) -> None:
        assert repo_name("Sales Dashboard!", _JOB) == "sales-dashboard-1234abcd"

    def test_build_mode_has_no_slug(self) -> None:
        assert repo_name(None, _JOB) == "page-1234abcd"

    def test_slug_with_nothing_usable(self) -> None:
        assert repo_name("--!!--", _JOB) == "page-1234abcd"

    def test_long_slug_is_cut(self) -> None:
        name = repo_name("a" * 100, _JOB)
        assert name == "a" * 40 + "-1234abcd"


class TestAppJwt:
    def test_claims_and_signature(self) -> None:
        token = app_jwt(_config(), now=1_000_000.0)
        claims = jwt.decode(
            token,
            _KEY.public_key(),
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims == {"iss": "42", "iat": 1_000_000 - 60, "exp": 1_000_000 + 540}


@pytest.mark.asyncio
class TestCreatePrivateRepo:
    async def test_creates_a_private_repo_with_a_scoped_token(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/orgs/example-org/repos"
            return httpx.Response(201, json=_repo_body("page-1234abcd"))

        host = _host(handler, calls)
        await host.create_private_repo("page-1234abcd")

        token_call, create_call = calls
        assert token_call.headers["authorization"].startswith("Bearer ")
        assert json.loads(token_call.content) == {
            "permissions": {"administration": "write", "metadata": "read"}
        }
        body = json.loads(create_call.content)
        assert body["name"] == "page-1234abcd"
        assert body["private"] is True
        assert body["auto_init"] is False
        assert create_call.headers["authorization"] == f"Bearer {_TOKEN}"

    async def test_existing_private_repo_of_the_org_is_accepted(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(422, json={"errors": [{"message": "name already exists"}]})
            assert request.url.path == "/repos/example-org/page-1234abcd"
            return httpx.Response(200, json=_repo_body("page-1234abcd"))

        await _host(handler, calls).create_private_repo("page-1234abcd")

    async def test_existing_public_repo_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(422, json={"errors": [{"message": "name already exists"}]})
            return httpx.Response(200, json=_repo_body("page-1234abcd", private=False))

        with pytest.raises(DevAgentRepoError, match="not private"):
            await _host(handler, []).create_private_repo("page-1234abcd")

    async def test_created_repo_that_is_not_private_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json=_repo_body("page-1234abcd", private=False))

        with pytest.raises(DevAgentRepoError, match="not private"):
            await _host(handler, []).create_private_repo("page-1234abcd")

    async def test_bad_name_is_refused_before_any_call(self) -> None:
        calls: list[httpx.Request] = []
        with pytest.raises(ValueError):
            await _host(lambda r: httpx.Response(500), calls).create_private_repo("../evil")
        assert calls == []

    async def test_error_text_never_holds_the_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text=f"echo {request.headers['authorization']}")

        with pytest.raises(DevAgentRepoError) as caught:
            await _host(handler, []).create_private_repo("page-1234abcd")
        assert _TOKEN not in str(caught.value)


@pytest.mark.asyncio
class TestIsPrivate:
    async def test_reads_the_private_flag(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_repo_body("page-1234abcd"))

        assert await _host(handler, []).is_private("page-1234abcd") is True


@pytest.mark.asyncio
class TestRemote:
    async def test_token_is_scoped_to_the_repo_and_kept_out_of_the_url(self) -> None:
        calls: list[httpx.Request] = []
        remote = await _host(lambda r: httpx.Response(500), calls).remote("page-1234abcd")

        assert json.loads(calls[0].content) == {
            "repositories": ["page-1234abcd"],
            "permissions": {"contents": "write", "metadata": "read"},
        }
        assert remote.url == "https://github.com/example-org/page-1234abcd.git"
        assert _TOKEN not in remote.url
        assert _TOKEN not in repr(remote)
        basic = base64.b64encode(f"x-access-token:{_TOKEN}".encode()).decode()
        assert remote.env == {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }


def test_config_repr_hides_the_key() -> None:
    assert "PRIVATE KEY" not in repr(_config())
