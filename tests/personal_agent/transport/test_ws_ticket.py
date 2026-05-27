"""Tests for WS ticket minting and consumption (ADR-0075 / FRE-388)."""

from __future__ import annotations

import time
from unittest.mock import patch
from uuid import uuid4

from personal_agent.service.auth import RequestUser
from personal_agent.service.ws_ticket import (
    _pending_tickets,
    consume_ws_ticket,
    mint_ws_ticket,
)


def _make_user() -> RequestUser:
    return RequestUser(user_id=uuid4(), email="test@example.com", display_name="Test")


class TestMintWSTicket:
    """Tests for mint_ws_ticket."""

    def setup_method(self) -> None:
        _pending_tickets.clear()

    def test_returns_string(self) -> None:
        user = _make_user()
        ticket = mint_ws_ticket(user, uuid4())
        assert isinstance(ticket, str)
        assert len(ticket) > 20

    def test_tickets_are_unique(self) -> None:
        user = _make_user()
        sid = uuid4()
        t1 = mint_ws_ticket(user, sid)
        t2 = mint_ws_ticket(user, sid)
        assert t1 != t2


class TestConsumeWSTicket:
    """Tests for consume_ws_ticket."""

    def setup_method(self) -> None:
        _pending_tickets.clear()

    def test_valid_consumption(self) -> None:
        user = _make_user()
        sid = uuid4()
        ticket = mint_ws_ticket(user, sid)
        result = consume_ws_ticket(ticket, sid)
        assert result is not None
        assert result.user_id == user.user_id
        assert result.email == user.email

    def test_single_use(self) -> None:
        user = _make_user()
        sid = uuid4()
        ticket = mint_ws_ticket(user, sid)
        first = consume_ws_ticket(ticket, sid)
        second = consume_ws_ticket(ticket, sid)
        assert first is not None
        assert second is None

    def test_unknown_ticket(self) -> None:
        result = consume_ws_ticket("nonexistent", uuid4())
        assert result is None

    def test_expired_ticket(self) -> None:
        user = _make_user()
        sid = uuid4()
        with patch("personal_agent.service.ws_ticket.settings") as mock_settings:
            mock_settings.ws_ticket_ttl_seconds = 0
            ticket = mint_ws_ticket(user, sid)

        # The ticket was minted with TTL=0, so it expires immediately
        time.sleep(0.01)
        result = consume_ws_ticket(ticket, sid)
        assert result is None

    def test_session_mismatch(self) -> None:
        user = _make_user()
        sid_a = uuid4()
        sid_b = uuid4()
        ticket = mint_ws_ticket(user, sid_a)
        result = consume_ws_ticket(ticket, sid_b)
        assert result is None
