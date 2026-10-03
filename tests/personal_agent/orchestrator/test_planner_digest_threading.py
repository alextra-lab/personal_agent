"""Tests for FRE-1472: the digest reaches the planner, its judgment, one terminal event per run.

ADR-0154 D1 (placement) and D5 (the relevance matrix keyed on the planner's decision, and one
terminal planner event per run), carrying ADR-0147 D3 and D5. One class per acceptance criterion.
AC-2 is an integration test against the real planner (``tests/integration``).

The decline row and the "failed with no fallback" row have no code path until FRE-1515 (ADR-0154
D2). They are seeded at the pure-function level here, as the owner decided on 2026-10-03.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, get_args
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from personal_agent.orchestrator.executor import _build_planner_memory_digest
from personal_agent.orchestrator.expansion_controller import (
    ExpansionController,
    ExpansionResult,
    _validate_plan_json,
    planner_outcome_fields,
    resolve_memory_relevance,
)
from personal_agent.orchestrator.expansion_types import MemoryRelevance, PlannerMemoryDigest
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec

_CONTROLLER_LOGGER = "personal_agent.orchestrator.expansion_controller"

#: The full field set of the terminal planner event (ADR-0154 D5, ADR-0147 D5, D6 fields).
_OUTCOME_FIELDS = frozenset(
    {
        "planner_decision",
        "planner_failure_reason",
        "is_fallback",
        "memory_relevance",
        "plan_task_count",
        "planner_deployment",
        "planner_mode",
        "planner_reasoning_chars",
        "planner_duration_ms",
        "planner_prompt_tokens",
        "planner_completion_tokens",
        "planner_input_chars",
        "planner_system_prompt_sha256",
        "memory_digest_eligible_items",
        "memory_digest_items",
        "memory_digest_items_dropped",
        "memory_digest_kinds",
        "memory_digest_max_line_chars",
        "memory_digest_tokens",
        "memory_digest_item_keys",
        "memory_rendered_item_keys",
    }
)

_COINED = "Zorblaxian"


def _digest(*descriptions: str) -> PlannerMemoryDigest:
    items = [
        {
            "type": "entity",
            "name": f"Item{i}",
            "entity_type": "Concept",
            "description": description,
        }
        for i, description in enumerate(descriptions)
    ]
    return _build_planner_memory_digest(items)


def _plan_json(tasks: int = 1, **extra: Any) -> str:
    return json.dumps(
        {
            "strategy": "HYBRID",
            "tasks": [
                {"name": f"task_{i}", "goal": f"Goal for task {i}", "type": "general"}
                for i in range(tasks)
            ],
            **extra,
        }
    )


def _sub_agent_result(task_name: str = "task_0") -> SubAgentResult:
    return SubAgentResult(
        task_id=uuid4(),
        spec_task=task_name,
        summary="Result summary",
        full_output="Result summary",
        tools_used=[],
        token_count=50,
        duration_ms=2000,
        success=True,
        error=None,
        cost_usd=0.0,
    )


def _client(content: str = "", **response: Any) -> AsyncMock:
    client = AsyncMock()
    client.respond = AsyncMock(return_value={"content": content, "cost_usd": 0.0, **response})
    return client


def _outcome_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "planner_outcome"
    ]


async def _run_planner(
    client: Any,
    *,
    digest: PlannerMemoryDigest | None = None,
    messages: list[dict[str, Any]] | None = None,
    query: str = "Plan lunch",
    input_max_chars: int = 64000,
) -> Any:
    return await ExpansionController()._run_planner(
        query=query,
        strategy="HYBRID",
        llm_client=client,
        trace_id="test-trace-fre1472",
        timeout_s=5.0,
        result=ExpansionResult(),
        messages=messages,
        input_max_chars=input_max_chars,
        memory_digest=digest,
    )


def _user_message(client: AsyncMock) -> str:
    return str(client.respond.call_args.kwargs["messages"][1]["content"])


def _system_prompt(client: AsyncMock) -> str:
    return str(client.respond.call_args.kwargs["messages"][0]["content"])


class TestAC1DigestReachesThePlannerCall:
    """AC-1 — the recorded planner input contains the digest block."""

    @pytest.mark.asyncio
    async def test_the_planner_call_input_carries_the_digest_lines(self) -> None:
        digest = _digest(f"The family dog is called {_COINED}.")
        client = _client(_plan_json())

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=[_sub_agent_result()],
        ):
            await ExpansionController().execute(
                query="Plan lunch",
                strategy="HYBRID",
                llm_client=client,
                trace_id="test-trace-ac1",
                messages=[{"role": "user", "content": "Plan lunch"}],
                memory_digest=digest,
            )

        user = _user_message(client)
        assert digest.text
        assert digest.text in user
        assert _COINED in user

    @pytest.mark.asyncio
    async def test_no_digest_leaves_no_memory_block(self) -> None:
        client = _client(_plan_json())

        await _run_planner(client, digest=None)

        assert "memory" not in _user_message(client).lower()


class TestAC3PlacementKeepsTheCache:
    """AC-3 — history, then digest, then query; the system prompt is byte-identical."""

    @pytest.mark.asyncio
    async def test_history_then_digest_then_query(self) -> None:
        digest = _digest(f"The family dog is called {_COINED}.")
        client = _client(_plan_json())
        messages = [
            {"role": "user", "content": "An older question about trains"},
            {"role": "assistant", "content": "An older answer"},
            {"role": "user", "content": "Plan lunch"},
        ]

        await _run_planner(client, digest=digest, messages=messages)

        user = _user_message(client)
        history_at = user.index("An older question about trains")
        digest_at = user.index(_COINED)
        query_at = user.index("Query: Plan lunch")
        assert history_at < digest_at < query_at

    @pytest.mark.asyncio
    async def test_the_system_prompt_is_identical_with_and_without_a_digest(self) -> None:
        with_digest = _client(_plan_json())
        without_digest = _client(_plan_json())

        await _run_planner(with_digest, digest=_digest(f"The family dog is called {_COINED}."))
        await _run_planner(without_digest, digest=None)

        assert _system_prompt(with_digest) == _system_prompt(without_digest)

    @pytest.mark.asyncio
    async def test_the_system_prompt_asks_for_memory_relevance(self) -> None:
        client = _client(_plan_json())

        await _run_planner(client)

        assert '"memory_relevance": "used|none_relevant"' in _system_prompt(client)


_ALL_RELEVANCE: tuple[MemoryRelevance, ...] = get_args(MemoryRelevance)


class TestAC4TheMatrixNeverContradictsItself:
    """AC-4 — every row of the ADR-0154 D5 matrix, for every value the model can state."""

    @pytest.mark.parametrize("decision", ["declined", "expanded", "failed"])
    @pytest.mark.parametrize("is_fallback", [False, True])
    @pytest.mark.parametrize("stated", _ALL_RELEVANCE)
    def test_an_empty_digest_is_always_not_applicable(
        self, decision: Any, is_fallback: bool, stated: MemoryRelevance
    ) -> None:
        assert resolve_memory_relevance(0, decision, is_fallback, stated) == "not_applicable"

    @pytest.mark.parametrize("is_fallback", [False, True])
    @pytest.mark.parametrize("stated", _ALL_RELEVANCE)
    def test_a_failed_run_with_a_digest_is_not_applicable(
        self, is_fallback: bool, stated: MemoryRelevance
    ) -> None:
        # Includes the ADR-0154 D2 row: a failed `planner_asked` turn has no fallback.
        assert resolve_memory_relevance(3, "failed", is_fallback, stated) == "not_applicable"

    @pytest.mark.parametrize("decision", ["declined", "expanded"])
    @pytest.mark.parametrize("stated", _ALL_RELEVANCE)
    def test_a_fallback_plan_with_a_digest_is_not_applicable(
        self, decision: Any, stated: MemoryRelevance
    ) -> None:
        assert resolve_memory_relevance(3, decision, True, stated) == "not_applicable"

    @pytest.mark.parametrize("decision", ["declined", "expanded"])
    @pytest.mark.parametrize("stated", ["used", "none_relevant", "unstated"])
    def test_a_planner_plan_with_a_digest_keeps_the_stated_judgment(
        self, decision: Any, stated: MemoryRelevance
    ) -> None:
        assert resolve_memory_relevance(3, decision, False, stated) == stated

    @pytest.mark.parametrize("decision", ["declined", "expanded"])
    def test_a_planner_plan_never_records_not_applicable(self, decision: Any) -> None:
        assert resolve_memory_relevance(3, decision, False, "not_applicable") == "unstated"

    @pytest.mark.parametrize("value", ["used", "none_relevant"])
    def test_the_validator_keeps_a_planner_value(self, value: str) -> None:
        plan = _validate_plan_json(_plan_json(memory_relevance=value))
        assert plan is not None
        assert plan.memory_relevance == value

    @pytest.mark.parametrize("value", [None, "", "USED", "maybe", "not_applicable", "unstated", 1])
    def test_the_validator_writes_unstated_for_a_missing_or_invalid_value(
        self, value: object
    ) -> None:
        raw = _plan_json() if value is None else _plan_json(memory_relevance=value)
        plan = _validate_plan_json(raw)
        assert plan is not None
        assert plan.memory_relevance == "unstated"

    @pytest.mark.asyncio
    async def test_an_empty_digest_overrides_the_model_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO", logger=_CONTROLLER_LOGGER)
        plan = await _run_planner(_client(_plan_json(memory_relevance="used")), digest=None)

        assert plan.memory_relevance == "not_applicable"
        assert _outcome_events(caplog)[0]["memory_relevance"] == "not_applicable"

    @pytest.mark.asyncio
    async def test_a_digest_keeps_the_model_value_on_an_expansion(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO", logger=_CONTROLLER_LOGGER)
        plan = await _run_planner(
            _client(_plan_json(memory_relevance="none_relevant")),
            digest=_digest("The user lives in Brittany."),
        )

        assert plan.memory_relevance == "none_relevant"
        assert _outcome_events(caplog)[0]["memory_relevance"] == "none_relevant"

    @pytest.mark.asyncio
    async def test_a_fallback_plan_with_a_digest_is_not_applicable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO", logger=_CONTROLLER_LOGGER)
        plan = await _run_planner(
            _client("not json"), digest=_digest("The user lives in Brittany.")
        )

        assert plan.is_fallback
        assert plan.memory_relevance == "not_applicable"
        assert _outcome_events(caplog)[0]["memory_relevance"] == "not_applicable"


async def _raise_timeout(**_: Any) -> Any:
    raise asyncio.TimeoutError


async def _raise_error(**_: Any) -> Any:
    raise RuntimeError("model server exploded")


class TestAC5ExactlyOneTerminalEventPerRun:
    """AC-5 — every path through `_run_planner` emits exactly one `planner_outcome`."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "respond", "decision", "failure_reason", "is_fallback"),
        [
            (
                "success",
                {"content": _plan_json(2), "usage": {"prompt_tokens": 640, "completion_tokens": 13}},
                "expanded",
                None,
                False,
            ),
            ("invalid", {"content": "not json"}, "failed", "invalid", True),
            (
                "truncated",
                {"content": '{"strategy": "HYB', "finish_reason": "length"},
                "failed",
                "invalid",
                True,
            ),
            ("timeout", _raise_timeout, "failed", "timeout", True),
            ("exception", _raise_error, "failed", "exception", True),
        ],
    )
    async def test_each_path_emits_one_event_with_the_full_field_set(
        self,
        caplog: pytest.LogCaptureFixture,
        label: str,
        respond: Any,
        decision: str,
        failure_reason: str | None,
        is_fallback: bool,
    ) -> None:
        caplog.set_level("INFO", logger=_CONTROLLER_LOGGER)
        client = AsyncMock()
        if callable(respond):
            client.respond = AsyncMock(side_effect=respond)
        else:
            client.respond = AsyncMock(return_value={"cost_usd": 0.0, **respond})
        client.model_key = "local_primary"
        digest = _digest("The user lives in Brittany.", "The user prefers trains.")

        await _run_planner(client, digest=digest)

        events = _outcome_events(caplog)
        assert len(events) == 1, label
        event = events[0]
        assert _OUTCOME_FIELDS <= set(event), _OUTCOME_FIELDS - set(event)
        assert event["planner_decision"] == decision
        assert event["planner_failure_reason"] == failure_reason
        assert event["is_fallback"] is is_fallback
        assert event["memory_digest_items"] == 2
        assert event["planner_system_prompt_sha256"]
        assert event["trace_id"] == "test-trace-fre1472"
        # The model was called on every one of these paths, so the call has a wall clock.
        assert isinstance(event["planner_duration_ms"], float)
        assert event["planner_deployment"] == "local_primary"
        if label == "success":
            assert event["planner_prompt_tokens"] == 640
            assert event["planner_completion_tokens"] == 13
            assert event["plan_task_count"] == 2
        else:
            assert event["planner_prompt_tokens"] is None
            assert event["planner_completion_tokens"] is None
        if label in ("timeout", "exception"):
            assert event["planner_reasoning_chars"] is None
        else:
            assert event["planner_reasoning_chars"] == 0

    @pytest.mark.asyncio
    async def test_input_too_large_emits_one_event(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("INFO", logger=_CONTROLLER_LOGGER)
        client = _client(_plan_json())

        await _run_planner(client, query="q" * 600, input_max_chars=500, digest=_digest("A fact."))

        client.respond.assert_not_called()
        events = _outcome_events(caplog)
        assert len(events) == 1
        event = events[0]
        assert _OUTCOME_FIELDS <= set(event)
        assert event["planner_decision"] == "failed"
        assert event["planner_failure_reason"] == "input_too_large"
        assert event["is_fallback"] is True
        assert event["planner_reasoning_chars"] is None
        assert event["planner_duration_ms"] is None
        assert event["planner_prompt_tokens"] is None
        assert event["planner_input_chars"]["message"] > 500
        assert event["memory_relevance"] == "not_applicable"

    @pytest.mark.asyncio
    async def test_the_prompt_hash_is_the_hash_of_the_system_prompt_sent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import hashlib

        caplog.set_level("INFO", logger=_CONTROLLER_LOGGER)
        client = _client(_plan_json())

        await _run_planner(client)

        expected = hashlib.sha256(_system_prompt(client).encode()).hexdigest()
        assert _outcome_events(caplog)[0]["planner_system_prompt_sha256"] == expected

    @pytest.mark.asyncio
    async def test_the_digest_fields_describe_the_digest(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO", logger=_CONTROLLER_LOGGER)
        digest = _digest("The user lives in Brittany.", "The user prefers trains.")

        await _run_planner(_client(_plan_json()), digest=digest)

        event = _outcome_events(caplog)[0]
        assert event["memory_digest_eligible_items"] == digest.eligible_count
        assert event["memory_digest_items"] == digest.item_count
        assert event["memory_digest_items_dropped"] == digest.dropped_count
        assert event["memory_digest_kinds"] == dict(digest.kind_counts)
        assert event["memory_digest_max_line_chars"] == digest.max_line_chars
        assert event["memory_digest_tokens"] == digest.estimated_tokens
        assert event["memory_digest_item_keys"] == [
            {"kind": k.kind, "identity": k.identity, "ordinal": k.ordinal} for k in digest.item_keys
        ]
        assert event["memory_rendered_item_keys"] == [
            {"kind": k.kind, "identity": k.identity, "ordinal": k.ordinal}
            for k in digest.rendered_item_keys
        ]

    @pytest.mark.parametrize(
        ("decision", "failure_reason", "is_fallback", "stated", "expected"),
        [
            # ADR-0154 D2 rows with no code path before FRE-1515 (owner decision 2026-10-03).
            ("declined", None, False, "used", "used"),
            ("declined", None, False, "none_relevant", "none_relevant"),
            ("failed", "invalid", False, "unstated", "not_applicable"),
            ("failed", "input_too_large", False, "unstated", "not_applicable"),
        ],
    )
    def test_the_field_builder_covers_the_decline_and_the_failure_without_fallback(
        self,
        decision: Any,
        failure_reason: Any,
        is_fallback: bool,
        stated: MemoryRelevance,
        expected: str,
    ) -> None:
        fields = planner_outcome_fields(
            decision=decision,
            failure_reason=failure_reason,
            is_fallback=is_fallback,
            stated_relevance=stated,
            plan_task_count=0,
            digest=_digest("The user lives in Brittany."),
            deployment="local_primary",
            mode="planner",
            reasoning_chars=0,
            duration_ms=800.0,
            prompt_tokens=600,
            completion_tokens=13,
            input_chars={"system": 1, "history": 0, "digest": 10, "message": 5},
            system_prompt_sha256="abc",
        )

        assert set(fields) == _OUTCOME_FIELDS
        assert fields["memory_relevance"] == expected
        assert fields["planner_decision"] == decision
        assert fields["planner_failure_reason"] == failure_reason

    def test_the_field_builder_with_no_digest_records_zero_items(self) -> None:
        fields = planner_outcome_fields(
            decision="expanded",
            failure_reason=None,
            is_fallback=False,
            stated_relevance="used",
            plan_task_count=1,
            digest=None,
            deployment=None,
            mode=None,
            reasoning_chars=0,
            duration_ms=None,
            prompt_tokens=None,
            completion_tokens=None,
            input_chars={"system": 1, "history": 0, "digest": 0, "message": 5},
            system_prompt_sha256="abc",
        )

        assert fields["memory_digest_items"] == 0
        assert fields["memory_digest_eligible_items"] == 0
        assert fields["memory_digest_item_keys"] == []
        assert fields["memory_rendered_item_keys"] == []
        assert fields["memory_relevance"] == "not_applicable"


class TestAC6NoMemoryReachesAWorker:
    """AC-6 — `SubAgentSpec.context` stays the `messages[-4:]` slice with a digest present."""

    @pytest.mark.asyncio
    async def test_the_worker_context_is_the_message_tail_only(self) -> None:
        digest = _digest(f"The family dog is called {_COINED}.")
        messages = [{"role": "user", "content": f"message {i}"} for i in range(7)]
        captured: list[SubAgentSpec] = []

        async def _capture(*, spec: SubAgentSpec, **_: Any) -> SubAgentResult:
            captured.append(spec)
            return _sub_agent_result(spec.task)

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=_capture,
        ):
            await ExpansionController().execute(
                query="message 6",
                strategy="HYBRID",
                llm_client=_client(_plan_json(2)),
                trace_id="test-trace-ac6",
                messages=messages,
                memory_digest=digest,
            )

        assert len(captured) == 2
        for spec in captured:
            assert spec.context == messages[-4:]
            assert _COINED not in json.dumps(spec.context)


class TestExecutorPassesTheDigest:
    """The executor builds the digest from ``ctx.memory_context`` and passes it to the planner."""

    @pytest.mark.asyncio
    async def test_step_init_passes_the_digest_of_the_memory_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        import personal_agent.orchestrator.executor as ex
        from personal_agent.governance.models import Mode
        from personal_agent.orchestrator.channels import Channel
        from personal_agent.orchestrator.types import ExecutionContext
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

        memory = [
            {
                "type": "entity",
                "name": "Dog",
                "entity_type": "Concept",
                "description": f"The family dog is called {_COINED}.",
            }
        ]
        gateway = GatewayOutput(
            intent=IntentResult(
                task_type=TaskType.ANALYSIS,
                complexity=Complexity.COMPLEX,
                confidence=0.9,
                signals=[],
            ),
            governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
            decomposition=DecompositionResult(strategy=DecompositionStrategy.HYBRID, reason="test"),
            context=AssembledContext(
                messages=[{"role": "user", "content": "Plan a walk"}],
                memory_context=memory,
                tool_definitions=None,
            ),
            session_id="s1",
            trace_id="t1",
        )
        ctx = ExecutionContext(
            session_id="s1",
            trace_id="t1",
            user_message="Plan a walk",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            gateway_output=gateway,
        )

        async def _no_progress(_ctx: ExecutionContext) -> None:
            return None

        controller = MagicMock()
        controller.execute = AsyncMock(return_value=ExpansionResult())
        monkeypatch.setattr(ex, "_report_turn_progress", _no_progress)
        monkeypatch.setattr(
            "personal_agent.orchestrator.expansion_controller.ExpansionController",
            lambda: controller,
        )
        monkeypatch.setattr(
            "personal_agent.llm_client.factory.get_llm_client",
            lambda role_name=None, mode=None: MagicMock(),
        )
        session = MagicMock()
        session.messages = []
        manager = MagicMock()
        manager.get_session = MagicMock(return_value=session)

        await ex.step_init(ctx, manager, TraceContext(trace_id="t1", session_id="s1"))

        digest = controller.execute.call_args.kwargs["memory_digest"]
        assert digest == _build_planner_memory_digest(memory)
        assert _COINED in digest.text
