"""Two step_init observability emits must fire on the live (gateway-driven) turn path.

FRE-944: the cache-reset decision emit. FRE-945: the sibling ``conversation_context_loaded``
emit, dark for the identical reason.

ADR-0081 §D3 makes the scheduler's per-turn evaluation the observability surface for
compaction: it is written to log *every* evaluation, so quality-slope inertness, ``L*``
and — after FRE-944 — the headroom to the token ceiling stay readable even when no reset
fires. ``conversation_context_loaded`` is the sibling per-turn record of what history
``step_init`` actually loaded and how much of it survived its own truncation step.

Neither ever fired in production. ``step_init``'s gateway-driven branch ends in an
unconditional ``return``, so ``_maybe_frozen_reset`` and the ``conversation_context_loaded``
emit beside it both sat below the return and were unreachable on 157/157 observed turns.

The pre-existing coverage in ``test_frozen_reset_wiring.py`` could not catch this: it calls
the helper directly with a hand-built context and never drives ``step_init``, so it proves
the function behaves when invoked while production never invoked it. These tests close that
gap by driving the real stage.

Loggers are captured by patching the module logger rather than
``structlog.testing.capture_logs()`` — ``telemetry/logger.py`` sets
``cache_logger_on_first_use=True`` and ``executor.py`` materializes ``log`` at import time,
so ``capture_logs()`` is unreliable under the shared suite (the FRE-552 pattern).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator import executor as ex
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GatewayOutput,
    GovernanceContext,
    IntentResult,
    TaskType,
)
from personal_agent.telemetry.trace import TraceContext

CACHE_RESET_DECISION = "cache_reset_decision"
CONVERSATION_CONTEXT_LOADED = "conversation_context_loaded"


def _capturing_log() -> tuple[MagicMock, list[tuple[str, dict[str, Any]]]]:
    """Return a mock module logger and the list its ``info`` calls land in.

    Returns:
        Tuple of (mock logger, captured ``(event, kwargs)`` pairs).
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    def _capture(event: str, **kw: Any) -> None:
        calls.append((event, dict(kw)))

    mock_log = MagicMock()
    mock_log.info.side_effect = _capture
    return mock_log, calls


def _gateway_output(
    messages: list[dict[str, str]],
    strategy: DecompositionStrategy = DecompositionStrategy.SINGLE,
) -> GatewayOutput:
    """Build a GatewayOutput — SINGLE by default, the ordinary production shape."""
    return GatewayOutput(
        intent=IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        ),
        governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
        decomposition=DecompositionResult(
            strategy=strategy, reason="test", constraints={"max_sub_agents": 2}
        ),
        context=AssembledContext(messages=messages, memory_context=None, tool_definitions=None),
        session_id="s1",
        trace_id="t1",
    )


def _gateway_ctx(
    messages: list[dict[str, str]],
    strategy: DecompositionStrategy = DecompositionStrategy.SINGLE,
) -> ExecutionContext:
    """Build an ExecutionContext carrying a gateway output, as the service does."""
    ctx = ExecutionContext(
        session_id="s1",
        trace_id="t1",
        user_message="hello",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        gateway_output=_gateway_output(messages, strategy),
    )
    ctx.messages = list(messages)
    return ctx


def _isolate_step_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the I/O-bound pre-branch helpers so the test exercises control flow only."""

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ex, "_maybe_reinject_pending_cloud_attachment", _noop)
    monkeypatch.setattr(ex, "_maybe_reinject_pending_document_continuation", _noop)
    monkeypatch.setattr(ex, "_maybe_resolve_artifact_builder", _noop)


async def _drive_gateway_turn(
    monkeypatch: pytest.MonkeyPatch, ctx: ExecutionContext
) -> list[tuple[str, dict[str, Any]]]:
    """Drive one ``step_init`` gateway turn; return the captured log calls.

    Real control flow through the live stage — the branch, the returns, and the
    scheduler evaluation are all genuine. The session load is stubbed
    (``get_session`` returns ``None``) and ``ctx.messages`` pre-seeded instead, so a
    regression in how persisted history reaches ``ctx.messages`` would not be caught
    here; that is not what these tests are asserting.
    """
    _isolate_step_init(monkeypatch)
    mock_log, calls = _capturing_log()
    monkeypatch.setattr(ex, "log", mock_log)

    session_manager = MagicMock()
    session_manager.get_session = MagicMock(return_value=None)

    state = await ex.step_init(ctx, session_manager, TraceContext(trace_id="t1", session_id="s1"))
    assert state == TaskState.LLM_CALL
    return calls


def _events(calls: list[tuple[str, dict[str, Any]]], event_name: str) -> list[dict[str, Any]]:
    """Return the payloads of every ``event_name`` emit in ``calls``."""
    return [kw for event, kw in calls if event == event_name]


def _decisions(calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Return the payloads of every cache-reset decision emit in ``calls``."""
    return _events(calls, CACHE_RESET_DECISION)


@pytest.mark.asyncio
async def test_gateway_path_emits_cache_reset_decision_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-944 AC-1: the live gateway turn emits the decision, exactly once.

    Red before the fix: the gateway branch returns before the scheduler is ever
    reached, so zero decision events are captured.
    """
    ctx = _gateway_ctx([{"role": "user", "content": "hello"}])
    calls = await _drive_gateway_turn(monkeypatch, ctx)

    decisions = _decisions(calls)
    assert len(decisions) == 1, f"expected exactly one {CACHE_RESET_DECISION}, got {len(decisions)}"

    payload = decisions[0]
    assert isinstance(payload["should_reset"], bool)
    assert isinstance(payload["reason"], str) and payload["reason"]


@pytest.mark.asyncio
async def test_gateway_emit_carries_headroom_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRE-944 AC-2: headroom is readable off one event, with no join.

    The accumulated token count and the ceiling it is compared against are both already
    derived by ``_derive_reset_inputs``; this asserts they reach the emit.
    """
    ctx = _gateway_ctx([{"role": "user", "content": "hello"}])
    calls = await _drive_gateway_turn(monkeypatch, ctx)

    payload = _decisions(calls)[0]
    for field in ("accumulated_tokens", "accum_max_tokens"):
        assert field in payload, f"{field} missing from the emit — headroom is unreadable"
        assert isinstance(payload[field], int | float) and not isinstance(payload[field], bool)
    assert payload["accum_max_tokens"] > 0


@pytest.mark.asyncio
async def test_gateway_path_never_compacts_even_when_reset_worthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-944: the gateway path evaluates and logs, but must never act.

    The ticket scopes this work to visibility only, so the evaluate-only boundary is
    load-bearing: forcing a reset-worthy decision must still leave the message list
    untouched and ``build_frozen_reset`` uncalled.

    Honest about what this proves: ``_emit_cache_reset_decision`` has no branch that
    could reach ``build_frozen_reset``, so on today's code shape this guard holds
    unconditionally rather than exercising a live conditional. It is a *shape* test —
    its value is catching a future edit that slides the act half onto the gateway
    branch, which is precisely the boundary this ticket's scope depends on.
    """
    monkeypatch.setattr(
        ex,
        "_derive_reset_inputs",
        lambda messages, backend: {
            "turns_since_reset": 99,
            "accumulated_tokens": 999_999,
            "accum_max_tokens": 1,
            "min_run_turns": 1,
            "reset_cost_tokens": 1.0,
            "delta_turn_tokens": 1.0,
            "quality_token_weight": 4000.0,
            "quality_slope": 0.0,
        },
    )

    called: list[bool] = []

    async def _spy_build_frozen_reset(*args: Any, **kwargs: Any) -> None:
        called.append(True)
        raise AssertionError("build_frozen_reset must not run on the gateway path")

    monkeypatch.setattr(
        "personal_agent.orchestrator.within_session_compression.build_frozen_reset",
        _spy_build_frozen_reset,
    )

    messages = [{"role": "user", "content": "hello"}]
    ctx = _gateway_ctx(messages)
    calls = await _drive_gateway_turn(monkeypatch, ctx)

    payload = _decisions(calls)[0]
    assert payload["should_reset"] is True, "harness failed to force a reset-worthy decision"
    assert called == [], "gateway path invoked compaction — the evaluate-only boundary broke"
    # step_init appends this turn's user message, so history legitimately grows by one.
    # What compaction would do instead is *replace* the list with a compacted form, so
    # the invariant to hold is that history stayed a strict forward extension.
    assert ctx.messages[: len(messages)] == messages
    assert len(ctx.messages) == len(messages) + 1
    # A reset stashes this turn's highlights; holding leaves the field at its default.
    assert not ctx.salient_highlights


@pytest.mark.asyncio
async def test_enforced_expansion_subpath_also_emits_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-944 AC-1: the enforced-expansion sub-path emits too.

    That sub-path returns from the middle of the gateway branch, so an emit placed just
    before the branch's final return would leave these turns silent — reproducing the
    original bug on a subset of production traffic (self-review finding).
    """
    from unittest.mock import AsyncMock

    from personal_agent.orchestrator.expansion_controller import ExpansionResult

    async def _noop_progress(c: ExecutionContext) -> None:
        return None

    monkeypatch.setattr(ex.settings, "orchestration_mode", "enforced")
    monkeypatch.setattr(ex, "_report_turn_progress", _noop_progress)

    controller = MagicMock()
    controller.execute = AsyncMock(
        return_value=ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[],
            synthesis_context="SYN",
            planner_cost_usd=0.0,
        )
    )
    monkeypatch.setattr(
        "personal_agent.orchestrator.expansion_controller.ExpansionController",
        lambda: controller,
    )
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client",
        lambda role_name=None: MagicMock(),
    )

    ctx = _gateway_ctx([{"role": "user", "content": "build X and Y"}], DecompositionStrategy.HYBRID)
    calls = await _drive_gateway_turn(monkeypatch, ctx)

    decisions = _decisions(calls)
    assert len(decisions) == 1, (
        f"expansion sub-path emitted {len(decisions)} decisions, expected exactly 1"
    )
    assert "accumulated_tokens" in decisions[0]


@pytest.mark.asyncio
async def test_legacy_helper_still_emits_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The act-half helper keeps emitting once — no double-emit, no lost emit."""
    mock_log, calls = _capturing_log()
    monkeypatch.setattr(ex, "log", mock_log)

    ctx = ExecutionContext(
        session_id="s1",
        trace_id="t1",
        user_message="hi",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    ctx.messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]

    await ex._maybe_frozen_reset(ctx)

    decisions = _decisions(calls)
    assert len(decisions) == 1
    assert "accumulated_tokens" in decisions[0]
    assert "accum_max_tokens" in decisions[0]


@pytest.mark.asyncio
async def test_gateway_path_emits_conversation_context_loaded_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-945 AC-1: the live gateway turn emits conversation_context_loaded, exactly once.

    Red before the fix: the gateway branch returns before this emit's call site — below
    ``apply_context_window``/``_maybe_frozen_reset`` — is ever reached, so zero events are
    captured.
    """
    ctx = _gateway_ctx([{"role": "user", "content": "hello"}])
    calls = await _drive_gateway_turn(monkeypatch, ctx)

    events = _events(calls, CONVERSATION_CONTEXT_LOADED)
    assert len(events) == 1, (
        f"expected exactly one {CONVERSATION_CONTEXT_LOADED}, got {len(events)}"
    )

    payload = events[0]
    assert payload["trace_id"] == "t1"
    assert payload["session_id"] == "s1"
    assert isinstance(payload["total_messages_in_db"], int)
    assert isinstance(payload["messages_loaded"], int) and payload["messages_loaded"] > 0
    assert isinstance(payload["messages_truncated"], int)
    assert isinstance(payload["estimated_tokens"], int | float) and not isinstance(
        payload["estimated_tokens"], bool
    )


@pytest.mark.asyncio
async def test_conversation_context_loaded_messages_truncated_is_zero_on_gateway_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-945: messages_truncated is a structural zero on the gateway path.

    ``step_init``'s own ``apply_context_window`` call — the only thing in this function
    that ever truncates — sits below the branch's return and never runs here. This pins
    that the field reports a real structural fact (0), not a fabricated stand-in value.
    """
    ctx = _gateway_ctx([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    calls = await _drive_gateway_turn(monkeypatch, ctx)

    payload = _events(calls, CONVERSATION_CONTEXT_LOADED)[0]
    assert payload["messages_truncated"] == 0


@pytest.mark.asyncio
async def test_conversation_context_loaded_total_vs_loaded_count_asymmetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-945: total_messages_in_db excludes this turn's message; messages_loaded includes it.

    This asymmetry pre-dates FRE-945 — it is how the legacy emit has always behaved — and
    must carry unchanged through the helper extraction.
    """
    messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    ctx = _gateway_ctx(messages)
    calls = await _drive_gateway_turn(monkeypatch, ctx)

    payload = _events(calls, CONVERSATION_CONTEXT_LOADED)[0]
    # The harness stubs session_manager.get_session to return None, so step_init never
    # counts a persisted session — total_messages_in_db stays 0 regardless of the seeded
    # ctx.messages.
    assert payload["total_messages_in_db"] == 0
    # ctx.messages holds the seeded history plus this turn's appended user message.
    assert payload["messages_loaded"] == len(messages) + 1


@pytest.mark.asyncio
async def test_enforced_expansion_subpath_also_emits_conversation_context_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-945 AC-2: the enforced-expansion sub-path emits conversation_context_loaded too.

    That sub-path returns from the middle of the gateway branch, so an emit placed just
    before the branch's final return would leave these turns silent — the same trap
    FRE-944's self-review caught for the sibling emit.
    """
    from unittest.mock import AsyncMock

    from personal_agent.orchestrator.expansion_controller import ExpansionResult

    async def _noop_progress(c: ExecutionContext) -> None:
        return None

    monkeypatch.setattr(ex.settings, "orchestration_mode", "enforced")
    monkeypatch.setattr(ex, "_report_turn_progress", _noop_progress)

    controller = MagicMock()
    controller.execute = AsyncMock(
        return_value=ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[],
            synthesis_context="SYN",
            planner_cost_usd=0.0,
        )
    )
    monkeypatch.setattr(
        "personal_agent.orchestrator.expansion_controller.ExpansionController",
        lambda: controller,
    )
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client",
        lambda role_name=None: MagicMock(),
    )

    ctx = _gateway_ctx([{"role": "user", "content": "build X and Y"}], DecompositionStrategy.HYBRID)
    calls = await _drive_gateway_turn(monkeypatch, ctx)

    events = _events(calls, CONVERSATION_CONTEXT_LOADED)
    assert len(events) == 1, (
        f"expansion sub-path emitted {len(events)} {CONVERSATION_CONTEXT_LOADED} events, expected 1"
    )


@pytest.mark.asyncio
async def test_gateway_path_conversation_context_loaded_adds_no_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-945 AC-3: adding the emit changes no behaviour.

    Reuses the FRE-944 no-compaction guard's forward-extension assertion style: the
    message list stays a strict forward extension and no highlights are stashed.
    """
    messages = [{"role": "user", "content": "hello"}]
    ctx = _gateway_ctx(messages)
    await _drive_gateway_turn(monkeypatch, ctx)

    assert ctx.messages[: len(messages)] == messages
    assert len(ctx.messages) == len(messages) + 1
    assert not ctx.salient_highlights


def test_conversation_context_loaded_helper_emits_full_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-945: the extracted helper emits the same schema the original inline call used.

    Calls the helper directly rather than driving the full legacy ``step_init`` path, which
    would pull in memory-graph queries and session-repository plumbing outside this
    ticket's scope. Proves the 2a extraction is behaviour-identical.
    """
    mock_log, calls = _capturing_log()
    monkeypatch.setattr(ex, "log", mock_log)

    ctx = ExecutionContext(
        session_id="s1",
        trace_id="t1",
        user_message="hi",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )

    ex._emit_conversation_context_loaded(
        ctx,
        total_messages_in_db=3,
        messages_loaded=4,
        messages_truncated=1,
        estimated_tokens=42,
    )

    events = _events(calls, CONVERSATION_CONTEXT_LOADED)
    assert len(events) == 1
    assert events[0] == {
        "trace_id": "t1",
        "session_id": "s1",
        "total_messages_in_db": 3,
        "messages_loaded": 4,
        "messages_truncated": 1,
        "estimated_tokens": 42,
    }
