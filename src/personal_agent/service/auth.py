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
    """

    user_id: UUID
    email: str


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
        RequestUser with stable user_id and verified email.

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
        user_id = await get_or_create_user_by_email(db, email)

    log.debug("request_user_resolved", email=email, user_id=str(user_id))
    return RequestUser(user_id=user_id, email=email)
