"""Tests for the sub-agent approval channel (FRE-1461).

A sub-agent could not ask the owner anything. The primary already can, through the
ADR-0076 constraint pause, and these tests assert that the same channel now reaches
the sub-agent tool path — with an answer that survives a fan-out.

AC-1: a request reaches the owner-facing surface, and the answer changes what the
    sub-agent does — it runs the tool on approve, and does not on deny.
AC-2: a denial is a refusal, not an error. The loop continues and the turn completes.
AC-3: no answer is not an indefinite wait. The pause is bounded, and a worker with
    too little budget left is denied outright rather than asked.
AC-4: the fan-out answer. Six sub-agents wanting the same tool raise ONE prompt.
AC-5: the seeded negative. A sub-agent needing no approval raises no prompt at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.governance.models import GovernanceConfig, Mode, ToolPolicy
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.constraint_options import ConstraintDecision
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_approval import (
    APPROVE_ACTION_ID,
    DENY_ACTION_ID,
    SUB_AGENT_APPROVAL_CONSTRAINT,
    ApprovalOutcome,
    SubAgentApprovalBroker,
    reset_sub_agent_approval_broker,
    resolve_sub_agent_approval_requirements,
    set_sub_agent_approval_broker,
)
from personal_agent.orchestrator.sub_agent_types import SubAgentSpec
from personal_agent.orchestrator.types import ExecutionContext

_TRANSPORT = "personal_agent.transport.agui.transport"


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        session_id="s1",
        trace_id="t1",
        user_message="hi",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )


def _spec(tools: list[str], task: str = "test task", timeout: float = 300.0) -> SubAgentSpec:
    return SubAgentSpec(
        task=task,
        context=[{"role": "user", "content": "do the thing"}],
        output_format="text",
        max_tokens=1024,
        timeout_seconds=timeout,
        tools=tools,
    )


def _llm_response(content: str, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or [],
        "usage": {},
        "response_id": None,
        "raw": {},
    }


def _stub_tool_layer(*tool_names: str) -> MagicMock:
    layer = MagicMock()
    layer.registry.get_tool_definitions_for_llm.return_value = [
        {"type": "function", "function": {"name": n, "description": "d", "parameters": {}}}
        for n in tool_names
    ]
    return layer


def _dispatch_result(tool_call_id: str, tool_name: str, content: str) -> dict[str, Any]:
    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "content": content,
        "success": True,
        "latency_ms": 1.0,
        "output_hash": "h",
        "gate_result": None,
        "args_hash": "",
        "loop_policy": None,
        "tool_layer_output": None,
        "tool_layer_error": None,
    }


def _wants_run_python_then_answers() -> AsyncMock:
    """A client that calls run_python once, then gives a final answer."""
    client = AsyncMock()
    client.respond = AsyncMock(
        side_effect=[
            _llm_response(
                "", tool_calls=[{"id": "c0", "name": "run_python", "arguments": '{"code": "1"}'}]
            ),
            _llm_response("final answer"),
        ]
    )
    return client


@pytest.fixture
def installed_broker() -> Iterator[SubAgentApprovalBroker]:
    """A turn-scoped broker published on the carrier, torn down after the test.

    Mirrors what ``execute_task`` does for a real turn: set at turn start, reset in
    a ``finally`` so no answer outlives its turn.
    """
    broker = SubAgentApprovalBroker(_ctx())
    token = set_sub_agent_approval_broker(broker)
    try:
        yield broker
    finally:
        reset_sub_agent_approval_broker(token)


async def _approve(**_kw: object) -> ConstraintDecision:
    return ConstraintDecision(APPROVE_ACTION_ID, "user_choice")


async def _deny(**_kw: object) -> ConstraintDecision:
    return ConstraintDecision(DENY_ACTION_ID, "timeout_default")


class TestApprovalReachesTheOwnerAndTheAnswerReturns:
    """AC-1 — the request is a real pause event, and the answer changes behaviour."""

    @pytest.mark.asyncio
    async def test_request_pushes_a_constraint_pause_not_just_a_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner-facing surface is the ADR-0076 card, not a log line.

        Drives the REAL ``_maybe_pause_for_constraint`` and captures what it pushes
        to the transport, so this cannot pass on a mock of the thing under test.
        """
        pushed: list[dict[str, object]] = []

        async def fake_load(user_id: object, constraint: str, **_kw: object) -> None:
            return None

        async def fake_push(**kwargs: object) -> dict[str, str]:
            event = kwargs["event"]
            pushed.append(
                {
                    "constraint": getattr(event, "constraint", None),
                    "options": getattr(event, "options", None),
                    "default_option": getattr(event, "default_option", None),
                }
            )
            return {"decision": APPROVE_ACTION_ID, "resolution": "user_choice"}

        async def fake_emit(**_kw: object) -> None:
            return None

        monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)
        monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)
        monkeypatch.setattr(f"{_TRANSPORT}.emit_constraint_resolved", fake_emit)

        broker = SubAgentApprovalBroker(_ctx())
        outcome = await broker.decide(
            "run_python", task="crunch the numbers", worker_remaining_seconds=10_000.0
        )

        assert outcome.approved is True
        assert len(pushed) == 1
        assert pushed[0]["constraint"] == SUB_AGENT_APPROVAL_CONSTRAINT
        assert pushed[0]["default_option"] == DENY_ACTION_ID
        assert APPROVE_ACTION_ID in (pushed[0]["options"] or [])

    @pytest.mark.asyncio
    async def test_approve_lets_the_tool_run(
        self, monkeypatch: pytest.MonkeyPatch, installed_broker: SubAgentApprovalBroker
    ) -> None:
        """On approve the tool is actually dispatched — the answer reaches the worker."""
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _approve)
        dispatch = AsyncMock(return_value=_dispatch_result("c0", "run_python", "42"))

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.resolve_sub_agent_approval_requirements",
                return_value=frozenset({"run_python"}),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch),
        ):
            result = await run_sub_agent(
                spec=_spec(["run_python"]),
                llm_client=_wants_run_python_then_answers(),
                trace_id="t",
            )

        assert dispatch.call_count == 1
        assert result.tools_used == ["run_python"]
        assert result.success is True

    @pytest.mark.asyncio
    async def test_deny_stops_the_tool(
        self, monkeypatch: pytest.MonkeyPatch, installed_broker: SubAgentApprovalBroker
    ) -> None:
        """On deny the tool never runs — the paired negative of the approve case."""
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _deny)
        dispatch = AsyncMock(return_value=_dispatch_result("c0", "run_python", "42"))

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.resolve_sub_agent_approval_requirements",
                return_value=frozenset({"run_python"}),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch),
        ):
            result = await run_sub_agent(
                spec=_spec(["run_python"]),
                llm_client=_wants_run_python_then_answers(),
                trace_id="t",
            )

        assert dispatch.call_count == 0
        assert result.tools_used == []


class TestDenialIsARefusalNotAnError:
    """AC-2 — the loop continues, the turn completes, nothing raises."""

    @pytest.mark.asyncio
    async def test_denied_round_continues_to_a_final_answer(
        self, monkeypatch: pytest.MonkeyPatch, installed_broker: SubAgentApprovalBroker
    ) -> None:
        """The denied round is absorbed and the sub-agent still answers."""
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _deny)

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.resolve_sub_agent_approval_requirements",
                return_value=frozenset({"run_python"}),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c0", "run_python", "42")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec(["run_python"]),
                llm_client=_wants_run_python_then_answers(),
                trace_id="t",
            )

        assert result.success is True
        assert result.error is None
        assert result.summary == "final answer"

    @pytest.mark.asyncio
    async def test_pause_failure_becomes_a_denial(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An exception inside the pause must not escape as a turn failure.

        ``_maybe_pause_for_constraint`` does not guard its own awaits, and the
        waiter contract re-raises a registration-callback exception after cleanup.
        """

        async def _explode(**_kw: object) -> ConstraintDecision:
            raise RuntimeError("waiter registration blew up")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _explode)
        broker = SubAgentApprovalBroker(_ctx())

        outcome = await broker.decide("run_python", task="t", worker_remaining_seconds=10_000.0)

        assert outcome.approved is False
        assert "pause_failed" in outcome.reason

    @pytest.mark.asyncio
    async def test_cancellation_still_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A denial must never swallow an external dispatch cancellation.

        ``run_sub_agent`` deliberately re-raises ``CancelledError`` to preserve it;
        catching ``BaseException`` in the broker would defeat that.
        """
        import asyncio

        async def _cancel(**_kw: object) -> ConstraintDecision:
            raise asyncio.CancelledError()

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _cancel)
        broker = SubAgentApprovalBroker(_ctx())

        with pytest.raises(asyncio.CancelledError):
            await broker.decide("run_python", task="t", worker_remaining_seconds=10_000.0)


class TestTheWaitIsBounded:
    """AC-3 — no answer is not an indefinite wait, and the wait sits in the budget."""

    @pytest.mark.asyncio
    async def test_timeout_denies_and_the_worker_still_returns(
        self, monkeypatch: pytest.MonkeyPatch, installed_broker: SubAgentApprovalBroker
    ) -> None:
        """A ``timeout_default`` resolution is a denial, and the sub-agent survives it."""
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _deny)

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.resolve_sub_agent_approval_requirements",
                return_value=frozenset({"run_python"}),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c0", "run_python", "42")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec(["run_python"]),
                llm_client=_wants_run_python_then_answers(),
                trace_id="t",
            )

        assert result.tools_used == []
        assert result.success is True

    @pytest.mark.asyncio
    async def test_pause_is_skipped_when_the_worker_budget_is_too_small(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The seeded negative for the budget rule: no card is raised at all.

        A worker whose remaining budget cannot absorb the pause plus the work of
        recording the refusal must be denied outright, not asked — otherwise its own
        ``wait_for`` deadline can fire mid-pause and the refusal is never recorded.
        """
        from personal_agent.config import settings

        asked = {"count": 0}

        async def _counting_pause(**_kw: object) -> ConstraintDecision:
            asked["count"] += 1
            return ConstraintDecision(APPROVE_ACTION_ID, "user_choice")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _counting_pause)
        broker = SubAgentApprovalBroker(_ctx())

        outcome = await broker.decide(
            "run_python",
            task="t",
            worker_remaining_seconds=settings.constraint_pause_timeout_seconds,
        )

        assert asked["count"] == 0
        assert outcome.approved is False
        assert "budget" in outcome.reason

    @pytest.mark.asyncio
    async def test_a_budget_above_the_reserve_does_ask(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The paired positive — the rule must not deny everything vacuously."""
        from personal_agent.config import settings
        from personal_agent.orchestrator.sub_agent_approval import (
            APPROVAL_FINALIZATION_RESERVE_SECONDS,
        )

        asked = {"count": 0}

        async def _counting_pause(**_kw: object) -> ConstraintDecision:
            asked["count"] += 1
            return ConstraintDecision(APPROVE_ACTION_ID, "user_choice")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _counting_pause)
        broker = SubAgentApprovalBroker(_ctx())

        outcome = await broker.decide(
            "run_python",
            task="t",
            worker_remaining_seconds=(
                settings.constraint_pause_timeout_seconds
                + APPROVAL_FINALIZATION_RESERVE_SECONDS
                + 1.0
            ),
        )

        assert asked["count"] == 1
        assert outcome.approved is True


class TestFanOutRaisesOnePrompt:
    """AC-4 — the fan-out answer, tested at fan-out scale."""

    @pytest.mark.asyncio
    async def test_six_sub_agents_produce_one_prompt(
        self, monkeypatch: pytest.MonkeyPatch, installed_broker: SubAgentApprovalBroker
    ) -> None:
        """Six sub-agents in one turn, each wanting run_python: ONE card.

        A per-call design that is correct once and intolerable eight times passes a
        single-sub-agent test, so this runs the real fan-out size observed in
        production (six on 2026-09-07, eight on 2026-09-05).
        """
        asked = {"count": 0}

        async def _counting_pause(**_kw: object) -> ConstraintDecision:
            asked["count"] += 1
            return ConstraintDecision(APPROVE_ACTION_ID, "user_choice")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _counting_pause)
        dispatch = AsyncMock(return_value=_dispatch_result("c0", "run_python", "42"))

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.resolve_sub_agent_approval_requirements",
                return_value=frozenset({"run_python"}),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch),
        ):
            results = [
                await run_sub_agent(
                    spec=_spec(["run_python"], task=f"task {i}"),
                    llm_client=_wants_run_python_then_answers(),
                    trace_id="t",
                )
                for i in range(6)
            ]

        assert asked["count"] == 1, "the fan-out must raise exactly one prompt"
        # And all six honour that single answer.
        assert dispatch.call_count == 6
        assert all(r.tools_used == ["run_python"] for r in results)

    @pytest.mark.asyncio
    async def test_one_denial_is_honoured_by_every_sibling(
        self, monkeypatch: pytest.MonkeyPatch, installed_broker: SubAgentApprovalBroker
    ) -> None:
        """The same reuse must hold for a denial — not only for the happy answer."""
        asked = {"count": 0}

        async def _counting_deny(**_kw: object) -> ConstraintDecision:
            asked["count"] += 1
            return ConstraintDecision(DENY_ACTION_ID, "user_choice")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _counting_deny)
        dispatch = AsyncMock(return_value=_dispatch_result("c0", "run_python", "42"))

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.resolve_sub_agent_approval_requirements",
                return_value=frozenset({"run_python"}),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch),
        ):
            for i in range(6):
                await run_sub_agent(
                    spec=_spec(["run_python"], task=f"task {i}"),
                    llm_client=_wants_run_python_then_answers(),
                    trace_id="t",
                )

        assert asked["count"] == 1
        assert dispatch.call_count == 0

    @pytest.mark.asyncio
    async def test_two_distinct_tools_ask_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The key is the tool name, so a second tool is a second decision.

        Guards the opposite failure from AC-4's: a cache keyed on nothing at all
        would silently apply one tool's approval to a different tool.
        """
        asked: list[str] = []

        async def _record(**kwargs: object) -> ConstraintDecision:
            asked.append(str(kwargs.get("context", "")))
            return ConstraintDecision(APPROVE_ACTION_ID, "user_choice")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _record)
        broker = SubAgentApprovalBroker(_ctx())

        await broker.decide("run_python", task="t", worker_remaining_seconds=10_000.0)
        await broker.decide("run_python", task="t", worker_remaining_seconds=10_000.0)
        await broker.decide("bash", task="t", worker_remaining_seconds=10_000.0)

        assert len(asked) == 2
        assert "run_python" in asked[0]
        assert "bash" in asked[1]


class TestSeededNegative:
    """AC-5 — a sub-agent needing no approval raises no prompt across a full turn."""

    @pytest.mark.asyncio
    async def test_no_approval_needed_raises_no_prompt(
        self, monkeypatch: pytest.MonkeyPatch, installed_broker: SubAgentApprovalBroker
    ) -> None:
        """A mechanism that asks on everything is worse than the silence it replaces.

        Uses the REAL requirement resolver against the REAL governance config, where
        run_python needs no approval in NORMAL — so this is a genuine negative, not a
        stubbed one.
        """
        asked = {"count": 0}

        async def _counting_pause(**_kw: object) -> ConstraintDecision:
            asked["count"] += 1
            return ConstraintDecision(APPROVE_ACTION_ID, "user_choice")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _counting_pause)
        dispatch = AsyncMock(return_value=_dispatch_result("c0", "run_python", "42"))

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch),
        ):
            result = await run_sub_agent(
                spec=_spec(["run_python"]),
                llm_client=_wants_run_python_then_answers(),
                trace_id="t",
            )

        assert asked["count"] == 0
        assert dispatch.call_count == 1
        assert result.tools_used == ["run_python"]

    @pytest.mark.asyncio
    async def test_grantless_sub_agent_never_looks_up_governance(self) -> None:
        """No grant, no lookup — an empty request must cost nothing at all."""
        called = {"count": 0}

        def _boom(*_a: object, **_kw: object) -> object:
            called["count"] += 1
            raise AssertionError("governance must not be consulted for an empty grant")

        with patch("personal_agent.orchestrator.sub_agent_approval.load_governance_config", _boom):
            assert resolve_sub_agent_approval_requirements([], trace_id="t") == frozenset()

        assert called["count"] == 0


class TestRequirementResolverFailsClosed:
    """A governance lookup failure must not silently disable the gate."""

    def test_lookup_failure_treats_every_tool_as_approval_required(self) -> None:
        """A broken config must make the gate ask, never silently disappear."""
        from personal_agent.config.governance_loader import GovernanceConfigError

        def _boom(*_a: object, **_kw: object) -> GovernanceConfig:
            raise GovernanceConfigError("config is unreadable")

        with patch("personal_agent.orchestrator.sub_agent_approval.load_governance_config", _boom):
            required = resolve_sub_agent_approval_requirements(["run_python"], trace_id="t")

        assert required == frozenset({"run_python"})

    def test_seeded_policy_is_read_through_to_the_resolver(self) -> None:
        """The resolver reads the real predicate, not a hardcoded answer."""
        config = GovernanceConfig(
            modes={},
            tools={
                "run_python": ToolPolicy(
                    category="compute",
                    allowed_in_modes=["NORMAL"],
                    requires_approval_in_modes=["NORMAL"],
                )
            },
            sub_agent_tools=["run_python"],
            mode_constraints={},
        )
        with (
            patch(
                "personal_agent.orchestrator.sub_agent_approval.load_governance_config",
                return_value=config,
            ),
            patch(
                "personal_agent.orchestrator.sub_agent_approval.get_current_mode",
                return_value=Mode.NORMAL,
            ),
        ):
            required = resolve_sub_agent_approval_requirements(["run_python"], trace_id="t")

        assert required == frozenset({"run_python"})


class TestNoBrokerDeniesRatherThanAllows:
    """Fail closed: an approval-required tool with no approver channel is denied."""

    @pytest.mark.asyncio
    async def test_missing_broker_denies_the_call(self) -> None:
        """No approver in scope is a denial, not the pre-FRE-1461 warn-and-proceed."""
        dispatch = AsyncMock(return_value=_dispatch_result("c0", "run_python", "42"))

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.resolve_sub_agent_approval_requirements",
                return_value=frozenset({"run_python"}),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.get_sub_agent_approval_broker",
                return_value=None,
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch),
        ):
            result = await run_sub_agent(
                spec=_spec(["run_python"]),
                llm_client=_wants_run_python_then_answers(),
                trace_id="t",
            )

        assert dispatch.call_count == 0
        assert result.tools_used == []


class TestApprovalOutcomeShape:
    """A recorded answer must not be editable after the fact."""

    def test_outcome_is_immutable(self) -> None:
        """The cached outcome is frozen, so a later reader cannot flip it."""
        outcome = ApprovalOutcome(approved=True, reason="user_choice")
        with pytest.raises(Exception):  # noqa: B017 — frozen dataclass raises FrozenInstanceError
            outcome.approved = False  # type: ignore[misc]
