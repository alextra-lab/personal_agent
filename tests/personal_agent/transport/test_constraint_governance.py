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
    _session_constraint_waiters,
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
    async def test_no_connection_registers_waiter_and_waits(self) -> None:
        """No socket must NOT resolve instantly — it waits out the timeout (FRE-928 AC-2).

        Rewritten from ``test_no_connection_returns_default_connection_lost``, which
        encoded the defect as the contract: the old code returned ``connection_lost``
        immediately, bypassing its own timeout. A headless/CLI caller still falls back
        to the default, but only when the timeout actually expires.
        """
        sid = f"no-conn-{uuid4()}"
        _active_connections.pop(sid, None)

        loop = asyncio.get_running_loop()
        started = loop.time()
        payload = await register_constraint_waiter(sid, "req", 0.3, _meta())
        elapsed = loop.time() - started

        assert payload["resolution"] == "timeout_default"
        assert payload["decision"] == "finish_now"
        assert elapsed >= 0.25, f"returned instantly ({elapsed:.3f}s) — bypassed the timeout"
        assert _session_constraint_waiters.get(sid) is None, "waiter registry leaked"

    @pytest.mark.asyncio
    async def test_no_connection_persists_pause_for_replay(self) -> None:
        """With no socket the pause is still pushed, so a reconnect can replay it (AC-1).

        Previously ``on_registered`` was skipped entirely on the no-connection path, so
        nothing was persisted and there was nothing to replay.
        """
        sid = f"no-conn-push-{uuid4()}"
        _active_connections.pop(sid, None)
        pushed = False

        async def on_registered() -> None:
            nonlocal pushed
            pushed = True

        await register_constraint_waiter(sid, "req-p", 0.2, _meta(), on_registered=on_registered)
        assert pushed, "pause event was never pushed/persisted — nothing to replay"

    @pytest.mark.asyncio
    async def test_pending_decision_survives_reconnect(self) -> None:
        """A reconnect must not discard a pending decision (AC-5).

        The old connection's eviction resolved the waiter as ``connection_lost``; the
        same client returning is not a departure. The decision arriving on the NEW
        connection must resolve the ORIGINAL waiter.
        """
        sid = f"reconnect-{uuid4()}"
        old_conn = _make_conn(sid)
        _active_connections[sid] = old_conn
        try:

            async def reconnect() -> None:
                await asyncio.sleep(0.05)
                # Fresh handshake evicts the old registration.
                _cancel_all_waiters(old_conn)
                new_conn = _make_conn(sid)
                _active_connections[sid] = new_conn
                await asyncio.sleep(0.05)
                # The replayed card is answered on the new socket.
                _resolve_constraint_decision(
                    new_conn, "req-rc", {"decision": "continue_10", "remember": False}
                )

            task = asyncio.create_task(reconnect())
            payload = await register_constraint_waiter(sid, "req-rc", 5.0, _meta())
            await task

            assert payload["resolution"] == "user_choice"
            assert payload["decision"] == "continue_10"
        finally:
            _active_connections.pop(sid, None)

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
    async def test_disconnect_does_not_resolve_constraint_waiter(self) -> None:
        """A disconnect leaves a constraint waiter pending; it rides its timeout (AC-5).

        Rewritten from ``test_disconnect_resolves_with_metadata_default``, which encoded
        the defect as the contract. A momentary drop is the normal condition for a mobile
        client — it must not be treated as a permanent one. ``_cancel_all_waiters`` now
        covers approval waiters only.
        """
        sid = f"disc-{uuid4()}"
        conn = _make_conn(sid)
        _active_connections[sid] = conn
        try:

            async def disconnector() -> None:
                await asyncio.sleep(0.05)
                _cancel_all_waiters(conn)
                _active_connections.pop(sid, None)

            task = asyncio.create_task(disconnector())
            payload = await register_constraint_waiter(sid, "req-x", 0.4, _meta())
            await task
            # Falls back on the TIMEOUT, not on the disconnect.
            assert payload["resolution"] == "timeout_default"
            assert payload["decision"] == "finish_now"
        finally:
            _active_connections.pop(sid, None)

    @pytest.mark.asyncio
    async def test_registry_cleaned_after_resolution(self) -> None:
        """The session waiter registry must not leak entries (codex finding 2)."""
        sid = f"leak-{uuid4()}"
        conn = _make_conn(sid)
        _active_connections[sid] = conn
        try:

            async def on_registered() -> None:
                _resolve_constraint_decision(conn, "req-lk", {"decision": "continue_10"})

            await register_constraint_waiter(
                sid, "req-lk", 5.0, _meta(), on_registered=on_registered
            )
            assert _session_constraint_waiters.get(sid) is None
        finally:
            _active_connections.pop(sid, None)

    @pytest.mark.asyncio
    async def test_duplicate_request_id_rejected(self) -> None:
        """A second waiter for the same request_id must not silently overwrite.

        Security-review finding: an overwrite orphans the first waiter — its timeout
        task resolves the survivor, leaving the original awaiting with no timeout.
        """
        sid = f"dupreg-{uuid4()}"
        _active_connections.pop(sid, None)

        async def register_and_hold() -> dict[str, object]:
            return await register_constraint_waiter(sid, "req-dup", 0.5, _meta())

        first = asyncio.create_task(register_and_hold())
        await asyncio.sleep(0.05)  # let the first register

        with pytest.raises(ValueError, match="already registered"):
            await register_constraint_waiter(sid, "req-dup", 0.5, _meta())

        payload = await first
        assert payload["resolution"] == "timeout_default"
        assert _session_constraint_waiters.get(sid) is None

    @pytest.mark.asyncio
    async def test_registry_cleaned_when_push_raises(self) -> None:
        """A failing push must not leak a registered waiter (codex finding 2).

        Session-scoped waiters lose the accidental safety net connection-scoped ones had
        (disconnect swept them), so the cleanup must cover ``on_registered`` itself.
        """
        sid = f"leak-raise-{uuid4()}"
        _active_connections.pop(sid, None)

        async def on_registered() -> None:
            raise RuntimeError("persist failed")

        with pytest.raises(RuntimeError, match="persist failed"):
            await register_constraint_waiter(
                sid, "req-lr", 5.0, _meta(), on_registered=on_registered
            )
        assert _session_constraint_waiters.get(sid) is None, "waiter leaked after failed push"


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
