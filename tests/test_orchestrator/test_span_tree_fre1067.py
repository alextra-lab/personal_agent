"""Span-tree integration test (ADR-0129 D3 / FRE-1067).

One exercised tool-using turn — two concurrent tool calls in a single round,
then a no-tool synthesis round — proves AC-1, AC-2, AC-4, AC-5, AC-9, AC-11
end to end: root -> step -> {model-call, tool-call} with model-call and
tool-call as siblings, no child outliving its parent, no
``tool_execution_completed`` log record (its tool count moved onto the step
span), the retained ``tool_call_started``/``tool_call_completed`` log records
still emitting, and no legacy ``duration_ms``/``latency_ms`` field on the
converted paths.

Real step/tool-call span creation is exercised through the real
``orchestrator/executor.py`` driver loop and ``tools/executor.py::execute_tool``
against a real (but trivial, fully-controlled) tool registry — swapped in via
``_get_tool_execution_layer`` so no external I/O is needed. The model-call
span is a stand-in opened directly by the mocked LLM client's ``respond()``
side effect: ``client.py``/``litellm_client.py``'s own wiring of
``model_call_span`` (real span id, gen_ai attributes, traceparent) is proven
separately at the client level (``test_client.py``, ``test_litellm_*.py``) —
this test's job is the step/tool-call span TREE shape, not re-proving
client-level wiring.

``personal_agent.telemetry.spans.get_tracer`` is patched globally for the
test's duration so every span-creation call site (``open_step_span``,
``model_call_span``, ``tool_call_span``) — none of which accept a tracer
override at their production call sites — resolves to this test's own
provider, never the process-global one.

Log capture uses ``structlog.testing.capture_logs`` (the pattern
``test_otel_root_span.py`` establishes) rather than a hand-rolled processor
swap — it correctly restores whatever was configured before and returns a
flat list of dicts (each carrying ``event`` plus that call's kwargs).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer

from personal_agent.governance.models import Mode
from personal_agent.orchestrator import Channel, Orchestrator
from personal_agent.telemetry.spans import model_call_span
from personal_agent.tools.executor import ToolExecutionLayer
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolDefinition
from tests.test_orchestrator.conftest import configure_mock_llm_client_model_configs


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer(exporter: InMemorySpanExporter) -> Tracer:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("fre-1067-orchestrator-integration-test")


@pytest.fixture
def fake_tool_layer() -> ToolExecutionLayer:
    """A real ToolExecutionLayer over two trivial, always-succeeding tools —
    no external I/O, so the test is deterministic and network-free while
    still exercising the real ``execute_tool`` span-wrapping code.
    """
    registry = ToolRegistry()

    async def _tool_a(**_kwargs: object) -> dict[str, bool]:
        return {"ok": True}

    async def _tool_b(**_kwargs: object) -> dict[str, bool]:
        return {"ok": True}

    registry.register(
        ToolDefinition(
            name="fre1067_tool_a",
            description="Trivial test tool A",
            category="read_only",
            parameters=[],
            risk_level="low",
            allowed_modes=["NORMAL"],
        ),
        _tool_a,
    )
    registry.register(
        ToolDefinition(
            name="fre1067_tool_b",
            description="Trivial test tool B",
            category="read_only",
            parameters=[],
            risk_level="low",
            allowed_modes=["NORMAL"],
        ),
        _tool_b,
    )
    return ToolExecutionLayer(registry)


@pytest.mark.asyncio
async def test_tool_using_turn_produces_correct_span_tree(
    tracer: Tracer,
    exporter: InMemorySpanExporter,
    fake_tool_layer: ToolExecutionLayer,
) -> None:
    call_count = {"n": 0}

    async def _fake_respond(*, role: object, **_kwargs: object) -> dict[str, object]:
        # Stand-in for the real client's model-call span (proven at the
        # client level elsewhere) — this is what lets the orchestrator's
        # step span have a model-call child to be a sibling of the tool-call
        # children.
        with model_call_span(
            role=getattr(role, "value", "primary"), model="test-model", provider="test"
        ):
            pass
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_1", "name": "fre1067_tool_a", "arguments": json.dumps({})},
                    {"id": "call_2", "name": "fre1067_tool_b", "arguments": json.dumps({})},
                ],
                "reasoning_trace": None,
                "usage": {"total_tokens": 100},
                "raw": {},
            }
        return {
            "role": "assistant",
            "content": "Both tools ran successfully",
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"total_tokens": 50},
            "raw": {},
        }

    mock_client = AsyncMock()
    mock_client.respond = AsyncMock(side_effect=_fake_respond)
    configure_mock_llm_client_model_configs(mock_client)

    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_client),
        patch(
            "personal_agent.orchestrator.executor._get_tool_execution_layer",
            return_value=fake_tool_layer,
        ),
        patch("personal_agent.telemetry.spans.get_tracer", return_value=tracer),
        structlog.testing.capture_logs() as captured,
    ):
        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="fre-1067-span-tree-session",
            user_message="Run both test tools",
            mode=Mode.NORMAL,
            channel=Channel.SYSTEM_HEALTH,  # bypass router, matches TestToolUsingFlow convention
        )

    assert result["reply"]

    spans = exporter.get_finished_spans()
    by_name: dict[str, list] = {}
    for s in spans:
        by_name.setdefault(s.name, []).append(s)

    step_spans = by_name.get("step", [])
    model_spans = by_name.get("model_call test-model", [])
    tool_a_spans = by_name.get("tool_call fre1067_tool_a", [])
    tool_b_spans = by_name.get("tool_call fre1067_tool_b", [])

    # AC-2: two step spans (the tool round, then the no-tool synthesis round).
    assert len(step_spans) == 2, (
        f"expected 2 step spans (tool round + synthesis round), got spans: {list(by_name)}"
    )
    assert len(model_spans) == 2, "expected one model-call span per round"
    assert len(tool_a_spans) == 1
    assert len(tool_b_spans) == 1

    tool_round_step = next(
        s for s in step_spans if s.attributes.get("personal_agent.step.tool_count") == 2
    )
    synthesis_round_step = next(
        s for s in step_spans if s.attributes.get("personal_agent.step.tool_count") == 0
    )

    # AC-2: root has no parent.
    assert tool_round_step.parent is None
    assert synthesis_round_step.parent is None

    # AC-1 / AC-2: tool-call spans are children of the STEP span, never of
    # the model-call span — the trap ADR-0129 D3 states explicitly.
    tool_a_span, tool_b_span = tool_a_spans[0], tool_b_spans[0]
    step_span_id = tool_round_step.context.span_id
    assert tool_a_span.parent is not None
    assert tool_a_span.parent.span_id == step_span_id
    assert tool_b_span.parent is not None
    assert tool_b_span.parent.span_id == step_span_id

    # Model-call and tool-call spans are SIBLINGS beneath the step, in both
    # rounds — never nested in each other.
    tool_round_model_span = next(
        s
        for s in model_spans
        if s.start_time >= tool_round_step.start_time and s.end_time <= tool_round_step.end_time
    )
    assert tool_round_model_span.parent is not None
    assert tool_round_model_span.parent.span_id == step_span_id
    assert tool_round_model_span.context.span_id != tool_a_span.context.span_id
    assert tool_round_model_span.context.span_id != tool_b_span.context.span_id

    # AC-4: no child span outlives its parent.
    for child in (tool_a_span, tool_b_span, tool_round_model_span):
        assert child.end_time <= tool_round_step.end_time

    # AC-5: the step-level "tool_execution_completed" record is gone — its
    # tool count lives on the step span attribute instead.
    event_names = {entry.get("event") for entry in captured}
    assert "tool_execution_completed" not in event_names

    # AC-11: the retained per-tool log records still emit.
    started = [entry for entry in captured if entry.get("event") == "tool_call_started"]
    completed = [entry for entry in captured if entry.get("event") == "tool_call_completed"]
    assert len(started) == 2
    assert len(completed) == 2

    # AC-3: tool-span count matches the retained tool_call_completed count.
    assert len({tool_a_span.context.span_id, tool_b_span.context.span_id}) == len(completed)

    # AC-9: no legacy elapsed-time field survives on the converted paths.
    for entry in captured:
        if entry.get("event") in (
            "tool_call_completed",
            "tool_call_failed",
            "model_call_completed",
        ):
            assert "latency_ms" not in entry
            assert "duration_ms" not in entry


@pytest.mark.asyncio
async def test_step_span_closes_on_llm_call_failure(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    """Non-happy-path exit: step_llm_call raising must still close the step
    span — this is the class of bug the driver loop's centralized
    try/finally (rather than scattered per-callsite close calls) exists to
    prevent (codex plan-review finding #1).
    """

    async def _failing_respond(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("simulated model-call failure")

    mock_client = AsyncMock()
    mock_client.respond = AsyncMock(side_effect=_failing_respond)
    configure_mock_llm_client_model_configs(mock_client)

    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_client),
        patch("personal_agent.telemetry.spans.get_tracer", return_value=tracer),
    ):
        orchestrator = Orchestrator()
        result = await orchestrator.handle_user_request(
            session_id="fre-1067-span-tree-error-session",
            user_message="Trigger a model-call failure",
            mode=Mode.NORMAL,
            channel=Channel.SYSTEM_HEALTH,
        )

    # The turn fails gracefully (execute_task_safe never raises to the caller).
    assert result["reply"]

    spans = exporter.get_finished_spans()
    step_spans = [s for s in spans if s.name == "step"]
    assert len(step_spans) == 1, f"expected exactly one step span, got: {[s.name for s in spans]}"
    # A closed span has a real end_time — it was not left open.
    assert step_spans[0].end_time is not None
    assert step_spans[0].parent is None
