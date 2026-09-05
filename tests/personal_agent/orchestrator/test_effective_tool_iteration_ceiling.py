"""ADR-0142 AC-1 (FRE-1391): the recorded ceiling is the one the loop actually used.

``_resolve_max_iterations`` stamps its return value onto ``ctx.effective_tool_iteration_
ceiling`` on every call, so the route-trace ledger can record what ran rather than what was
configured. A turn that never received a grant would read the same value either way — the
fails-if case is a *granted* turn, where the two values diverge.
"""

from __future__ import annotations

import personal_agent.orchestrator.executor as ex
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext


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
