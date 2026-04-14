"""In-memory sliding window rate limiter for the Seshat API Gateway.

Each token has an independent sliding window.  When the window expires the
counter resets.  This implementation is intentionally simple (single-process,
no Redis) — a Redis-backed implementation can be swapped in later without
changing the interface.

Usage::

    limiter = RateLimiter()
    limiter.check(token_info)  # raises HTTPException(429) if over limit
"""

import time
from dataclasses import dataclass

import structlog
from fastapi import HTTPException

from personal_agent.gateway.auth import TokenInfo

log = structlog.get_logger(__name__)

_WINDOW_SECONDS = 3600  # 1 hour


@dataclass
class RateLimitState:
    """Mutable sliding-window state for a single token.

    Attributes:
        token_name: Human-readable identifier for log messages.
        window_start: Unix timestamp when the current window began.
        request_count: Number of requests seen in the current window.
        max_per_hour: Limit declared in token configuration.
    """

    token_name: str
    window_start: float
    request_count: int
    max_per_hour: int


class RateLimiter:
    """Per-token sliding-window rate limiter.

    The limiter maintains one :class:`RateLimitState` per ``TokenInfo.name``.
    When the window (1 hour) expires the counter resets automatically.

    This class is **not thread-safe** for concurrent asyncio tasks that share
    the same event loop without locking.  For the current single-process local
    deployment this is acceptable; add an ``asyncio.Lock`` when moving to a
    multi-worker deployment.

    Example::

        limiter = RateLimiter()

        @router.get("/search")
        async def search(token: TokenInfo = Depends(...)):
            limiter.check(token)
            ...
    """

    def __init__(self) -> None:
        """Initialise with empty window registry."""
        self._windows: dict[str, RateLimitState] = {}

    def check(self, token: TokenInfo) -> None:
        """Assert the token has not exceeded its hourly rate limit.

        Resets the window if more than one hour has elapsed since
        ``window_start``.

        Args:
            token: Validated token whose ``rate_limit`` will be enforced.

        Raises:
            HTTPException(429): When the request count for the current window
                exceeds ``token.rate_limit``.
        """
        now = time.monotonic()
        state = self._windows.get(token.name)

        if state is None or (now - state.window_start) >= _WINDOW_SECONDS:
            # Start a fresh window
            self._windows[token.name] = RateLimitState(
                token_name=token.name,
                window_start=now,
                request_count=1,
                max_per_hour=token.rate_limit,
            )
            return

        state.request_count += 1
        if state.request_count > state.max_per_hour:
            log.warning(
                "gateway_rate_limit_exceeded",
                token_name=token.name,
                request_count=state.request_count,
                max_per_hour=state.max_per_hour,
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limited",
                    "message": (
                        f"Rate limit exceeded: {state.max_per_hour} requests/hour "
                        f"for token '{token.name}'"
                    ),
                    "status": 429,
                },
            )

    def reset(self, token_name: str) -> None:
        """Remove the window state for a token (used in tests).

        Args:
            token_name: Name of the token whose state should be cleared.
        """
        self._windows.pop(token_name, None)


# Module-level singleton
_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Return the module-level RateLimiter singleton.

    Returns:
        Shared :class:`RateLimiter` instance.
    """
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
