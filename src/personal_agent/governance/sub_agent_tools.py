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
#
# FRE-1461 (2026-09-08): the premise above — "runs unattended" — is no longer true.
# A sub-agent can now reach the owner through the ADR-0076 constraint pause
# (`orchestrator/sub_agent_approval.py`), so an approval-gated tool DOES have a
# correct outcome in an attended mode. The directive itself stands unchanged: this
# ticket removes the reason that forced the revocation, and revisiting the
# revocation on its own merits is a separate owner decision, not a side effect.
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


def sub_agent_tool_requires_approval(
    tool_name: str,
    mode: Mode,
    config: GovernanceConfig,
) -> bool:
    """Report whether a granted tool needs the owner's word before a sub-agent runs it.

    Reads the same two policy fields, in the same order, as the primary's own gate
    (``personal_agent.tools.executor._check_permissions``): the always-on
    ``requires_approval`` flag, or this mode's membership of
    ``requires_approval_in_modes``. Sharing the rule is the point — a sub-agent that
    asked about a different set of tools than the primary does would be a second,
    silently divergent policy.

    A grant is necessary but not sufficient (see :class:`SubAgentToolGrant`), and so
    is this answer: an approved call still passes through
    ``ToolExecutionLayer.execute_tool``, which enforces the tool's own
    ``allowed_in_modes``/``forbidden_in_modes`` on top. This predicate can never
    grant access that the tool's base policy forbids.

    Args:
        tool_name: The granted tool name about to be dispatched.
        mode: Current brainstem operational mode.
        config: Loaded governance configuration.

    Returns:
        ``True`` when the owner must be asked before this call runs. ``False`` when
        the tool has no governance policy entry at all — an unpoliced tool is not
        made approval-gated by this ticket.
    """
    policy = config.tools.get(tool_name)
    if policy is None:
        return False
    return policy.requires_approval or mode.value in policy.requires_approval_in_modes
