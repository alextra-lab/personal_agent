"""Unit tests for FRE-998 — user identity on the graph write path.

These exercise the Cypher and parameters :meth:`MemoryService.create_session` and
:meth:`MemoryService.create_conversation` emit, against a fake Neo4j driver — no
live graph, so they run in ``make test``. The round-trip proof (write through the
real service, read the property back with Cypher) lives in
``test_graph_user_identity_integration.py``.

Per ADR-0107 and FRE-998's design decision: the ``user_id`` **property** is the
authoritative identity record on ``:Session`` and ``:Turn``; the
``(:Person)-[:PARTICIPATED_IN]->(:Turn)`` edge is a best-effort traversal
affordance that writes nothing when the ``:Person`` is missing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from personal_agent.memory.models import SessionNode, TurnNode
from personal_agent.memory.service import MemoryService


class _FakeResult:
    """Minimal stand-in for ``neo4j.AsyncResult`` supporting ``single()``."""

    def __init__(self, row: object | None) -> None:
        self._row = row

    async def single(self) -> object | None:
        return self._row


class _FakeSession:
    """Records every ``run(query, **params)`` and answers the identity-edge probe."""

    def __init__(self, recorder: list[tuple[str, dict[str, Any]]], *, person_exists: bool) -> None:
        self._recorder = recorder
        self._person_exists = person_exists

    async def run(self, query: str, **params: Any) -> _FakeResult:
        self._recorder.append((query, params))
        if "PARTICIPATED_IN" in query:
            return _FakeResult({"ok": 1} if self._person_exists else None)
        return _FakeResult(None)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def _fake_service(*, person_exists: bool = True) -> tuple[MemoryService, list[tuple[str, dict]]]:
    """Build a MemoryService wired to a fake driver.

    Args:
        person_exists: Whether the ``MATCH (p:Person {user_id})`` probe finds a node.

    Returns:
        The service and the list its fake session appends ``(query, params)`` to.
    """
    service = MemoryService()  # fre-375-allow: unit test with a fake driver, no real connection
    recorder: list[tuple[str, dict[str, Any]]] = []
    driver = MagicMock()
    driver.session = lambda: _FakeSession(recorder, person_exists=person_exists)
    service.driver = driver
    service.connected = True
    return service, recorder


def _find(recorder: list[tuple[str, dict[str, Any]]], needle: str) -> tuple[str, dict[str, Any]]:
    """Return the single recorded statement containing ``needle``."""
    matches = [entry for entry in recorder if needle in entry[0]]
    assert matches, f"no recorded Cypher containing {needle!r}"
    return matches[0]


def _session_node(session_id: str) -> SessionNode:
    now = datetime.now(timezone.utc)
    return SessionNode(session_id=session_id, started_at=now, ended_at=now, turn_count=1)


def _turn_node(session_id: str) -> TurnNode:
    return TurnNode(
        turn_id=str(uuid.uuid4()),
        trace_id=str(uuid.uuid4()),
        session_id=session_id,
        timestamp=datetime.now(timezone.utc),
        user_message="hello",
        assistant_response="hi",
        key_entities=[],
    )


class TestSessionIdentity:
    """``create_session`` writes the authoritative identity property."""

    @pytest.mark.asyncio
    async def test_writes_user_id_property(self) -> None:
        service, recorder = _fake_service()
        user_id = uuid.uuid4()

        ok = await service.create_session(_session_node("s-1"), user_id=user_id)

        assert ok is True
        query, params = _find(recorder, "MERGE (s:Session")
        assert "s.user_id" in query
        assert params["user_id"] == str(user_id)

    @pytest.mark.asyncio
    async def test_none_user_id_does_not_clobber(self) -> None:
        """A null identity must never erase a previously-attributed session.

        The Session node is re-MERGEd on every consolidation that brings new
        turns, so a bare ``SET s.user_id = $user_id`` would wipe the existing
        backfilled attribution the first time an old session was touched.
        """
        service, recorder = _fake_service()

        ok = await service.create_session(_session_node("s-1"), user_id=None)

        assert ok is True
        query, params = _find(recorder, "MERGE (s:Session")
        assert params["user_id"] is None
        assert "COALESCE($user_id, s.user_id)" in query

    @pytest.mark.asyncio
    async def test_user_id_is_optional(self) -> None:
        """Existing callers that pass no identity keep working."""
        service, recorder = _fake_service()

        assert await service.create_session(_session_node("s-1")) is True
        _, params = _find(recorder, "MERGE (s:Session")
        assert params["user_id"] is None


class TestTurnIdentity:
    """``create_conversation`` writes identity as a property, not only as an edge."""

    @pytest.mark.asyncio
    async def test_writes_user_id_property(self) -> None:
        service, recorder = _fake_service()
        user_id = uuid.uuid4()

        ok = await service.create_conversation(_turn_node("s-1"), user_id=user_id)

        assert ok is True
        query, params = _find(recorder, "MERGE (t:Turn")
        assert "t.user_id" in query
        assert params["user_id_str"] == str(user_id)

    @pytest.mark.asyncio
    async def test_none_user_id_does_not_clobber(self) -> None:
        service, recorder = _fake_service()

        ok = await service.create_conversation(_turn_node("s-1"), user_id=None)

        assert ok is True
        query, params = _find(recorder, "MERGE (t:Turn")
        assert params["user_id_str"] is None
        assert "COALESCE($user_id_str, t.user_id)" in query

    @pytest.mark.asyncio
    async def test_identity_survives_a_missing_person_node(self) -> None:
        """The property is written even when the edge cannot be.

        This is the failure that silently cost 1828 historical turns their
        identity: ``MATCH (p:Person {user_id})`` matched nothing, so the MERGE
        wrote no edge and nothing recorded who the turn belonged to.
        """
        service, recorder = _fake_service(person_exists=False)
        user_id = uuid.uuid4()

        ok = await service.create_conversation(_turn_node("s-1"), user_id=user_id)

        assert ok is True
        _, params = _find(recorder, "MERGE (t:Turn")
        assert params["user_id_str"] == str(user_id)


class TestParticipatedInEdgeHonesty:
    """The edge-write log must report what actually happened."""

    @pytest.mark.asyncio
    async def test_missing_person_logs_a_warning_not_success(self) -> None:
        service, _ = _fake_service(person_exists=False)

        with patch("personal_agent.memory.service.log") as mock_log:
            await service.create_conversation(_turn_node("s-1"), user_id=uuid.uuid4())

        warned = {call.args[0] for call in mock_log.warning.call_args_list}
        infoed = {call.args[0] for call in mock_log.info.call_args_list}
        assert "participated_in_person_missing" in warned
        assert "participated_in_edge_written" not in infoed

    @pytest.mark.asyncio
    async def test_present_person_logs_success(self) -> None:
        service, _ = _fake_service(person_exists=True)

        with patch("personal_agent.memory.service.log") as mock_log:
            await service.create_conversation(_turn_node("s-1"), user_id=uuid.uuid4())

        warned = {call.args[0] for call in mock_log.warning.call_args_list}
        infoed = {call.args[0] for call in mock_log.info.call_args_list}
        assert "participated_in_edge_written" in infoed
        assert "participated_in_person_missing" not in warned
