"""Session REST endpoints for the Seshat API Gateway.

Exposes PostgreSQL session data over HTTP under ``/sessions/*``.  All
endpoints require the ``sessions:read`` scope **and** a verified
``Cf-Access-Authenticated-User-Email`` header. Session rows are scoped to
the resolved user_id so a holder of the bearer token cannot read another
user's data — closes the cross-user data leak fixed in this hotfix.
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
from personal_agent.service.auth import _CF_EMAIL_HEADER, _get_user_with_display_name
from personal_agent.service.models import SessionProfileUpdate
from personal_agent.telemetry.trace import SystemTraceContext

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


async def _require_request_user_id(request: Request, db: AsyncSession) -> UUID:
    """Resolve the authenticated user's UUID from CF Access headers.

    The gateway router's bearer token authorizes *access* to the endpoint
    family; this helper additionally pins each request to one user so the
    bearer-token holder cannot enumerate other users' sessions. Mirrors
    :func:`personal_agent.service.auth.get_request_user` but returns just
    the ``user_id`` (no full ``RequestUser``) so the gateway router stays
    free of FastAPI-only types.

    Args:
        request: Incoming FastAPI request.
        db: Active async SQLAlchemy session for the users-table lookup.

    Returns:
        Stable ``user_id`` UUID for the requester.

    Raises:
        HTTPException(401): When the ``Cf-Access-Authenticated-User-Email``
            header is absent. The gateway never falls back to a default
            owner — token-only callers are rejected outright.
    """
    email = request.headers.get(_CF_EMAIL_HEADER)
    if not email:
        raise HTTPException(
            status_code=401,
            detail="Authentication required (missing CF Access user header)",
        )
    user_id, _ = await _get_user_with_display_name(db, email)
    return UUID(str(user_id))


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

    user_id = await _require_request_user_id(request, db)
    ctx = SystemTraceContext.new("session_api")
    log.info(
        "gateway_sessions_list",
        limit=limit,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
    )

    repo = SessionRepository(db)
    sessions = await repo.list_recent(limit, user_id=user_id)
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

    user_id = await _require_request_user_id(request, db)
    ctx = SystemTraceContext.new("session_api", session_id=session_id)
    log.info(
        "gateway_sessions_get",
        session_id=session_id,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
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
    # 404 (not 403) on ownership mismatch — do not confirm existence of
    # other users' sessions.
    session = await repo.get(uuid, user_id=user_id)
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

    user_id = await _require_request_user_id(request, db)
    ctx = SystemTraceContext.new("session_api", session_id=session_id)
    log.info(
        "gateway_sessions_get_messages",
        session_id=session_id,
        limit=limit,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
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
    session = await repo.get(uuid, user_id=user_id)
    if session is None:
        raise not_found("session")

    messages: list[dict[str, Any]] = list(session.messages or [])
    if limit > 0:
        messages = messages[-limit:]

    # FRE-426: attach each assistant turn's stored rating so the PWA can render
    # the rating control with the user's previously-submitted score (and the
    # rated-vs-default visual state) across reloads.
    es_client = getattr(request.app.state, "es_client", None)
    await _attach_turn_ratings(messages, es_client, ctx_trace_id=ctx.trace_id)
    return messages


async def _attach_turn_ratings(
    messages: list[dict[str, Any]],
    es_client: Any | None,
    *,
    ctx_trace_id: str,
) -> None:
    """Annotate assistant messages with their stored rating (FRE-426).

    Joins ``user-turn-ratings-*`` by ``trace_id`` in a single query and sets
    ``rating`` on each matching message. Best-effort: on ES miss/unavailable
    the messages are returned unannotated (the control falls back to its
    unrated default).

    Args:
        messages: Message dicts to annotate in place.
        es_client: AsyncElasticsearch client from app state, or None.
        ctx_trace_id: Request trace ID for log correlation.
    """
    if es_client is None:
        return
    trace_ids = [
        str(m["trace_id"]) for m in messages if m.get("role") == "assistant" and m.get("trace_id")
    ]
    if not trace_ids:
        return
    try:
        resp = await es_client.search(
            index="user-turn-ratings-*",
            query={"terms": {"trace_id": trace_ids}},
            size=len(trace_ids),
            _source=["trace_id", "rating"],
        )
    except Exception:
        log.warning("session_messages_rating_join_failed", trace_id=ctx_trace_id)
        return
    by_trace: dict[str, int] = {}
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        tid = src.get("trace_id")
        rating = src.get("rating")
        if isinstance(tid, str) and isinstance(rating, int):
            by_trace[tid] = rating
    for m in messages:
        # Only the assistant turn is rateable; the user message in the same turn
        # shares the trace_id, so guard on role to avoid tagging it.
        if m.get("role") != "assistant":
            continue
        tid = m.get("trace_id")
        if isinstance(tid, str) and tid in by_trace:
            m["rating"] = by_trace[tid]


@router.patch("/{session_id}")
async def update_session_profile(
    request: Request,
    session_id: str,
    body: SessionProfileUpdate,
    token: TokenInfo = Depends(require_scope("sessions:write")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> dict[str, Any]:
    """Set a session's server-owned execution profile (ADR-0079 / FRE-416).

    The profile is the source of truth for which models run the session's
    turns. This is the canonical write path used by the PWA profile toggle;
    the change is persisted and emitted to the active client as a
    ``session_profile`` STATE_DELTA. The write is scoped to the authenticated
    user so a token holder cannot mutate another user's session.

    Args:
        request: FastAPI request (injected).
        session_id: UUID string of the session.
        body: New execution profile.
        token: Validated bearer token with ``sessions:write`` scope.
        db: Async SQLAlchemy session (injected).

    Returns:
        The updated session dict.

    Raises:
        HTTPException(422): When ``session_id`` is not a valid UUID or the
            profile name is not a known profile.
        HTTPException(404): When the session does not exist or is owned by
            another user.
    """
    get_rate_limiter().check(token)
    from personal_agent.config.profile import is_valid_profile
    from personal_agent.service.models import SessionUpdate
    from personal_agent.service.repositories.session_repository import SessionRepository
    from personal_agent.transport.agui.transport import emit_session_profile

    user_id = await _require_request_user_id(request, db)
    ctx = SystemTraceContext.new("session_api", session_id=session_id)

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

    if not is_valid_profile(body.profile):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_parameter",
                "message": f"unknown execution profile: {body.profile}",
                "status": 422,
            },
        )

    repo = SessionRepository(db)
    session = await repo.update(
        uuid, SessionUpdate(execution_profile=body.profile), user_id=user_id
    )
    if session is None:
        raise not_found("session")

    await emit_session_profile(session_id=session_id, profile=body.profile)
    log.info(
        "gateway_sessions_set_profile",
        session_id=session_id,
        profile=body.profile,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
    )
    return _session_to_dict(session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_title(messages: list[dict[str, Any]]) -> str | None:
    """Derive a session title from the first user message.

    Args:
        messages: List of message dicts from the session.

    Returns:
        First 60 characters of the first user message, with a trailing
        ellipsis when truncated, or ``None`` when no user message exists.
    """
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content"):
            text = str(msg["content"]).strip()
            if text:
                return text[:60] + ("…" if len(text) > 60 else "")
    return None


def _session_to_dict(session: Any) -> dict[str, Any]:
    """Serialise a ``SessionModel`` to a plain dict.

    Args:
        session: SQLAlchemy ``SessionModel`` instance.

    Returns:
        Dict with serialised session fields including a derived ``title``.
    """
    msgs = list(session.messages or [])
    return {
        "session_id": str(session.session_id),
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "last_active_at": session.last_active_at.isoformat() if session.last_active_at else None,
        "mode": session.mode,
        "channel": session.channel,
        # ADR-0079: server-authoritative execution profile for PWA hydration.
        "execution_profile": getattr(session, "execution_profile", "local") or "local",
        "message_count": len(msgs),
        "title": _extract_title(msgs),
    }
