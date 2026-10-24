"""FRE-1531 (ADR-0153 D11): identity comes from a verified Cloudflare Access JWT.

With ``gateway_auth_enabled=True`` the plaintext ``Cf-Access-Authenticated-User-Email``
header must never decide identity. These tests run the real routes, real verifiers
and real RS256 signatures. Only the JWKS fetch and the database are stubbed.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, generate_private_key
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.config import settings
from personal_agent.gateway.app import create_gateway_router
from personal_agent.gateway.auth import TokenInfo, verify_token
from personal_agent.service import artifacts_router as artifacts_module
from personal_agent.service import cf_access_jwt
from personal_agent.service.artifacts_router import router as artifacts_router
from personal_agent.service.database import get_db_session

_TEAM = "team.cloudflareaccess.com"
_MAIN_AUD = "main-app-audience-tag"
_WORKER_AUD = "worker-audience-tag"
_KID = "kid-1"
_INTERNAL_TOKEN = "internal-token"
_OWNER = "owner@example.com"
_HEADER_EMAIL = "spoofed@example.com"
_ARTIFACT_PATH = f"/api/v1/artifacts/{uuid4()}"


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwk(key: RSAPrivateKey) -> dict[str, Any]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": _KID,
        "use": "sig",
        "alg": "RS256",
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }


def _sign(
    key: RSAPrivateKey,
    *,
    aud: str | list[str] = _MAIN_AUD,
    email: str = _OWNER,
    expired: bool = False,
) -> str:
    now = int(time.time())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    claims = {
        "iss": f"https://{_TEAM}",
        "aud": aud,
        "email": email,
        "sub": "user-1",
        "iat": now - 60,
        "exp": now - 10 if expired else now + 600,
    }
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": _KID})


@pytest.fixture
def rsa_key() -> RSAPrivateKey:
    return generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _production_auth(rsa_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Production posture: auth on, both audiences set, JWKS served from ``rsa_key``."""
    monkeypatch.setattr(settings, "gateway_auth_enabled", True)
    monkeypatch.setattr(settings, "agent_owner_email", _OWNER)
    monkeypatch.setattr(settings, "cf_access_team_domain", _TEAM)
    monkeypatch.setattr(settings, "cf_access_aud", _WORKER_AUD)
    monkeypatch.setattr(settings, "cf_access_main_aud", _MAIN_AUD)
    monkeypatch.setattr(settings, "artifact_resolve_internal_token", _INTERNAL_TOKEN, raising=False)
    jwks = {"keys": [_jwk(rsa_key)]}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return jwks

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str) -> _Resp:
            return _Resp()

    monkeypatch.setattr(cf_access_jwt, "create_guarded_http_client", _Client)
    cf_access_jwt.reset_verifier_for_testing()
    yield
    cf_access_jwt.reset_verifier_for_testing()


class _EmptySession:
    """DB stub: every query finds no row."""

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        return SimpleNamespace(one_or_none=lambda: None, all=lambda: [])


def _artifacts_app() -> FastAPI:
    app = FastAPI()
    app.include_router(artifacts_router)

    async def _db() -> Any:
        yield _EmptySession()

    app.dependency_overrides[get_db_session] = _db
    return app


@pytest.fixture
def resolved_emails() -> Iterator[AsyncMock]:
    """Stub the users-table lookup and record which email reached it."""
    user_id = UUID("00000000-0000-0000-0000-0000000000aa")
    with (
        patch(
            "personal_agent.service.auth._get_user_with_display_name",
            new_callable=AsyncMock,
            return_value=(user_id, None),
        ) as lookup,
        patch("personal_agent.service.auth._get_db_session") as db,
    ):
        db.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        db.return_value.__aexit__ = AsyncMock(return_value=False)
        yield lookup


def _get(app: FastAPI, path: str, headers: dict[str, str]) -> Any:
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.get(path, headers=headers)


# AC-1 ----------------------------------------------------------------------


def test_ac1_email_header_without_jwt_is_401(resolved_emails: AsyncMock) -> None:
    resp = _get(
        _artifacts_app(),
        _ARTIFACT_PATH,
        {"cf-access-authenticated-user-email": _HEADER_EMAIL},
    )
    assert resp.status_code == 401
    resolved_emails.assert_not_awaited()


# AC-2 ----------------------------------------------------------------------


def test_ac2_jwt_signed_by_foreign_key_is_401(resolved_emails: AsyncMock) -> None:
    foreign = generate_private_key(public_exponent=65537, key_size=2048)
    resp = _get(
        _artifacts_app(),
        _ARTIFACT_PATH,
        {"cf-access-jwt-assertion": _sign(foreign)},
    )
    assert resp.status_code == 401
    resolved_emails.assert_not_awaited()


def test_expired_jwt_is_401(rsa_key: RSAPrivateKey, resolved_emails: AsyncMock) -> None:
    resp = _get(
        _artifacts_app(),
        _ARTIFACT_PATH,
        {"cf-access-jwt-assertion": _sign(rsa_key, expired=True)},
    )
    assert resp.status_code == 401
    resolved_emails.assert_not_awaited()


# AC-3 ----------------------------------------------------------------------


def test_ac3_worker_audience_jwt_is_401_on_main_api(
    rsa_key: RSAPrivateKey, resolved_emails: AsyncMock
) -> None:
    resp = _get(
        _artifacts_app(),
        _ARTIFACT_PATH,
        {"cf-access-jwt-assertion": _sign(rsa_key, aud=_WORKER_AUD)},
    )
    assert resp.status_code == 401
    resolved_emails.assert_not_awaited()


def test_ac3_main_app_jwt_is_rejected_on_internal_endpoint(rsa_key: RSAPrivateKey) -> None:
    resp = _get(
        _artifacts_app(),
        f"/internal/artifacts/{uuid4()}",
        {
            "x-internal-token": _INTERNAL_TOKEN,
            "x-cf-access-jwt-assertion": _sign(rsa_key, aud=_MAIN_AUD),
        },
    )
    assert resp.status_code == 401


def test_ac3_worker_jwt_still_passes_internal_endpoint(
    rsa_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Worker path is unchanged: a Worker-audience JWT is accepted (artifact not found)."""
    monkeypatch.setattr(
        artifacts_module, "get_or_create_user_by_email", AsyncMock(return_value=uuid4())
    )
    resp = _get(
        _artifacts_app(),
        f"/internal/artifacts/{uuid4()}",
        {
            "x-internal-token": _INTERNAL_TOKEN,
            "x-cf-access-jwt-assertion": _sign(rsa_key, aud=_WORKER_AUD),
        },
    )
    assert resp.status_code == 404


# AC-4 ----------------------------------------------------------------------


def test_ac4_valid_main_jwt_resolves_user_from_claim_not_header(
    rsa_key: RSAPrivateKey, resolved_emails: AsyncMock
) -> None:
    resp = _get(
        _artifacts_app(),
        _ARTIFACT_PATH,
        {
            "cf-access-jwt-assertion": _sign(rsa_key, email="Claim@Example.com"),
            "cf-access-authenticated-user-email": _HEADER_EMAIL,
        },
    )
    assert resp.status_code == 404  # authenticated; the artifact does not exist
    resolved_emails.assert_awaited_once()
    assert resolved_emails.await_args.args[1] == "claim@example.com"


def test_list_aud_claim_is_accepted(rsa_key: RSAPrivateKey, resolved_emails: AsyncMock) -> None:
    """Cloudflare issues ``aud`` as a one-element list (measured on a live token)."""
    resp = _get(
        _artifacts_app(),
        _ARTIFACT_PATH,
        {"cf-access-jwt-assertion": _sign(rsa_key, aud=[_MAIN_AUD])},
    )
    assert resp.status_code == 404
    resolved_emails.assert_awaited_once()


# AC-5 ----------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["cf_access_main_aud", "cf_access_team_domain"])
def test_ac5_unconfigured_verifier_is_503_not_header_trust(
    missing: str,
    rsa_key: RSAPrivateKey,
    resolved_emails: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, missing, None)
    cf_access_jwt.reset_verifier_for_testing()
    resp = _get(
        _artifacts_app(),
        _ARTIFACT_PATH,
        {
            "cf-access-authenticated-user-email": _HEADER_EMAIL,
            "cf-access-jwt-assertion": _sign(rsa_key),
        },
    )
    assert resp.status_code == 503
    resolved_emails.assert_not_awaited()


# AC-6 ----------------------------------------------------------------------


def test_ac6_dev_mode_without_headers_resolves_to_owner(
    resolved_emails: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "gateway_auth_enabled", False)
    resp = _get(_artifacts_app(), _ARTIFACT_PATH, {})
    assert resp.status_code == 404
    assert resolved_emails.await_args.args[1] == _OWNER


def test_dev_mode_still_accepts_the_plain_header(
    resolved_emails: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eval harnesses send the header to auth-disabled gateways. That stays."""
    monkeypatch.setattr(settings, "gateway_auth_enabled", False)
    resp = _get(
        _artifacts_app(),
        _ARTIFACT_PATH,
        {"cf-access-authenticated-user-email": _HEADER_EMAIL},
    )
    assert resp.status_code == 404
    assert resolved_emails.await_args.args[1] == _HEADER_EMAIL


# Fold-in: the gateway modules that read the header directly ----------------

_GATEWAY_ROUTES = [
    ("session_api", "GET", "/api/v1/sessions"),
    ("knowledge_api", "GET", "/api/v1/knowledge/search?q=x"),
    ("feedback_api", "POST", f"/api/v1/turns/{uuid4()}/rating"),
]


def _gateway_app() -> FastAPI:
    app = FastAPI()
    app.include_router(create_gateway_router())
    # The bearer-token layer is separate from user identity. Grant every scope here.
    app.dependency_overrides[verify_token] = lambda: TokenInfo(
        name="test",
        scopes=frozenset({"sessions:read", "knowledge:read", "knowledge:write", "feedback:write"}),
        rate_limit=10_000,
    )
    app.state.knowledge_graph = AsyncMock()
    app.state.es_client = None
    app.state.db_session_factory = lambda: AsyncMock()
    return app


@pytest.mark.parametrize(("module", "method", "path"), _GATEWAY_ROUTES)
def test_gateway_modules_reject_header_only_identity(module: str, method: str, path: str) -> None:
    lookup = AsyncMock(return_value=(uuid4(), None))
    with patch(f"personal_agent.gateway.{module}._get_user_with_display_name", lookup):
        with TestClient(_gateway_app(), raise_server_exceptions=False) as client:
            resp = client.request(
                method,
                path,
                headers={"cf-access-authenticated-user-email": _HEADER_EMAIL},
                json={"session_id": str(uuid4()), "trace_id": str(uuid4()), "rating": 1}
                if method == "POST"
                else None,
            )
    assert resp.status_code == 401
    lookup.assert_not_awaited()


@pytest.mark.parametrize(("module", "method", "path"), _GATEWAY_ROUTES)
def test_gateway_modules_use_the_verified_claim(
    rsa_key: RSAPrivateKey, module: str, method: str, path: str
) -> None:
    lookup = AsyncMock(return_value=(uuid4(), None))
    with patch(f"personal_agent.gateway.{module}._get_user_with_display_name", lookup):
        with TestClient(_gateway_app(), raise_server_exceptions=False) as client:
            client.request(
                method,
                path,
                headers={
                    "cf-access-jwt-assertion": _sign(rsa_key, email="Claim@Example.com"),
                    "cf-access-authenticated-user-email": _HEADER_EMAIL,
                },
                json={"session_id": str(uuid4()), "trace_id": str(uuid4()), "rating": 1}
                if method == "POST"
                else None,
            )
    lookup.assert_awaited()
    assert lookup.await_args.args[1] == "claim@example.com"
