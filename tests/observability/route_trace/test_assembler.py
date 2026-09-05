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
from personal_agent.observability.route_trace.assembler import (
    assemble_route_trace,
    assemble_sub_agent_route_trace,
)
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.sub_agent_types import SubAgentResult
from personal_agent.request_gateway.types import Complexity, DecompositionStrategy, TaskType


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
    # ADR-0129 D3 / FRE-1067: RequestTimer is retired — these two fields have
    # no source and are always None going forward (Postgres schema unchanged).
    assert row.latency_total_ms is None
    assert row.latency_breakdown is None
    assert row.pedagogical_outcomes is None


def test_skills_loaded_includes_evidence_skill_bodies() -> None:
    """FRE-1291: hybrid/keyword routing never writes ctx.loaded_skills, but the FRE-1004
    evidence pipeline (ctx.turn_evidence.assembled_context.skill_bodies) already records
    what those modes injected, gated on proof the block reached the model. The assembler
    must read it too, not just ctx.loaded_skills.
    """
    ctx = _base_ctx(
        loaded_skills=set(),
        turn_evidence=SimpleNamespace(
            assembled_context=SimpleNamespace(skill_bodies=["web-search"])
        ),
    )
    row = _assemble(ctx)
    assert row.skills_loaded == ("web-search",)


def test_skills_loaded_unions_loaded_skills_and_evidence_skill_bodies() -> None:
    """model_decided's ctx.loaded_skills source and the evidence-record source both
    contribute, sorted and deduplicated.
    """
    ctx = _base_ctx(
        loaded_skills={"recall"},
        turn_evidence=SimpleNamespace(
            assembled_context=SimpleNamespace(skill_bodies=["web-search", "recall"])
        ),
    )
    row = _assemble(ctx)
    assert row.skills_loaded == ("recall", "web-search")


def test_skills_loaded_handles_missing_turn_evidence() -> None:
    """Pre-LLM-call / build-failure paths carry no turn_evidence — must not raise."""
    row = _assemble(_base_ctx(loaded_skills={"recall"}, turn_evidence=None))
    assert row.skills_loaded == ("recall",)


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


def test_none_model_role_pre_llm() -> None:
    row = _assemble(_base_ctx(selected_model_role=None))
    assert row.model_role is None


def test_delegate_passed_to_synthesis_flag() -> None:
    sub = SubAgentResult(
        task_id=uuid4(),
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


def _sub(summary: str, **overrides: object) -> SubAgentResult:
    """A successful sub-agent result with the given summary."""
    defaults: dict[str, object] = dict(
        task_id=uuid4(),
        spec_task="x",
        summary=summary,
        full_output=summary,
        tools_used=[],
        token_count=10,
        duration_ms=5.0,
        success=True,
        cost_usd=0.0,
    )
    defaults.update(overrides)
    return SubAgentResult(**defaults)  # type: ignore[arg-type]


def test_reply_overlap_full_containment() -> None:
    """FRE-515: summary tokens fully present in the reply → overlap 1.0."""
    summary = "postgres connection pooling explained"
    reply = "Here is the answer: postgres connection pooling explained in detail."
    row = _assemble(_base_ctx(sub_agent_results=[_sub(summary)], final_reply=reply))
    assert row.sub_agents[0]["reply_overlap"] == 1.0


def test_reply_overlap_unrelated_reply_is_zero() -> None:
    """FRE-515: no summary token appears in the reply → overlap 0.0."""
    row = _assemble(
        _base_ctx(
            sub_agent_results=[_sub("quantum entanglement decoherence")],
            final_reply="Sorry, the model call failed before synthesis.",
        )
    )
    assert row.sub_agents[0]["reply_overlap"] == 0.0


def test_effective_tool_iteration_ceiling_defaults_to_none() -> None:
    """ADR-0142 AC-1 (FRE-1391): unset on a ctx whose tool loop never ran."""
    row = _assemble(_base_ctx())
    assert row.effective_tool_iteration_ceiling is None


def test_effective_tool_iteration_ceiling_reads_the_stamped_value() -> None:
    """ADR-0142 AC-1 (FRE-1391): the assembler reads the post-grant stamp, not a re-derivation."""
    row = _assemble(_base_ctx(effective_tool_iteration_ceiling=35))
    assert row.effective_tool_iteration_ceiling == 35


def test_constraint_resolutions_defaults_to_empty() -> None:
    """ADR-0142 AC-2 (FRE-1391): a quiet turn's resolution list is empty, not absent."""
    row = _assemble(_base_ctx())
    assert row.constraint_resolutions == ()


def test_constraint_resolutions_preserves_order() -> None:
    """ADR-0142 AC-2 (FRE-1391): two pauses map to two entries, in order."""
    from personal_agent.orchestrator.types import ConstraintResolutionRecord

    ctx = _base_ctx(
        constraint_resolutions=[
            ConstraintResolutionRecord(constraint="tool_iteration_limit", action_id="continue_10"),
            ConstraintResolutionRecord(constraint="context_compression", action_id="stop_here"),
        ]
    )
    row = _assemble(ctx)
    assert row.constraint_resolutions == (
        {"constraint": "tool_iteration_limit", "action_id": "continue_10"},
        {"constraint": "context_compression", "action_id": "stop_here"},
    )


def test_reply_overlap_partial() -> None:
    """FRE-515: half the distinct content tokens contained → overlap 0.5."""
    summary = "alpha_token beta_token"
    reply = "the reply mentions alpha_token only"
    row = _assemble(_base_ctx(sub_agent_results=[_sub(summary)], final_reply=reply))
    assert row.sub_agents[0]["reply_overlap"] == 0.5


def test_reply_overlap_none_when_summary_has_no_content_tokens() -> None:
    """FRE-515: token-free summary (too short / punctuation) → overlap None."""
    row = _assemble(_base_ctx(sub_agent_results=[_sub("ok — a b c!")], final_reply="anything"))
    assert row.sub_agents[0]["reply_overlap"] is None


def test_error_fields_populated() -> None:
    classified = SimpleNamespace(category="timeout")
    row = _assemble(_base_ctx(error=ValueError("boom"), classified_error=classified))
    assert row.error_type == "ValueError"
    assert row.error_class == "timeout"


# ---------------------------------------------------------------------------
# FRE-517 — per-segment (sub-agent) route-trace rows
# ---------------------------------------------------------------------------


def test_assemble_sub_agent_route_trace_segment_shape() -> None:
    """A segment row carries the sub-agent's UUID task_id + self-sourced cost (FRE-517)."""
    ctx = _base_ctx()
    sub = _sub("postgres pooling notes", token_count=37, cost_usd=0.03)
    row = assemble_sub_agent_route_trace(ctx, sub)

    assert row.task_id == sub.task_id  # the sub-agent's UUID is the segment key
    assert str(row.trace_id) == ctx.trace_id
    assert str(row.session_id) == ctx.session_id
    assert row.model_role == "sub_agent"
    # Cost is self-sourced (api_costs has no task_id), so live == authoritative, reconciled.
    assert row.cost_live_usd == 0.03
    assert row.cost_authoritative_usd == 0.03
    assert row.cost_reconciled is True
    # Token split is unavailable; the estimate is preserved with an explicit flag.
    assert row.input_tokens == 0 and row.output_tokens == 0
    assert row.sub_agents[0]["token_count"] == 37
    assert row.sub_agents[0]["token_split_available"] is False
    assert row.sub_agents[0]["task_id"] == str(sub.task_id)
    assert row.error_type is None
    assert row.fallback_triggered is False


def test_assemble_sub_agent_route_trace_marks_failure() -> None:
    """A failed sub-agent segment records error_type without raising (FRE-517)."""
    sub = _sub("x", success=False, error="boom")
    row = assemble_sub_agent_route_trace(_base_ctx(), sub)
    assert row.error_type == "sub_agent_failed"
