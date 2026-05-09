"""Tests for MemoryService owner identity bootstrap (FRE-213 / ADR-0052)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from personal_agent.memory.service import MemoryService


def _make_service(connected: bool = True) -> MemoryService:
    svc = MemoryService()
    svc.connected = connected
    if connected:
        svc.driver = MagicMock()
    return svc


def _make_session_mock(run_records: list[dict[str, Any]] | None = None) -> AsyncMock:
    """Return an async context-manager mock for driver.session()."""
    session = AsyncMock()
    session.run = AsyncMock()
    if run_records is not None:
        result = AsyncMock()
        result.single = AsyncMock(return_value=run_records[0] if run_records else None)
        session.run.return_value = result
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestBootstrapOwnerIdentity:
    """Tests for MemoryService.bootstrap_owner_identity()."""

    @pytest.mark.asyncio
    async def test_bootstrap_noop_when_not_connected(self) -> None:
        svc = _make_service(connected=False)
        result = await svc.bootstrap_owner_identity(
            agent_id="test", user_id=uuid4(), email="a@b.com", name="Alex"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_bootstrap_noop_when_name_empty(self) -> None:
        svc = _make_service(connected=True)
        result = await svc.bootstrap_owner_identity(
            agent_id="test", user_id=uuid4(), email="a@b.com", name=""
        )
        assert result is False
        # driver.session() should never be called
        svc.driver.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_bootstrap_idempotent(self) -> None:
        """Running bootstrap twice should not raise and should succeed both times."""
        svc = _make_service(connected=True)
        uid = uuid4()

        session_mock = _make_session_mock()
        svc.driver.session = MagicMock(return_value=session_mock)

        r1 = await svc.bootstrap_owner_identity("seshat-local", uid, "alex@x.com", "Alex")
        r2 = await svc.bootstrap_owner_identity("seshat-local", uid, "alex@x.com", "Alex")

        assert r1 is True
        assert r2 is True
        # run() was called twice (once per invocation, once for constraint, once for merge)
        assert session_mock.__aenter__.return_value.run.call_count >= 2

    @pytest.mark.asyncio
    async def test_bootstrap_does_not_adopt_by_name(self) -> None:
        """Bootstrap anchors on user_id — the Cypher MERGE (person:Person {user_id: …})
        never touches a same-named node that lacks user_id."""
        svc = _make_service(connected=True)
        uid = uuid4()
        session_mock = _make_session_mock()
        svc.driver.session = MagicMock(return_value=session_mock)

        await svc.bootstrap_owner_identity("seshat-local", uid, "alex@x.com", "Alex")

        # Inspect the MERGE Cypher call — it must use user_id, not name, as the anchor
        run_mock: AsyncMock = session_mock.__aenter__.return_value.run
        merge_calls = [
            call
            for call in run_mock.call_args_list
            if "MERGE" in str(call.args[0]) and "user_id" in str(call.args[0])
        ]
        assert len(merge_calls) >= 1, "Expected a MERGE anchored on user_id"
        # None of the MERGE calls should use toLower or name as the anchor key
        name_anchor_calls = [
            call
            for call in run_mock.call_args_list
            if "toLower" in str(call.args[0]) or "name: $name" in str(call.args[0]).replace(" ", "").lower()
        ]
        assert len(name_anchor_calls) == 0, "Bootstrap must not anchor by name"

    @pytest.mark.asyncio
    async def test_bootstrap_returns_false_on_neo4j_error(self) -> None:
        svc = _make_service(connected=True)
        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(side_effect=RuntimeError("Neo4j boom"))
        session_mock.__aexit__ = AsyncMock(return_value=None)
        svc.driver.session = MagicMock(return_value=session_mock)

        result = await svc.bootstrap_owner_identity(
            "seshat-local", uuid4(), "a@b.com", "Alex"
        )
        assert result is False


class TestGetOrProvisionUserPerson:
    """Tests for MemoryService.get_or_provision_user_person()."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_not_connected(self) -> None:
        svc = _make_service(connected=False)
        result = await svc.get_or_provision_user_person(uuid4(), "a@b.com", None)
        assert result == {}

    @pytest.mark.asyncio
    async def test_first_provision_creates_person(self) -> None:
        svc = _make_service(connected=True)
        uid = uuid4()

        fake_facts = {"name": "Alex", "location": "Paris"}
        record = {"facts": fake_facts}
        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=None)
        result_mock = AsyncMock()
        result_mock.single = AsyncMock(return_value=record)
        session_mock.run = AsyncMock(return_value=result_mock)
        svc.driver.session = MagicMock(return_value=session_mock)

        result = await svc.get_or_provision_user_person(uid, "alex@x.com", "Alex")
        assert result == {"name": "Alex", "location": "Paris"}

    @pytest.mark.asyncio
    async def test_uses_email_localpart_when_display_name_null(self) -> None:
        svc = _make_service(connected=True)
        uid = uuid4()

        # Return the name that was passed in (simulates ON CREATE using email_localpart)
        async def _run(query: str, **kwargs: Any) -> AsyncMock:
            rm = AsyncMock()
            rm.single = AsyncMock(return_value={"facts": {"name": kwargs.get("name", "")}})
            return rm

        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=None)
        session_mock.run = AsyncMock(side_effect=_run)
        svc.driver.session = MagicMock(return_value=session_mock)

        result = await svc.get_or_provision_user_person(uid, "lextra@gmail.com", None)
        # Should derive name from email local-part
        assert result.get("name") == "lextra"

    @pytest.mark.asyncio
    async def test_provision_returns_empty_on_none_single(self) -> None:
        svc = _make_service(connected=True)
        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=None)
        result_mock = AsyncMock()
        result_mock.single = AsyncMock(return_value=None)
        session_mock.run = AsyncMock(return_value=result_mock)
        svc.driver.session = MagicMock(return_value=session_mock)

        result = await svc.get_or_provision_user_person(uuid4(), "a@b.com", None)
        assert result == {}

    @pytest.mark.asyncio
    async def test_provision_omits_none_values_from_result(self) -> None:
        svc = _make_service(connected=True)
        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=None)
        result_mock = AsyncMock()
        result_mock.single = AsyncMock(
            return_value={"facts": {"name": "Alex", "location": None, "pronouns": None}}
        )
        session_mock.run = AsyncMock(return_value=result_mock)
        svc.driver.session = MagicMock(return_value=session_mock)

        result = await svc.get_or_provision_user_person(uuid4(), "a@b.com", "Alex")
        assert "location" not in result
        assert "pronouns" not in result
        assert result["name"] == "Alex"
