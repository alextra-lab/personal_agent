"""Sub-agent tool principal (FRE-1388).

A sub-agent is a distinct governance principal from the primary: a model-authored
task, dispatched by a model-authored plan, running unattended, whose output the
primary treats as a finding. Its tool grant set is independent of the primary's
per-tool ``allowed_in_modes`` — this module is the only place that decides what a
sub-agent may use, and it does not fall back to the primary's policy for anything
absent from ``GovernanceConfig.sub_agent_tools``.

Owner decision (Linear FRE-1388, 2026-09-04): the grant set is ``run_python`` only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from personal_agent.governance.models import GovernanceConfig, Mode

# Owner directive (FRE-1388, 2026-09-04): a sub-agent runs unattended, so in a mode
# with no interactive approver, a tool's `requires_approval_in_modes` has no correct
# outcome. Sub-agents hold no tools at all in ALERT or DEGRADED. This binds every
# future grant, not only run_python, so it is enforced here rather than left as a
# per-tool config knob a future grant could omit.
SUB_AGENT_DENIED_MODES: frozenset[Mode] = frozenset({Mode.ALERT, Mode.DEGRADED})


@dataclass(frozen=True)
class SubAgentToolGrant:
    """Result of checking a sub-agent's requested tools against its grant set.

    Attributes:
        granted: Requested tool names the sub-agent may use.
        denied: Requested tool names refused, in request order.
        denial_reason: Human-readable reason for the refusal, or ``None`` when
            ``denied`` is empty.
    """

    granted: tuple[str, ...]
    denied: tuple[str, ...]
    denial_reason: str | None = None


def evaluate_sub_agent_tool_grant(
    requested_tools: Sequence[str],
    mode: Mode,
    config: GovernanceConfig,
) -> SubAgentToolGrant:
    """Filter a sub-agent's requested tools against the sub-agent principal's grant set.

    Args:
        requested_tools: Tool names a sub-agent task asked for.
        mode: Current brainstem operational mode.
        config: Loaded governance configuration.

    Returns:
        A :class:`SubAgentToolGrant` with the allowed subset and the refused subset.
        Both are empty when ``requested_tools`` is empty — no request, no refusal.
    """
    if not requested_tools:
        return SubAgentToolGrant(granted=(), denied=())

    if mode in SUB_AGENT_DENIED_MODES:
        return SubAgentToolGrant(
            granted=(),
            denied=tuple(requested_tools),
            denial_reason=f"sub-agents hold no tools in {mode.value} mode",
        )

    allowed = set(config.sub_agent_tools)
    granted = tuple(t for t in requested_tools if t in allowed)
    denied = tuple(t for t in requested_tools if t not in allowed)
    denial_reason = f"not in sub-agent tool grant set: {', '.join(denied)}" if denied else None
    return SubAgentToolGrant(granted=granted, denied=denied, denial_reason=denial_reason)
