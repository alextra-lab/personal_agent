"""Tests for get_owner_stanza() in orchestrator/prompts.py (FRE-213 / ADR-0052)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from personal_agent.orchestrator.prompts import get_owner_stanza


def _make_memory_service(facts: dict[str, Any] | None = None) -> MagicMock:
    """Return a mock MemoryService whose get_or_provision_user_person returns facts."""
    svc = MagicMock()
    svc.get_or_provision_user_person = AsyncMock(return_value=facts or {})
    return svc


class TestGetOwnerStanza:
    @pytest.mark.asyncio
    async def test_empty_when_memory_service_none(self) -> None:
        result = await get_owner_stanza(None, uuid4(), "a@b.com", None)
        assert result == ""

    @pytest.mark.asyncio
    async def test_empty_when_user_id_none(self) -> None:
        svc = _make_memory_service({"name": "Alex"})
        result = await get_owner_stanza(svc, None, "a@b.com", None)
        assert result == ""

    @pytest.mark.asyncio
    async def test_empty_when_email_none(self) -> None:
        svc = _make_memory_service({"name": "Alex"})
        result = await get_owner_stanza(svc, uuid4(), None, None)
        assert result == ""

    @pytest.mark.asyncio
    async def test_empty_when_facts_empty(self) -> None:
        svc = _make_memory_service({})
        result = await get_owner_stanza(svc, uuid4(), "a@b.com", None)
        assert result == ""

    @pytest.mark.asyncio
    async def test_empty_when_name_missing_from_facts(self) -> None:
        svc = _make_memory_service({"location": "Paris"})
        result = await get_owner_stanza(svc, uuid4(), "a@b.com", None)
        assert result == ""

    @pytest.mark.asyncio
    async def test_renders_name_only(self) -> None:
        svc = _make_memory_service({"name": "Alex"})
        result = await get_owner_stanza(svc, uuid4(), "a@b.com", None)
        assert "You are assisting Alex" in result
        assert "Do not tool-call" in result

    @pytest.mark.asyncio
    async def test_renders_full_properties(self) -> None:
        facts = {
            "name": "Alex",
            "location": "Paris",
            "pronouns": "he/him",
            "role": "Engineer",
            "languages": "English, French",
        }
        svc = _make_memory_service(facts)
        result = await get_owner_stanza(svc, uuid4(), "a@b.com", None)
        assert "Alex" in result
        assert "Paris" in result
        assert "he/him" in result
        assert "Engineer" in result
        assert "English, French" in result

    @pytest.mark.asyncio
    async def test_omits_missing_properties(self) -> None:
        svc = _make_memory_service({"name": "Alex", "location": "Berlin"})
        result = await get_owner_stanza(svc, uuid4(), "a@b.com", None)
        assert "Berlin" in result
        assert "pronouns" not in result.lower()
        assert "role" not in result.lower()

    @pytest.mark.asyncio
    async def test_truncates_long_field_to_120_chars(self) -> None:
        long_value = "x" * 200
        svc = _make_memory_service({"name": "Alex", "location": long_value})
        result = await get_owner_stanza(svc, uuid4(), "a@b.com", None)
        # The location value in the stanza must be at most 120 chars
        for line in result.split("\n"):
            if "Location" in line:
                assert len(line.split(": ", 1)[1]) <= 120

    @pytest.mark.asyncio
    async def test_field_whitelist_enforced(self) -> None:
        """Unknown properties on the node must not appear in the stanza."""
        svc = _make_memory_service({"name": "Alex", "secret_project": "Classified"})
        result = await get_owner_stanza(svc, uuid4(), "a@b.com", None)
        assert "Classified" not in result
        assert "secret_project" not in result

    @pytest.mark.asyncio
    async def test_stanza_for_non_owner_user(self) -> None:
        """Non-owner users (no is_owner) still get a stanza — only name is needed."""
        svc = _make_memory_service({"name": "Susan"})
        result = await get_owner_stanza(svc, uuid4(), "susan@x.com", "Susan")
        assert "You are assisting Susan" in result

    @pytest.mark.asyncio
    async def test_distinguishes_owner_alex_from_extracted_alex(self) -> None:
        """Stanza query is by user_id, not by name — the mock simulates the
        correct Person being returned for the given user_id."""
        owner_uid = uuid4()
        other_uid = uuid4()

        # owner's facts
        owner_facts = {"name": "Alex", "location": "Paris", "is_owner": True}
        # extracted third-party "Alex" facts (different Person node — no user_id)
        other_facts = {"name": "Alex"}  # same name, different node

        async def _provision(user_id, email, display_name):  # noqa: ANN001
            if user_id == owner_uid:
                return owner_facts
            return other_facts

        svc = MagicMock()
        svc.get_or_provision_user_person = AsyncMock(side_effect=_provision)

        owner_stanza = await get_owner_stanza(svc, owner_uid, "alex@x.com", "Alex")
        other_stanza = await get_owner_stanza(svc, other_uid, "other@x.com", None)

        # Both stanzas say "Alex" but they come from different :Person nodes
        assert "Alex" in owner_stanza
        assert "Alex" in other_stanza
        # The key invariant: they are computed independently via user_id, not name
        assert svc.get_or_provision_user_person.call_count == 2
        first_call_uid = svc.get_or_provision_user_person.call_args_list[0].kwargs.get("user_id")
        second_call_uid = svc.get_or_provision_user_person.call_args_list[1].kwargs.get("user_id")
        assert first_call_uid == owner_uid
        assert second_call_uid == other_uid
