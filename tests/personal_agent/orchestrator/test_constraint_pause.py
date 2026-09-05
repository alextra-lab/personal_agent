"""Tests for _maybe_pause_for_constraint decision logic (ADR-0076 / FRE-389)."""

from __future__ import annotations

from uuid import uuid4

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext

_TRANSPORT = "personal_agent.transport.agui.transport"


def _ctx() -> ExecutionContext:
    """A minimal execution context for ADR-0142 pause-accounting tests (FRE-1391)."""
    return ExecutionContext(
        session_id="s1",
        trace_id="t1",
        user_message="hi",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )


@pytest.mark.asyncio
async def test_preference_applied_bypasses_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """A standing preference is applied without pushing a pause event (AC-7)."""

    async def fake_load(user_id: object, constraint: str, **_kw: object) -> str:
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
    assert result.resolution == "preference_applied"
    assert pushed["called"] is False


def _capture_waiting_phases(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Capture PhaseStart/PhaseEnd events pushed during a constraint pause."""
    import personal_agent.transport.agui.transport as transport_mod
    from personal_agent.transport.events import PhaseEndEvent, PhaseStartEvent

    captured: list[object] = []

    async def _capture(event: object, session_id: str) -> None:
        if isinstance(event, (PhaseStartEvent, PhaseEndEvent)):
            captured.append(event)

    monkeypatch.setattr(transport_mod, "_push_event", _capture)
    return captured


@pytest.mark.asyncio
async def test_pause_emits_waiting_for_choice_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0123 §1 (FRE-934): a real pause is bracketed by a WAITING_FOR_CHOICE phase."""
    from personal_agent.transport.events import Phase, PhaseEndEvent, PhaseStartEvent

    async def fake_load(user_id: object, constraint: str, **_kw: object) -> None:
        return None

    async def fake_push(**kwargs: object) -> dict[str, str]:
        return {"decision": "continue_10", "resolution": "user_choice"}

    async def fake_emit(**kwargs: object) -> None:
        return None

    monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)
    monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)
    monkeypatch.setattr(f"{_TRANSPORT}.emit_constraint_resolved", fake_emit)
    events = _capture_waiting_phases(monkeypatch)

    await ex._maybe_pause_for_constraint(
        session_id="s1",
        trace_id="t1",
        user_id=uuid4(),
        constraint="artifact_builder",
        context="ctx",
    )

    waiting = [e for e in events if e.phase is Phase.WAITING_FOR_CHOICE]
    assert [type(e) for e in waiting] == [PhaseStartEvent, PhaseEndEvent]
    assert waiting[0].detail == "artifact_builder"
    assert waiting[0].phase_id == waiting[1].phase_id


@pytest.mark.asyncio
async def test_preference_bypass_emits_no_waiting_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pause happened → no WAITING_FOR_CHOICE phase (the wrap is scoped to the wait)."""

    async def fake_load(user_id: object, constraint: str, **_kw: object) -> str:
        return "continue_10"

    monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)
    events = _capture_waiting_phases(monkeypatch)

    await ex._maybe_pause_for_constraint(
        session_id="s1",
        trace_id="t1",
        user_id=uuid4(),
        constraint="tool_iteration_limit",
        context="ctx",
    )

    assert events == []


@pytest.mark.asyncio
async def test_no_ws_default_no_resolution_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """connection_lost applies the default silently — no CONSTRAINT_RESOLVED (AC-13)."""

    async def fake_load(user_id: object, constraint: str, **_kw: object) -> None:
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
    assert result.resolution == "connection_lost"
    assert emitted == []


@pytest.mark.asyncio
async def test_user_choice_emits_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user decision returns its action_id and emits CONSTRAINT_RESOLVED (AC-3/4)."""

    async def fake_load(user_id: object, constraint: str, **_kw: object) -> None:
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
    assert result.resolution == "user_choice"
    assert len(emitted) == 1
    assert emitted[0]["action_id"] == "continue_10"
    assert emitted[0]["resolution"] == "user_choice"


@pytest.mark.asyncio
async def test_timeout_default_resolution_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout is distinguishable from a user choice.

    ADR-0122 §4 routes the two differently at the artifact-builder build boundary.
    """

    async def fake_load(user_id: object, constraint: str, **_kw: object) -> None:
        return None

    async def fake_push(**kwargs: object) -> dict[str, str]:
        return {"decision": "finish_now", "resolution": "timeout_default"}

    async def fake_emit(**kwargs: object) -> None:
        return None

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
    assert result.resolution == "timeout_default"


@pytest.mark.asyncio
async def test_remember_saves_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    """remember=true persists the chosen action via _save_constraint_preference (AC-6)."""

    async def fake_load(user_id: object, constraint: str, **_kw: object) -> None:
        return None

    async def fake_push(**kwargs: object) -> dict[str, object]:
        return {"decision": "finish_now", "resolution": "user_choice", "remember": True}

    async def fake_emit(**kwargs: object) -> None:
        return None

    saved: list[tuple[str, str, str]] = []

    async def fake_save(
        user_id: object,
        constraint_name: str,
        action_id: str,
        *,
        trace_id: str,
        session_id: str,
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


@pytest.mark.asyncio
async def test_artifact_builder_pause_carries_computed_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor guard accepts the computed-options path and emits them on the event.

    ADR-0122 §3 / FRE-881, AC-6: for ``artifact_builder`` the option set on the
    ``ConstraintPauseEvent`` comes from ``resolve_options_and_default`` (catalog-
    derived), not the static ``CONSTRAINT_OPTIONS`` dict — which has no such key
    and would ``KeyError`` on the old guard.
    """
    import personal_agent.orchestrator.constraint_options as co

    async def fake_load(user_id: object, constraint: str, **_kw: object) -> None:
        return None

    captured: dict[str, object] = {}

    async def fake_push(**kwargs: object) -> dict[str, str]:
        event = kwargs["event"]
        captured["event"] = event
        return {"decision": event.default_option, "resolution": "user_choice"}

    async def fake_emit(**kwargs: object) -> None:
        return None

    # Isolate the executor wiring from the live catalog/settings: prove the guard
    # routes artifact_builder through the computed resolver and plumbs its result
    # onto the event. (compute_artifact_builder_options is asserted against the
    # catalog directly in test_constraint_options_computed.)
    monkeypatch.setattr(co, "resolve_options_and_default", lambda c: (["m_a", "m_b"], "m_b"))
    monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)
    monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)
    monkeypatch.setattr(f"{_TRANSPORT}.emit_constraint_resolved", fake_emit)

    result = await ex._maybe_pause_for_constraint(
        session_id="s1",
        trace_id="t1",
        user_id=uuid4(),
        constraint="artifact_builder",
        context="Choose a builder",
    )

    event = captured["event"]
    assert list(event.options) == ["m_a", "m_b"]  # type: ignore[attr-defined]
    assert event.default_option == "m_b"  # type: ignore[attr-defined]
    assert event.constraint == "artifact_builder"  # type: ignore[attr-defined]
    assert result == "m_b"
    assert result.resolution == "user_choice"


class TestPauseAccounting:
    """ADR-0142 pause accounting on ``ExecutionContext`` (FRE-1391)."""

    def test_quiet_turn_reads_zero_not_absent(self) -> None:
        """AC-4: a fresh ctx (no pause) reports explicit zeros, not None."""
        ctx = _ctx()
        assert ctx.pause_count == 0
        assert ctx.credited_pause_seconds == 0.0
        assert ctx.constraint_resolutions == []

    @pytest.mark.asyncio
    async def test_preference_bypass_records_no_pause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A preference bypass never pauses, so it must not be counted as one."""
        ctx = _ctx()

        async def fake_load(user_id: object, constraint: str, **_kw: object) -> str:
            return "continue_10"

        monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)

        await ex._maybe_pause_for_constraint(
            session_id="s1",
            trace_id="t1",
            user_id=uuid4(),
            constraint="tool_iteration_limit",
            context="ctx",
            ctx=ctx,
        )

        assert ctx.pause_count == 0
        assert ctx.credited_pause_seconds == 0.0
        assert ctx.constraint_resolutions == []

    @pytest.mark.asyncio
    async def test_two_pauses_produce_two_entries_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-2: two pauses append two entries, in order — a scalar would overwrite the first."""
        ctx = _ctx()

        async def fake_load(user_id: object, constraint: str, **_kw: object) -> None:
            return None

        decisions = iter(
            [
                {"decision": "continue_10", "resolution": "user_choice"},
                {"decision": "stop_here", "resolution": "timeout_default"},
            ]
        )

        async def fake_push(**kwargs: object) -> dict[str, str]:
            return next(decisions)

        async def fake_emit(**kwargs: object) -> None:
            return None

        monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)
        monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)
        monkeypatch.setattr(f"{_TRANSPORT}.emit_constraint_resolved", fake_emit)

        await ex._maybe_pause_for_constraint(
            session_id="s1",
            trace_id="t1",
            user_id=uuid4(),
            constraint="tool_iteration_limit",
            context="first pause",
            ctx=ctx,
        )
        await ex._maybe_pause_for_constraint(
            session_id="s1",
            trace_id="t1",
            user_id=uuid4(),
            constraint="context_compression",
            context="second pause",
            ctx=ctx,
        )

        assert [r.constraint for r in ctx.constraint_resolutions] == [
            "tool_iteration_limit",
            "context_compression",
        ]
        assert [r.action_id for r in ctx.constraint_resolutions] == [
            "continue_10",
            "stop_here",
        ]
        assert ctx.pause_count == 2
        assert ctx.credited_pause_seconds >= 0.0

    @pytest.mark.asyncio
    async def test_connection_lost_is_not_recorded_as_a_pause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defensive connection_lost branch stands for an immediate no-client
        default, not a resolved wait — it must not be credited as one (found in
        codex review: this early return sits before the accounting block).
        """
        ctx = _ctx()

        async def fake_load(user_id: object, constraint: str, **_kw: object) -> None:
            return None

        async def fake_push(**kwargs: object) -> dict[str, str]:
            return {"decision": "finish_now", "resolution": "connection_lost"}

        monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)
        monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)

        await ex._maybe_pause_for_constraint(
            session_id="s1",
            trace_id="t1",
            user_id=uuid4(),
            constraint="tool_iteration_limit",
            context="ctx",
            ctx=ctx,
        )

        assert ctx.pause_count == 0
        assert ctx.constraint_resolutions == []
        assert ctx.credited_pause_seconds == 0.0
