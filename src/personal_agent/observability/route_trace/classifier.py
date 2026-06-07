"""Programmatic orchestration-event classification (FRE-452, taxonomy §3).

Reads a turn's terminal control-flow state and returns the single orchestration event
that best describes it. Only the **reliably programmatic** subset of the taxonomy is
emitted here: ``primary_handled``, ``delegate_called``, ``fallback_triggered``.

``delegate_result_used`` and ``delegate_result_discarded`` are *hybrid* (taxonomy §3.3 /
§3.4 / §6): there is no harness flag for genuine incorporation, so this classifier returns
the ``delegate_called`` floor and the disposition signals are persisted on the row
(``sub_agents`` JSONB + ``delegate_result_passed_to_synthesis``) for later rubric
refinement. The classifier never invents a hybrid label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from personal_agent.observability.route_trace.types import OrchestrationEvent

if TYPE_CHECKING:
    from personal_agent.orchestrator.types import ExecutionContext


def classify_orchestration_event(ctx: ExecutionContext) -> OrchestrationEvent:
    """Classify the turn's terminal orchestration event (taxonomy §3, §5.2 convention).

    The §5.2 single-best-terminal-event convention is applied (flagged ``[proposed — M2
    validates]`` in the spec): a turn carries the one event that best describes its
    terminal control-flow outcome.

    Args:
        ctx: The turn's execution context at completion. Only ``sub_agent_results`` and
            ``expansion_phase_results`` are read; both may be ``None`` (pre-expansion or
            primary-only turns).

    Returns:
        The programmatic orchestration event. One of ``"primary_handled"``,
        ``"delegate_called"``, or ``"fallback_triggered"``. The hybrid
        used/discarded events are never returned (taxonomy §6).
    """
    subs = list(getattr(ctx, "sub_agent_results", None) or [])
    phases = list(getattr(ctx, "expansion_phase_results", None) or [])

    # fallback_triggered (§3.5): a sub-agent/phase failed and the primary took over —
    # either an expansion phase failed, or every sub-agent failed.
    phase_failed = any(not getattr(p, "success", True) for p in phases)
    subs_all_failed = bool(subs) and all(not getattr(s, "success", True) for s in subs)
    if phase_failed or subs_all_failed:
        return "fallback_triggered"

    # primary_handled (§3.1): no sub-agent contribution at all.
    if not subs:
        return "primary_handled"

    # delegate_called (§3.2): sub-agents ran. used/discarded is hybrid — not decided here.
    return "delegate_called"
