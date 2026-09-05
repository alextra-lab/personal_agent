"""FRE-972 — the compaction/consent gate must measure the session's window.

Before the fix, ``step_init``'s truncation and ``step_llm_call``'s hard-
compression/consent gate both sized against ``settings.context_window_max_tokens``
(96,000, the local Qwen budget) regardless of the session's selected primary
model. On a claude_sonnet session (200K real window) this fired premature
truncation and a spurious "Context window nearly full" consent popup at
~48% of the real window.

These tests prove the outcome the ticket names as its own test: a sonnet
session does not truncate / trigger hard compression until it approaches a
fraction of 200K, while a small-window session — sized identically — does,
because the same history now crosses the small window's threshold but not
sonnet's larger one.

FRE-1411: the owner's 2026-09-05 swap loaded the local Qwen primary at its
full natural window (262,144), so no local deployment is smaller than
sonnet's 200K any more — the pair this file compares against local Qwen
originally is retired here in favor of ``gpt-5.4-mini`` (context_length
128,000, ``config/models.yaml``), the smallest ``kind: llm`` deployment left
in the catalog below sonnet's window. It plays the small-window role only for
this test's arithmetic; the assertions exercise ``resolve_active_context_length``
directly and do not depend on gpt-5.4-mini ever actually being selected as a
session's real primary.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import tiktoken

from personal_agent.config.selection import reset_current_selection, set_current_selection
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import step_init, step_llm_call
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import ExecutionContext
from personal_agent.telemetry.trace import TraceContext

_ENCODING = tiktoken.get_encoding("cl100k_base")
_UNIT = "the quick brown fox jumps over the lazy dog. "
_UNIT_TOKENS = len(_ENCODING.encode(_UNIT))


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> object:
    """Restore executor's lazily-cached registry globals after each test.

    step_llm_call seeds the module-level ``_tool_registry`` /
    ``_tool_execution_layer`` from the patched (empty) registry used below.
    Without this restore the patched registry leaks past the patch scope and
    pollutes later tests in the process (mirrors the identical fixture in
    ``test_content_widening.py``).
    """
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


_SMALL_KEY = "gpt-5.4-mini"  # context_length 128000 (config/models.yaml)
_SONNET_KEY = "claude_sonnet"  # context_length 200000 (config/models.yaml)


def _sized_text(target_tokens: int) -> str:
    """Plain text whose cl100k_base token count is ~target_tokens."""
    reps = max(1, int(target_tokens / _UNIT_TOKENS))
    return _UNIT * reps


def _big_history(turns: int, tokens_per_turn: int) -> list[dict[str, str]]:
    history = [{"role": "system", "content": "system prompt"}]
    for i in range(turns):
        history.append({"role": "user", "content": f"turn {i} " + _sized_text(tokens_per_turn)})
        history.append({"role": "assistant", "content": f"reply {i}"})
    return history


# ---------------------------------------------------------------------------
# step_init's apply_context_window call — fix #1
# ---------------------------------------------------------------------------


async def _run_step_init_with_selection(
    primary_key: str, history: list[dict[str, str]]
) -> list[dict[str, Any]]:
    sm = SessionManager()
    sid = sm.create_session(Mode.NORMAL, Channel.CHAT, session_id=f"sess-{primary_key}")
    sm.update_session(sid, messages=list(history))

    ctx = ExecutionContext(
        session_id=sid,
        trace_id=f"trace-{primary_key}",
        user_message="continue",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        gateway_output=None,
    )
    trace_ctx = TraceContext(trace_id=ctx.trace_id, session_id=sid)

    token = set_current_selection({"primary": primary_key})
    try:
        # The ADR-0081 frozen-reset cache scheduler (_maybe_frozen_reset) runs
        # right after apply_context_window and independently recompacts
        # history for cache economics, off its own static-budget-derived
        # ceiling — a separate mechanism the ticket doesn't touch. Disabled
        # here so this test isolates apply_context_window's own truncation
        # decision, which is what FRE-972 fixes.
        with patch(
            "personal_agent.orchestrator.executor._maybe_frozen_reset",
            new=AsyncMock(return_value=None),
        ):
            await step_init(ctx, sm, trace_ctx)
    finally:
        reset_current_selection(token)
    return ctx.messages


@pytest.mark.asyncio
async def test_step_init_does_not_truncate_sonnet_session_within_its_real_window() -> None:
    """Sonnet session with history sized between the small and sonnet budgets.

    ~163K tokens of history: over gpt-5.4-mini's ~123.5K trim budget, under
    sonnet's ~195.5K trim budget (reserved_tokens=4500 in both cases).
    """
    history = _big_history(turns=18, tokens_per_turn=9000)

    messages = await _run_step_init_with_selection(_SONNET_KEY, history)

    # Nothing dropped: original history plus the freshly-appended user turn.
    assert len(messages) == len(history) + 1


@pytest.mark.asyncio
async def test_step_init_truncates_small_window_session_at_its_own_window() -> None:
    """A small-window selection on the same history truncates it.

    Proves the truncation is keyed to the *selected* model, not one static
    constant that would treat both sessions identically.
    """
    history = _big_history(turns=18, tokens_per_turn=9000)

    messages = await _run_step_init_with_selection(_SMALL_KEY, history)

    assert len(messages) < len(history) + 1


# ---------------------------------------------------------------------------
# step_llm_call's hard-compression / consent-popup gate — fix #2
# ---------------------------------------------------------------------------


def _make_mock_llm_client() -> MagicMock:
    mock_client = MagicMock()
    mock_client.respond = AsyncMock(
        return_value={
            "content": "ok",
            "tool_calls": [],
            "response_id": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    mock_client.model_configs = {}
    return mock_client


async def _run_step_llm_call_with_selection(
    primary_key: str, history: list[dict[str, str]], pause_mock: AsyncMock
) -> None:
    ctx = ExecutionContext(
        session_id="sess-gate",
        trace_id="trace-gate",
        user_message="continue",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=list(history),
    )
    trace_ctx = TraceContext(trace_id=ctx.trace_id, session_id=ctx.session_id)
    mock_llm = _make_mock_llm_client()

    token = set_current_selection({"primary": primary_key})
    try:
        with (
            patch(
                "personal_agent.orchestrator.skills.get_skill_bodies",
                return_value=("", ()),
            ),
            patch("personal_agent.orchestrator.skills.assemble_skill_index", return_value=""),
            patch(
                "personal_agent.orchestrator.skills.assemble_skill_index_directive",
                return_value="",
            ),
            patch(
                "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
                return_value="",
            ),
            patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
            patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
            patch(
                "personal_agent.orchestrator.executor.get_default_registry",
                return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
            ),
            patch(
                "personal_agent.orchestrator.executor._maybe_pause_for_constraint",
                pause_mock,
            ),
        ):
            await step_llm_call(ctx, MagicMock(), trace_ctx)
    finally:
        reset_current_selection(token)


@pytest.mark.asyncio
async def test_sonnet_session_does_not_trigger_consent_popup_below_its_real_window() -> None:
    """Sonnet session with history above the small window's hard threshold but below its own.

    ~145K tokens: over gpt-5.4-mini's hard threshold (0.85 * 128,000 = 108.8K),
    under sonnet's (0.85 * 200,000 = 170K). The old static-96K gate would have
    fired the popup on either session at this size.
    """
    history = _big_history(turns=16, tokens_per_turn=9000)
    pause_mock = AsyncMock(return_value="compress_and_continue")

    await _run_step_llm_call_with_selection(_SONNET_KEY, history, pause_mock)

    assert pause_mock.await_count == 0, (
        "consent popup fired on a sonnet session below its real 200K window"
    )


@pytest.mark.asyncio
async def test_small_window_session_still_triggers_consent_popup_at_its_own_threshold() -> None:
    """A small-window selection on the same history still fires.

    The gate isn't disabled, it's now correctly scaled per session.
    """
    history = _big_history(turns=16, tokens_per_turn=9000)
    pause_mock = AsyncMock(return_value="compress_and_continue")

    await _run_step_llm_call_with_selection(_SMALL_KEY, history, pause_mock)

    assert pause_mock.await_count == 1
