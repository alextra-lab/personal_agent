"""Tests for constraint governance waiter mechanics + wire mapping (ADR-0076 / FRE-389)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from personal_agent.orchestrator.constraint_options import (
    CONSTRAINT_OPTIONS,
    default_action_id,
    option_ids,
)
from personal_agent.service.auth import RequestUser
from personal_agent.transport.agui.adapter import to_agui_event
from personal_agent.transport.agui.ws_endpoint import (
    WaiterMetadata,
    _active_connections,
    _cancel_all_waiters,
    _ConnectionState,
    _resolve_all_waiters_user_cancel,
    _resolve_constraint_decision,
    clear_cancel_flag,
    is_cancel_requested,
    register_constraint_waiter,
)
from personal_agent.transport.events import (
    CancelledEvent,
    ConstraintPauseEvent,
    ConstraintResolvedEvent,
)


def _make_conn(session_id: str) -> _ConnectionState:
    return _ConnectionState(
        websocket=None,  # type: ignore[arg-type]
        user=RequestUser(user_id=uuid4(), email="test@example.com", display_name="Test"),
        session_id=session_id,
        outbound_queue=asyncio.Queue(maxsize=100),
    )


def _meta() -> WaiterMetadata:
    return WaiterMetadata(
        constraint="tool_iteration_limit",
        options=["continue_10", "finish_now"],
        default_option="finish_now",
        created_at=0.0,
    )


class TestActionIdRegistry:
    """The action-ID registry exposes stable IDs and a safe default."""

    def test_option_ids(self) -> None:
        assert option_ids("tool_iteration_limit") == ["continue_10", "finish_now"]
        assert option_ids("context_compression") == ["compress_continue", "stop_here"]

    def test_default_is_last_option(self) -> None:
        for name, opts in CONSTRAINT_OPTIONS.items():
            assert default_action_id(name) == opts[-1].action_id

    def test_constraint_literal_admits_attachment_cost_and_artifact_builder(self) -> None:
        """The ConstraintName literal is widened (ADR-0122 §3 / FRE-881).

        Runtime proof the pre-existing ``attachment_cost`` drift is closed (it was
        passed at the executor but absent from the closed literal) and that the new
        computed-options constraint ``artifact_builder`` is admitted. The removal of
        the executor's ``# type: ignore[arg-type]`` is proven separately by ``mypy``.
        """
        from typing import get_args

        from personal_agent.transport.events import ConstraintName

        members = set(get_args(ConstraintName))
        assert members == {
            "tool_iteration_limit",
            "context_compression",
            "attachment_cost",
            "artifact_builder",
        }


class TestAdapterMappings:
    """Wire envelopes match the ADR-0076 protocol."""

    def test_pause_envelope(self) -> None:
        ev = ConstraintPauseEvent(
            request_id="r1",
            session_id="s1",
            trace_id="t1",
            constraint="tool_iteration_limit",
            context="Reached 25 tool calls.",
            options=["continue_10", "finish_now"],
            default_option="finish_now",
            expires_at="2026-05-28T00:00:00Z",
        )
        env = to_agui_event(ev, seq=7)
        assert env["type"] == "CONSTRAINT_PAUSE"
        assert env["request_id"] == "r1"
        assert env["data"]["options"] == ["continue_10", "finish_now"]
        assert env["data"]["default_option"] == "finish_now"
        assert env["seq"] == 7

    def test_resolved_envelope(self) -> None:
        ev = ConstraintResolvedEvent(
            request_id="r1",
            session_id="s1",
            constraint="tool_iteration_limit",
            action_id="continue_10",
            resolution="user_choice",
        )
        env = to_agui_event(ev, seq=8)
        assert env["type"] == "CONSTRAINT_RESOLVED"
        assert env["data"]["action_id"] == "continue_10"
        assert env["data"]["resolution"] == "user_choice"

    def test_cancelled_envelope(self) -> None:
        env = to_agui_event(CancelledEvent(session_id="s1", trace_id="t1", reason="user_cancel"))
        assert env["type"] == "CANCELLED"
        assert env["data"]["reason"] == "user_cancel"


class TestConstraintWaiter:
    """register_constraint_waiter lifecycle: race, timeout, validation, cancel."""

    @pytest.mark.asyncio
    async def test_no_connection_returns_default_connection_lost(self) -> None:
        sid = f"no-conn-{uuid4()}"
        _active_connections.pop(sid, None)
        payload = await register_constraint_waiter(sid, "req", 1.0, _meta())
        assert payload["resolution"] == "connection_lost"
        assert payload["decision"] == "finish_now"

    @pytest.mark.asyncio
    async def test_register_before_push_no_race(self) -> None:
        """A decision arriving inside on_registered (before await) is captured (AC-14)."""
        sid = f"race-{uuid4()}"
        conn = _make_conn(sid)
        _active_connections[sid] = conn
        try:

            async def on_registered() -> None:
                # Simulate a client that replies in the same tick as the push.
                _resolve_constraint_decision(
                    conn, "req-r", {"decision": "continue_10", "remember": False}
                )

            payload = await register_constraint_waiter(
                sid, "req-r", 5.0, _meta(), on_registered=on_registered
            )
            assert payload["decision"] == "continue_10"
            assert payload["resolution"] == "user_choice"
        finally:
            _active_connections.pop(sid, None)

    @pytest.mark.asyncio
    async def test_duplicate_decision_resolved_once(self) -> None:
        """A second decision for the same request_id is dropped (AC-15)."""
        sid = f"dup-{uuid4()}"
        conn = _make_conn(sid)
        _active_connections[sid] = conn
        try:

            async def on_registered() -> None:
                _resolve_constraint_decision(conn, "req-d", {"decision": "continue_10"})
                _resolve_constraint_decision(conn, "req-d", {"decision": "finish_now"})

            payload = await register_constraint_waiter(
                sid, "req-d", 5.0, _meta(), on_registered=on_registered
            )
            assert payload["decision"] == "continue_10"
        finally:
            _active_connections.pop(sid, None)

    @pytest.mark.asyncio
    async def test_invalid_action_substitutes_default(self) -> None:
        """An action_id not in the registered options falls back to default (AC-17)."""
        sid = f"invalid-{uuid4()}"
        conn = _make_conn(sid)
        _active_connections[sid] = conn
        try:

            async def on_registered() -> None:
                _resolve_constraint_decision(conn, "req-i", {"decision": "bogus_action"})

            payload = await register_constraint_waiter(
                sid, "req-i", 5.0, _meta(), on_registered=on_registered
            )
            assert payload["decision"] == "finish_now"
        finally:
            _active_connections.pop(sid, None)

    @pytest.mark.asyncio
    async def test_timeout_applies_default(self) -> None:
        """No reply before expiry resolves with the default option (AC-5)."""
        sid = f"timeout-{uuid4()}"
        conn = _make_conn(sid)
        _active_connections[sid] = conn
        try:
            payload = await register_constraint_waiter(sid, "req-t", 0.1, _meta())
            assert payload["resolution"] == "timeout_default"
            assert payload["decision"] == "finish_now"
        finally:
            _active_connections.pop(sid, None)

    @pytest.mark.asyncio
    async def test_user_cancel_resolves_pending(self) -> None:
        """USER_CANCEL resolves a pending waiter with the default + user_cancel."""
        sid = f"cancel-{uuid4()}"
        conn = _make_conn(sid)
        _active_connections[sid] = conn
        try:

            async def canceller() -> None:
                await asyncio.sleep(0.05)
                resolved = _resolve_all_waiters_user_cancel(conn)
                assert "req-c" in resolved

            task = asyncio.create_task(canceller())
            payload = await register_constraint_waiter(sid, "req-c", 5.0, _meta())
            await task
            assert payload["resolution"] == "user_cancel"
            assert payload["decision"] == "finish_now"
        finally:
            _active_connections.pop(sid, None)

    @pytest.mark.asyncio
    async def test_disconnect_resolves_with_metadata_default(self) -> None:
        """_cancel_all_waiters resolves constraint waiters with connection_lost."""
        sid = f"disc-{uuid4()}"
        conn = _make_conn(sid)
        _active_connections[sid] = conn
        try:

            async def disconnector() -> None:
                await asyncio.sleep(0.05)
                _cancel_all_waiters(conn)

            task = asyncio.create_task(disconnector())
            payload = await register_constraint_waiter(sid, "req-x", 5.0, _meta())
            await task
            assert payload["resolution"] == "connection_lost"
            assert payload["decision"] == "finish_now"
        finally:
            _active_connections.pop(sid, None)


class TestCancelFlag:
    """is_cancel_requested / clear_cancel_flag accessors."""

    def test_flag_lifecycle(self) -> None:
        sid = f"flag-{uuid4()}"
        conn = _make_conn(sid)
        _active_connections[sid] = conn
        try:
            assert is_cancel_requested(sid) is False
            conn.cancel_requested = True
            assert is_cancel_requested(sid) is True
            clear_cancel_flag(sid)
            assert is_cancel_requested(sid) is False
        finally:
            _active_connections.pop(sid, None)

    def test_no_connection_not_cancelled(self) -> None:
        sid = f"flag-none-{uuid4()}"
        _active_connections.pop(sid, None)
        assert is_cancel_requested(sid) is False
