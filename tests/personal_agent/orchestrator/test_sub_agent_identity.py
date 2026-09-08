"""FRE-1467 — a sub-agent's TraceContext must carry the turn's identity.

Before this change ``orchestrator/sub_agent.py`` built two separate contexts,
``TraceContext(trace_id=trace_id, session_id=session_id)``, one for the
sub-agent's own inference call and one for every tool dispatch. Neither carried
``user_id``, ``authenticated`` or ``eval_mode``, so every identity-scoped tool
was unreachable to a sub-agent: ``recall_personal_history`` raised
``missing_user_id`` and ``search_memory`` fell to its fail-closed paths.

The tests assert the value observed at a boundary — what ``respond`` receives,
what ``dispatch_tool_call`` receives, what the real executor does — rather than
the shape of the wiring that produced it.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentSpec

_USER_ID = UUID("11111111-2222-3333-4444-555555555555")


def _spec(tools: list[str] | None = None) -> SubAgentSpec:
    return SubAgentSpec(
        task="test task",
        context=[{"role": "user", "content": "do the thing"}],
        output_format="text",
        max_tokens=1024,
        timeout_seconds=30.0,
        tools=tools or [],
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


def _tool_using_client() -> AsyncMock:
    """A client that calls one granted tool, then answers."""
    client = AsyncMock()
    client.respond = AsyncMock(
        side_effect=[
            _llm_response(
                "", tool_calls=[{"id": "c0", "name": "search_memory", "arguments": "{}"}]
            ),
            _llm_response("final answer"),
        ]
    )
    return client


class TestSubAgentContextCarriesIdentity:
    """AC-1 — both constructions carry the turn's user_id and authenticated."""

    @pytest.mark.asyncio
    async def test_llm_call_carries_identity(self) -> None:
        """Site 1: the sub-agent's own inference call (former sub_agent.py:570)."""
        client = AsyncMock()
        client.respond = AsyncMock(return_value="done")

        await run_sub_agent(
            spec=_spec(),
            llm_client=client,
            trace_id="trace-id",
            session_id="sess-id",
            user_id=_USER_ID,
            authenticated=True,
        )

        trace_ctx = client.respond.call_args.kwargs["trace_ctx"]
        assert trace_ctx.user_id == _USER_ID
        assert trace_ctx.authenticated is True

    @pytest.mark.asyncio
    async def test_tool_dispatch_carries_identity(self) -> None:
        """Site 2: every tool dispatch (former sub_agent.py:684).

        This is the site the ticket exists for — ``recall_personal_history`` and
        ``search_memory`` read ``ctx.user_id`` and ``ctx.authenticated`` from
        exactly this context.
        """
        dispatch_mock = AsyncMock(return_value=_dispatch_result("c0", "search_memory", "ok"))

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("search_memory"),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch_mock),
        ):
            await run_sub_agent(
                spec=_spec(["search_memory"]),
                llm_client=_tool_using_client(),
                trace_id="trace-id",
                session_id="sess-id",
                user_id=_USER_ID,
                authenticated=True,
            )

        assert dispatch_mock.call_count == 1
        trace_ctx = dispatch_mock.call_args.kwargs["trace_ctx"]
        assert trace_ctx.user_id == _USER_ID
        assert trace_ctx.authenticated is True

    @pytest.mark.asyncio
    async def test_both_boundaries_receive_the_same_context(self) -> None:
        """AC-1's failure clause, turned into an assertion.

        The AC fails a fix that threads one construction and misses the other,
        because the two are built separately and can drift. One shared object
        cannot drift, so this asserts identity of the object itself.
        """
        dispatch_mock = AsyncMock(return_value=_dispatch_result("c0", "search_memory", "ok"))
        client = _tool_using_client()

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("search_memory"),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch_mock),
        ):
            await run_sub_agent(
                spec=_spec(["search_memory"]),
                llm_client=client,
                trace_id="trace-id",
                session_id="sess-id",
                user_id=_USER_ID,
                authenticated=True,
            )

        llm_ctx = client.respond.call_args_list[0].kwargs["trace_ctx"]
        tool_ctx = dispatch_mock.call_args.kwargs["trace_ctx"]
        assert llm_ctx is tool_ctx

    @pytest.mark.asyncio
    async def test_no_identity_supplied_stays_unauthenticated(self) -> None:
        """Seeded negative: the defaults must not manufacture an identity.

        A caller that has not been updated — and every headless path — must keep
        producing a context with no user_id and authenticated False, or the
        FRE-229 filter would reveal group rows to an unauthenticated turn.
        """
        client = AsyncMock()
        client.respond = AsyncMock(return_value="done")

        await run_sub_agent(spec=_spec(), llm_client=client, trace_id="t")

        trace_ctx = client.respond.call_args.kwargs["trace_ctx"]
        assert trace_ctx.user_id is None
        assert trace_ctx.authenticated is False

    @pytest.mark.asyncio
    async def test_context_carries_eval_mode(self) -> None:
        """Fold-in: eval_mode is on the primary's context and is now on this one.

        It changes no granted tool today. It is threaded because this change is
        what makes it load-bearing — before it, a sub-agent's memory read
        returned nothing, so FRE-375 substrate routing could not matter.
        """
        dispatch_mock = AsyncMock(return_value=_dispatch_result("c0", "search_memory", "ok"))

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("search_memory"),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch_mock),
        ):
            await run_sub_agent(
                spec=_spec(["search_memory"]),
                llm_client=_tool_using_client(),
                trace_id="t",
                eval_mode=True,
            )

        assert dispatch_mock.call_args.kwargs["trace_ctx"].eval_mode is True


class TestRecallPersonalHistoryIsReachable:
    """AC-2, the half decidable from this seat.

    The live half — the tool returning turns inside a real turn — is a
    post-deploy step, because build never deploys. What is decidable here is
    that the identity guard no longer fires on the context ``run_sub_agent``
    now builds.
    """

    @pytest.mark.asyncio
    async def test_real_executor_passes_the_identity_guard(self) -> None:
        """The real executor, not a mock, on the context a sub-agent really gets.

        The context is captured from a real ``run_sub_agent`` dispatch rather
        than hand-built, so this cannot pass while the threading is broken.

        Before this change the executor raised ``missing_user_id — this is a
        bug; report it (FRE-343)``. After it, execution reaches the memory
        service, which is absent in a unit test — a different error, and that
        difference is the proof the guard was passed.
        """
        from personal_agent.tools.executor import ToolExecutionError
        from personal_agent.tools.personal_history import recall_personal_history_executor

        dispatch_mock = AsyncMock(
            return_value=_dispatch_result("c0", "recall_personal_history", "ok")
        )
        client = AsyncMock()
        client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "",
                    tool_calls=[{"id": "c0", "name": "recall_personal_history", "arguments": "{}"}],
                ),
                _llm_response("final answer"),
            ]
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("recall_personal_history"),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch_mock),
        ):
            await run_sub_agent(
                spec=_spec(["recall_personal_history"]),
                llm_client=client,
                trace_id="t",
                session_id="s",
                user_id=_USER_ID,
                authenticated=True,
            )

        sub_agent_ctx = dispatch_mock.call_args.kwargs["trace_ctx"]

        with pytest.raises(ToolExecutionError) as excinfo:
            await recall_personal_history_executor(days_ago=7, ctx=sub_agent_ctx)

        assert "missing_user_id" not in str(excinfo.value)
        assert "Memory service unavailable" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_the_guard_still_fires_without_identity(self) -> None:
        """Seeded negative: the guard itself is untouched.

        Without this, the test above cannot distinguish "identity threaded" from
        "guard deleted".
        """
        from personal_agent.telemetry.trace import TraceContext
        from personal_agent.tools.executor import ToolExecutionError
        from personal_agent.tools.personal_history import recall_personal_history_executor

        ctx = TraceContext(trace_id="t", session_id="s")

        with pytest.raises(ToolExecutionError) as excinfo:
            await recall_personal_history_executor(days_ago=7, ctx=ctx)

        assert "missing_user_id" in str(excinfo.value)


class TestExpansionThreadsIdentityToEveryWorker:
    """AC-1 — both worker call sites in the dispatch phase, not just the first."""

    @staticmethod
    def _plan_with_one_task() -> Any:
        from personal_agent.orchestrator.expansion_types import ExpansionPlan, PlanTask

        return ExpansionPlan(strategy="HYBRID", tasks=[PlanTask(name="t1", goal="do a thing")])

    @pytest.mark.asyncio
    async def test_execute_threads_identity_into_dispatch(self) -> None:
        """``ExpansionController.execute`` → ``run_sub_agent``."""
        from personal_agent.orchestrator.expansion_controller import ExpansionController
        from personal_agent.orchestrator.sub_agent_types import SubAgentResult

        run_mock = AsyncMock(
            return_value=SubAgentResult(
                task_id=uuid4(),
                spec_task="t1",
                summary="s",
                full_output="s",
                tools_used=[],
                token_count=1,
                duration_ms=1.0,
                success=True,
            )
        )
        controller = ExpansionController()

        with (
            patch.object(
                ExpansionController,
                "_run_planner",
                AsyncMock(return_value=self._plan_with_one_task()),
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                run_mock,
            ),
        ):
            await controller.execute(
                query="q",
                strategy="HYBRID",
                llm_client=AsyncMock(),
                trace_id="t",
                messages=[{"role": "user", "content": "q"}],
                session_id="s",
                user_id=_USER_ID,
                authenticated=True,
            )

        assert run_mock.call_count == 1
        assert run_mock.call_args.kwargs["user_id"] == _USER_ID
        assert run_mock.call_args.kwargs["authenticated"] is True

    @pytest.mark.asyncio
    async def test_redispatch_on_gap_forwards_identity(self) -> None:
        """The second worker call — a replacement dispatch — takes the same identity.

        Driven through a real ``_run_dispatch``: the first worker states a tool
        gap, governance grants the named tool, and the replacement dispatch is
        the call this asserts on. AC-1's failure clause is precisely a fix that
        threads the first call and misses this one.
        """
        from personal_agent.orchestrator import expansion_controller as ec
        from personal_agent.orchestrator.expansion_controller import (
            ExpansionController,
            ExpansionResult,
        )
        from personal_agent.orchestrator.sub_agent_types import SubAgentResult

        calls: list[dict[str, Any]] = []

        async def _dispatch(**kwargs: Any) -> SubAgentResult:
            calls.append(kwargs)
            gap = None if "retry" in kwargs["spec"].task else "search_memory"
            return SubAgentResult(
                task_id=uuid4(),
                spec_task="t1",
                summary="s",
                full_output="s",
                tools_used=[],
                token_count=1,
                duration_ms=1.0,
                success=True,
                stated_tool_gap=gap,
            )

        with (
            patch.object(ec, "get_current_mode", lambda: Mode.NORMAL),
            patch.object(
                ec,
                "get_shared_tool_execution_layer",
                lambda: _stub_tool_layer("search_memory"),
            ),
            patch.object(ec, "run_sub_agent", _dispatch),
        ):
            await ExpansionController()._run_dispatch(
                plan=self._plan_with_one_task(),
                llm_client=AsyncMock(),
                trace_id="t",
                messages=[],
                result=ExpansionResult(),
                user_id=_USER_ID,
                authenticated=True,
            )

        assert len(calls) == 2, "the stated gap did not produce a replacement dispatch"
        assert calls[1]["user_id"] == _USER_ID
        assert calls[1]["authenticated"] is True

    @pytest.mark.asyncio
    async def test_planner_call_carries_identity(self) -> None:
        """The planner's own context answers "whose turn is this?" the same way.

        The planner calls no tool, so no read widens here. It is asserted so the
        expansion path has one answer rather than two.
        """
        from personal_agent.orchestrator.expansion_controller import (
            ExpansionController,
            ExpansionResult,
        )

        client = AsyncMock()
        client.respond = AsyncMock(return_value={"content": "{}", "cost_usd": 0.0})

        await ExpansionController()._run_planner(
            query="q",
            strategy="HYBRID",
            llm_client=client,
            trace_id="t",
            timeout_s=5.0,
            result=ExpansionResult(),
            session_id="s",
            user_id=_USER_ID,
            authenticated=True,
            eval_mode=True,
        )

        trace_ctx = client.respond.call_args.kwargs["trace_ctx"]
        assert trace_ctx.user_id == _USER_ID
        assert trace_ctx.authenticated is True
        assert trace_ctx.eval_mode is True

    def test_executor_passes_identity_to_expansion(self) -> None:
        """``executor.py`` is where the identity enters the expansion path at all.

        Scoped to the ``controller.execute`` call itself. A module-wide search
        would pass vacuously: ``executor.py`` already builds the *primary's* own
        context from ``ctx.user_id`` and ``ctx.authenticated`` at ``run_task``,
        which says nothing about the expansion call.
        """
        from personal_agent.orchestrator import executor as executor_module

        src = inspect.getsource(executor_module)
        marker = "controller.execute("
        start = src.find(marker)
        assert start != -1, "executor.py no longer calls controller.execute"
        call_src = src[start : src.find(")", src.find("turn_deadline_monotonic", start))]

        assert "user_id=ctx.user_id" in call_src, (
            "the controller.execute call does not pass ctx.user_id — "
            "recall_personal_history stays unreachable to every sub-agent"
        )
        assert "authenticated=ctx.authenticated" in call_src, (
            "the controller.execute call does not pass ctx.authenticated — "
            "every sub-agent memory read stays fail-closed"
        )


class TestGrantSetMatchesTheNewReads:
    """AC-3 / AC-4 — the recorded decisions answer the reads this change opens."""

    def test_recall_personal_history_is_granted(self) -> None:
        """AC-4: FRE-1463 refused it because it could not work. It can now."""
        from personal_agent.config.governance_loader import load_governance_config

        config = load_governance_config()
        assert "recall_personal_history" in config.granted_sub_agent_tool_names()

    def test_no_recorded_reason_still_claims_the_sub_agent_has_no_identity(self) -> None:
        """AC-3: the justification this change falsifies must not survive it.

        ``search_memory`` was granted *because* the sub-agent carried no
        ``user_id``. That sentence is now false, and a grant resting on it is a
        grant resting on nothing.
        """
        from personal_agent.config.governance_loader import load_governance_config

        config = load_governance_config()
        stale = [
            name
            for name, decision in config.sub_agent_tools.items()
            if "carries no user_id" in decision.reason or "never carries one" in decision.reason
        ]
        assert stale == [], (
            f"{stale} still justify a decision by the missing identity FRE-1467 threads"
        )
