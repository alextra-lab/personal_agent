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
4. **Per-call approval** (FRE-1565) — :meth:`SubAgentApprovalBroker.run_once`, for a
   side-effecting tool whose sub-agent decision is ``approval: per_call``. Each distinct
   call raises its own card with the exact arguments, and an identical call later in the
   turn is not run again. The per-tool-per-turn rule below holds for every other tool.

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
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from personal_agent.brainstem import ModeManagerError, get_current_mode
from personal_agent.config import settings
from personal_agent.config.governance_loader import (
    GovernanceConfigError,
    load_governance_config,
)
from personal_agent.governance.sub_agent_tools import (
    sub_agent_tool_asks_per_call,
    sub_agent_tool_requires_approval,
)
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

#: Longest canonical arguments a per-call card carries (FRE-1565). The card shows the
#: arguments in full or not at all: a shortened command could hide the part that sends
#: data out. A longer call is refused without a card.
PER_CALL_CARD_MAX_ARGUMENT_CHARS = 4000

# The pause resolutions that are not an answer from the owner. After one of them on a
# per-call card, the broker stops asking for the rest of the turn: a turn with no PWA
# client would otherwise wait the full pause timeout once per call (FRE-1565).
_NO_ANSWER_RESOLUTIONS = frozenset({"timeout_default", "connection_lost", "user_cancel"})


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


@dataclass(frozen=True)
class SideEffectOutcome:
    """One per-call decision and its result, shared by identical calls in a turn (FRE-1565).

    Attributes:
        approved: True only when the owner approved this exact call.
        reason: The pause's resolution for a real decision, else a local reason.
        content: The tool-role message for the worker: the tool's result when approved,
            else the refusal.
        executed: True only for the call that ran the tool. A coalesced copy is False.
        coalesced: True when an identical earlier call in this turn produced this outcome.
    """

    approved: bool
    reason: str
    content: str
    executed: bool = False
    coalesced: bool = False


def canonical_arguments(arguments: Mapping[str, object]) -> str:
    """Render a call's arguments as one stable string: the card text and the ledger key.

    Args:
        arguments: The call's arguments, after the sub-agent clamp.

    Returns:
        Compact JSON with sorted keys. Two calls are identical when these strings are.
    """
    return json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _refusal_content(tool_name: str, reason: str) -> str:
    """The tool-role message a worker receives for a refused per-call request.

    Args:
        tool_name: The refused tool.
        reason: Why it was refused.

    Returns:
        The JSON content of the tool-role message.
    """
    return json.dumps(
        {
            "status": "error",
            "hint": (
                f"{tool_name} was not approved for this call ({reason}). "
                "Continue without it. Do not retry the same change with other arguments."
            ),
        }
    )


def resolve_sub_agent_per_call_tools(
    granted_tools: Sequence[str],
    *,
    trace_id: str,
) -> frozenset[str]:
    """Return which of a sub-agent's granted tools ask the owner on every call (FRE-1565).

    Fails closed like :func:`resolve_sub_agent_approval_requirements`: a governance
    lookup failure returns every granted name, so a broken config asks per call rather
    than once per turn.

    Args:
        granted_tools: The tools this sub-agent may use, already filtered by the grant set.
        trace_id: Request trace identifier, for logging.

    Returns:
        The subset of ``granted_tools`` whose decision is ``approval: per_call``.
    """
    if not granted_tools:
        return frozenset()
    try:
        config = load_governance_config()
    except GovernanceConfigError as exc:
        log.warning(
            "sub_agent_per_call_lookup_failed",
            error=str(exc),
            granted_tools=list(granted_tools),
            trace_id=trace_id,
        )
        return frozenset(granted_tools)
    return frozenset(name for name in granted_tools if sub_agent_tool_asks_per_call(name, config))


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
        # FRE-1565: one future per distinct per-call request (tool, canonical arguments).
        # Only the first request for a key resolves it.
        self._side_effects: dict[tuple[str, str], asyncio.Future[SideEffectOutcome]] = {}
        self._owner_unreachable: str | None = None

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
        self,
        tool_name: str,
        task: str,
        worker_remaining_seconds: float,
        *,
        context: str | None = None,
    ) -> ApprovalOutcome:
        """Raise one pause for one tool, converting every failure into a denial.

        Args:
            tool_name: The granted tool about to be dispatched.
            task: The sub-agent's task description for the card.
            worker_remaining_seconds: What is left of this worker's own deadline.
            context: The card text. ``None`` gives the per-tool-per-turn card.

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
                # The card names the tool and the task, and NOT the call's
                # arguments — unlike the primary's own gate, which sends `args`.
                # That is deliberate and follows from the turn-wide scope: this
                # answer covers every sibling's differing, model-authored
                # arguments for the rest of the turn, so showing one call's
                # arguments would tell the owner they were deciding about that
                # specific call when they are not. A card must not misrepresent
                # what it is asking. Per-call argument review needs a per-call
                # decision, which is the design this ticket rejected on fan-out
                # grounds.
                context=context
                or (
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

    async def run_once(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        worker_type: str,
        task: str,
        deadline_monotonic: float,
        execute: Callable[[], Awaitable[str]],
    ) -> SideEffectOutcome:
        """Ask the owner about this exact call, run it once on approve (FRE-1565).

        A ``per_call`` tool: every distinct call raises its own card, which shows the
        exact arguments, and one answer covers one call. An identical call (same tool,
        same canonical arguments) later in the turn raises no card and does not run
        again: it receives the first call's outcome, with ``coalesced=True``. That is
        the duplicate control: two workers asking for the same Linear issue make one.

        Args:
            tool_name: The granted per-call tool about to be dispatched.
            arguments: The call's arguments, after the sub-agent clamp.
            worker_type: The worker type, shown on the card.
            task: The worker's task description, shown on the card.
            deadline_monotonic: The worker's own absolute deadline. The remaining time
                is read after the card lock is taken, not before.
            execute: Runs the tool and returns the tool-role message content. Called at
                most once per distinct call per turn, and only on approve.

        Returns:
            The :class:`SideEffectOutcome` for this call.

        Raises:
            asyncio.CancelledError: Propagated. The shared outcome is still resolved
                first, with a refusal, so no identical call waits forever and none runs
                a second time.
        """
        canonical = canonical_arguments(arguments)
        key = (tool_name, canonical)
        existing = self._side_effects.get(key)
        if existing is not None:
            # shield: cancelling this follower must not cancel the shared future.
            earlier = await asyncio.shield(existing)
            log.info(
                "sub_agent_side_effect_coalesced",
                tool_name=tool_name,
                worker_type=worker_type,
                approved=earlier.approved,
                reason=earlier.reason,
                trace_id=self._ctx.trace_id,
                session_id=self._ctx.session_id,
            )
            return replace(earlier, executed=False, coalesced=True)

        future: asyncio.Future[SideEffectOutcome] = asyncio.get_running_loop().create_future()
        self._side_effects[key] = future
        outcome: SideEffectOutcome | None = None
        try:
            outcome = await self._decide_and_run(
                tool_name, canonical, worker_type, task, deadline_monotonic, execute
            )
            return outcome
        finally:
            if not future.done():
                future.set_result(
                    outcome
                    if outcome is not None
                    else SideEffectOutcome(
                        approved=False,
                        reason="cancelled_before_completion",
                        content=_refusal_content(tool_name, "cancelled_before_completion"),
                    )
                )

    async def _decide_and_run(
        self,
        tool_name: str,
        canonical: str,
        worker_type: str,
        task: str,
        deadline_monotonic: float,
        execute: Callable[[], Awaitable[str]],
    ) -> SideEffectOutcome:
        """Raise the per-call card, then run the call on approve.

        Args:
            tool_name: The per-call tool.
            canonical: The call's canonical arguments.
            worker_type: The worker type, shown on the card.
            task: The worker's task description, shown on the card.
            deadline_monotonic: The worker's own absolute deadline.
            execute: Runs the tool and returns the tool-role message content.

        Returns:
            The :class:`SideEffectOutcome` of this call.
        """
        if len(canonical) > PER_CALL_CARD_MAX_ARGUMENT_CHARS:
            decision = ApprovalOutcome(approved=False, reason="arguments_too_long_for_card")
        else:
            # One card at a time. The owner reads the cards in order, and the budget
            # read below cannot go stale behind another card's wait.
            async with self._lock:
                if self._owner_unreachable is not None:
                    decision = ApprovalOutcome(
                        approved=False, reason="owner_did_not_answer_this_turn"
                    )
                else:
                    decision = await self._ask(
                        tool_name,
                        task,
                        deadline_monotonic - time.monotonic(),
                        context=(
                            f"A {worker_type} worker wants to run {tool_name} with these "
                            f"exact arguments: {canonical}. "
                            f"Task: {task[:_TASK_PREVIEW_CHARS]}. "
                            "Allowing covers this one call only."
                        ),
                    )
                    if decision.reason in _NO_ANSWER_RESOLUTIONS or decision.reason.startswith(
                        "pause_failed"
                    ):
                        self._owner_unreachable = decision.reason

        log.info(
            "sub_agent_side_effect_decided",
            tool_name=tool_name,
            worker_type=worker_type,
            approved=decision.approved,
            reason=decision.reason,
            argument_chars=len(canonical),
            trace_id=self._ctx.trace_id,
            session_id=self._ctx.session_id,
        )
        if not decision.approved:
            return SideEffectOutcome(
                approved=False,
                reason=decision.reason,
                content=_refusal_content(tool_name, decision.reason),
            )
        content = await execute()
        return SideEffectOutcome(
            approved=True, reason=decision.reason, content=content, executed=True
        )


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
