"""FRE-860 (ADR-0098 D4/D6) — prune_expired_sessions job wrapper (pure unit).

Mirrors ``test_uploads_router.py::test_expire_pending_uploads_deletes_old_rows``:
a stub async session intercepts the SQL execute/commit calls, so this test
never touches real Postgres. ``run_session_retention_loop`` itself (the
``while True: sleep; call; except: log`` wrapper) is intentionally left
untested here, matching this codebase's existing convention:
``cost_gate/reaper.py``'s ``run_reaper`` has no dedicated test either — only
the underlying sweep (``reap_stale``) gets real-DB coverage. The loop wrapper
has no interesting logic of its own.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from personal_agent.service.session_retention import prune_expired_sessions


class _StubSession:
    """Minimal async SQLAlchemy session that can be pre-loaded with a result."""

    def __init__(self, rowcount: int) -> None:
        self._rowcount = rowcount
        self.execute = AsyncMock(return_value=SimpleNamespace(rowcount=rowcount))
        self.commit = AsyncMock()

    async def __aenter__(self) -> "_StubSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass


@pytest.mark.asyncio
async def test_prune_expired_sessions_returns_rowcount() -> None:
    """prune_expired_sessions delegates to the repository and returns its rowcount."""
    session = _StubSession(rowcount=4)

    def _factory() -> Any:
        return session

    n = await prune_expired_sessions(_factory, retention_days=180)

    assert n == 4
    session.execute.assert_called_once()
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_prune_expired_sessions_defaults_retention_days_from_settings() -> None:
    """When retention_days is omitted, the settings default is used (no crash)."""
    session = _StubSession(rowcount=0)

    def _factory() -> Any:
        return session

    n = await prune_expired_sessions(_factory)

    assert n == 0
    session.execute.assert_called_once()
