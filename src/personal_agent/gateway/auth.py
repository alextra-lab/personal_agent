"""Bearer-token authentication and scope validation for the Seshat API Gateway.

Tokens are declared in ``config/gateway_access.yaml``. In local dev mode
(``settings.gateway_auth_enabled = False``) the dependency returns a permissive
``TokenInfo`` so endpoints work without any token.

Token comparison uses constant-time ``hmac.compare_digest`` to prevent timing
attacks.  Tokens are *not* hashed in the YAML (kept simple for local dev);
production deployments should inject secrets via environment variables.

Usage::

    @router.get("/knowledge/search")
    async def search(token: TokenInfo = Depends(require_scope("knowledge:read"))):
        ...
"""

import hmac
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml  # type: ignore[import-untyped]
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from personal_agent.config.settings import get_settings

log = structlog.get_logger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class TokenInfo:
    """Validated token metadata extracted from the bearer token.

    Attributes:
        name: Human-readable token name (e.g. ``"claude-code-local"``).
        scopes: Frozenset of granted permission scopes.
        rate_limit: Maximum requests per hour for this token.
    """

    name: str
    scopes: frozenset[str]
    rate_limit: int  # requests per hour


# Permissive token returned when auth is disabled
_DEV_TOKEN = TokenInfo(
    name="dev-local-no-auth",
    scopes=frozenset(
        [
            "knowledge:read",
            "knowledge:write",
            "sessions:read",
            "sessions:write",
            "observations:read",
            "observations:write",
        ]
    ),
    rate_limit=100_000,
)


def _expand_env_var(value: str) -> str:
    """Expand ``${VAR_NAME}`` patterns using os.environ.

    Args:
        value: Raw value string, potentially containing ``${VAR}`` references.

    Returns:
        Expanded string.  Unexpanded references are returned as-is so that
        local dev YAML with literal secrets still works.
    """
    import re

    def replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))

    return re.sub(r"\$\{([^}]+)\}", replace, value)


def load_token_config(path: Path) -> dict[str, TokenInfo]:
    """Parse gateway_access.yaml and build a lookup dict keyed by token secret.

    Args:
        path: Path to the YAML access-control config file.

    Returns:
        Mapping of token secret → ``TokenInfo``.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If a token entry is missing required fields.
    """
    if not path.exists():
        log.warning("gateway_access_config_not_found", path=str(path))
        return {}

    with path.open() as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    tokens: list[dict[str, Any]] = raw.get("tokens", [])
    result: dict[str, TokenInfo] = {}

    for entry in tokens:
        name: str = entry.get("name", "")
        secret_raw: str = entry.get("secret", "")
        scopes_raw: Sequence[str] = entry.get("scopes", [])
        rate_limit_raw: str | int = entry.get("rate_limit", 100)

        if not name or not secret_raw:
            log.warning("gateway_access_config_invalid_entry", entry=entry)
            continue

        secret = _expand_env_var(str(secret_raw))

        # Parse "1000/hour" or plain int
        if isinstance(rate_limit_raw, str) and "/" in rate_limit_raw:
            rate_limit = int(rate_limit_raw.split("/")[0])
        else:
            rate_limit = int(rate_limit_raw)

        result[secret] = TokenInfo(
            name=name,
            scopes=frozenset(scopes_raw),
            rate_limit=rate_limit,
        )

    log.info("gateway_access_config_loaded", token_count=len(result))
    return result


# Module-level cache: populated on first call to verify_token
_token_registry: dict[str, TokenInfo] | None = None


def _get_token_registry() -> dict[str, TokenInfo]:
    """Return (and lazily initialise) the token registry.

    Returns:
        Mapping of secret → TokenInfo.
    """
    global _token_registry
    if _token_registry is None:
        settings = get_settings()
        config_path = Path(settings.gateway_access_config)
        _token_registry = load_token_config(config_path)
    return _token_registry


def _reset_token_registry() -> None:
    """Clear the cached registry (used in tests)."""
    global _token_registry
    _token_registry = None


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
) -> TokenInfo:
    """FastAPI dependency that validates a Bearer token.

    When ``settings.gateway_auth_enabled`` is False the dependency always
    succeeds and returns :data:`_DEV_TOKEN` (permissive, no token needed).

    Args:
        credentials: Injected by FastAPI from the ``Authorization`` header.

    Returns:
        Validated ``TokenInfo``.

    Raises:
        HTTPException(401): When auth is enabled and token is missing or invalid.
    """
    settings = get_settings()
    if not settings.gateway_auth_enabled:
        return _DEV_TOKEN

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "unauthorized",
                "message": "Missing or invalid bearer token",
                "status": 401,
            },
        )

    raw_token = credentials.credentials
    registry = _get_token_registry()

    # Constant-time comparison — iterate all entries to avoid early-exit leaks
    matched: TokenInfo | None = None
    for stored_secret, info in registry.items():
        if hmac.compare_digest(raw_token.encode(), stored_secret.encode()):
            matched = info

    if matched is None:
        log.warning("gateway_auth_failed", token_prefix=raw_token[:6] + "…")
        raise HTTPException(
            status_code=401,
            detail={
                "error": "unauthorized",
                "message": "Invalid bearer token",
                "status": 401,
            },
        )

    return matched


def require_scope(scope: str) -> Callable[..., Any]:
    """Return a FastAPI dependency factory that checks a specific scope.

    The returned dependency first calls :func:`verify_token` (which handles
    auth-disabled fast-path), then asserts the given scope is present.

    Args:
        scope: Required scope string (e.g. ``"knowledge:read"``).

    Returns:
        An async callable suitable for use with ``Depends()``.

    Example::

        @router.get("/search")
        async def search(token: TokenInfo = Depends(require_scope("knowledge:read"))):
            ...
    """

    async def _check(token: TokenInfo = Depends(verify_token)) -> TokenInfo:  # noqa: B008
        if scope not in token.scopes:
            log.warning(
                "gateway_scope_denied",
                token_name=token.name,
                required_scope=scope,
                granted_scopes=list(token.scopes),
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "forbidden",
                    "message": f"Token does not have required scope: {scope}",
                    "status": 403,
                },
            )
        return token

    return _check
