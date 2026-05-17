"""Cloudflare Access JWT verification.

Endpoints that trust a CF Access identity must verify the
``Cf-Access-Jwt-Assertion`` JWT against Cloudflare's per-team JWKS before
treating any forwarded email as authoritative. CF Access signs the JWT
with RS256 keys served from
``https://<team_domain>/cdn-cgi/access/certs`` and includes the verified
email in the ``email`` claim.

The Worker fronting ``artifacts.frenchforet.com`` forwards the JWT to the
gateway as ``X-Cf-Access-Jwt-Assertion`` so the gateway can re-verify
end-to-end. The Worker cannot be trusted to filter — only to forward.

Configuration: set ``cf_access_team_domain`` and ``cf_access_aud`` in
``personal_agent.config.settings``. When either is unset,
``get_verifier()`` returns ``None`` and call sites must fail closed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
import structlog

from personal_agent.config import settings

log = structlog.get_logger(__name__)

# Refresh the JWKS at most once per hour under normal operation. A
# signing-key miss (kid not present) forces an immediate refresh.
_JWKS_TTL_SECONDS = 3600
_JWKS_FETCH_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class CFAccessClaims:
    """The claims we depend on from a verified CF Access JWT."""

    email: str
    sub: str
    aud: str
    iss: str


class CFAccessVerifierError(RuntimeError):
    """Raised when a JWT fails verification.

    Verification failure modes are kept opaque to callers — the response
    to the client is always 401 regardless of which check failed, to
    avoid leaking signing-key state or claim shape.
    """


class CFAccessVerifier:
    """Cached JWKS client and JWT verifier scoped to a single team/aud."""

    def __init__(self, *, team_domain: str, audience: str) -> None:
        """Build a verifier for the given Cloudflare Access team + app aud."""
        self._team_domain = team_domain.strip().rstrip("/")
        self._audience = audience
        self._certs_url = f"https://{self._team_domain}/cdn-cgi/access/certs"
        self._jwks: dict[str, Any] = {}
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def certs_url(self) -> str:
        """The JWKS endpoint URL this verifier polls."""
        return self._certs_url

    async def verify(self, token: str) -> CFAccessClaims:
        """Verify ``token`` against the team JWKS and configured audience.

        Args:
            token: The raw ``Cf-Access-Jwt-Assertion`` value.

        Returns:
            The verified claims subset.

        Raises:
            CFAccessVerifierError: When the token is empty, malformed,
                signed by an unknown key (even after JWKS refresh), has
                an ``aud`` mismatch, is expired, or is missing required
                claims.
        """
        if not token:
            raise CFAccessVerifierError("missing token")

        await self._ensure_jwks(force=False)
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise CFAccessVerifierError(f"malformed header: {exc}") from exc

        kid = unverified_header.get("kid")
        if not kid:
            raise CFAccessVerifierError("token header missing kid")

        key = self._find_key(kid)
        if key is None:
            # Possible key rotation — refresh once and retry.
            await self._ensure_jwks(force=True)
            key = self._find_key(kid)
            if key is None:
                raise CFAccessVerifierError(f"no signing key for kid={kid}")

        try:
            claims = jwt.decode(
                token,
                key=jwt.PyJWK(key).key,
                algorithms=["RS256"],
                audience=self._audience,
                options={"require": ["exp", "iat", "aud", "email"]},
            )
        except jwt.PyJWTError as exc:
            raise CFAccessVerifierError(f"verification failed: {exc}") from exc

        return CFAccessClaims(
            email=str(claims["email"]).strip().lower(),
            sub=str(claims.get("sub", "")),
            aud=str(claims.get("aud", "")),
            iss=str(claims.get("iss", "")),
        )

    async def _ensure_jwks(self, *, force: bool) -> None:
        """Populate the JWKS cache. Force=True bypasses the TTL."""
        if not force and self._jwks and (time.monotonic() - self._cached_at) < _JWKS_TTL_SECONDS:
            return
        async with self._lock:
            if (
                not force
                and self._jwks
                and (time.monotonic() - self._cached_at) < _JWKS_TTL_SECONDS
            ):
                return
            async with httpx.AsyncClient(timeout=_JWKS_FETCH_TIMEOUT_SECONDS) as client:
                resp = await client.get(self._certs_url)
                resp.raise_for_status()
                self._jwks = resp.json()
                self._cached_at = time.monotonic()
            keys = self._jwks.get("keys", [])
            log.info(
                "cf_access_jwks_refreshed",
                certs_url=self._certs_url,
                key_count=len(keys) if isinstance(keys, list) else 0,
                forced=force,
            )

    def _find_key(self, kid: str) -> dict[str, Any] | None:
        for key in self._jwks.get("keys", []):
            if isinstance(key, dict) and key.get("kid") == kid:
                return key
        return None


_singleton: CFAccessVerifier | None = None


def get_verifier() -> CFAccessVerifier | None:
    """Return the process-wide verifier, or None if CF Access is unconfigured.

    Callers must fail closed when this returns ``None`` — refusing to
    authenticate is the only safe behavior on a misconfigured deployment.
    """
    global _singleton
    if _singleton is not None:
        return _singleton

    domain = settings.cf_access_team_domain
    aud = settings.cf_access_aud
    if not domain or not aud:
        return None

    _singleton = CFAccessVerifier(team_domain=domain, audience=aud)
    log.info(
        "cf_access_verifier_initialized",
        team_domain=domain,
        aud_prefix=aud[:12] + "…" if len(aud) > 12 else aud,
    )
    return _singleton


def reset_verifier_for_testing() -> None:
    """Test-only hook to drop the singleton between scenarios."""
    global _singleton
    _singleton = None
