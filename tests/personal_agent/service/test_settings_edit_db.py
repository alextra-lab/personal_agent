"""Edit settings through the API on real rows of the test Postgres (FRE-1722, FRE-1723).

Proves AC-1, AC-2 and AC-3 of both tickets against the tables that the migration creates. The
FRE-1723 tests also reserve against a real ``CostGate``.

Marker: ``integration`` — excluded from ``make test`` because it needs a live DB.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from personal_agent.config import settings as app_settings
from personal_agent.config.model_loader import (
    load_model_config,
    resolve_role_model_key,
    resolve_role_target,
)
from personal_agent.config.seshat_effort import effort_spec, file_seshat_effort, preset_values
from personal_agent.config.setting_overrides import current_overrides, replace_overrides
from personal_agent.cost_gate import (
    BudgetConfig,
    BudgetDenied,
    CapEntry,
    CostGate,
    DenialReason,
    set_default_gate,
    sync_budget_policies_to_db,
)
from personal_agent.service import settings_edit
from personal_agent.service.app import app
from personal_agent.service.auth import RequestUser, get_or_create_user_by_email, get_request_user
from personal_agent.service.database import get_db_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_URL = "/api/v1/settings/overrides"
_PREF_URL = "/api/v1/preferences/constraint"
_OWNER = "fre1722-owner@example.com"
_ROLE = "compressor"
_PARAMETER = f"role.{_ROLE}"
_SAVED = "saved_choice.tool_iteration_limit"


@pytest_asyncio.fixture
async def sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(app_settings.database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db_session] = _db
    monkeypatch.setattr(
        settings_edit,
        "settings",
        SimpleNamespace(gateway_auth_enabled=True, agent_owner_email=_OWNER),
    )
    monkeypatch.setattr(
        settings_edit, "build_provider_availability", lambda *_args: lambda _provider: True
    )
    replace_overrides({})
    async with maker() as db:
        await db.execute(text("DELETE FROM settings_overrides WHERE parameter = :p"), {"p": _PARAMETER})
        await db.commit()
    yield maker
    async with maker() as db:
        await db.execute(text("DELETE FROM settings_overrides WHERE parameter = :p"), {"p": _PARAMETER})
        await db.execute(text("DELETE FROM users WHERE email LIKE 'fre1722-%'"))
        await db.commit()
    replace_overrides({})
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_request_user, None)
    await engine.dispose()


async def _user(maker: async_sessionmaker[AsyncSession], email: str) -> UUID:
    async with maker() as db:
        return await get_or_create_user_by_email(db, email)


def _client(user_id: UUID, email: str = _OWNER) -> httpx.AsyncClient:
    async def _override_user() -> RequestUser:
        return RequestUser(user_id=user_id, email=email)

    app.dependency_overrides[get_request_user] = _override_user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _audit(maker: async_sessionmaker[AsyncSession], user_id: UUID) -> list[tuple[object, ...]]:
    async with maker() as db:
        result = await db.execute(
            text(
                "SELECT parameter, action, old_value, new_value, changed_by, changed_at "
                "FROM settings_edit_audit WHERE changed_by = :u ORDER BY audit_id"
            ),
            {"u": user_id},
        )
        return [tuple(row) for row in result.all()]


async def _stored(maker: async_sessionmaker[AsyncSession]) -> object:
    async with maker() as db:
        result = await db.execute(
            text("SELECT value FROM settings_overrides WHERE parameter = :p"), {"p": _PARAMETER}
        )
        return result.scalar_one_or_none()


def _other_llm() -> str:
    config = load_model_config()
    bound = config.roles[_ROLE].deployment
    return next(
        key for key, model in config.models.items() if model.kind.value == "llm" and key != bound
    )


async def test_ac1_ac3_role_binding_edit_and_clear_on_real_rows(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _user(sessions, _OWNER)
    file_key = resolve_role_target(_ROLE)[0]
    target = _other_llm()

    async with _client(user_id) as client:
        resp = await client.put(_URL, json={"parameter": _PARAMETER, "value": target})
        assert resp.status_code == 200
        assert await _stored(sessions) == target
        assert resolve_role_target(_ROLE)[0] == target
        assert resolve_role_model_key(_ROLE) == target

        resp = await client.delete(_URL, params={"parameter": _PARAMETER})
        assert resp.json()["cleared"] is True
        assert await _stored(sessions) is None
        assert resolve_role_target(_ROLE)[0] == file_key
        assert current_overrides().get(_PARAMETER) is None

        resp = await client.delete(_URL, params={"parameter": _PARAMETER})
        assert resp.json()["cleared"] is False

    rows = await _audit(sessions, user_id)
    assert [row[:4] for row in rows] == [
        (_PARAMETER, "set", file_key, target),
        (_PARAMETER, "clear", target, file_key),
    ]
    assert all(row[4] == user_id and row[5] is not None for row in rows)


async def test_ac2_invalid_and_non_owner_write_no_row(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    owner_id = await _user(sessions, _OWNER)
    other_id = await _user(sessions, "fre1722-other@example.com")

    async with _client(owner_id) as client:
        resp = await client.put(_URL, json={"parameter": _PARAMETER, "value": "no_such"})
        assert resp.status_code == 422
    async with _client(other_id, email="fre1722-other@example.com") as client:
        resp = await client.put(_URL, json={"parameter": _PARAMETER, "value": _other_llm()})
        assert resp.status_code == 403

    assert await _stored(sessions) is None
    assert await _audit(sessions, owner_id) == []
    assert await _audit(sessions, other_id) == []


async def test_ac3_a_saved_choice_edit_and_clear_write_one_audit_row_each(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _user(sessions, _OWNER)
    constraint = _SAVED.removeprefix("saved_choice.")

    async with _client(user_id) as client:
        resp = await client.put(
            _PREF_URL, json={"constraint_name": constraint, "preferred_action": "continue_10"}
        )
        assert resp.status_code == 200
        resp = await client.put(
            _PREF_URL, json={"constraint_name": constraint, "preferred_action": "finish_now"}
        )
        assert resp.status_code == 200
        resp = await client.delete(_PREF_URL, params={"constraint_name": constraint})
        assert resp.json()["cleared"] is True
        resp = await client.delete(_PREF_URL, params={"constraint_name": constraint})
        assert resp.json()["cleared"] is False

    assert [row[:4] for row in await _audit(sessions, user_id)] == [
        (_SAVED, "set", None, "continue_10"),
        (_SAVED, "set", "continue_10", "finish_now"),
        (_SAVED, "clear", "finish_now", None),
    ]


async def test_startup_load_fills_the_process_copy_from_the_table(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _other_llm()
    async with sessions() as db:
        await db.execute(
            text(
                "INSERT INTO settings_overrides (parameter, value, updated_by) "
                "VALUES (:p, CAST(:v AS JSONB), :u)"
            ),
            {"p": _PARAMETER, "v": f'"{target}"', "u": uuid4()},
        )
        await db.commit()

    monkeypatch.setattr(settings_edit, "AsyncSessionLocal", sessions)
    await settings_edit.load_overrides_at_startup()

    assert current_overrides()[_PARAMETER] == target
    assert resolve_role_model_key(_ROLE) == target


# ---------------------------------------------------------------------------
# FRE-1723: effort presets and budget caps
# ---------------------------------------------------------------------------

_DEEP = "effort.level.deep"
_PRESET = {
    "expansion": True,
    "sub_agent_rounds": 3,
    "planner_policy": "baseline",
    "primary_mode": None,
}


@pytest_asyncio.fixture
async def capped(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[str]:
    """A unique capped role, and budget file values that no test reads from the ignored file."""
    role = f"test_fre1723_{uuid4().hex[:8]}"
    budget = BudgetConfig(
        roles={},
        caps=[CapEntry(time_window="daily", role=role, cap_usd=Decimal("10.00"))],
    )
    monkeypatch.setattr(settings_edit, "load_budget_config", lambda: budget)
    yield role
    async with sessions() as db:
        await db.execute(
            text("DELETE FROM settings_overrides WHERE parameter IN (:cap, :deep)"),
            {"cap": f"cap.daily.{role}", "deep": _DEEP},
        )
        await db.execute(
            text(
                "DELETE FROM budget_reservations WHERE counter_id IN "
                "(SELECT id FROM budget_counters WHERE role = :r)"
            ),
            {"r": role},
        )
        await db.execute(text("DELETE FROM budget_counters WHERE role = :r"), {"r": role})
        await db.execute(text("DELETE FROM budget_policies WHERE role = :r"), {"r": role})
        await db.commit()


def _file_budget(role: str) -> BudgetConfig:
    return BudgetConfig(
        roles={},
        caps=[CapEntry(time_window="daily", role=role, cap_usd=Decimal("10.00"))],
    )


async def test_fre1723_ac2_a_lowered_cap_refuses_a_real_reservation_and_clear_restores(
    sessions: async_sessionmaker[AsyncSession], capped: str
) -> None:
    user_id = await _user(sessions, _OWNER)
    parameter = f"cap.daily.{capped}"
    gate = CostGate(config=_file_budget(capped), db_url=app_settings.database_url)
    await gate.connect()
    try:
        async with _client(user_id) as client:
            resp = await client.put(_URL, json={"parameter": parameter, "value": 2})
            assert resp.status_code == 200

            with pytest.raises(BudgetDenied) as denied:
                await gate.reserve(capped, Decimal("3.00"))
            assert denied.value.cap == Decimal("2.0")
            assert denied.value.denial_reason == DenialReason.CAP_EXCEEDED.value

            resp = await client.delete(_URL, params={"parameter": parameter})
            assert resp.json()["cleared"] is True

        assert await gate.reserve(capped, Decimal("3.00")) is not None
    finally:
        await gate.disconnect()

    assert [row[:4] for row in await _audit(sessions, user_id)] == [
        (parameter, "set", 10.0, 2.0),
        (parameter, "clear", 2.0, 10.0),
    ]


async def test_fre1723_ac2_a_gateway_restart_keeps_the_cap(
    sessions: async_sessionmaker[AsyncSession],
    capped: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _user(sessions, _OWNER)
    parameter = f"cap.daily.{capped}"
    async with _client(user_id) as client:
        resp = await client.put(_URL, json={"parameter": parameter, "value": 2})
        assert resp.status_code == 200

    # A new process: an empty copy, the startup load, then a gate built from the file.
    replace_overrides({})
    monkeypatch.setattr(settings_edit, "AsyncSessionLocal", sessions)
    await settings_edit.load_overrides_at_startup()
    gate = CostGate(config=_file_budget(capped), db_url=app_settings.database_url)
    await gate.connect()
    try:
        with pytest.raises(BudgetDenied):
            await gate.reserve(capped, Decimal("3.00"))
        await sync_budget_policies_to_db(gate.config, gate.pool)
        assert gate.pool is not None
        async with gate.pool.acquire() as conn:
            mirrored = await conn.fetchval(
                "SELECT cap_usd FROM budget_policies WHERE role = $1", capped
            )
        assert mirrored == Decimal("2")
    finally:
        await gate.disconnect()


async def test_fre1723_a_script_gate_enforces_the_stored_cap_and_takes_no_role_binding(
    sessions: async_sessionmaker[AsyncSession], capped: str
) -> None:
    user_id = await _user(sessions, _OWNER)
    parameter = f"cap.daily.{capped}"
    async with _client(user_id) as client:
        await client.put(_URL, json={"parameter": parameter, "value": 2})
        await client.put(_URL, json={"parameter": _PARAMETER, "value": _other_llm()})

    # A script: an empty copy, no startup load, a gate built from the file.
    replace_overrides({})
    gate = CostGate(config=_file_budget(capped), db_url=app_settings.database_url)
    await gate.connect()
    try:
        with pytest.raises(BudgetDenied):
            await gate.reserve(capped, Decimal("3.00"))
    finally:
        await gate.disconnect()
    assert _PARAMETER not in current_overrides()


async def test_fre1723_a_cap_edit_updates_budget_policies(
    sessions: async_sessionmaker[AsyncSession], capped: str
) -> None:
    user_id = await _user(sessions, _OWNER)
    parameter = f"cap.daily.{capped}"
    gate = CostGate(config=_file_budget(capped), db_url=app_settings.database_url)
    await gate.connect()
    set_default_gate(gate)

    async def _mirrored() -> Decimal:
        assert gate.pool is not None
        async with gate.pool.acquire() as conn:
            value: Decimal = await conn.fetchval(
                "SELECT cap_usd FROM budget_policies WHERE role = $1", capped
            )
        return value

    try:
        async with _client(user_id) as client:
            await client.put(_URL, json={"parameter": parameter, "value": 2})
            assert await _mirrored() == Decimal("2")
            await client.delete(_URL, params={"parameter": parameter})
            assert await _mirrored() == Decimal("10")
    finally:
        set_default_gate(None)
        await gate.disconnect()


async def test_fre1723_ac3_a_negative_cap_writes_no_row(
    sessions: async_sessionmaker[AsyncSession], capped: str
) -> None:
    user_id = await _user(sessions, _OWNER)
    parameter = f"cap.daily.{capped}"
    async with _client(user_id) as client:
        resp = await client.put(_URL, json={"parameter": parameter, "value": -1})
    assert resp.status_code == 422
    assert "greater than or equal to 0" in resp.json()["detail"]
    async with sessions() as db:
        result = await db.execute(
            text("SELECT count(*) FROM settings_overrides WHERE parameter = :p"), {"p": parameter}
        )
        assert result.scalar_one() == 0
    assert await _audit(sessions, user_id) == []


async def test_fre1723_ac1_ac3_a_preset_edit_clear_and_restart_on_real_rows(
    sessions: async_sessionmaker[AsyncSession],
    capped: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _user(sessions, _OWNER)
    file_preset = preset_values(file_seshat_effort(), "deep")
    async with _client(user_id) as client:
        resp = await client.put(_URL, json={"parameter": _DEEP, "value": _PRESET})
        assert resp.status_code == 200
        assert effort_spec("deep").sub_agent_rounds == 3

        replace_overrides({})
        assert effort_spec("deep") == file_seshat_effort().levels["deep"]
        monkeypatch.setattr(settings_edit, "AsyncSessionLocal", sessions)
        await settings_edit.load_overrides_at_startup()
        assert effort_spec("deep").sub_agent_rounds == 3

        resp = await client.delete(_URL, params={"parameter": _DEEP})
        assert resp.json()["cleared"] is True
    assert effort_spec("deep") == file_seshat_effort().levels["deep"]

    assert [row[:4] for row in await _audit(sessions, user_id)] == [
        (_DEEP, "set", file_preset, _PRESET),
        (_DEEP, "clear", _PRESET, file_preset),
    ]


async def test_fre1723_the_menu_shows_a_stored_preset_and_cap_after_a_reload(
    sessions: async_sessionmaker[AsyncSession], capped: str
) -> None:
    user_id = await _user(sessions, _OWNER)
    parameter = f"cap.daily.{capped}"
    gate = CostGate(config=_file_budget(capped), db_url=app_settings.database_url)
    await gate.connect()
    set_default_gate(gate)

    def _row(menu: dict[str, Any], name: str) -> dict[str, Any]:
        rows = (row for group in menu["groups"] for row in group["rows"])
        return next(row for row in rows if row["name"] == name)

    try:
        async with _client(user_id) as client:
            await client.put(_URL, json={"parameter": parameter, "value": 2})
            await client.put(_URL, json={"parameter": _DEEP, "value": _PRESET})

            menu = (await client.get("/api/v1/settings")).json()
            cap = _row(menu, parameter)
            assert (cap["value"], cap["source"], cap["clearable"]) == (2.0, "override", True)
            assert cap["file_default"]["value"] == 10.0
            preset = _row(menu, _DEEP)
            assert (preset["value"], preset["source"]) == (_PRESET, "override")
            assert preset["file_default"]["value"] == preset_values(file_seshat_effort(), "deep")

            await client.delete(_URL, params={"parameter": parameter})
            await client.delete(_URL, params={"parameter": _DEEP})

            menu = (await client.get("/api/v1/settings")).json()
            cap = _row(menu, parameter)
            assert (cap["value"], cap["source"], cap["clearable"]) == (10.0, "config file", False)
            assert _row(menu, _DEEP)["source"] == "config file"
    finally:
        set_default_gate(None)
        await gate.disconnect()
