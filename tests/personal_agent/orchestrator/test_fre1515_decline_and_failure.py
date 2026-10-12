"""FRE-1515 AC-2, AC-3, AC-4 (ADR-0154 D2, D3) — the planner may decline, and a failure never expands.

On a ``planner_asked`` turn the executor passes ``planner_may_decline``. The planner then sees
the decline rule, and its schema admits ``SINGLE``:

- ``SINGLE`` with no tasks is a decline: no worker, no synthesis message, back to ``LLM_CALL``.
- ``SINGLE`` with tasks, or anything that is not ``HYBRID``/``DECOMPOSE`` with a valid task,
  is a failure. A failure (also a timeout, an exception, or ``input_too_large``) returns to
  ``LLM_CALL`` the same way, and the fallback planner does not run.
- A ``DECOMPOSE`` answer runs as ``HYBRID`` under min(3, ``expansion_budget``).

Without the flag, the fallback planner keeps its role.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.expansion_controller import (
    _PLANNER_DECLINE_RULE,
    ExpansionController,
    ExpansionResult,
)
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec
from personal_agent.orchestrator.types import ExecutionContext, PlannerRunRecord, TaskState
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

QUERY = "Tell me about the weather today"
DECLINE = json.dumps({"strategy": "SINGLE", "tasks": []})


def _plan(strategy: str, tasks: int) -> str:
    return json.dumps(
        {
            "strategy": strategy,
            "tasks": [
                {"name": f"task_{i}", "goal": f"Goal {i}", "type": "general"} for i in range(tasks)
            ],
        }
    )


def _client(content: str = "", *, provider: str | None = None, **kw: Any) -> MagicMock:
    client = MagicMock()
    client.respond = AsyncMock(return_value={"content": content, "cost_usd": 0.0}, **kw)
    client.provider = provider
    client.model_key = "planner-test"
    client.model_def = None
    return client


def _sub_result(spec: SubAgentSpec, **_: Any) -> SubAgentResult:
    return SubAgentResult(
        task_id=uuid4(),
        spec_task=spec.task,
        summary="done",
        full_output="done",
        tools_used=[],
        token_count=1,
        duration_ms=1,
        success=True,
        cost_usd=0.0,
    )


class _Harness:
    """Counts what the controller did: workers, degradation reports and planner events."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.workers = AsyncMock(side_effect=_sub_result)
        self.degradations = AsyncMock()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.runs: list[PlannerRunRecord] = []
        mod = "personal_agent.orchestrator.expansion_controller"
        monkeypatch.setattr(f"{mod}.run_sub_agent", self.workers)
        monkeypatch.setattr(f"{mod}.report_degradation", self.degradations)
        real_logger = __import__(mod, fromlist=["logger"]).logger

        def _capture(level: str) -> Any:
            original = getattr(real_logger, level)

            def _inner(event: str, **payload: Any) -> Any:
                self.events.append((event, payload))
                return original(event, **payload)

            return _inner

        for level in ("info", "warning"):
            monkeypatch.setattr(real_logger, level, _capture(level))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [p for e, p in self.events if e == event]

    async def run(
        self,
        client: MagicMock,
        *,
        may_decline: bool,
        strategy: str = "HYBRID",
        budget: int | None = 3,
        query: str = QUERY,
    ) -> ExpansionResult:
        return await ExpansionController().execute(
            query=query,
            strategy=strategy,
            llm_client=MagicMock(),
            planner_llm_client=client,
            trace_id="t-fre1515",
            messages=[{"role": "user", "content": query}],
            session_id="s-fre1515",
            expansion_budget=budget,
            planner_may_decline=may_decline,
            on_planner_run=self.runs.append,
        )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(monkeypatch)


class TestAC2ADeclineIsHonoured:
    """A ``SINGLE`` answer with no tasks dispatches nothing and reports nothing."""

    @pytest.mark.asyncio
    async def test_a_decline_dispatches_no_worker(self, harness: _Harness) -> None:
        result = await harness.run(_client(DECLINE), may_decline=True)

        harness.workers.assert_not_called()
        assert result.plan is None
        assert result.sub_agent_results == []
        assert result.synthesis_context == ""
        harness.degradations.assert_not_called()
        assert not harness.named("fallback_planner_used")
        assert [r.decision for r in harness.runs] == ["declined"]
        assert harness.runs[0].is_fallback is False
        outcomes = harness.named("planner_outcome")
        assert len(outcomes) == 1
        assert outcomes[0]["planner_decision"] == "declined"
        assert outcomes[0]["plan_task_count"] == 0

    @pytest.mark.asyncio
    async def test_a_decline_with_no_tasks_key_is_a_decline(self, harness: _Harness) -> None:
        await harness.run(_client(json.dumps({"strategy": "SINGLE"})), may_decline=True)
        assert [r.decision for r in harness.runs] == ["declined"]

    @pytest.mark.asyncio
    async def test_the_planner_is_offered_the_decline(self, harness: _Harness) -> None:
        client = _client(DECLINE, provider="anthropic")
        await harness.run(client, may_decline=True)

        kwargs = client.respond.call_args.kwargs
        assert _PLANNER_DECLINE_RULE in kwargs["messages"][0]["content"]
        schema = kwargs["response_format"]["json_schema"]["schema"]
        assert "SINGLE" in schema["properties"]["strategy"]["enum"]

    @pytest.mark.asyncio
    async def test_without_the_flag_no_decline_is_offered(self, harness: _Harness) -> None:
        client = _client(_plan("HYBRID", 1), provider="anthropic")
        await harness.run(client, may_decline=False)

        kwargs = client.respond.call_args.kwargs
        assert _PLANNER_DECLINE_RULE not in kwargs["messages"][0]["content"]
        schema = kwargs["response_format"]["json_schema"]["schema"]
        assert "SINGLE" not in schema["properties"]["strategy"]["enum"]


def _timeout_client() -> MagicMock:
    client = _client()
    client.respond = AsyncMock(side_effect=asyncio.TimeoutError())
    return client


def _raising_client() -> MagicMock:
    client = _client()
    client.respond = AsyncMock(side_effect=RuntimeError("engine down"))
    return client


class TestAC3AFailureDoesNotExpand:
    """Every planner failure on a ``planner_asked`` turn returns to the tool loop."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("make_client", "reason"),
        [
            (lambda: _client("this is not json"), "invalid"),
            (lambda: _client(_plan("SINGLE", 2)), "invalid"),
            (lambda: _client(_plan("PARALLEL", 2)), "invalid"),
            (
                lambda: _client(
                    json.dumps({"tasks": [{"name": "a", "goal": "b", "type": "general"}]})
                ),
                "invalid",
            ),
            (lambda: _client(_plan("HYBRID", 0)), "invalid"),
            (_timeout_client, "timeout"),
            (_raising_client, "exception"),
        ],
    )
    async def test_a_failure_dispatches_nothing_and_skips_the_fallback(
        self, harness: _Harness, make_client: Any, reason: str
    ) -> None:
        result = await harness.run(make_client(), may_decline=True)

        harness.workers.assert_not_called()
        assert result.plan is None
        assert not harness.named("fallback_planner_used")
        assert [(r.decision, r.failure_reason, r.is_fallback) for r in harness.runs] == [
            ("failed", reason, False)
        ]
        outcomes = harness.named("planner_outcome")
        assert len(outcomes) == 1
        assert outcomes[0]["planner_decision"] == "failed"
        assert outcomes[0]["planner_failure_reason"] == reason
        assert outcomes[0]["is_fallback"] is False
        harness.degradations.assert_awaited_once()
        assert harness.degradations.call_args.kwargs["severity"] == "warning"

    @pytest.mark.asyncio
    async def test_input_too_large_dispatches_nothing(
        self, harness: _Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "planner_input_max_chars", 50)
        client = _client(DECLINE)

        result = await harness.run(client, may_decline=True, query="x" * 500)

        client.respond.assert_not_called()  # the model was not called (ADR-0154 D1)
        harness.workers.assert_not_called()
        assert result.plan is None
        assert not harness.named("fallback_planner_used")
        assert [(r.decision, r.failure_reason) for r in harness.runs] == [
            ("failed", "input_too_large")
        ]
        assert len(harness.named("planner_outcome")) == 1

    @pytest.mark.asyncio
    async def test_without_the_flag_the_fallback_planner_still_runs(
        self, harness: _Harness
    ) -> None:
        result = await harness.run(_client("this is not json"), may_decline=False)

        assert len(harness.named("fallback_planner_used")) == 1
        assert result.plan is not None and result.plan.is_fallback
        assert harness.workers.await_count == len(result.plan.tasks) > 0
        assert [(r.decision, r.failure_reason, r.is_fallback) for r in harness.runs] == [
            ("failed", "invalid", True)
        ]
        assert len(harness.named("planner_outcome")) == 1

    @pytest.mark.asyncio
    async def test_without_the_flag_a_single_answer_is_still_a_failure(
        self, harness: _Harness
    ) -> None:
        """Today's behaviour off ``planner_asked``: no decline exists, so the fallback runs."""
        await harness.run(_client(DECLINE), may_decline=False)
        assert len(harness.named("fallback_planner_used")) == 1


class TestAC4DecomposeRunsAsHybridUnderTheCap:
    """D3: at most min(3, ``expansion_budget``) workers, and the plan records ``HYBRID``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("budget", "workers"), [(2, 2), (5, 3), (None, 3)])
    async def test_five_decompose_tasks_run_as_hybrid(
        self, harness: _Harness, budget: int | None, workers: int
    ) -> None:
        result = await harness.run(_client(_plan("DECOMPOSE", 5)), may_decline=True, budget=budget)

        assert harness.workers.await_count == workers
        assert result.plan is not None
        assert result.plan.strategy == "HYBRID"
        assert len(result.plan.tasks) == workers
        assert [r.decision for r in harness.runs] == ["expanded"]
        outcomes = harness.named("planner_outcome")
        assert len(outcomes) == 1
        assert outcomes[0]["plan_task_count"] == workers
        assert "(strategy: HYBRID)" in result.synthesis_context


# ── The executor: the same paths through step_init ──────────────────────────


def _gateway(
    *,
    strategy: DecompositionStrategy,
    reason: str,
    budget: int = 3,
    task_type: TaskType = TaskType.CONVERSATIONAL,
    gate_reason: Any = None,
) -> GatewayOutput:
    return GatewayOutput(
        intent=IntentResult(
            task_type=task_type, complexity=Complexity.SIMPLE, confidence=0.9, signals=[]
        ),
        governance=GovernanceContext(
            mode=Mode.NORMAL, expansion_permitted=True, expansion_budget=budget
        ),
        decomposition=DecompositionResult(
            strategy=strategy, reason=reason, planner_gate_reason=gate_reason
        ),
        context=AssembledContext(
            messages=[{"role": "user", "content": QUERY}],
            memory_context=None,
            tool_definitions=None,
        ),
        session_id="s1",
        trace_id="t1",
    )


def _session_manager() -> MagicMock:
    session = MagicMock()
    session.messages = []
    manager = MagicMock()
    manager.get_session = MagicMock(return_value=session)
    return manager


async def _step_init(
    monkeypatch: pytest.MonkeyPatch, gw: GatewayOutput, planner: MagicMock
) -> tuple[ExecutionContext, TaskState]:
    async def _no_progress(_ctx: ExecutionContext) -> None:
        return None

    monkeypatch.setattr(ex, "_report_turn_progress", _no_progress)
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client",
        lambda role_name=None, mode=None: planner if mode == "planner" else MagicMock(),
    )
    ctx = ExecutionContext(
        session_id="s1",
        trace_id="t1",
        user_message=QUERY,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        gateway_output=gw,
    )
    state = await ex.step_init(
        ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
    )
    return ctx, state


_ASKED = {"strategy": DecompositionStrategy.HYBRID, "reason": "planner_asked"}


class TestTheExecutorReturnsToTheToolLoop:
    """AC-2 and AC-3 through ``step_init``: the next state, the messages and the record."""

    @pytest.mark.asyncio
    async def test_a_decline_goes_to_llm_call_with_no_synthesis(
        self, harness: _Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, state = await _step_init(monkeypatch, _gateway(**_ASKED), _client(DECLINE))

        assert state == TaskState.LLM_CALL
        harness.workers.assert_not_called()
        assert ctx.synthesis_appended is False
        assert [m["role"] for m in ctx.messages] == ["user"]
        assert ctx.messages[-1]["content"] == QUERY
        assert ctx.expansion_strategy is None
        assert ctx.expansion_plan is None
        assert ctx.sub_agents_dispatched == 0
        assert ctx.planner_run is not None and ctx.planner_run.decision == "declined"

    @pytest.mark.asyncio
    async def test_a_failure_goes_to_llm_call_without_the_fallback(
        self, harness: _Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, state = await _step_init(monkeypatch, _gateway(**_ASKED), _client("not json"))

        assert state == TaskState.LLM_CALL
        harness.workers.assert_not_called()
        assert not harness.named("fallback_planner_used")
        assert ctx.synthesis_appended is False
        assert [m["role"] for m in ctx.messages] == ["user"]
        assert ctx.expansion_strategy is None
        assert ctx.planner_run is not None
        assert (ctx.planner_run.decision, ctx.planner_run.failure_reason) == ("failed", "invalid")

    @pytest.mark.asyncio
    async def test_another_hybrid_reason_keeps_the_fallback(
        self, harness: _Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``planner_may_decline`` is true exactly when the reason is ``planner_asked``."""
        gw = _gateway(
            strategy=DecompositionStrategy.HYBRID,
            reason="analysis_moderate_hybrid",
            task_type=TaskType.ANALYSIS,
        )
        ctx, state = await _step_init(monkeypatch, gw, _client("not json"))

        assert state == TaskState.LLM_CALL
        assert len(harness.named("fallback_planner_used")) == 1
        assert harness.workers.await_count > 0
        assert ctx.synthesis_appended is True
        # A fallback plan dispatched workers, so the turn's expansion label stands.
        assert ctx.expansion_strategy == "hybrid"
        assert ctx.planner_run is not None and ctx.planner_run.is_fallback is True

    @pytest.mark.asyncio
    async def test_decompose_runs_as_hybrid_under_the_budget(
        self, harness: _Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, _ = await _step_init(
            monkeypatch, _gateway(**_ASKED, budget=2), _client(_plan("DECOMPOSE", 5))
        )

        assert harness.workers.await_count == 2
        assert ctx.sub_agents_dispatched == 2
        assert ctx.expansion_plan is not None and ctx.expansion_plan.strategy == "HYBRID"
        assert ctx.expansion_strategy == "hybrid"
        assert ctx.planner_run is not None and ctx.planner_run.decision == "expanded"


class TestTheExecutorStampsTheGateReason:
    """D6: ``planner_gate_reason`` reaches the context, on a forced turn too."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["conversational_always_single", "expansion_denied"])
    async def test_the_gate_reason_is_stamped(
        self, monkeypatch: pytest.MonkeyPatch, reason: str
    ) -> None:
        gw = _gateway(
            strategy=DecompositionStrategy.SINGLE, reason=reason, gate_reason="planner_mode_absent"
        )
        ctx, state = await _step_init(monkeypatch, gw, _client(DECLINE))

        assert state == TaskState.LLM_CALL
        assert ctx.planner_gate_reason == "planner_mode_absent"
        assert ctx.planner_run is None

    @pytest.mark.asyncio
    async def test_no_gate_reason_on_a_planner_turn(
        self, harness: _Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, _ = await _step_init(monkeypatch, _gateway(**_ASKED), _client(DECLINE))
        assert ctx.planner_gate_reason is None
