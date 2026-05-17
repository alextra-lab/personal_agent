"""Unit tests for the Cloudflare Access JWT verifier.

These tests build an in-memory RSA keypair, sign JWTs with controlled
claims, and assert that the verifier accepts / rejects each scenario per
its contract. The JWKS endpoint is mocked at the httpx layer so no
network calls are made.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    generate_private_key,
)

from personal_agent.service import cf_access_jwt
from personal_agent.service.cf_access_jwt import (
    CFAccessVerifier,
    CFAccessVerifierError,
)

_AUD = "test-audience-tag"
_ISS = "https://team.cloudflareaccess.com"
_TEAM = "team.cloudflareaccess.com"
_KID = "test-kid-1"


def _b64url_uint(value: int) -> str:
    """Encode an int as base64url with no padding (JWK 'n' / 'e' format)."""
    byte_len = (value.bit_length() + 7) // 8
    raw = value.to_bytes(byte_len, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwk_for(private_key: RSAPrivateKey, kid: str = _KID) -> dict[str, Any]:
    public_numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64url_uint(public_numbers.n),
        "e": _b64url_uint(public_numbers.e),
    }


def _sign(
    private_key: RSAPrivateKey,
    *,
    email: str = "alex@example.com",
    aud: str = _AUD,
    kid: str = _KID,
    expired: bool = False,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": _ISS,
        "aud": aud,
        "email": email,
        "sub": "user-123",
        "iat": now - 60,
        "exp": now - 10 if expired else now + 600,
    }
    if extra_claims:
        claims.update(extra_claims)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def rsa_key() -> RSAPrivateKey:
    return generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier_with_jwks(
    rsa_key: RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> CFAccessVerifier:
    """Build a verifier whose JWKS fetch returns our test public key."""
    verifier = CFAccessVerifier(team_domain=_TEAM, audience=_AUD)
    jwks = {"keys": [_jwk_for(rsa_key)]}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return jwks

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str) -> _FakeResp:
            return _FakeResp()

    monkeypatch.setattr(cf_access_jwt.httpx, "AsyncClient", _FakeClient)
    return verifier


@pytest.mark.asyncio
async def test_verify_happy_path(
    rsa_key: RSAPrivateKey, verifier_with_jwks: CFAccessVerifier
) -> None:
    token = _sign(rsa_key, email="Alex@Example.com")
    claims = await verifier_with_jwks.verify(token)
    assert claims.email == "alex@example.com"  # normalized to lowercase
    assert claims.aud == _AUD
    assert claims.sub == "user-123"


@pytest.mark.asyncio
async def test_verify_rejects_empty_token(verifier_with_jwks: CFAccessVerifier) -> None:
    with pytest.raises(CFAccessVerifierError, match="missing token"):
        await verifier_with_jwks.verify("")


@pytest.mark.asyncio
async def test_verify_rejects_malformed_token(
    verifier_with_jwks: CFAccessVerifier,
) -> None:
    with pytest.raises(CFAccessVerifierError, match="malformed header"):
        await verifier_with_jwks.verify("not.a.jwt")


@pytest.mark.asyncio
async def test_verify_rejects_unknown_kid(
    rsa_key: RSAPrivateKey, verifier_with_jwks: CFAccessVerifier
) -> None:
    token = _sign(rsa_key, kid="not-in-jwks")
    with pytest.raises(CFAccessVerifierError, match="no signing key"):
        await verifier_with_jwks.verify(token)


@pytest.mark.asyncio
async def test_verify_rejects_aud_mismatch(
    rsa_key: RSAPrivateKey, verifier_with_jwks: CFAccessVerifier
) -> None:
    token = _sign(rsa_key, aud="wrong-audience")
    with pytest.raises(CFAccessVerifierError, match="verification failed"):
        await verifier_with_jwks.verify(token)


@pytest.mark.asyncio
async def test_verify_rejects_expired_token(
    rsa_key: RSAPrivateKey, verifier_with_jwks: CFAccessVerifier
) -> None:
    token = _sign(rsa_key, expired=True)
    with pytest.raises(CFAccessVerifierError, match="verification failed"):
        await verifier_with_jwks.verify(token)


@pytest.mark.asyncio
async def test_verify_rejects_token_signed_by_other_key(
    verifier_with_jwks: CFAccessVerifier,
) -> None:
    other_key = generate_private_key(public_exponent=65537, key_size=2048)
    token = _sign(other_key)
    # The kid matches a key in the JWKS, but the signature was made by a
    # different private key — must fail verification.
    with pytest.raises(CFAccessVerifierError, match="verification failed"):
        await verifier_with_jwks.verify(token)


@pytest.mark.asyncio
async def test_verify_rejects_missing_email_claim(
    rsa_key: RSAPrivateKey, verifier_with_jwks: CFAccessVerifier
) -> None:
    """pyjwt's ``options.require=['email']`` makes a missing email fatal."""
    # We can't use `_sign` here because we need to omit email entirely.
    now = int(time.time())
    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    token = jwt.encode(
        {
            "iss": _ISS,
            "aud": _AUD,
            "sub": "user-x",
            "iat": now - 1,
            "exp": now + 300,
        },
        pem,
        algorithm="RS256",
        headers={"kid": _KID},
    )
    with pytest.raises(CFAccessVerifierError):
        await verifier_with_jwks.verify(token)


@pytest.mark.asyncio
async def test_get_verifier_returns_none_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cf_access_jwt.reset_verifier_for_testing()
    monkeypatch.setattr(cf_access_jwt.settings, "cf_access_team_domain", None, raising=False)
    monkeypatch.setattr(cf_access_jwt.settings, "cf_access_aud", None, raising=False)
    assert cf_access_jwt.get_verifier() is None


@pytest.mark.asyncio
async def test_get_verifier_is_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    cf_access_jwt.reset_verifier_for_testing()
    monkeypatch.setattr(cf_access_jwt.settings, "cf_access_team_domain", _TEAM, raising=False)
    monkeypatch.setattr(cf_access_jwt.settings, "cf_access_aud", _AUD, raising=False)
    first = cf_access_jwt.get_verifier()
    second = cf_access_jwt.get_verifier()
    try:
        assert first is not None
        assert first is second
        assert first.certs_url == f"https://{_TEAM}/cdn-cgi/access/certs"
    finally:
        cf_access_jwt.reset_verifier_for_testing()


@pytest.mark.asyncio
async def test_jwks_refresh_retries_on_unknown_kid(
    rsa_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a kid miss, the verifier refreshes the JWKS and retries once."""
    verifier = CFAccessVerifier(team_domain=_TEAM, audience=_AUD)

    # First call: empty JWKS. Second call (force refresh): the real key.
    fetch_count = {"n": 0}
    jwks_state = [{"keys": []}, {"keys": [_jwk_for(rsa_key)]}]

    class _FakeResp:
        status_code = 200

        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str) -> _FakeResp:
            idx = min(fetch_count["n"], len(jwks_state) - 1)
            payload = jwks_state[idx]
            fetch_count["n"] += 1
            return _FakeResp(payload)

    monkeypatch.setattr(cf_access_jwt.httpx, "AsyncClient", _FakeClient)

    token = _sign(rsa_key)
    claims = await verifier.verify(token)
    assert claims.email == "alex@example.com"
    assert fetch_count["n"] == 2  # initial fetch + forced refresh after miss


def test_jwt_module_imports_clean() -> None:
    """Smoke import — guards against accidental module-load failures."""
    from personal_agent.service import cf_access_jwt as mod

    assert hasattr(mod, "CFAccessVerifier")
    assert hasattr(mod, "CFAccessVerifierError")
    assert hasattr(mod, "get_verifier")
