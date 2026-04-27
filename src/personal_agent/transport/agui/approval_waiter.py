"""Approval waiter registry for tool-call HITL round-trips (FRE-261).

Each pending approval is represented by an :class:`asyncio.Future` keyed on a
unique ``request_id``.  The SSE endpoint pushes a
:class:`~personal_agent.transport.events.ToolApprovalRequestEvent` to the
frontend; the frontend POSTs a decision to ``/agui/approval/{request_id}``,
which resolves the Future; and the tool executor awaits that Future before
proceeding.

This module is intentionally free of FastAPI / SQLAlchemy dependencies so it
can be imported from tool executor code without triggering circular imports.

Pattern modelled on :mod:`personal_agent.events.session_write_waiter`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ApprovalDecision:
    """Result of a HITL approval round-trip.

    Attributes:
        decision: Outcome of the approval request.
        reason: Optional human-supplied explanation for the decision.
    """

    decision: Literal["approve", "deny", "timeout"]
    reason: str | None = None


@dataclass(frozen=True)
class ApprovalWaiterEntry:
    """Registry entry for a pending approval request.

    Attributes:
        future: Asyncio Future resolved when the decision arrives.
        session_id: Session that owns this approval request (used for auth
            checks in the endpoint).
    """

    future: asyncio.Future[ApprovalDecision]
    session_id: str


# request_id -> ApprovalWaiterEntry
_pending: dict[str, ApprovalWaiterEntry] = {}


def register_approval_waiter(request_id: str, session_id: str) -> asyncio.Future[ApprovalDecision]:
    """Register a Future that resolves when an approval decision arrives.

    Call this *before* pushing the ``ToolApprovalRequestEvent`` to the SSE
    queue so that a race-free resolve path is guaranteed.

    Args:
        request_id: Unique identifier for this approval request (UUID string).
        session_id: Session that owns this request — used for auth checks
            when the frontend POSTs the decision.

    Returns:
        Future that will be resolved by :func:`resolve_approval` or timed out
        by :func:`wait_for_approval`.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[ApprovalDecision] = loop.create_future()
    _pending[request_id] = ApprovalWaiterEntry(future=fut, session_id=session_id)
    log.debug("approval_waiter.registered", request_id=request_id, session_id=session_id)
    return fut


def resolve_approval(
    request_id: str,
    decision: ApprovalDecision,
    caller_session_id: str,
) -> bool:
    """Resolve a pending approval request with the given decision.

    Args:
        request_id: The approval request to resolve.
        decision: The decision to deliver.
        caller_session_id: Session ID of the caller — must match the session
            that registered the waiter to prevent cross-session resolution.

    Returns:
        ``True`` if the Future was successfully resolved; ``False`` if the
        ``request_id`` is unknown, already resolved, or the ``caller_session_id``
        does not match the registered session.
    """
    entry = _pending.get(request_id)
    if entry is None:
        log.warning("approval_waiter.resolve_unknown", request_id=request_id)
        return False

    if entry.session_id != caller_session_id:
        log.warning(
            "approval_waiter.session_mismatch",
            request_id=request_id,
            expected_session_id=entry.session_id,
            caller_session_id=caller_session_id,
        )
        return False

    if entry.future.done():
        log.warning("approval_waiter.already_resolved", request_id=request_id)
        return False

    entry.future.set_result(decision)
    log.info(
        "approval_waiter.resolved",
        request_id=request_id,
        decision=decision.decision,
        session_id=caller_session_id,
    )
    return True


async def wait_for_approval(request_id: str, timeout_seconds: float) -> ApprovalDecision:
    """Await the decision for an approval request, with a timeout.

    On timeout, the waiter is cleaned up and a
    ``ApprovalDecision(decision="timeout")`` is returned.

    Args:
        request_id: The approval request to wait on.  Must already be
            registered via :func:`register_approval_waiter`.
        timeout_seconds: Maximum seconds to wait before returning a timeout
            decision.

    Returns:
        The :class:`ApprovalDecision` resolved by the frontend, or a timeout
        decision if the deadline elapses first.
    """
    entry = _pending.get(request_id)
    if entry is None:
        log.warning("approval_waiter.wait_unknown", request_id=request_id)
        return ApprovalDecision(decision="timeout", reason="request_id not registered")

    try:
        result = await asyncio.wait_for(entry.future, timeout=timeout_seconds)
        return result
    except asyncio.TimeoutError:
        log.warning(
            "approval_waiter.timeout",
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )
        return ApprovalDecision(decision="timeout", reason=f"timed out after {timeout_seconds}s")
    finally:
        clear_approval_waiter(request_id)


def clear_approval_waiter(request_id: str) -> None:
    """Remove the waiter entry for ``request_id``.

    Idempotent — safe to call even if the entry does not exist or has
    already been removed.

    Args:
        request_id: The approval request to clear.
    """
    _pending.pop(request_id, None)
    log.debug("approval_waiter.cleared", request_id=request_id)


def get_waiter_session_id(request_id: str) -> str | None:
    """Return the session ID associated with a pending approval request.

    Used by the endpoint to verify that the caller's session matches the
    session that registered the waiter before resolving it.

    Args:
        request_id: The approval request to look up.

    Returns:
        The registered session ID, or ``None`` if the request is unknown.
    """
    entry = _pending.get(request_id)
    return entry.session_id if entry is not None else None
