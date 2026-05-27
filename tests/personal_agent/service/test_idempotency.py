"""Tests for the message-level idempotency guard (FRE-392).

Covers MessageDeduplicator — the in-process dedup store that prevents a
WebSocket reconnect from causing a second orchestrator invocation for the
same message.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

from personal_agent.service.idempotency import MessageDeduplicator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session() -> str:
    return str(uuid4())


def _trace() -> str:
    return str(uuid4())


def _msg_id() -> str:
    return str(uuid4())


# ---------------------------------------------------------------------------
# Basic dedup behaviour
# ---------------------------------------------------------------------------


def test_first_message_not_duplicate() -> None:
    d = MessageDeduplicator()
    result = d.check_and_record(_session(), "hello world", _trace())
    assert result.is_duplicate is False
    assert result.original_trace_id is None


def test_same_session_same_content_is_duplicate() -> None:
    d = MessageDeduplicator()
    session = _session()
    original_trace = _trace()

    d.check_and_record(session, "hello world", original_trace)
    result = d.check_and_record(session, "hello world", _trace())

    assert result.is_duplicate is True
    assert result.original_trace_id == original_trace


def test_different_session_same_content_not_duplicate() -> None:
    d = MessageDeduplicator()
    msg = "hello world"
    d.check_and_record(_session(), msg, _trace())

    result = d.check_and_record(_session(), msg, _trace())
    assert result.is_duplicate is False


def test_same_session_different_content_not_duplicate() -> None:
    d = MessageDeduplicator()
    session = _session()
    d.check_and_record(session, "hello world", _trace())

    result = d.check_and_record(session, "goodbye world", _trace())
    assert result.is_duplicate is False


# ---------------------------------------------------------------------------
# client_msg_id behaviour
# ---------------------------------------------------------------------------


def test_client_msg_id_deduplicates_regardless_of_content() -> None:
    """Same client_msg_id → duplicate even if content differs (key takes precedence)."""
    d = MessageDeduplicator()
    session = _session()
    msg_id = _msg_id()
    original_trace = _trace()

    d.check_and_record(session, "content A", original_trace, client_msg_id=msg_id)
    result = d.check_and_record(session, "content B", _trace(), client_msg_id=msg_id)

    assert result.is_duplicate is True
    assert result.original_trace_id == original_trace


def test_different_client_msg_id_same_content_not_duplicate() -> None:
    """Different client_msg_id means independent sends — never a duplicate."""
    d = MessageDeduplicator()
    session = _session()
    msg = "hello world"

    d.check_and_record(session, msg, _trace(), client_msg_id=_msg_id())
    result = d.check_and_record(session, msg, _trace(), client_msg_id=_msg_id())

    assert result.is_duplicate is False


def test_content_hash_fallback_when_no_client_msg_id() -> None:
    """Without client_msg_id the guard still deduplicates via content hash."""
    d = MessageDeduplicator()
    session = _session()
    original_trace = _trace()

    d.check_and_record(session, "some message", original_trace)
    result = d.check_and_record(session, "some message", _trace(), client_msg_id=None)

    assert result.is_duplicate is True
    assert result.original_trace_id == original_trace


# ---------------------------------------------------------------------------
# TTL / expiry
# ---------------------------------------------------------------------------


def test_entry_expires_after_ttl() -> None:
    d = MessageDeduplicator(ttl_seconds=60.0)
    session = _session()
    original_trace = _trace()
    msg = "expiring message"

    with patch("personal_agent.service.idempotency.time.monotonic", return_value=0.0):
        d.check_and_record(session, msg, original_trace)

    # Past the TTL window
    with patch("personal_agent.service.idempotency.time.monotonic", return_value=61.0):
        result = d.check_and_record(session, msg, _trace())

    assert result.is_duplicate is False


def test_entry_within_ttl_still_duplicate() -> None:
    d = MessageDeduplicator(ttl_seconds=60.0)
    session = _session()
    original_trace = _trace()
    msg = "still-live message"

    with patch("personal_agent.service.idempotency.time.monotonic", return_value=0.0):
        d.check_and_record(session, msg, original_trace)

    with patch("personal_agent.service.idempotency.time.monotonic", return_value=59.9):
        result = d.check_and_record(session, msg, _trace())

    assert result.is_duplicate is True
    assert result.original_trace_id == original_trace


# ---------------------------------------------------------------------------
# release()
# ---------------------------------------------------------------------------


def test_release_allows_immediate_resend() -> None:
    d = MessageDeduplicator()
    session = _session()
    msg = "send me again"

    first = d.check_and_record(session, msg, _trace())
    assert first.is_duplicate is False

    d.release(session, msg, client_msg_id=None)

    result = d.check_and_record(session, msg, _trace())
    assert result.is_duplicate is False


def test_release_with_client_msg_id() -> None:
    d = MessageDeduplicator()
    session = _session()
    msg_id = _msg_id()

    d.check_and_record(session, "any content", _trace(), client_msg_id=msg_id)
    d.release(session, "any content", client_msg_id=msg_id)

    result = d.check_and_record(session, "any content", _trace(), client_msg_id=msg_id)
    assert result.is_duplicate is False


def test_release_nonexistent_entry_is_noop() -> None:
    d = MessageDeduplicator()
    # Should not raise
    d.release(_session(), "ghost message", client_msg_id=None)


# ---------------------------------------------------------------------------
# cleanup_expired()
# ---------------------------------------------------------------------------


def test_cleanup_expired_removes_old_entries() -> None:
    d = MessageDeduplicator(ttl_seconds=60.0)
    session = _session()

    with patch("personal_agent.service.idempotency.time.monotonic", return_value=0.0):
        d.check_and_record(session, "msg A", _trace())
        d.check_and_record(session, "msg B", _trace())

    with patch("personal_agent.service.idempotency.time.monotonic", return_value=61.0):
        removed = d.cleanup_expired()

    assert removed == 2


def test_cleanup_expired_leaves_live_entries() -> None:
    d = MessageDeduplicator(ttl_seconds=60.0)
    session = _session()

    with patch("personal_agent.service.idempotency.time.monotonic", return_value=0.0):
        d.check_and_record(session, "msg A", _trace())  # will expire

    with patch("personal_agent.service.idempotency.time.monotonic", return_value=30.0):
        d.check_and_record(session, "msg B", _trace())  # still live at 61

    with patch("personal_agent.service.idempotency.time.monotonic", return_value=61.0):
        removed = d.cleanup_expired()

    assert removed == 1

    # "msg B" is still a duplicate
    with patch("personal_agent.service.idempotency.time.monotonic", return_value=61.0):
        result = d.check_and_record(session, "msg B", _trace())
    assert result.is_duplicate is True


def test_cleanup_expired_returns_zero_when_nothing_to_remove() -> None:
    d = MessageDeduplicator()
    assert d.cleanup_expired() == 0
