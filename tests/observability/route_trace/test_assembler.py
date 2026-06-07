"""Unit tests for the route-trace assembler (FRE-452).

The assembler is a pure ``ExecutionContext`` → :class:`RouteTraceRow` adapter. These tests
cover field mapping, the PII preview gate, cost reconciliation tolerance, and the
null-path cases (pre-gateway, pre-LLM, failure-before-synthesis) the row must survive.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from personal_agent.governance.models import Mode
from personal_agent.llm_client.types import ModelRole
from personal_agent.observability.route_trace.assembler import assemble_route_trace
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.sub_agent_types import SubAgentResult
from personal_agent.request_gateway.types import Complexity, DecompositionStrategy, TaskType
from personal_agent.telemetry.request_timer import RequestTimer


def _gateway_output() -> SimpleNamespace:
    """A gateway-output stand-in carrying real enums (assembler reads ``.value``)."""
    return SimpleNamespace(
        intent=SimpleNamespace(
            task_type=TaskType.MEMORY_RECALL,
            complexity=Complexity.SIMPLE,
            confidence=0.82,
        ),
        decomposition=SimpleNamespace(strategy=DecompositionStrategy.SINGLE, reason="calm/simple"),
        governance=SimpleNamespace(mode=Mode.NORMAL),
        degraded_stages=["context"],
    )


def _base_ctx(**overrides: object) -> SimpleNamespace:
    """Build a populated ctx stand-in; override individual attributes per test."""
    timer = RequestTimer(trace_id="t")
    timer.record_instant("llm_call:primary")
    defaults: dict[str, object] = dict(
        trace_id=str(uuid4()),
        session_id=str(uuid4()),
        user_message="What did I say about Postgres last week?",
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}],
        gateway_output=_gateway_output(),
        channel=Channel.CHAT,
        selected_model_role=ModelRole.PRIMARY,
        routing_history=[{"decision": "HANDLE", "confidence": 0.9}],
        tool_iteration_count=1,
        steps=[{"type": "tool_call", "metadata": {"tool_name": "web_search"}}],
        loaded_skills={"recall"},
        sub_agent_results=None,
        expansion_phase_results=[],
        expansion_strategy=None,
        final_reply="Here is what you said.",
        request_timer=timer,
        turn_cost_usd=0.01,
        error=None,
        classified_error=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _assemble(ctx: SimpleNamespace, **kw: object):
    base = dict(
        authoritative_cost_usd=0.01,
        input_tokens=100,
        output_tokens=50,
        store_preview=False,
        preview_chars=280,
    )
    base.update(kw)
    return assemble_route_trace(ctx, **base)  # type: ignore[arg-type]


def test_full_population_maps_fields() -> None:
    row = _assemble(_base_ctx())
    assert row.task_type == "memory_recall"
    assert row.complexity == "simple"
    assert row.intent_confidence == 0.82
    assert row.decomposition_strategy == "single"
    assert row.gateway_label == "memory_recall/single"
    assert row.mode == "NORMAL"
    assert row.channel == "CHAT"
    assert row.model_role == "primary"
    assert row.degraded_stages == ("context",)
    assert row.tools_used == ("web_search",)
    assert row.skills_loaded == ("recall",)
    assert row.orchestration_event == "primary_handled"
    assert row.fallback_triggered is False
    assert row.final_reply_chars == len("Here is what you said.")
    assert row.latency_total_ms is not None
    assert row.latency_breakdown is not None
    assert row.pedagogical_outcomes is None


def test_preview_gate_off_stores_hash_not_text() -> None:
    row = _assemble(_base_ctx(), store_preview=False)
    assert row.user_message_preview is None
    assert row.user_message_sha256 is not None
    assert len(row.user_message_sha256) == 16
    assert row.user_message_chars > 0


def test_preview_gate_on_truncates() -> None:
    row = _assemble(_base_ctx(), store_preview=True, preview_chars=4)
    assert row.user_message_preview == "What"


def test_cost_reconciled_within_tolerance() -> None:
    row = _assemble(_base_ctx(turn_cost_usd=0.01), authoritative_cost_usd=0.01)
    assert row.cost_reconciled is True
    assert row.cost_live_usd == 0.01
    assert row.cost_authoritative_usd == 0.01


def test_cost_not_reconciled_beyond_tolerance() -> None:
    row = _assemble(_base_ctx(turn_cost_usd=0.57), authoritative_cost_usd=0.90)
    assert row.cost_reconciled is False


def test_none_gateway_output_yields_unknown_label() -> None:
    row = _assemble(_base_ctx(gateway_output=None))
    assert row.task_type is None
    assert row.decomposition_strategy is None
    assert row.gateway_label == "unknown/unknown"
    assert row.orchestration_event == "primary_handled"  # no subs → still classifiable


def test_none_request_timer_yields_no_latency() -> None:
    row = _assemble(_base_ctx(request_timer=None))
    assert row.latency_total_ms is None
    assert row.latency_breakdown is None


def test_none_model_role_pre_llm() -> None:
    row = _assemble(_base_ctx(selected_model_role=None))
    assert row.model_role is None


def test_delegate_passed_to_synthesis_flag() -> None:
    sub = SubAgentResult(
        task_id="s1",
        spec_task="x",
        summary="useful summary",
        full_output="full",
        tools_used=["web_search"],
        token_count=20,
        duration_ms=5.0,
        success=True,
        cost_usd=0.02,
    )
    row = _assemble(_base_ctx(sub_agent_results=[sub]))
    assert row.sub_agent_count == 1
    assert row.orchestration_event == "delegate_called"
    assert row.delegate_result_passed_to_synthesis is True
    assert row.sub_agents[0]["summary_chars"] == len("useful summary")
    assert row.sub_agents[0]["success"] is True


def test_error_fields_populated() -> None:
    classified = SimpleNamespace(category="timeout")
    row = _assemble(_base_ctx(error=ValueError("boom"), classified_error=classified))
    assert row.error_type == "ValueError"
    assert row.error_class == "timeout"
