"""Inbound user identity resolution via Cloudflare Access (ADR-0064, ADR-0153 D11).

With gateway_auth_enabled=True, the email comes from the verified claim of the
Cf-Access-Jwt-Assertion JWT and maps to a stable user_id UUID in the Postgres
users table. The plaintext Cf-Access-Authenticated-User-Email header is read
only in dev mode (gateway_auth_enabled=False), which also falls back to the
deployment owner identity when no header is present.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncGenerator
from uuid import UUID

import structlog
from fastapi import HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.config.settings import get_settings
from personal_agent.service.cf_access_jwt import CFAccessVerifierError, get_main_app_verifier
from personal_agent.service.database import AsyncSessionLocal
from personal_agent.service.models import UserModel
from personal_agent.telemetry.trace import SystemTraceContext

log = structlog.get_logger(__name__)
settings = get_settings()

_CF_EMAIL_HEADER = "cf-access-authenticated-user-email"
_CF_JWT_HEADER = "cf-access-jwt-assertion"
_jwt_identity_logged = False


@dataclass(frozen=True)
class RequestUser:
    """Resolved identity for the current HTTP request.

    Attributes:
        user_id: Stable UUID from the users table. Use this as the FK
            for ownership checks — emails may change.
        email: Verified CF Access email for logging/display only.
        display_name: Human-readable name from the users table (nullable).
            Falls back to None when the users row has no display_name set.
    """

    user_id: UUID
    email: str
    display_name: str | None = None


@asynccontextmanager
async def _get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Internal async context manager for a short-lived DB session."""
    async with AsyncSessionLocal() as session:
        yield session


async def get_or_create_user_by_email(db: AsyncSession, email: str) -> UUID:
    """Return the user_id for email, creating a new user row if needed.

    This is an upsert: first attempt a SELECT; on miss INSERT with RETURNING.
    Idempotent — concurrent calls with the same email converge on the same UUID
    because email has a UNIQUE constraint.

    Args:
        db: Active async SQLAlchemy session.
        email: Verified CF Access email (lowercase).

    Returns:
        Stable UUID for this email.
    """
    result = await db.execute(select(UserModel.user_id).where(UserModel.email == email.lower()))
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    insert_result = await db.execute(
        text(
            "INSERT INTO users (user_id, email, created_at) "
            "VALUES (gen_random_uuid(), :email, :now) "
            "ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email "
            "RETURNING user_id"
        ),
        {"email": email.lower(), "now": datetime.now(timezone.utc)},
    )
    user_id: UUID = insert_result.scalar_one()
    await db.commit()
    return user_id


async def upsert_display_name_for_email(db: AsyncSession, email: str, display_name: str) -> UUID:
    """Ensure user row exists and set display_name if still unset or default.

    Coalesce rule: only writes display_name when the existing value is NULL or
    equals the email local-part (meaning it was never enriched by entity extraction).
    Idempotent — safe to call on every startup.

    Args:
        db: Active async SQLAlchemy session.
        email: CF Access email (will be lowercased).
        display_name: Human-readable name to seed.

    Returns:
        Stable UUID for this email.
    """
    email = email.lower()
    local_part = email.split("@")[0]

    result = await db.execute(
        select(UserModel.user_id, UserModel.display_name).where(UserModel.email == email)
    )
    row = result.one_or_none()

    if row is not None:
        user_id: UUID = row.user_id
        existing_name: str | None = row.display_name
        if existing_name is None or existing_name == local_part:
            await db.execute(
                text("UPDATE users SET display_name = :dn WHERE user_id = :uid"),
                {"dn": display_name, "uid": str(user_id)},
            )
            await db.commit()
        return user_id

    # Row does not exist — insert with display_name already set.
    insert_result = await db.execute(
        text(
            "INSERT INTO users (user_id, email, display_name, created_at) "
            "VALUES (gen_random_uuid(), :email, :dn, :now) "
            "ON CONFLICT (email) DO UPDATE "
            "  SET display_name = EXCLUDED.display_name "
            "  WHERE users.display_name IS NULL "
            "     OR users.display_name = :local_part "
            "RETURNING user_id"
        ),
        {
            "email": email,
            "dn": display_name,
            "now": datetime.now(timezone.utc),
            "local_part": local_part,
        },
    )
    user_id = insert_result.scalar_one()
    await db.commit()
    return user_id


async def _get_user_with_display_name(db: AsyncSession, email: str) -> tuple[UUID, str | None]:
    """Resolve (user_id, display_name) for email, creating the row if needed.

    Args:
        db: Active async SQLAlchemy session.
        email: Verified CF Access email (lowercase).

    Returns:
        Tuple of (stable user_id UUID, display_name or None).
    """
    result = await db.execute(
        select(UserModel.user_id, UserModel.display_name).where(UserModel.email == email.lower())
    )
    row = result.one_or_none()
    if row is not None:
        return row.user_id, row.display_name

    insert_result = await db.execute(
        text(
            "INSERT INTO users (user_id, email, created_at) "
            "VALUES (gen_random_uuid(), :email, :now) "
            "ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email "
            "RETURNING user_id"
        ),
        {"email": email.lower(), "now": datetime.now(timezone.utc)},
    )
    user_id: UUID = insert_result.scalar_one()
    await db.commit()
    return user_id, None


async def _verified_jwt_email(request: Request, trace_id: str) -> str:
    """Return the email claim of the request's Cloudflare Access JWT.

    Args:
        request: The incoming FastAPI request.
        trace_id: Trace id for the log lines.

    Returns:
        The verified, lowercased ``email`` claim.

    Raises:
        HTTPException: 503 when the main-app verifier is not configured, 401 when the
            JWT is absent or fails verification. The 401 is opaque by design.
    """
    verifier = get_main_app_verifier()
    if verifier is None:
        log.error(
            "request_auth_misconfigured",
            reason="cf_access_team_domain or cf_access_main_aud unset",
            trace_id=trace_id,
        )
        raise HTTPException(status_code=503, detail="Authentication unavailable")

    token = request.headers.get(_CF_JWT_HEADER)
    if not token:
        log.warning("unauthenticated_request", path=request.url.path, trace_id=trace_id)
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        claims = await verifier.verify(token)
    except CFAccessVerifierError as exc:
        log.warning(
            "request_jwt_invalid",
            path=request.url.path,
            error_class=type(exc).__name__,
            trace_id=trace_id,
        )
        raise HTTPException(status_code=401, detail="Authentication required") from exc
    return claims.email


async def resolve_request_email(request: Request, *, owner_fallback: bool = True) -> str:
    """Resolve the caller's email for a request.

    With ``gateway_auth_enabled=True`` the email comes only from the verified
    ``Cf-Access-Jwt-Assertion`` claim (ADR-0153 D11). The plaintext
    ``Cf-Access-Authenticated-User-Email`` header is not read in that mode.

    With ``gateway_auth_enabled=False`` (dev, CLI, eval) the header is accepted as
    before. If it is absent and ``owner_fallback`` is true, the deployment owner is
    used (ADR-0064 D4).

    Args:
        request: The incoming FastAPI request.
        owner_fallback: Whether dev mode may fall back to ``agent_owner_email``.

    Returns:
        The caller's email.

    Raises:
        HTTPException: 401 when no identity can be resolved, 503 when production
            verification is not configured.
    """
    trace_id = SystemTraceContext.new("request_auth").trace_id
    if settings.gateway_auth_enabled:
        return await _verified_jwt_email(request, trace_id)

    email: str | None = request.headers.get(_CF_EMAIL_HEADER)
    if not email and owner_fallback:
        email = settings.agent_owner_email
    if not email:
        log.warning(
            "unauthenticated_request",
            path=request.url.path,
            gateway_auth_enabled=False,
            trace_id=trace_id,
        )
        raise HTTPException(status_code=401, detail="Authentication required")
    return email


async def get_request_user(request: Request) -> RequestUser:
    """FastAPI dependency — resolve the authenticated user for a request.

    Identity comes from :func:`resolve_request_email`: the verified Cloudflare
    Access JWT in production, the header or the owner in dev mode.

    Args:
        request: The incoming FastAPI request.

    Returns:
        RequestUser with stable user_id, verified email, and optional display_name.

    Raises:
        HTTPException: 401 when no identity can be resolved, 503 when production
            verification is not configured.
    """
    global _jwt_identity_logged
    ctx = SystemTraceContext.new("request_auth")
    email = await resolve_request_email(request)

    async with _get_db_session() as db:
        user_id, display_name = await _get_user_with_display_name(db, email)

    if settings.gateway_auth_enabled and not _jwt_identity_logged:
        # One production-visible proof that JWT identity works. The per-request line
        # below is debug level and is not emitted in production.
        _jwt_identity_logged = True
        log.info(
            "request_user_jwt_identity_first_verified",
            user_id=str(user_id),
            trace_id=ctx.trace_id,
        )
    log.debug(
        "request_user_resolved",
        email=email,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
    )
    return RequestUser(user_id=user_id, email=email, display_name=display_name)
