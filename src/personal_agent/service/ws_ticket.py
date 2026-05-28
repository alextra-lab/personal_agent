"""Short-lived single-use WebSocket tickets (ADR-0075 / FRE-388).

Browsers cannot send custom HTTP headers on WebSocket connections.  Passing
the real bearer token as a query parameter leaks it to proxy logs and error
telemetry.  Instead, the PWA mints a ticket over HTTPS and passes it as a
query parameter on the WS handshake:

1. ``POST /api/ws-ticket`` with ``Authorization: Bearer <token>``
2. Server validates, mints ticket scoped to ``(user_id, session_id)``
3. PWA opens ``wss://host/ws/{session_id}?ticket=<ticket>``
4. WS endpoint calls ``consume_ws_ticket`` — single-use, 30 s TTL
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from uuid import UUID

from personal_agent.config.settings import get_settings
from personal_agent.service.auth import RequestUser
from personal_agent.telemetry import get_logger

log = get_logger(__name__)
settings = get_settings()


@dataclass(frozen=True)
class _WSTicketEntry:
    """Internal ticket record stored in the pending dict."""

    user_id: UUID
    session_id: UUID
    email: str
    display_name: str | None
    expires_at: float  # time.monotonic() deadline


_pending_tickets: dict[str, _WSTicketEntry] = {}


def mint_ws_ticket(user: RequestUser, session_id: UUID) -> str:
    """Mint a single-use WS ticket scoped to a user and session.

    Args:
        user: Authenticated user from the HTTPS request.
        session_id: Session the ticket authorizes access to.

    Returns:
        Cryptographically random ticket string (43 characters).
    """
    _evict_expired()
    ticket_id = secrets.token_urlsafe(32)
    _pending_tickets[ticket_id] = _WSTicketEntry(
        user_id=user.user_id,
        session_id=session_id,
        email=user.email,
        display_name=user.display_name,
        expires_at=time.monotonic() + settings.ws_ticket_ttl_seconds,
    )
    log.debug(
        "ws_ticket.minted",
        user_id=str(user.user_id),
        session_id=str(session_id),
    )
    return ticket_id


def consume_ws_ticket(ticket_id: str, session_id: UUID) -> RequestUser | None:
    """Validate and consume a ticket.  Returns None on any failure.

    Failures: ticket not found, already consumed, expired, or session_id
    mismatch.  The ticket is always removed from the pending dict on
    consumption (single-use).

    Args:
        ticket_id: The ticket string from the query parameter.
        session_id: The session_id from the WS URL path.

    Returns:
        Authenticated ``RequestUser`` if valid, else ``None``.
    """
    entry = _pending_tickets.pop(ticket_id, None)
    if entry is None:
        log.debug(
            "ws_ticket.not_found_or_reused",
            ticket_prefix=ticket_id[:8],
            session_id=str(session_id),
        )
        return None

    if time.monotonic() > entry.expires_at:
        log.debug("ws_ticket.expired", ticket_prefix=ticket_id[:8], session_id=str(session_id))
        return None

    if entry.session_id != session_id:
        log.warning(
            "ws_ticket.session_mismatch",
            ticket_prefix=ticket_id[:8],
            expected_session=str(entry.session_id),
            actual_session=str(session_id),
            session_id=str(session_id),
        )
        return None

    return RequestUser(
        user_id=entry.user_id,
        email=entry.email,
        display_name=entry.display_name,
    )


def _evict_expired() -> None:
    """Remove expired tickets to prevent unbounded growth."""
    now = time.monotonic()
    expired = [tid for tid, entry in _pending_tickets.items() if now > entry.expires_at]
    for tid in expired:
        del _pending_tickets[tid]
