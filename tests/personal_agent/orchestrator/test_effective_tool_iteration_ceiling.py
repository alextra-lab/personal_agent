"""ADR-0142 AC-1 (FRE-1391): the recorded ceiling is the one the loop actually used.

``_resolve_max_iterations`` stamps its return value onto ``ctx.effective_tool_iteration_
ceiling`` on every call, so the route-trace ledger can record what ran rather than what was
configured. A turn that never received a grant would read the same value either way — the
fails-if case is a *granted* turn, where the two values diverge.
"""

from __future__ import annotations

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.config import settings
from personal_agent.config.settings import AppConfig
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


def _gateway_output(task_type: TaskType) -> GatewayOutput:
    return GatewayOutput(
        intent=IntentResult(
            task_type=task_type,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        ),
        governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
        decomposition=DecompositionResult(strategy=DecompositionStrategy.SINGLE, reason="test"),
        context=AssembledContext(
            messages=[{"role": "user", "content": "hello"}],
            memory_context=None,
            tool_definitions=None,
        ),
        session_id="test-session",
        trace_id="test-trace",
    )


def _ctx(**overrides: object) -> ExecutionContext:
    defaults: dict[str, object] = dict(
        session_id="s1",
        trace_id="t1",
        user_message="hi",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    defaults.update(overrides)
    return ExecutionContext(**defaults)  # type: ignore[arg-type]


def test_stamps_ctx_with_the_returned_value() -> None:
    ctx = _ctx()
    resolved = ex._resolve_max_iterations(ctx)
    assert ctx.effective_tool_iteration_ceiling == resolved


def test_never_resolved_reads_none_not_a_stale_default() -> None:
    ctx = _ctx()
    assert ctx.effective_tool_iteration_ceiling is None


def test_recorded_ceiling_reflects_the_grant_not_the_configured_setting() -> None:
    """Fails-if case: a granted turn's stamp must diverge from the base config value."""
    ctx = _ctx()
    baseline = ex._resolve_max_iterations(ctx)
    assert ctx.effective_tool_iteration_ceiling == baseline

    # Simulate an accepted "Continue (10 more)" at a tool_iteration_limit pause (ADR-0076).
    ctx.tool_iteration_bonus += 10
    granted = ex._resolve_max_iterations(ctx)

    assert granted == baseline + 10
    assert ctx.effective_tool_iteration_ceiling == granted
    # The stamp moved with the grant; a bug recording the configured setting instead
    # would have left it at `baseline`.
    assert ctx.effective_tool_iteration_ceiling != baseline


# --- FRE-1394 / ADR-0142 D1: the task type stops allocating capability ---------------------


@pytest.mark.parametrize("task_type", list(TaskType), ids=lambda t: t.value)
def test_ceiling_is_the_same_for_every_task_type(task_type: TaskType) -> None:
    """AC-1 at the unit level: every task type resolves to the one global ceiling.

    Fails-if case: the old per-type cap gave ``conversational`` 6 and ``memory_recall`` 8.
    """
    ctx = _ctx(gateway_output=_gateway_output(task_type))
    assert ex._resolve_max_iterations(ctx) == settings.orchestrator_max_tool_iterations
    assert ctx.effective_tool_iteration_ceiling == settings.orchestrator_max_tool_iterations


@pytest.mark.parametrize("task_type", list(TaskType), ids=lambda t: t.value)
def test_grants_add_to_the_uniform_ceiling_for_every_task_type(task_type: TaskType) -> None:
    """AC-1's one exception: only an explicit grant moves a turn above the global ceiling."""
    ctx = _ctx(gateway_output=_gateway_output(task_type))
    ctx.tool_iteration_bonus = 10
    ctx.grounding_retrieval_grant = 3
    assert ex._resolve_max_iterations(ctx) == settings.orchestrator_max_tool_iterations + 13


def test_the_per_task_type_setting_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployed .env that still sets the deleted variable boots, and the value is ignored."""
    monkeypatch.setenv(
        "AGENT_ORCHESTRATOR_MAX_TOOL_ITERATIONS_BY_TASK_TYPE", '{"conversational": 6}'
    )
    config = AppConfig()
    assert "orchestrator_max_tool_iterations_by_task_type" not in AppConfig.model_fields
    assert not hasattr(config, "orchestrator_max_tool_iterations_by_task_type")
