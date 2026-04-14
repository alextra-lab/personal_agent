"""Session REST endpoints for the Seshat API Gateway.

Exposes PostgreSQL session data over HTTP under ``/sessions/*``.  All
endpoints require the ``sessions:read`` scope.

The router delegates to :class:`~personal_agent.service.repositories.session_repository.SessionRepository`
via a :class:`~sqlalchemy.ext.asyncio.AsyncSession` obtained from the app's
session factory stored in ``request.app.state.db_session_factory``.
"""

from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.gateway.auth import TokenInfo, require_scope
from personal_agent.gateway.errors import not_found, service_unavailable
from personal_agent.gateway.rate_limiting import get_rate_limiter

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


# ---------------------------------------------------------------------------
# Dependency: resolve DB session factory from app state
# ---------------------------------------------------------------------------


async def _get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Provide an async database session from the app-state factory.

    Args:
        request: Incoming FastAPI request.

    Yields:
        Async SQLAlchemy session.

    Raises:
        HTTPException(503): When no session factory is attached.
    """
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise service_unavailable("Database session factory is not available")

    async with factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_sessions(
    request: Request,
    limit: int = 20,
    token: TokenInfo = Depends(require_scope("sessions:read")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> list[dict[str, Any]]:
    """List recent sessions ordered by last activity.

    Args:
        request: FastAPI request (injected).
        limit: Maximum number of sessions to return (default 20).
        token: Validated bearer token with ``sessions:read`` scope.
        db: Async SQLAlchemy session (injected).

    Returns:
        List of session summary dicts.
    """
    get_rate_limiter().check(token)
    from personal_agent.service.repositories.session_repository import SessionRepository

    log.info("gateway_sessions_list", limit=limit, token_name=token.name)

    repo = SessionRepository(db)
    sessions = await repo.list_recent(limit)
    return [_session_to_dict(s) for s in sessions]


@router.get("/{session_id}")
async def get_session(
    request: Request,
    session_id: str,
    token: TokenInfo = Depends(require_scope("sessions:read")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> dict[str, Any]:
    """Retrieve a single session by ID.

    Args:
        request: FastAPI request (injected).
        session_id: UUID string of the session.
        token: Validated bearer token with ``sessions:read`` scope.
        db: Async SQLAlchemy session (injected).

    Returns:
        Session dict.

    Raises:
        HTTPException(422): When ``session_id`` is not a valid UUID.
        HTTPException(404): When the session does not exist.
    """
    get_rate_limiter().check(token)
    from personal_agent.service.repositories.session_repository import SessionRepository

    log.info("gateway_sessions_get", session_id=session_id, token_name=token.name)

    try:
        uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_parameter",
                "message": "session_id must be a valid UUID",
                "status": 422,
            },
        ) from exc

    repo = SessionRepository(db)
    session = await repo.get(uuid)
    if session is None:
        raise not_found("session")
    return _session_to_dict(session)


@router.get("/{session_id}/messages")
async def get_session_messages(
    request: Request,
    session_id: str,
    limit: int = 50,
    token: TokenInfo = Depends(require_scope("sessions:read")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> list[dict[str, Any]]:
    """Retrieve messages for a session.

    Args:
        request: FastAPI request (injected).
        session_id: UUID string of the session.
        limit: Maximum number of messages to return (default 50).
        token: Validated bearer token with ``sessions:read`` scope.
        db: Async SQLAlchemy session (injected).

    Returns:
        List of message dicts in chronological order.

    Raises:
        HTTPException(422): When ``session_id`` is not a valid UUID.
        HTTPException(404): When the session does not exist.
    """
    get_rate_limiter().check(token)
    from personal_agent.service.repositories.session_repository import SessionRepository

    log.info(
        "gateway_sessions_get_messages",
        session_id=session_id,
        limit=limit,
        token_name=token.name,
    )

    try:
        uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_parameter",
                "message": "session_id must be a valid UUID",
                "status": 422,
            },
        ) from exc

    repo = SessionRepository(db)
    session = await repo.get(uuid)
    if session is None:
        raise not_found("session")

    messages: list[dict[str, Any]] = list(session.messages or [])
    if limit > 0:
        messages = messages[-limit:]
    return messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_to_dict(session: Any) -> dict[str, Any]:
    """Serialise a ``SessionModel`` to a plain dict.

    Args:
        session: SQLAlchemy ``SessionModel`` instance.

    Returns:
        Dict with serialised session fields.
    """
    return {
        "session_id": str(session.session_id),
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "last_active_at": session.last_active_at.isoformat() if session.last_active_at else None,
        "mode": session.mode,
        "channel": session.channel,
        "message_count": len(session.messages or []),
    }
