"""FRE-1298 — the current date/time reaches the model in the VOLATILE tail.

Covers:
- AC-1: the ISO date, time, and named IANA zone all appear in the volatile block
  (the last user message), never in ``system_prompt`` (the cached static prefix).
- AC-4: a turn making two sequential model calls (the same ``ctx`` driven through
  ``step_llm_call`` twice, mirroring how the orchestrator's tool loop re-invokes it)
  renders the identical value in both.
- AC-5: ``render_current_datetime_block`` resolves the correct local day and UTC
  offset at a DST boundary, for both the summer (CEST) and winter (CET) cases —
  not a fixed offset, and not UTC formatted and called done.

All instants are passed explicitly (fixed clock) rather than read from the wall
clock, per AC-7.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.orchestrator.prompts import render_current_datetime_block

# ---------------------------------------------------------------------------
# AC-5 — unit-level: the render function itself, at DST boundaries.
# ---------------------------------------------------------------------------


def test_summer_boundary_rolls_to_next_local_day_cest() -> None:
    """23:30 UTC in June is 01:30 the next day in Europe/Paris (CEST, UTC+02:00)."""
    instant = datetime(2026, 6, 15, 23, 30, 0, tzinfo=UTC)
    block = render_current_datetime_block(instant)

    assert "Current date: 2026-06-16" in block
    assert "Current time: 01:30:00" in block
    assert "Timezone: Europe/Paris (UTC+02:00)" in block


def test_winter_instant_uses_cet_offset() -> None:
    """A winter instant resolves the CET (UTC+01:00) offset, not the summer one."""
    instant = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    block = render_current_datetime_block(instant)

    assert "Current date: 2026-01-15" in block
    assert "Current time: 11:00:00" in block
    assert "Timezone: Europe/Paris (UTC+01:00)" in block


# ---------------------------------------------------------------------------
# AC-1 / AC-4 — functional: driven through the real step_llm_call.
# ---------------------------------------------------------------------------

_FIXED_INSTANT = datetime(2026, 6, 15, 23, 30, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> object:
    """Restore the executor's lazily-cached registry globals after each test.

    Mirrors the identical fixture in test_deployment_context_prompt.py /
    test_artifact_builder_planning_note_injection.py.
    """
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


def _make_ctx() -> object:
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    ctx = ExecutionContext(
        session_id="test-session",
        trace_id="test-trace",
        user_message="what is today's date",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": "what is today's date"}],
    )
    # AC-7: fixed clock, never the wall clock, in a test asserting on the
    # rendered value.
    ctx.turn_started_at = _FIXED_INSTANT
    return ctx


def _mock_llm() -> MagicMock:
    client = MagicMock()
    client.respond = AsyncMock(
        return_value={
            "content": "it is 2026-06-16",
            "tool_calls": [],
            "response_id": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    client.model_configs = {}
    return client


async def _drive_step_llm_call(ctx: object, mock_llm: MagicMock) -> None:
    from personal_agent.telemetry.trace import TraceContext

    session = MagicMock()
    session.add_message = AsyncMock()
    session.get_messages = AsyncMock(return_value=[])

    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
    ):
        from personal_agent.orchestrator.executor import step_llm_call

        await step_llm_call(ctx, session, TraceContext.new_trace())  # type: ignore[arg-type]


def _last_user_content(messages: list[dict[str, object]]) -> str:
    return next(str(m["content"]) for m in reversed(messages) if m.get("role") == "user")


@pytest.mark.asyncio
async def test_datetime_block_reaches_the_model_in_the_volatile_tail() -> None:
    """AC-1: ISO date, time, and named zone all appear in the last user message.

    Not in system_prompt (the cached static prefix).
    """
    ctx = _make_ctx()
    mock_llm = _mock_llm()

    await _drive_step_llm_call(ctx, mock_llm)

    call_kwargs = mock_llm.respond.call_args.kwargs
    system_prompt = call_kwargs.get("system_prompt") or ""
    last_user_content = _last_user_content(call_kwargs["messages"])

    assert "2026-06-16" in last_user_content
    assert "01:30:00" in last_user_content
    assert "Europe/Paris" in last_user_content
    assert "2026-06-16" not in system_prompt
    assert "Europe/Paris" not in system_prompt


@pytest.mark.asyncio
async def test_same_timestamp_across_two_sequential_calls_in_one_turn() -> None:
    """AC-4: two calls against the SAME ctx must render the identical value.

    Mirrors the orchestrator's tool loop, which re-invokes step_llm_call
    against shared state — never redriving the wall clock or drifting between
    calls.
    """
    ctx = _make_ctx()
    mock_llm = _mock_llm()

    await _drive_step_llm_call(ctx, mock_llm)
    first_content = _last_user_content(mock_llm.respond.call_args.kwargs["messages"])

    await _drive_step_llm_call(ctx, mock_llm)
    second_content = _last_user_content(mock_llm.respond.call_args.kwargs["messages"])

    assert first_content == second_content
    assert "2026-06-16" in first_content
    assert "01:30:00" in first_content
