"""Unit tests for the Cloudflare Access JWT verifier.

These tests build an in-memory RSA keypair, sign JWTs with controlled
claims, and assert that the verifier accepts / rejects each scenario per
its contract. The JWKS endpoint is mocked at the httpx layer so no
network calls are made.
"""

from __future__ import annotations

import asyncio
import base64
import time
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    generate_private_key,
)

from personal_agent.service import cf_access_jwt
from personal_agent.service.cf_access_jwt import (
    CFAccessClaims,
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

    monkeypatch.setattr(cf_access_jwt, "create_guarded_http_client", _FakeClient)
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
    """Pyjwt's ``options.require=['email']`` makes a missing email fatal."""
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

    monkeypatch.setattr(cf_access_jwt, "create_guarded_http_client", _FakeClient)

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


# ---------------------------------------------------------------------------
# FRE-1530 — Defect A: hostile tokens must end as CFAccessVerifierError (401)
# ---------------------------------------------------------------------------


def _deeply_nested_token(depth: int = 20_000) -> str:
    """Build a token whose header is a JSON array nested ``depth`` levels deep."""
    header = ("[" * depth + "]" * depth).encode("ascii")
    segment = base64.urlsafe_b64encode(header).rstrip(b"=").decode("ascii")
    return f"{segment}.e30.c2ln"


@pytest.mark.asyncio
async def test_verify_rejects_deeply_nested_header_as_verifier_error(
    verifier_with_jwks: CFAccessVerifier,
) -> None:
    """A deeply nested header raised RecursionError out of ``verify`` (a 500, not a 401)."""
    with pytest.raises(CFAccessVerifierError, match="malformed header"):
        await verifier_with_jwks.verify(_deeply_nested_token())


@pytest.mark.asyncio
async def test_verify_wraps_recursion_error_from_header_parse(
    verifier_with_jwks: CFAccessVerifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local catch must hold even if PyJWT stops raising RecursionError itself."""

    def _boom(token: str) -> dict[str, Any]:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(cf_access_jwt.jwt, "get_unverified_header", _boom)
    with pytest.raises(CFAccessVerifierError, match="malformed header"):
        await verifier_with_jwks.verify("a.b.c")


@pytest.mark.asyncio
async def test_verify_wraps_recursion_error_from_decode(
    rsa_key: RSAPrivateKey,
    verifier_with_jwks: CFAccessVerifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(cf_access_jwt.jwt, "decode", _boom)
    with pytest.raises(CFAccessVerifierError, match="verification failed"):
        await verifier_with_jwks.verify(_sign(rsa_key))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"iat": {}}, id="iat-object"),
        pytest.param({"exp": []}, id="exp-array"),
        pytest.param({"exp": float("inf")}, id="exp-infinity"),
    ],
)
async def test_verify_rejects_signed_token_with_malformed_time_claims(
    rsa_key: RSAPrivateKey,
    verifier_with_jwks: CFAccessVerifier,
    claims: dict[str, Any],
) -> None:
    """End-to-end contract: malformed time claims are a 401.

    PyJWT 2.13 leaked TypeError / OverflowError here. 2.15 converts them, so this guards
    against a library regression rather than a local catch.
    """
    token = _sign(rsa_key, extra_claims=claims)
    with pytest.raises(CFAccessVerifierError, match="verification failed"):
        await verifier_with_jwks.verify(token)


# ---------------------------------------------------------------------------
# FRE-1530 — Defect B: unknown ``kid`` must not force a JWKS fetch per request
# ---------------------------------------------------------------------------


class _UpstreamDown(Exception):
    """Raised by the fake JWKS endpoint when it is switched off."""


class _FakeJwksServer:
    """A JWKS endpoint double: counts fetches, can fail, can hold the first fetch."""

    def __init__(self, jwks: dict[str, Any]) -> None:
        self.jwks = jwks
        self.count = 0
        self.fail = False
        self.hold: asyncio.Event | None = None
        self.started = asyncio.Event()


class _FakeClock:
    """A controllable ``time.monotonic`` for the verifier module."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def _install_fake_jwks(monkeypatch: pytest.MonkeyPatch, jwks: dict[str, Any]) -> _FakeJwksServer:
    server = _FakeJwksServer(jwks)

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return server.jwks

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str) -> _Resp:
            server.count += 1
            if server.fail:
                raise _UpstreamDown("jwks endpoint down")
            if server.hold is not None:
                server.started.set()
                await server.hold.wait()
            return _Resp()

    monkeypatch.setattr(cf_access_jwt, "create_guarded_http_client", _Client)
    return server


def _install_fake_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(cf_access_jwt, "time", SimpleNamespace(monotonic=clock.monotonic))
    return clock


def test_forced_refresh_cooldown_is_a_named_constant() -> None:
    """AC-8: the cooldown is a module constant beside the TTL, and it is a real window."""
    cooldown = cf_access_jwt._JWKS_FORCE_REFRESH_COOLDOWN_SECONDS
    assert isinstance(cooldown, int | float)
    assert 0 < cooldown


@pytest.mark.asyncio
async def test_unknown_kid_burst_causes_at_most_one_forced_fetch(
    rsa_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7: N distinct unknown kids inside the window cost one fetch, not N."""
    server = _install_fake_jwks(monkeypatch, {"keys": [_jwk_for(rsa_key)]})
    _install_fake_clock(monkeypatch)
    verifier = CFAccessVerifier(team_domain=_TEAM, audience=_AUD)

    await verifier.verify(_sign(rsa_key))  # warm the cache with a valid token
    assert server.count == 1

    for i in range(20):
        with pytest.raises(CFAccessVerifierError, match="no signing key"):
            await verifier.verify(_sign(rsa_key, kid=f"unknown-{i}"))

    assert server.count - 1 <= 1  # at most one forced fetch after the warm-up


@pytest.mark.asyncio
async def test_concurrent_unknown_kids_share_one_forced_fetch(
    rsa_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cooldown is re-checked inside the lock, not only before it.

    A TTL refresh holds the lock while the unknown-kid requests queue behind it. None of
    them has stamped the cooldown yet, so only the in-lock check stops all of them fetching.
    """
    server = _install_fake_jwks(monkeypatch, {"keys": [_jwk_for(rsa_key)]})
    clock = _install_fake_clock(monkeypatch)
    verifier = CFAccessVerifier(team_domain=_TEAM, audience=_AUD)

    await verifier.verify(_sign(rsa_key))  # warm
    assert server.count == 1

    clock.now += cf_access_jwt._JWKS_TTL_SECONDS + 1  # the cache is now stale
    server.hold = asyncio.Event()  # the next fetch blocks until we release it

    tasks = [
        asyncio.create_task(verifier.verify(_sign(rsa_key, kid=f"unknown-{i}")))
        for i in range(10)
    ]
    await server.started.wait()  # the TTL refresh holds the lock
    for _ in range(5):
        await asyncio.sleep(0)  # let every other task queue on the lock
    server.hold.set()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, CFAccessVerifierError) for r in results)
    # warm-up (1) + the TTL refresh (1) + exactly one forced refresh.
    assert server.count == 3


@pytest.mark.asyncio
async def test_failed_forced_fetch_keeps_old_jwks_and_starts_cooldown(
    rsa_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing JWKS endpoint is not hit again inside the window, and the old keys survive."""
    server = _install_fake_jwks(monkeypatch, {"keys": [_jwk_for(rsa_key)]})
    clock = _install_fake_clock(monkeypatch)
    verifier = CFAccessVerifier(team_domain=_TEAM, audience=_AUD)

    await verifier.verify(_sign(rsa_key))  # warm
    server.fail = True

    with pytest.raises(_UpstreamDown):
        await verifier.verify(_sign(rsa_key, kid="unknown-1"))
    assert server.count == 2

    with pytest.raises(CFAccessVerifierError, match="no signing key"):
        await verifier.verify(_sign(rsa_key, kid="unknown-2"))
    assert server.count == 2  # inside the window: no new attempt

    claims = await verifier.verify(_sign(rsa_key))  # the old keys still verify
    assert claims.email == "alex@example.com"
    assert server.count == 2

    clock.now += cf_access_jwt._JWKS_FORCE_REFRESH_COOLDOWN_SECONDS + 1
    with pytest.raises(_UpstreamDown):
        await verifier.verify(_sign(rsa_key, kid="unknown-3"))
    assert server.count == 3  # the window ended: one new attempt


@pytest.mark.asyncio
async def test_rotated_kid_verifies_on_first_miss_after_load(
    rsa_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-9 (a): the cooldown does not delay the first legitimate rotation refresh."""
    new_key = generate_private_key(public_exponent=65537, key_size=2048)
    server = _install_fake_jwks(monkeypatch, {"keys": [_jwk_for(rsa_key)]})
    _install_fake_clock(monkeypatch)
    verifier = CFAccessVerifier(team_domain=_TEAM, audience=_AUD)

    await verifier.verify(_sign(rsa_key))  # warm with the old key
    server.jwks = {"keys": [_jwk_for(new_key, kid="rotated-kid")]}  # Cloudflare rotates

    claims = await verifier.verify(_sign(new_key, kid="rotated-kid"))
    assert claims.email == "alex@example.com"
    assert server.count == 2


@pytest.mark.asyncio
async def test_rotated_kid_verifies_after_cooldown_when_window_was_used(
    rsa_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-9 (b): after a burst used the window, a rotated kid verifies once the window ends."""
    new_key = generate_private_key(public_exponent=65537, key_size=2048)
    server = _install_fake_jwks(monkeypatch, {"keys": [_jwk_for(rsa_key)]})
    clock = _install_fake_clock(monkeypatch)
    verifier = CFAccessVerifier(team_domain=_TEAM, audience=_AUD)

    await verifier.verify(_sign(rsa_key))  # warm
    with pytest.raises(CFAccessVerifierError, match="no signing key"):
        await verifier.verify(_sign(rsa_key, kid="junk"))  # uses the window
    assert server.count == 2

    server.jwks = {"keys": [_jwk_for(new_key, kid="rotated-kid")]}  # Cloudflare rotates
    rotated = _sign(new_key, kid="rotated-kid")

    with pytest.raises(CFAccessVerifierError, match="no signing key"):
        await verifier.verify(rotated)  # inside the window
    assert server.count == 2

    clock.now += cf_access_jwt._JWKS_FORCE_REFRESH_COOLDOWN_SECONDS
    claims = await verifier.verify(rotated)  # exactly one cooldown period later
    assert claims.email == "alex@example.com"
    assert server.count == 3


@pytest.mark.asyncio
async def test_parallel_requests_with_rotated_kid_all_verify(
    rsa_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that arrives during the rotation refresh waits for it, not a 401.

    The first request with the rotated kid starts the forced fetch. A second one with the
    same kid arrives while that fetch is in flight. It must queue behind the fetch and find
    the new key, not fail on the old cache.
    """
    new_key = generate_private_key(public_exponent=65537, key_size=2048)
    server = _install_fake_jwks(monkeypatch, {"keys": [_jwk_for(rsa_key)]})
    _install_fake_clock(monkeypatch)
    verifier = CFAccessVerifier(team_domain=_TEAM, audience=_AUD)

    await verifier.verify(_sign(rsa_key))  # warm with the old key
    server.jwks = {"keys": [_jwk_for(new_key, kid="rotated-kid")]}  # Cloudflare rotates
    server.hold = asyncio.Event()

    first = asyncio.create_task(verifier.verify(_sign(new_key, kid="rotated-kid")))
    await server.started.wait()  # the forced fetch is in flight
    second = asyncio.create_task(verifier.verify(_sign(new_key, kid="rotated-kid")))
    for _ in range(5):
        await asyncio.sleep(0)  # let the second request reach the refresh step
    server.hold.set()

    results = await asyncio.gather(first, second, return_exceptions=True)
    assert [type(r) for r in results] == [CFAccessClaims, CFAccessClaims]
    assert server.count == 2  # warm-up + one forced fetch shared by both
