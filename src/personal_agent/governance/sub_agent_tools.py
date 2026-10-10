"""Sub-agent tool principal (FRE-1388).

A sub-agent is a distinct governance principal from the primary: a model-authored
task, dispatched by a model-authored plan, running unattended, whose output the
primary treats as a finding. Its tool grant set is independent of the primary's
per-tool ``allowed_in_modes`` — this module is the only place that decides what a
sub-agent may use, and it does not fall back to the primary's policy for anything
absent from ``GovernanceConfig.sub_agent_tools``.

Owner decision (Linear FRE-1388, 2026-09-04): the grant set is ``run_python`` only.

FRE-1463 (2026-09-08) widened it and changed its shape. ``sub_agent_tools`` is now a
per-tool decision record rather than a list of granted names, so a refusal is an entry
carrying its reason instead of an absence indistinguishable from an unconsidered tool.
``web_search`` and ``search_memory`` join ``run_python``; ``fetch_url`` stays refused.
``recall_personal_history`` was refused the same day, then FRE-1467 reversed that refusal
the same day, and FRE-1473 gave it a sub-agent-only parameter ceiling (see
:func:`clamp_sub_agent_tool_params`). Read ``GovernanceConfig.granted_sub_agent_tool_names()``
for the grant set — the mapping's keys include the refusals.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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


def _describe_denial(denied: Sequence[str], config: GovernanceConfig) -> str:
    """Phrase a refusal so it names each tool and, where recorded, says why.

    A tool refused by an explicit decision record carries that record's reason
    (FRE-1463 AC-3). A tool nobody has decided on has no reason to quote, and
    keeps the pre-FRE-1463 wording.

    Args:
        denied: The refused tool names, in request order.
        config: Loaded governance configuration.

    Returns:
        A single human-readable sentence naming every refused tool.
    """
    parts: list[str] = []
    for name in denied:
        decision = config.sub_agent_tools.get(name)
        if decision is not None and not decision.granted:
            parts.append(f"{name} ({decision.reason})")
        else:
            parts.append(name)
    return f"not in sub-agent tool grant set: {', '.join(parts)}"


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

    # The granted subset, never the mapping's keys — a refused decision is still a key.
    allowed = set(config.granted_sub_agent_tool_names())
    granted = tuple(t for t in requested_tools if t in allowed)
    denied = tuple(t for t in requested_tools if t not in allowed)
    denial_reason = _describe_denial(denied, config) if denied else None
    return SubAgentToolGrant(granted=granted, denied=denied, denial_reason=denial_reason)


@dataclass(frozen=True)
class ParamClamp:
    """One tool argument the sub-agent principal's ceiling reduced (FRE-1473).

    Attributes:
        param: The clamped argument's name.
        requested: The value the sub-agent asked for.
        applied: The ceiling value used instead.
    """

    param: str
    requested: int | float | str
    applied: int


def clamp_sub_agent_tool_params(
    tool_name: str,
    arguments: Mapping[str, Any],
    config: GovernanceConfig,
) -> tuple[dict[str, Any], tuple[ParamClamp, ...]]:
    """Bound a sub-agent's tool arguments by this principal's declared limits.

    Two limits exist, both declared beside the grant they constrain. A ceiling (FRE-1473)
    reduces a numeric argument that exceeds it and leaves an in-bounds request unchanged. A
    forced value (FRE-1564) replaces the argument whatever the sub-agent sent, wrong type
    included, and is set when the sub-agent omitted it. A tool with no recorded decision, or a
    decision with neither limit, is a no-op: this function never invents a limit the config does
    not declare. It carries no notion of "primary" versus "sub-agent" itself — the caller must
    never invoke this for a primary-principal call (see ``dispatch_tool_call``'s ``principal``
    gate).

    Args:
        tool_name: The tool about to be dispatched.
        arguments: The sub-agent's requested arguments. Never mutated.
        config: Loaded governance configuration.

    Returns:
        A ``(clamped_arguments, applied_clamps)`` pair. ``clamped_arguments`` is always a new
        dict, equal to ``arguments`` when ``applied_clamps`` is empty. ``applied_clamps`` holds
        one entry per parameter actually reduced, in ``param_ceilings`` declaration order.
    """
    clamped = dict(arguments)
    decision = config.sub_agent_tools.get(tool_name)
    if decision is None:
        return clamped, ()

    applied: list[ParamClamp] = []
    for param, ceiling in decision.param_ceilings.items():
        requested = clamped.get(param)
        if not isinstance(requested, (int, float)) or isinstance(requested, bool):
            continue
        if requested > ceiling:
            applied.append(ParamClamp(param=param, requested=requested, applied=ceiling))
            clamped[param] = ceiling

    # FRE-1564: a pinned value replaces whatever the model sent, wrong type included, and is
    # set when the model omitted the argument so the tool's own default never decides it.
    for param, forced in decision.param_forced.items():
        if param in clamped and clamped[param] != forced:
            sent = clamped[param]
            shown = sent if isinstance(sent, (int, float, str)) else repr(sent)
            applied.append(ParamClamp(param=param, requested=shown, applied=forced))
        clamped[param] = forced

    return clamped, tuple(applied)


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
        ``True`` when the owner must be asked before this call runs: always for a
        ``per_call`` tool (FRE-1565), else by the tool's own policy. ``False`` when the
        tool is not ``per_call`` and has no governance policy entry at all — an
        unpoliced tool is not made approval-gated by FRE-1461.
    """
    if sub_agent_tool_asks_per_call(tool_name, config):
        return True
    policy = config.tools.get(tool_name)
    if policy is None:
        return False
    return policy.requires_approval or mode.value in policy.requires_approval_in_modes


def sub_agent_tool_asks_per_call(tool_name: str, config: GovernanceConfig) -> bool:
    """Report whether a sub-agent's call of this tool asks the owner every time (FRE-1565).

    A side-effecting worker tool is approved per call, with the call's exact arguments on
    the card, whatever the tool's own policy says. One answer then covers one call, not
    every later call of every worker in the turn.

    Args:
        tool_name: The granted tool name about to be dispatched.
        config: Loaded governance configuration.

    Returns:
        ``True`` when the tool's sub-agent decision is granted with ``approval: per_call``.
    """
    decision = config.sub_agent_tools.get(tool_name)
    return decision is not None and decision.granted and decision.approval == "per_call"
