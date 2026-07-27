"""The turn evidence record is built at the real admission point (FRE-1004).

ADR-0125 AC-3 defines the admission point as the final serialized model input. These
tests pin the two things that make that claim true rather than approximately true: the
executor's manifest is built from the same wire form the LLM clients dispatch, and the
record describes exactly one primary call even when the turn makes several.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.captains_log.turn_evidence import DropReason, EvidenceState


def _make_ctx(**overrides: object) -> object:
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    kwargs: dict[str, object] = {
        "session_id": "test-session",
        "trace_id": "test-trace",
        "user_message": "hello",
        "mode": Mode.NORMAL,
        "channel": Channel.CHAT,
        "messages": [{"role": "user", "content": "hello"}],
    }
    kwargs.update(overrides)
    return ExecutionContext(**kwargs)  # type: ignore[arg-type]


def _mock_llm() -> MagicMock:
    client = MagicMock()
    client.respond = AsyncMock(
        return_value={
            "content": "I understand.",
            "tool_calls": [],
            "response_id": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    client.model_configs = {}
    return client


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals():
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


class TestWireFormSyncGuard:
    """The executor's manifest input must equal what the clients actually dispatch.

    Both clients prepend the system prompt then call ``sanitise_messages``. If either
    diverges from ``build_wire_messages``, the evidence record starts describing
    messages that never reached the provider — silently. This guard fails CI instead.
    """

    def _client_wire(self, messages: list[dict], system_prompt: str) -> list[dict]:
        """Reproduce the clients' pre-flight literally, as written in their source."""
        from personal_agent.llm_client.history_sanitiser import sanitise_messages

        request_messages = list(messages)
        if system_prompt:
            request_messages.insert(0, {"role": "system", "content": system_prompt})
        return sanitise_messages(request_messages, trace_id="t")[0]

    def test_matches_the_clients_on_a_clean_history(self) -> None:
        from personal_agent.orchestrator.executor import build_wire_messages

        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        assert build_wire_messages(messages, "sys", "t") == self._client_wire(messages, "sys")

    def test_matches_the_clients_on_an_orphaned_tool_call(self) -> None:
        """The lossy path: sanitisation drops an orphan, so the two must still agree."""
        from personal_agent.orchestrator.executor import build_wire_messages

        messages = [
            {"role": "user", "content": "q1"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ],
            },
            {"role": "user", "content": "q2"},
        ]
        expected = self._client_wire(messages, "sys")
        actual = build_wire_messages(messages, "sys", "t")

        assert actual == expected
        # Guard against a vacuous pass: sanitisation must actually have changed something.
        assert actual != [{"role": "system", "content": "sys"}, *messages]

    def test_system_prompt_is_prepended_before_sanitising(self) -> None:
        from personal_agent.orchestrator.executor import build_wire_messages

        wire = build_wire_messages([{"role": "user", "content": "q"}], "sys", "t")
        assert wire[0] == {"role": "system", "content": "sys"}

    def test_no_system_prompt_prepends_nothing(self) -> None:
        from personal_agent.orchestrator.executor import build_wire_messages

        wire = build_wire_messages([{"role": "user", "content": "q"}], "", "t")
        assert all(m["role"] != "system" for m in wire)


class TestEvidenceReachesTheCapture:
    """End to end through ``step_llm_call``: the record lands, and it names one call."""

    async def _run(self, ctx: object, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        from personal_agent.config import settings
        from personal_agent.telemetry.trace import TraceContext

        monkeypatch.setattr(settings, "prefer_primitives_enabled", False)

        client = _mock_llm()
        session = MagicMock()
        session.add_message = AsyncMock()
        session.get_messages = AsyncMock(return_value=[])

        with (
            patch("personal_agent.llm_client.factory.get_llm_client", return_value=client),
            patch(
                "personal_agent.orchestrator.executor.get_default_registry",
                return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
            ),
        ):
            from personal_agent.orchestrator.executor import step_llm_call

            await step_llm_call(ctx, session, TraceContext.new_trace())  # type: ignore[arg-type]
        return client

    @pytest.mark.asyncio
    async def test_admitted_memory_is_recorded_by_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.captains_log.turn_evidence import build_recall_candidates

        memory = [
            {
                "type": "entity",
                "name": "Paris",
                "entity_type": "LOCATION",
                "description": "capital",
            },
            {"type": "entity", "name": "Ghost", "entity_type": "PERSON", "description": ""},
        ]
        ctx = _make_ctx(
            memory_context=memory, recall_candidates=build_recall_candidates(memory, {})
        )
        await self._run(ctx, monkeypatch)

        evidence = ctx.turn_evidence  # type: ignore[attr-defined]
        assert evidence is not None
        assert [i.identity for i in evidence.recall.items if i.admitted] == ["Paris"]
        ghost = next(i for i in evidence.recall.items if i.identity == "Ghost")
        assert ghost.drop_reason is DropReason.NOT_RENDERED
        assert evidence.assembled_context.memory_identities == ["Paris"]
        assert evidence.primary_call_index == 0

    @pytest.mark.asyncio
    async def test_record_is_written_once_and_describes_call_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second primary call must not overwrite the assembly call's record."""
        from personal_agent.captains_log.turn_evidence import build_recall_candidates

        memory = [
            {"type": "entity", "name": "Paris", "entity_type": "LOCATION", "description": "capital"}
        ]
        ctx = _make_ctx(
            memory_context=memory, recall_candidates=build_recall_candidates(memory, {})
        )
        await self._run(ctx, monkeypatch)
        first = ctx.turn_evidence  # type: ignore[attr-defined]

        ctx.tool_iteration_count = 2  # type: ignore[attr-defined]
        await self._run(ctx, monkeypatch)

        assert ctx.turn_evidence is first  # type: ignore[attr-defined]
        assert ctx.turn_evidence.primary_call_index == 0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_turn_with_no_recall_records_empty_not_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_ctx()
        await self._run(ctx, monkeypatch)

        evidence = ctx.turn_evidence  # type: ignore[attr-defined]
        assert evidence is not None
        assert evidence.recall.state is EvidenceState.EMPTY
        assert evidence.recall.items == []
        assert evidence.assembled_context.state is EvidenceState.PRESENT

    @pytest.mark.asyncio
    async def test_assembled_context_names_the_wire_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_ctx(
            messages=[
                {"role": "user", "content": "older", "trace_id": "t-prev"},
                {"role": "assistant", "content": "older answer", "trace_id": "t-prev"},
                {"role": "user", "content": "hello", "trace_id": "t-cur"},
            ]
        )
        client = await self._run(ctx, monkeypatch)

        kwargs = client.respond.call_args.kwargs
        sent_messages = kwargs["messages"]
        slice_ = ctx.turn_evidence.assembled_context.conversation_slice  # type: ignore[attr-defined]

        # The record covers the *wire* form, which carries the system prompt the client
        # prepends — so it holds one more message than the executor handed over, but
        # only when there is a system prompt to prepend.
        expected = len(sent_messages) + (1 if kwargs.get("system_prompt") else 0)
        assert len(slice_) == expected
        assert "t-prev" in {m.origin_trace_id for m in slice_}
        assert "t-cur" in {m.origin_trace_id for m in slice_}


class TestStepInitThreadsCandidates:
    """The real stage must hand Stage 6/7's candidates to the execution context.

    ``build_turn_evidence`` is only as good as its input: if ``step_init`` stopped
    populating ``ctx.recall_candidates``, every turn would silently record an empty
    recall set and every unit test above would stay green. This drives the live stage.
    """

    def _drive(self, monkeypatch: pytest.MonkeyPatch, gw_context):
        from unittest.mock import MagicMock as MM

        from personal_agent.governance.models import Mode
        from personal_agent.orchestrator import executor as ex
        from personal_agent.orchestrator.channels import Channel
        from personal_agent.orchestrator.types import ExecutionContext
        from personal_agent.request_gateway.types import (
            Complexity,
            DecompositionResult,
            DecompositionStrategy,
            GatewayOutput,
            GovernanceContext,
            IntentResult,
            TaskType,
        )
        from personal_agent.telemetry.trace import TraceContext

        async def _noop(*a, **k):
            return None

        for name in (
            "_maybe_reinject_pending_cloud_attachment",
            "_maybe_reinject_pending_document_continuation",
            "_maybe_resolve_artifact_builder",
        ):
            monkeypatch.setattr(ex, name, _noop)

        ctx = ExecutionContext(
            session_id="s1",
            trace_id="t1",
            user_message="hello",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            gateway_output=GatewayOutput(
                intent=IntentResult(
                    task_type=TaskType.CONVERSATIONAL,
                    complexity=Complexity.SIMPLE,
                    confidence=0.9,
                    signals=[],
                ),
                governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
                decomposition=DecompositionResult(
                    strategy=DecompositionStrategy.SINGLE, reason="t", constraints={}
                ),
                context=gw_context,
                session_id="s1",
                trace_id="t1",
            ),
        )
        ctx.messages = list(gw_context.messages)
        session_manager = MM()
        session_manager.get_session = MM(return_value=None)

        return ctx, session_manager, TraceContext(trace_id="t1", session_id="s1")

    @pytest.mark.asyncio
    async def test_candidates_reach_the_context_even_when_budget_dropped_memory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The case that matters: memory_context is None, candidates must survive."""
        from personal_agent.captains_log.turn_evidence import build_recall_candidates
        from personal_agent.orchestrator import executor as ex
        from personal_agent.request_gateway.types import AssembledContext

        memory = [{"type": "entity", "name": "Paris", "description": "capital"}]
        gw_context = AssembledContext(
            messages=[{"role": "user", "content": "hello"}],
            memory_context=None,  # Stage 7 dropped it
            tool_definitions=None,
            recall_candidates=build_recall_candidates(memory, {"Paris": 0.8}),
            trimmed=True,
            overflow_action="dropped_memory_context",
        )
        ctx, session_manager, trace_ctx = self._drive(monkeypatch, gw_context)

        await ex.step_init(ctx, session_manager, trace_ctx)

        assert [c.identity for c in ctx.recall_candidates] == ["Paris"]
        assert ctx.memory_context is None

    @pytest.mark.asyncio
    async def test_candidates_reach_the_context_on_the_ordinary_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.captains_log.turn_evidence import build_recall_candidates
        from personal_agent.orchestrator import executor as ex
        from personal_agent.request_gateway.types import AssembledContext

        memory = [{"type": "entity", "name": "Paris", "description": "capital"}]
        gw_context = AssembledContext(
            messages=[{"role": "user", "content": "hello"}],
            memory_context=memory,
            tool_definitions=None,
            recall_candidates=build_recall_candidates(memory, {"Paris": 0.8}),
        )
        ctx, session_manager, trace_ctx = self._drive(monkeypatch, gw_context)

        await ex.step_init(ctx, session_manager, trace_ctx)

        assert [c.identity for c in ctx.recall_candidates] == ["Paris"]
        assert ctx.recall_candidates[0].score == pytest.approx(0.8)


class TestWireFormObservationIsSilent:
    """Deriving the wire form must not inflate the history_sanitised series.

    That event is documented as counting real-world dispatch occurrence rates, and the
    real dispatch sanitises again inside the client. Emitting from the observation too
    would double the count on exactly the turns that reach the admission point.
    """

    def test_build_wire_messages_emits_no_history_sanitised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.llm_client import history_sanitiser as hs
        from personal_agent.orchestrator.executor import build_wire_messages

        events: list[str] = []
        mock_log = MagicMock()
        mock_log.info.side_effect = lambda event, **kw: events.append(event)
        mock_log.debug.side_effect = lambda event, **kw: events.append(event)
        monkeypatch.setattr(hs, "log", mock_log)

        build_wire_messages([{"role": "user", "content": "q"}], "sys", "t")
        assert events == []

    def test_a_real_dispatch_still_emits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The guard must not have silenced the event for genuine dispatches."""
        from personal_agent.llm_client import history_sanitiser as hs

        events: list[str] = []
        mock_log = MagicMock()
        mock_log.info.side_effect = lambda event, **kw: events.append(event)
        mock_log.debug.side_effect = lambda event, **kw: events.append(event)
        monkeypatch.setattr(hs, "log", mock_log)

        hs.sanitise_messages([{"role": "user", "content": "q"}], trace_id="t")
        assert events == ["history_sanitised"]
