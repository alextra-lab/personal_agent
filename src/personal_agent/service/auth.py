"""Inbound user identity resolution via Cloudflare Access (ADR-0064).

Reads Cf-Access-Authenticated-User-Email from inbound requests and maps
it to a stable user_id UUID in the Postgres users table. Provides a
dev-mode fallback to the deployment owner identity when no CF header is
present and gateway_auth_enabled=False.
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
from personal_agent.service.database import AsyncSessionLocal
from personal_agent.service.models import UserModel

log = structlog.get_logger(__name__)
settings = get_settings()

_CF_EMAIL_HEADER = "cf-access-authenticated-user-email"


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


async def get_request_user(request: Request) -> RequestUser:
    """FastAPI dependency — resolve the authenticated user for a request.

    Resolution order:
    1. Cf-Access-Authenticated-User-Email header (CF Access, production path).
    2. Dev-mode fallback: if gateway_auth_enabled=False and agent_owner_email
       is set, use the deployment owner identity (CLI / local dev path).
    3. Raise HTTP 401 if neither condition is met.

    Args:
        request: The incoming FastAPI request.

    Returns:
        RequestUser with stable user_id, verified email, and optional display_name.

    Raises:
        HTTPException: 401 when no identity can be resolved.
    """
    email: str | None = request.headers.get(_CF_EMAIL_HEADER)

    if not email:
        if not settings.gateway_auth_enabled and settings.agent_owner_email:
            email = settings.agent_owner_email
        else:
            log.warning(
                "unauthenticated_request",
                path=request.url.path,
                gateway_auth_enabled=settings.gateway_auth_enabled,
            )
            raise HTTPException(status_code=401, detail="Authentication required")

    async with _get_db_session() as db:
        user_id, display_name = await _get_user_with_display_name(db, email)

    log.debug("request_user_resolved", email=email, user_id=str(user_id))
    return RequestUser(user_id=user_id, email=email, display_name=display_name)
