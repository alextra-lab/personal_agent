"""Tests for the in-memory sliding-window rate limiter (FRE-206)."""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from personal_agent.gateway.auth import TokenInfo
from personal_agent.gateway.rate_limiting import RateLimiter, _WINDOW_SECONDS


def _make_token(name: str = "test-token", rate_limit: int = 3) -> TokenInfo:
    return TokenInfo(
        name=name,
        scopes=frozenset(["knowledge:read"]),
        rate_limit=rate_limit,
    )


def test_first_request_is_allowed() -> None:
    """First request within a window should never be blocked."""
    limiter = RateLimiter()
    token = _make_token(rate_limit=5)
    limiter.check(token)  # should not raise


def test_requests_within_limit_are_allowed() -> None:
    """All requests up to the rate limit must succeed."""
    limiter = RateLimiter()
    token = _make_token(rate_limit=5)
    for _ in range(5):
        limiter.check(token)  # none should raise


def test_exceeding_limit_raises_429() -> None:
    """The (limit + 1)-th request in a window should raise HTTP 429."""
    limiter = RateLimiter()
    token = _make_token(rate_limit=3)

    for _ in range(3):
        limiter.check(token)

    with pytest.raises(HTTPException) as exc_info:
        limiter.check(token)

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["error"] == "rate_limited"  # type: ignore[index]


def test_window_reset_clears_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """After one window expires (1 hour), the counter should reset."""
    limiter = RateLimiter()
    token = _make_token(rate_limit=2)

    limiter.check(token)
    limiter.check(token)

    # Exhaust the limit
    with pytest.raises(HTTPException):
        limiter.check(token)

    # Simulate time advancing past the window
    state = limiter._windows[token.name]
    limiter._windows[token.name].window_start = state.window_start - (_WINDOW_SECONDS + 1)

    # After window reset, new requests should succeed
    limiter.check(token)
    assert limiter._windows[token.name].request_count == 1


def test_reset_clears_state() -> None:
    """reset() should remove the token's window state entirely."""
    limiter = RateLimiter()
    token = _make_token(rate_limit=1)
    limiter.check(token)

    assert token.name in limiter._windows
    limiter.reset(token.name)
    assert token.name not in limiter._windows


def test_different_tokens_are_independent() -> None:
    """Rate limits for different tokens must not interfere with each other."""
    limiter = RateLimiter()
    token_a = _make_token(name="a", rate_limit=1)
    token_b = _make_token(name="b", rate_limit=5)

    # Exhaust token_a
    limiter.check(token_a)
    with pytest.raises(HTTPException):
        limiter.check(token_a)

    # token_b should be unaffected
    limiter.check(token_b)  # should not raise
