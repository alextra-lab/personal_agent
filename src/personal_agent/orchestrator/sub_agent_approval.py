"""The sub-agent's channel to the owner (FRE-1461).

A sub-agent could not ask the owner anything. The primary already could: it stops
and asks through the ADR-0076 constraint pause, which ADR-0144 D3 had already reused
for a second purpose, so the mechanism was general and only the sub-agent path was
never wired to it. "Unattended" was a property of the implementation, not of
sub-agents.

This module is that wiring. It owns three things:

1. **Whether an ask is needed** — :func:`resolve_sub_agent_approval_requirements`,
   which reads the same governance fields the primary's own gate reads.
2. **The fan-out policy** — :class:`SubAgentApprovalBroker`, which asks **once per
   distinct tool name, per turn** and applies that answer to every sibling in the
   same turn. One turn spawned six sub-agents on 2026-09-07 and eight on 2026-09-05;
   a mechanism that asked each of them separately would be switched off by whoever
   met it first, which is why the answer's scope is the turn and not the call.
3. **The safe branch** — every failure to obtain an answer resolves to a denial.

Why the tool NAME is the cache key. Arguments differ per sub-agent by design, so
keying on the call would restore one prompt per worker. The owner's decision is
about the capability, not the call site — the same reading the primary's gate takes,
which keys approval on policy and mode, never on argument identity.

How the broker reaches the sub-agent. ``dispatch_tool_call`` deliberately accepts
request primitives and never an ``ExecutionContext``, so the broker crosses that
boundary through an async-safe ``ContextVar`` — the mechanism
``constraint_options.py`` already uses for the artifact-builder resolution, and
``config.selection`` for per-turn model selection. A child task inherits a COPY of
the context, so a rebind inside a child would not propagate outward; this design
never rebinds. The carrier holds one broker object and the cache is mutated in
place, so an answer recorded inside ``asyncio.wait_for``'s internal task is visible
to the next sub-agent. ``execute_task`` sets the carrier at turn start and resets it
in the same ``finally`` that resets the artifact carrier, so no answer outlives its
turn.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from personal_agent.brainstem import ModeManagerError, get_current_mode
from personal_agent.config import settings
from personal_agent.config.governance_loader import (
    GovernanceConfigError,
    load_governance_config,
)
from personal_agent.governance.sub_agent_tools import sub_agent_tool_requires_approval
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.orchestrator.types import ExecutionContext

log = get_logger(__name__)

#: The ADR-0076 constraint this module raises. Registered in
#: ``constraint_options.CONSTRAINT_OPTIONS`` and in the ``ConstraintName`` literal.
#: Typed as that literal rather than ``str`` so the pause call type-checks against
#: ``ConstraintName`` without an ignore — the drift FRE-881 closed for
#: ``attachment_cost`` began with exactly such an ignore.
SUB_AGENT_APPROVAL_CONSTRAINT: Literal["sub_agent_tool_approval"] = "sub_agent_tool_approval"

#: The two answers. ``DENY_ACTION_ID`` is last in the option list, which is what
#: makes it the safe default applied on timeout or a lost connection.
APPROVE_ACTION_ID = "approve_sub_agent_tool"
DENY_ACTION_ID = "deny_sub_agent_tool"

# The work that happens AROUND the wait, which the worker must have budget left to
# finish after the pause returns: option resolution, waiter registration, the
# WAITING_FOR_CHOICE phase span, pause accounting, the CONSTRAINT_RESOLVED emission,
# and the refusal message append. All of it is local and in-process — no network call
# sits on the refusal path — so a small fixed reserve covers it. Without this reserve
# a worker holding "the pause ceiling plus epsilon" clears the budget guard and can
# still have its own wait_for deadline fire mid-pause, losing the refusal it was
# supposed to record.
APPROVAL_FINALIZATION_RESERVE_SECONDS = 5.0

# Longest task description carried into the pause card. The card explains WHY the
# owner is being asked; it is not a place to render an entire sub-agent brief.
_TASK_PREVIEW_CHARS = 120


@dataclass(frozen=True)
class ApprovalOutcome:
    """One resolved answer about one tool, for one turn.

    Attributes:
        approved: True only when the owner actively chose to allow the tool. Every
            other path — timeout, no client, a failed pause, an exhausted worker
            budget — is False.
        reason: How the outcome was reached, for logs and for the tool-role message
            the sub-agent receives. Carries the pause's own ``resolution`` for a
            real decision, or a local reason string otherwise.
    """

    approved: bool
    reason: str


def resolve_sub_agent_approval_requirements(
    granted_tools: Sequence[str],
    *,
    trace_id: str,
) -> frozenset[str]:
    """Return which of a sub-agent's granted tools need the owner's word first.

    Resolved once per sub-agent rather than once per tool call: the governance
    config is read from YAML on every call, and the answer cannot change inside a
    single worker's loop in any way that matters.

    Fails closed. A governance or mode lookup failure returns **every** granted name,
    so a broken config makes the machinery ask (and, with no approver, deny) instead
    of silently disabling the gate. This mirrors
    ``expansion_controller._compute_sub_agent_grants``, which denies every requested
    tool on the same failure.

    Args:
        granted_tools: The tools this sub-agent may use — already filtered through
            the FRE-1388 grant set, never the raw model-authored request.
        trace_id: Request trace identifier, for logging.

    Returns:
        The subset of ``granted_tools`` requiring approval in the current mode.
        Empty for an empty grant, without consulting governance at all — no request,
        no lookup.
    """
    if not granted_tools:
        return frozenset()

    try:
        mode = get_current_mode()
        config = load_governance_config()
    except (GovernanceConfigError, ModeManagerError) as exc:
        log.warning(
            "sub_agent_approval_lookup_failed",
            error=str(exc),
            granted_tools=list(granted_tools),
            trace_id=trace_id,
        )
        return frozenset(granted_tools)

    return frozenset(
        name for name in granted_tools if sub_agent_tool_requires_approval(name, mode, config)
    )


class SubAgentApprovalBroker:
    """This turn's approval channel, shared by every sub-agent it spawns.

    One instance per turn. It holds the turn's ``ExecutionContext`` so the pause it
    raises is accounted against that turn (ADR-0142 D4a) exactly as the primary's own
    pauses are, and it holds the per-tool answers so a fan-out asks once.
    """

    def __init__(self, ctx: "ExecutionContext") -> None:
        """Create the broker for one turn.

        Args:
            ctx: The turn's execution context. Supplies the session, trace and user
                identity the pause needs, and gives the pause its ADR-0142 D4a
                lifetime cap and pause accounting.
        """
        self._ctx = ctx
        self._decisions: dict[str, ApprovalOutcome] = {}
        self._lock = asyncio.Lock()

    async def decide(
        self,
        tool_name: str,
        *,
        task: str,
        worker_remaining_seconds: float,
    ) -> ApprovalOutcome:
        """Return this turn's answer for ``tool_name``, asking the owner if needed.

        The whole sequence — cache lookup, the pause, the failure-to-denial
        conversion and the cache write — runs under one lock, so a sibling arriving
        while an ask is in flight waits for that answer rather than raising a second
        card. Dispatch is serialized today, so the lock costs nothing now; it is what
        keeps the one-prompt guarantee true if dispatch ever becomes concurrent.

        Args:
            tool_name: The granted tool about to be dispatched.
            task: The sub-agent's task description, shown on the card so the owner
                knows what they are approving. Truncated for display.
            worker_remaining_seconds: What is left of this worker's own deadline. A
                pause is opened only when this exceeds the pause ceiling plus
                :data:`APPROVAL_FINALIZATION_RESERVE_SECONDS`; otherwise the call is
                denied without a card, so the worker always survives its own pause
                with enough budget left to record the refusal.

        Returns:
            The :class:`ApprovalOutcome` for this tool, for this turn.

        Raises:
            asyncio.CancelledError: Propagated, never converted to a denial. An
                external dispatch cancellation must stay a cancellation —
                ``run_sub_agent`` re-raises it deliberately. The Stop button does not
                take this path: it resolves the waiter with ``user_cancel``, which
                arrives here as an ordinary denial.
        """
        async with self._lock:
            cached = self._decisions.get(tool_name)
            if cached is not None:
                log.info(
                    "sub_agent_tool_approval_reused",
                    tool_name=tool_name,
                    approved=cached.approved,
                    reason=cached.reason,
                    trace_id=self._ctx.trace_id,
                    session_id=self._ctx.session_id,
                )
                return cached

            outcome = await self._ask(tool_name, task, worker_remaining_seconds)
            self._decisions[tool_name] = outcome
            return outcome

    async def _ask(
        self, tool_name: str, task: str, worker_remaining_seconds: float
    ) -> ApprovalOutcome:
        """Raise one pause for one tool, converting every failure into a denial.

        Args:
            tool_name: The granted tool about to be dispatched.
            task: The sub-agent's task description for the card.
            worker_remaining_seconds: What is left of this worker's own deadline.

        Returns:
            The resolved :class:`ApprovalOutcome`.
        """
        floor = settings.constraint_pause_timeout_seconds + APPROVAL_FINALIZATION_RESERVE_SECONDS
        if worker_remaining_seconds <= floor:
            log.warning(
                "sub_agent_tool_approval_skipped_no_budget",
                tool_name=tool_name,
                worker_remaining_seconds=round(worker_remaining_seconds, 3),
                required_seconds=round(floor, 3),
                trace_id=self._ctx.trace_id,
                session_id=self._ctx.session_id,
            )
            return ApprovalOutcome(approved=False, reason="insufficient_worker_budget")

        # Imported here, not at module scope: executor.py imports this module's
        # carrier for the turn lifecycle, so a module-level import would close a
        # cycle.
        from personal_agent.orchestrator.executor import (  # noqa: PLC0415
            _maybe_pause_for_constraint,
        )

        try:
            decision = await _maybe_pause_for_constraint(
                session_id=self._ctx.session_id,
                trace_id=self._ctx.trace_id,
                user_id=self._ctx.user_id,
                constraint=SUB_AGENT_APPROVAL_CONSTRAINT,
                context=(
                    f"A sub-agent wants to use {tool_name}. "
                    f"Task: {task[:_TASK_PREVIEW_CHARS]}. "
                    f"Allowing covers every sub-agent in this turn."
                ),
                # No stored preference for this constraint. A remembered "always
                # allow" would hand an unattended worker a governed tool for every
                # future turn without anyone seeing it — the same reasoning that
                # keeps attachment_cost preference-free (ADR-0101 §8b / FRE-691).
                allow_preference=False,
                ctx=self._ctx,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # _maybe_pause_for_constraint does not guard its own awaits, and the
            # waiter contract re-raises a registration-callback failure after
            # cleanup. A safety mechanism that breaks the turn gets disabled by
            # whoever meets it first, so a broken pause is a denial, not an error.
            log.warning(
                "sub_agent_tool_approval_pause_failed",
                tool_name=tool_name,
                error=str(exc),
                trace_id=self._ctx.trace_id,
                session_id=self._ctx.session_id,
            )
            return ApprovalOutcome(approved=False, reason=f"pause_failed: {exc}")

        approved = str(decision) == APPROVE_ACTION_ID
        log.info(
            "sub_agent_tool_approval_decided",
            tool_name=tool_name,
            approved=approved,
            action_id=str(decision),
            resolution=decision.resolution,
            trace_id=self._ctx.trace_id,
            session_id=self._ctx.session_id,
        )
        return ApprovalOutcome(approved=approved, reason=decision.resolution)


_sub_agent_approval_broker: contextvars.ContextVar[SubAgentApprovalBroker | None] = (
    contextvars.ContextVar("sub_agent_approval_broker", default=None)
)


def set_sub_agent_approval_broker(
    broker: SubAgentApprovalBroker | None,
) -> contextvars.Token[SubAgentApprovalBroker | None]:
    """Publish this turn's approval broker for the current async context.

    Args:
        broker: The broker to carry to the sub-agent tool boundary, or ``None``.

    Returns:
        A token for :func:`reset_sub_agent_approval_broker`, used by
        ``execute_task``'s lifecycle ``finally`` and by test isolation.
    """
    return _sub_agent_approval_broker.set(broker)


def get_sub_agent_approval_broker() -> SubAgentApprovalBroker | None:
    """Return this turn's approval broker, or ``None`` when there is no turn.

    Returns:
        The broker set by ``execute_task``, or ``None`` for a caller outside a turn.
        ``None`` is not "allow": the sub-agent tool loop treats a missing approver as
        a denial, because an approval-required tool with nobody to ask has no other
        safe answer.
    """
    return _sub_agent_approval_broker.get()


def reset_sub_agent_approval_broker(
    token: contextvars.Token[SubAgentApprovalBroker | None],
) -> None:
    """Restore the approval broker to a prior value.

    Args:
        token: The token returned by :func:`set_sub_agent_approval_broker`.
    """
    _sub_agent_approval_broker.reset(token)
