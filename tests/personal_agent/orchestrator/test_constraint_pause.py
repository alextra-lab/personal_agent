"""Tests for _maybe_pause_for_constraint decision logic (ADR-0076 / FRE-389)."""

from __future__ import annotations

from uuid import uuid4

import pytest

import personal_agent.orchestrator.executor as ex

_TRANSPORT = "personal_agent.transport.agui.transport"


@pytest.mark.asyncio
async def test_preference_applied_bypasses_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """A standing preference is applied without pushing a pause event (AC-7)."""

    async def fake_load(user_id: object, constraint: str) -> str:
        return "continue_10"

    pushed = {"called": False}

    async def fake_push(**kwargs: object) -> dict[str, str]:
        pushed["called"] = True
        return {}

    monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)
    monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)

    result = await ex._maybe_pause_for_constraint(
        session_id="s1",
        trace_id="t1",
        user_id=uuid4(),
        constraint="tool_iteration_limit",
        context="ctx",
    )
    assert result == "continue_10"
    assert pushed["called"] is False


@pytest.mark.asyncio
async def test_no_ws_default_no_resolution_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """connection_lost applies the default silently — no CONSTRAINT_RESOLVED (AC-13)."""

    async def fake_load(user_id: object, constraint: str) -> None:
        return None

    async def fake_push(**kwargs: object) -> dict[str, str]:
        return {"decision": "finish_now", "resolution": "connection_lost"}

    emitted: list[dict[str, object]] = []

    async def fake_emit(**kwargs: object) -> None:
        emitted.append(kwargs)

    monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)
    monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)
    monkeypatch.setattr(f"{_TRANSPORT}.emit_constraint_resolved", fake_emit)

    result = await ex._maybe_pause_for_constraint(
        session_id="s1",
        trace_id="t1",
        user_id=uuid4(),
        constraint="tool_iteration_limit",
        context="ctx",
    )
    assert result == "finish_now"
    assert emitted == []


@pytest.mark.asyncio
async def test_user_choice_emits_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user decision returns its action_id and emits CONSTRAINT_RESOLVED (AC-3/4)."""

    async def fake_load(user_id: object, constraint: str) -> None:
        return None

    async def fake_push(**kwargs: object) -> dict[str, str]:
        return {"decision": "continue_10", "resolution": "user_choice"}

    emitted: list[dict[str, object]] = []

    async def fake_emit(**kwargs: object) -> None:
        emitted.append(kwargs)

    monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)
    monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)
    monkeypatch.setattr(f"{_TRANSPORT}.emit_constraint_resolved", fake_emit)

    result = await ex._maybe_pause_for_constraint(
        session_id="s1",
        trace_id="t1",
        user_id=uuid4(),
        constraint="tool_iteration_limit",
        context="ctx",
    )
    assert result == "continue_10"
    assert len(emitted) == 1
    assert emitted[0]["action_id"] == "continue_10"
    assert emitted[0]["resolution"] == "user_choice"


@pytest.mark.asyncio
async def test_remember_saves_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    """remember=true persists the chosen action via _save_constraint_preference (AC-6)."""

    async def fake_load(user_id: object, constraint: str) -> None:
        return None

    async def fake_push(**kwargs: object) -> dict[str, object]:
        return {"decision": "finish_now", "resolution": "user_choice", "remember": True}

    async def fake_emit(**kwargs: object) -> None:
        return None

    saved: list[tuple[str, str, str]] = []

    async def fake_save(
        user_id: object, constraint_name: str, action_id: str, *, session_id: str
    ) -> None:
        saved.append((constraint_name, action_id, session_id))

    monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)
    monkeypatch.setattr(ex, "_save_constraint_preference", fake_save)
    monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)
    monkeypatch.setattr(f"{_TRANSPORT}.emit_constraint_resolved", fake_emit)

    result = await ex._maybe_pause_for_constraint(
        session_id="sess-1",
        trace_id="t1",
        user_id=uuid4(),
        constraint="tool_iteration_limit",
        context="ctx",
    )
    assert result == "finish_now"
    assert saved == [("tool_iteration_limit", "finish_now", "sess-1")]
