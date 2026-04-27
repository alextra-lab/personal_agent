"""Unit tests for the approval waiter registry (FRE-261).

Tests cover: register/resolve happy path, double-resolve, session mismatch,
timeout, concurrent waiters, and clear idempotency.
"""

from __future__ import annotations

import asyncio

import pytest

from personal_agent.transport.agui.approval_waiter import (
    ApprovalDecision,
    _pending,
    clear_approval_waiter,
    get_waiter_session_id,
    register_approval_waiter,
    resolve_approval,
    wait_for_approval,
)


@pytest.fixture(autouse=True)
def _clean_pending():
    """Ensure _pending dict is empty before and after each test."""
    _pending.clear()
    yield
    _pending.clear()


@pytest.mark.asyncio
async def test_register_and_resolve_approve() -> None:
    """Registering a waiter and resolving it with 'approve' returns correct decision."""
    fut = register_approval_waiter("req-1", "session-A")
    assert not fut.done()

    decision = ApprovalDecision(decision="approve", reason="looks fine")
    ok = resolve_approval("req-1", decision, "session-A")
    assert ok is True
    assert fut.done()
    assert fut.result() == decision


@pytest.mark.asyncio
async def test_register_and_resolve_deny() -> None:
    """Resolving with 'deny' propagates correctly."""
    fut = register_approval_waiter("req-2", "session-B")
    decision = ApprovalDecision(decision="deny", reason="too risky")
    ok = resolve_approval("req-2", decision, "session-B")
    assert ok is True
    assert fut.result().decision == "deny"


@pytest.mark.asyncio
async def test_resolve_unknown_request_id_returns_false() -> None:
    """resolve_approval returns False for an unknown request_id."""
    ok = resolve_approval("nonexistent", ApprovalDecision(decision="approve"), "session-X")
    assert ok is False


@pytest.mark.asyncio
async def test_resolve_session_mismatch_returns_false() -> None:
    """resolve_approval returns False when caller_session_id differs from registered."""
    register_approval_waiter("req-3", "session-C")
    ok = resolve_approval("req-3", ApprovalDecision(decision="approve"), "session-WRONG")
    assert ok is False
    # The Future should still be pending.
    assert not _pending["req-3"].future.done()


@pytest.mark.asyncio
async def test_double_resolve_returns_false() -> None:
    """A second resolve call on an already-resolved Future returns False."""
    register_approval_waiter("req-4", "session-D")
    resolve_approval("req-4", ApprovalDecision(decision="approve"), "session-D")
    ok = resolve_approval("req-4", ApprovalDecision(decision="deny"), "session-D")
    assert ok is False


@pytest.mark.asyncio
async def test_wait_for_approval_receives_decision() -> None:
    """wait_for_approval returns the decision resolved by resolve_approval."""
    register_approval_waiter("req-5", "session-E")

    async def _resolve_soon() -> None:
        await asyncio.sleep(0.01)
        resolve_approval("req-5", ApprovalDecision(decision="approve", reason="ok"), "session-E")

    asyncio.ensure_future(_resolve_soon())
    result = await wait_for_approval("req-5", timeout_seconds=5.0)
    assert result.decision == "approve"
    assert result.reason == "ok"
    # Waiter should be cleaned up after wait_for_approval returns.
    assert "req-5" not in _pending


@pytest.mark.asyncio
async def test_wait_for_approval_timeout() -> None:
    """wait_for_approval returns timeout decision when deadline elapses."""
    register_approval_waiter("req-6", "session-F")
    result = await wait_for_approval("req-6", timeout_seconds=0.05)
    assert result.decision == "timeout"
    # Waiter cleaned up.
    assert "req-6" not in _pending


@pytest.mark.asyncio
async def test_concurrent_waiters_resolve_independently() -> None:
    """Multiple concurrent waiters are isolated from each other."""
    register_approval_waiter("req-A", "session-1")
    register_approval_waiter("req-B", "session-2")

    async def _resolve_a() -> None:
        await asyncio.sleep(0.01)
        resolve_approval("req-A", ApprovalDecision(decision="approve"), "session-1")

    async def _resolve_b() -> None:
        await asyncio.sleep(0.02)
        resolve_approval("req-B", ApprovalDecision(decision="deny"), "session-2")

    asyncio.ensure_future(_resolve_a())
    asyncio.ensure_future(_resolve_b())

    result_a, result_b = await asyncio.gather(
        wait_for_approval("req-A", timeout_seconds=5.0),
        wait_for_approval("req-B", timeout_seconds=5.0),
    )
    assert result_a.decision == "approve"
    assert result_b.decision == "deny"
    assert "req-A" not in _pending
    assert "req-B" not in _pending


@pytest.mark.asyncio
async def test_get_waiter_session_id_returns_registered_session() -> None:
    """get_waiter_session_id returns the session ID for a known request."""
    register_approval_waiter("req-7", "session-G")
    assert get_waiter_session_id("req-7") == "session-G"


@pytest.mark.asyncio
async def test_get_waiter_session_id_unknown_returns_none() -> None:
    """get_waiter_session_id returns None for an unknown request_id."""
    assert get_waiter_session_id("nonexistent") is None


@pytest.mark.asyncio
async def test_clear_approval_waiter_idempotent() -> None:
    """clear_approval_waiter can be called multiple times without error."""
    register_approval_waiter("req-8", "session-H")
    clear_approval_waiter("req-8")
    assert "req-8" not in _pending
    # Second call is a no-op.
    clear_approval_waiter("req-8")


@pytest.mark.asyncio
async def test_wait_for_approval_unknown_request_returns_timeout() -> None:
    """wait_for_approval returns a timeout decision for an unregistered request_id."""
    result = await wait_for_approval("req-unknown", timeout_seconds=0.05)
    assert result.decision == "timeout"
