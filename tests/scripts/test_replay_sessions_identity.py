"""Unit tests for FRE-998 — the replay script must not fabricate identity or orphan turns.

``scripts/replay_sessions_to_neo4j.py`` is the proven cause of the two graph
defects FRE-998 investigates (measured 2026-07-28 against the live graph):

1. It called ``consolidator._process_capture`` directly, bypassing ``consolidate()``
   and therefore ``_consolidate_sessions`` — so every Turn it wrote was born with
   no Session node, and ``link_session_turns`` (which MATCHes one) could never
   link it. 1828 turns across 1039 sessions.
2. It read identity from ``metadata['user_id']``/``['owner_id']`` and fell back to
   ``uuid4()``. Zero of those 1034 sessions carried either key, while all 1034 had
   the authoritative ``sessions.user_id`` column populated — so it stamped a fresh
   random UUID per session, which then matched no ``:Person`` and silently wrote
   no ``PARTICIPATED_IN`` edge.

These tests pin both fixes so a rerun cannot recreate the damage.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.replay_sessions_to_neo4j import _replay_session, _resolve_session_user_id


def _session_row(**overrides: Any) -> dict[str, Any]:
    """A Postgres session row as the script's SELECT returns it."""
    row: dict[str, Any] = {
        "session_id": uuid.uuid4(),
        "created_at": datetime.now(timezone.utc),
        "user_id": uuid.uuid4(),
        "metadata": {},
        "messages": [
            {"role": "user", "content": "hello", "timestamp": "2026-05-01T10:00:00"},
            {"role": "assistant", "content": "hi", "timestamp": "2026-05-01T10:00:01"},
        ],
    }
    row.update(overrides)
    return row


def _consolidator() -> MagicMock:
    con = MagicMock()
    con._process_capture = AsyncMock(return_value={"turns_created": 1})
    con._consolidate_sessions = AsyncMock(return_value=1)
    return con


class TestIdentityResolution:
    def test_reads_the_authoritative_column(self) -> None:
        user_id = uuid.uuid4()
        row = _session_row(user_id=user_id, metadata={})

        assert _resolve_session_user_id(row) == user_id

    def test_column_wins_over_stale_metadata(self) -> None:
        """Metadata is not the identity source; the column is."""
        user_id = uuid.uuid4()
        row = _session_row(user_id=user_id, metadata={"user_id": str(uuid.uuid4())})

        assert _resolve_session_user_id(row) == user_id

    def test_missing_identity_resolves_to_none_never_a_random_uuid(self) -> None:
        row = _session_row(user_id=None)

        assert _resolve_session_user_id(row) is None

    def test_unparseable_identity_resolves_to_none(self) -> None:
        row = _session_row(user_id="not-a-uuid")

        assert _resolve_session_user_id(row) is None


class TestUnattributableSessionIsSkipped:
    @pytest.mark.asyncio
    async def test_no_captures_are_written(self) -> None:
        """Refusing to write beats inventing an identity that resolves to nobody."""
        con = _consolidator()

        counts = await _replay_session(_session_row(user_id=None), con, 0, False)

        con._process_capture.assert_not_awaited()
        con._consolidate_sessions.assert_not_awaited()
        assert counts["turns_processed"] == 0
        assert counts["errors"] == 1


class TestReplayCreatesSessionNodes:
    @pytest.mark.asyncio
    async def test_capture_carries_the_column_identity(self) -> None:
        user_id = uuid.uuid4()
        con = _consolidator()

        await _replay_session(_session_row(user_id=user_id), con, 0, False)

        capture = con._process_capture.await_args.args[0]
        assert capture.user_id == user_id

    @pytest.mark.asyncio
    async def test_session_consolidation_runs(self) -> None:
        """The bypass that orphaned 1828 turns is closed."""
        row = _session_row()
        con = _consolidator()

        await _replay_session(row, con, 0, False)

        con._consolidate_sessions.assert_awaited_once()
        captures, session_ids = con._consolidate_sessions.await_args.args[:2]
        assert session_ids == {str(row["session_id"])}
        assert [c.session_id for c in captures] == [str(row["session_id"])]

    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self) -> None:
        con = _consolidator()

        await _replay_session(_session_row(), con, 0, True)

        con._process_capture.assert_not_awaited()
        con._consolidate_sessions.assert_not_awaited()
