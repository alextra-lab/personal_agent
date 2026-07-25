"""AG-UI implementation of UITransportProtocol.

Durably writes internal events to the Postgres ``session_events`` table via
:class:`~personal_agent.transport.agui.event_buffer.SessionEventBuffer`, then
pushes the sequenced envelopes to a bounded per-session asyncio.Queue.
The WebSocket endpoint in
:mod:`personal_agent.transport.agui.ws_endpoint` drains the queue and
streams events to the connected client; on reconnect, events are replayed
from Postgres.

This class satisfies :class:`~personal_agent.transport.protocols.UITransportProtocol`
via structural typing — no explicit base class is required.

See: docs/architecture_decisions/ADR-0075-websocket-transport.md
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, TypedDict
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from personal_agent.error_classification import ClassifiedError

from personal_agent.service.database import AsyncSessionLocal
from personal_agent.telemetry import get_logger
from personal_agent.transport.agui.adapter import to_agui_event
from personal_agent.transport.agui.event_buffer import SessionEventBuffer
from personal_agent.transport.agui.ws_endpoint import (
    ApprovalDecision,
    WaiterMetadata,
    get_event_queue,
    register_constraint_waiter,
    register_waiter,
)
from personal_agent.transport.events import (
    CancelledEvent,
    ClassifiedErrorEvent,
    ConstraintPauseEvent,
    ConstraintResolvedEvent,
    InternalEvent,
    InterruptEvent,
    Phase,
    PhaseEndEvent,
    PhaseStartEvent,
    StateUpdateEvent,
    TextDeltaEvent,
    ToolApprovalRequestEvent,
    ToolEndEvent,
    ToolStartEvent,
)

log = get_logger(__name__)

# ── Per-session emit serialization (FRE-518) ────────────────────────────────
#
# Concurrent emitters (the main chat coroutine pushing the final response delta
# + the ``cg:turn-projector`` consumer task pushing ``turn_status``) persist on
# separate DB connections, so the ``await buf.append`` resume order can invert
# the Postgres ``seq`` order. A higher-seq event enqueued before a lower-seq one
# then gets permanently dropped by the sender's ``max_sent_seq`` guard and the
# client's ``lastSeq`` guard, orphaning the final response from the live + replay
# paths (ADR-0075). Serialising the ``persist → set seq → enqueue`` critical
# section per session restores the invariant *enqueue order == seq order*.
_session_emit_locks: dict[str, asyncio.Lock] = {}
# Bounded to avoid unbounded growth across distinct sessions; far beyond any
# realistic concurrent-session working set so a held lock is never evicted in
# practice (mirrors the projector's ``_MAX_TRACKED_TRACES`` pattern).
_MAX_EMIT_LOCKS = 4096


def _get_emit_lock(session_id: str) -> asyncio.Lock:
    """Return (creating if needed) the per-session emit-serialization lock.

    Eviction skips any **held** lock (FRE-986): evicting a lock mid-emit would let a
    later emit for that session obtain a *second* lock, breaking the one-stable-lock
    invariant this cache exists to uphold (FRE-518) — and with it the race-freedom of
    the ``phase_state`` snapshot, whose build must be serialized against its session's
    other emits. The oldest *unheld* entry is evicted; if every entry is held (never
    the case below any realistic concurrent-session working set) the cache grows
    transiently rather than corrupt the invariant.
    """
    lock = _session_emit_locks.get(session_id)
    if lock is None:
        if len(_session_emit_locks) >= _MAX_EMIT_LOCKS:
            for candidate, candidate_lock in _session_emit_locks.items():
                if not candidate_lock.locked():
                    del _session_emit_locks[candidate]
                    break
        lock = asyncio.Lock()
        _session_emit_locks[session_id] = lock
    return lock


async def _persist_and_enqueue(session_id: str, make_event: Callable[[], InternalEvent]) -> None:
    """Persist an event, then enqueue the sequenced envelope for live WS delivery.

    ``make_event`` is called **inside** the per-session emit lock, so the seq order equals
    enqueue order across concurrent emitters (FRE-518) *and* an event whose content is derived
    from mutable in-process state (the ``phase_state`` snapshot, read from ``_phase_registry``)
    is built at seq-assignment time. That is what makes the highest-seq snapshot always reflect
    the latest registry state: were the snapshot built *before* acquiring the lock, a preempted
    ``phase_end`` could enqueue a stale empty snapshot above a concurrent ``phase_start``'s
    snapshot and defeat newest-wins convergence (FRE-986, ADR-0123 §6).

    Args:
        session_id: Target session identifier.
        make_event: Zero-arg factory producing the event to emit, invoked under the lock.
    """
    async with _get_emit_lock(session_id):
        envelope = to_agui_event(make_event())
        event_type = envelope["type"]
        try:
            async with AsyncSessionLocal() as db:
                buf = SessionEventBuffer(db)
                seq = await buf.append(
                    session_id=UUID(session_id),
                    event_type=event_type,
                    payload=envelope,
                )
            envelope["seq"] = seq
        except Exception:
            log.exception(
                "transport.persist_event_failed", session_id=session_id, event_type=event_type
            )
            return

        queue = get_event_queue(session_id)
        try:
            queue.put_nowait(envelope)
        except asyncio.QueueFull:
            log.warning(
                "transport.queue_full",
                session_id=session_id,
                event_type=event_type,
            )


async def _push_event(event: InternalEvent, session_id: str) -> None:
    """Persist a pre-built event, then enqueue it for live WS delivery (FRE-518)."""
    await _persist_and_enqueue(session_id, lambda: event)


# ── Session-keyed current-phase projection (FRE-986, ADR-0123 §6) ────────────
#
# The phase surface must be a *projection of current phase state*, not an accumulation of the
# event log. ``PhaseStart`` / ``PhaseEnd`` remain on the wire as deltas (they carry the AC-2 gap
# semantics and feed the AC-7 summary), but every transition additionally emits a full-state
# ``phase_state`` snapshot — the complete set of currently-active phases, keyed by session — so a
# reconnecting client converges from the newest message alone and self-corrects when a delta is
# dropped, exactly as ``turn_status`` does (``projector.py`` docstring). Single-instance topology:
# this in-process registry is authoritative (owner-approved 2026-07-25; no Redis bus).


class _PhaseActive(TypedDict):
    """One active-phase entry in a ``phase_state`` snapshot (the client-facing wire shape)."""

    phase: str
    phase_id: str
    started_at: str
    detail: str | None
    parent_id: str | None


class _PhaseSnapshot(TypedDict):
    """The ``phase_state`` full-state payload — every currently-active phase for a session."""

    active: list[_PhaseActive]


@dataclass(frozen=True)
class _PhaseRecord:
    """One currently-active phase instance held in the in-process registry."""

    phase: Phase
    phase_id: str
    started_at: str  # ISO-8601 UTC, held verbatim (AC-3(b) byte-equality)
    detail: str | None
    parent_id: str | None

    def as_active(self) -> _PhaseActive:
        """The snapshot ``active`` entry — the fields a client needs to render this phase."""
        return {
            "phase": self.phase.value,
            "phase_id": self.phase_id,
            "started_at": self.started_at,
            "detail": self.detail,
            "parent_id": self.parent_id,
        }


#: session_id → phase_id → record. A session appears iff it has ≥1 active phase; it self-cleans
#: on the last phase end (``phase_span`` guarantees start/end pairing), so this stays small under
#: normal operation and the cap below is a pure backstop.
_phase_registry: dict[str, dict[str, _PhaseRecord]] = {}
#: Backstop bound on tracked sessions. Reached only on a leak (phases started without a paired
#: end while the process lives); a process crash clears the dict outright. We never evict an
#: *active* session — that would let its next transition emit a false authoritative empty
#: snapshot — so the cap instead rejects a brand-new session, degrading it to delta-only.
_MAX_PHASE_SESSIONS = 8192


def _phase_registry_add(
    session_id: str,
    *,
    phase: Phase,
    phase_id: str,
    started_at: str,
    detail: str | None,
    parent_id: str | None,
) -> bool:
    """Record a started phase, returning whether the session is tracked.

    Session-sticky: an already-tracked session always accepts the phase; a brand-new session is
    rejected at the cap (returns ``False``) so it degrades to delta-only rather than an active
    session ever being evicted (FRE-986).

    Returns:
        ``True`` if the session is tracked (a snapshot should be emitted), ``False`` if rejected.
    """
    phases = _phase_registry.get(session_id)
    if phases is None:
        if len(_phase_registry) >= _MAX_PHASE_SESSIONS:
            log.warning("transport.phase_registry_full", session_id=session_id)
            return False
        phases = _phase_registry[session_id] = {}
    phases[phase_id] = _PhaseRecord(
        phase=phase, phase_id=phase_id, started_at=started_at, detail=detail, parent_id=parent_id
    )
    return True


def _phase_registry_remove(session_id: str, phase_id: str) -> None:
    """Drop an ended phase; drop the session key when it empties (self-clean)."""
    phases = _phase_registry.get(session_id)
    if phases is None:
        return
    phases.pop(phase_id, None)
    if not phases:
        del _phase_registry[session_id]


def _phase_snapshot_value(session_id: str) -> _PhaseSnapshot:
    """The full-state ``phase_state`` payload — all currently-active phases for the session."""
    phases = _phase_registry.get(session_id, {})
    return {"active": [rec.as_active() for rec in phases.values()]}


async def _emit_phase_snapshot(session_id: str) -> None:
    """Emit the current-phase full-state replacement (a ``phase_state`` STATE_DELTA).

    The value is read from ``_phase_registry`` inside the emit lock (via ``_persist_and_enqueue``),
    so the highest-seq snapshot always reflects the latest registry state (race-freedom, FRE-986).
    """
    await _persist_and_enqueue(
        session_id,
        lambda: StateUpdateEvent(
            key="phase_state", value=_phase_snapshot_value(session_id), session_id=session_id
        ),
    )


async def _emit_phase_snapshot_best_effort(session_id: str, phase_id: str) -> None:
    """Emit the ``phase_state`` snapshot, swallowing any failure (AC-6).

    Independent of the paired delta emit: the snapshot fires whether or not the delta
    succeeded, and a snapshot failure is a cosmetic loss that must never fail a turn.
    """
    try:
        await _emit_phase_snapshot(session_id)
    except Exception:
        log.exception(
            "transport.phase_snapshot_emit_failed", session_id=session_id, phase_id=phase_id
        )


async def emit_done(session_id: str) -> None:
    """Persist the terminal ``DONE`` row, then enqueue the close sentinel (FRE-518).

    Runs under the same per-session emit lock as :func:`_push_event` so the DONE
    row's ``seq`` is ordered after every prior live emit and the ``None`` sentinel
    is enqueued in seq order behind them. The sentinel closes the sender's live
    drain loop; the persisted DONE row is what reconnect replay delivers.

    Best-effort: a persistence failure still pushes the sentinel so the live
    socket is not left hanging.

    Args:
        session_id: Target session identifier.
    """
    async with _get_emit_lock(session_id):
        try:
            async with AsyncSessionLocal() as db:
                buf = SessionEventBuffer(db)
                await buf.append(
                    session_id=UUID(session_id),
                    event_type="DONE",
                    payload={"type": "DONE"},
                )
        except Exception:
            log.exception("transport.persist_done_failed", session_id=session_id)
        queue = get_event_queue(session_id)
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            log.warning("transport.queue_full", session_id=session_id, event_type="DONE")


async def register_and_push_constraint(
    *,
    session_id: str,
    request_id: str,
    event: ConstraintPauseEvent,
    metadata: WaiterMetadata,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Register a constraint waiter, push the pause event, await the decision.

    Registration happens before the push (race-free, ADR-0076). The push runs
    **unconditionally**, including when no WebSocket connection is attached: the
    event is persisted, so a client reconnecting inside the timeout is replayed
    the pause and can still answer it (FRE-928). A caller that genuinely has no
    client — headless, CLI — falls back to the default when the timeout expires.

    Args:
        session_id: Target session identifier.
        request_id: Unique identifier for this pause round-trip.
        event: The ``ConstraintPauseEvent`` to deliver once registered.
        metadata: Waiter metadata (options, default) for validation/timeout.
        timeout_seconds: Seconds before the default option auto-applies.

    Returns:
        Resolution payload dict with ``decision``, ``resolution``, and an
        optional ``remember`` flag.
    """

    async def _push() -> None:
        await _push_event(event, session_id)

    return await register_constraint_waiter(
        session_id=session_id,
        request_id=request_id,
        timeout_seconds=timeout_seconds,
        metadata=metadata,
        on_registered=_push,
    )


async def emit_constraint_resolved(
    *,
    request_id: str,
    session_id: str,
    constraint: str,
    action_id: str,
    resolution: str,
) -> None:
    """Persist + enqueue a ``CONSTRAINT_RESOLVED`` event (ADR-0076)."""
    await _push_event(
        ConstraintResolvedEvent(
            request_id=request_id,
            session_id=session_id,
            constraint=constraint,
            action_id=action_id,
            resolution=resolution,  # type: ignore[arg-type]
        ),
        session_id,
    )


async def emit_cancelled(*, session_id: str, trace_id: str, reason: str = "user_cancel") -> None:
    """Persist + enqueue a ``CANCELLED`` event (ADR-0076 Stop button)."""
    await _push_event(
        CancelledEvent(session_id=session_id, trace_id=trace_id, reason=reason),
        session_id,
    )


async def emit_classified_error(
    *,
    session_id: str,
    trace_id: str,
    classified: ClassifiedError,
) -> None:
    """Persist + enqueue a ``RUN_ERROR`` event (FRE-398).

    Args:
        session_id: Target session identifier.
        trace_id: Trace context identifier for telemetry correlation.
        classified: Structured error description from the classifier.
    """
    await _push_event(
        ClassifiedErrorEvent(
            session_id=session_id,
            trace_id=trace_id,
            category=classified.category,
            reason=classified.reason,
            next_step=classified.next_step,
            actions=list(classified.actions),
            partial=classified.partial,
        ),
        session_id,
    )


async def emit_turn_status(*, session_id: str, value: Mapping[str, Any]) -> None:
    """Persist + enqueue a ``turn_status`` STATE_DELTA event (ADR-0076).

    Args:
        session_id: Target session identifier.
        value: Turn metrics payload (context tokens, tool iteration, cost).
    """
    await _push_event(
        StateUpdateEvent(key="turn_status", value=dict(value), session_id=session_id),
        session_id,
    )


async def emit_phase_start(
    *,
    session_id: str | None,
    phase: Phase,
    phase_id: str,
    started_at: str,
    detail: str | None = None,
    parent_id: str | None = None,
) -> None:
    """Persist + enqueue a ``PHASE_START`` event (ADR-0123 §2).

    Best-effort by construction: a falsy ``session_id`` (headless / CLI / eval —
    no transport client) is a no-op, and any ``Exception`` from the emit path is
    logged and swallowed so a cosmetic progress failure can never fail a turn
    (AC-6). ``BaseException`` / :class:`asyncio.CancelledError` are deliberately
    **not** caught — a cancelled turn must stay cancelled.

    Args:
        session_id: Target session identifier, or ``None`` to skip emission.
        phase: Which phase began.
        phase_id: Unique id for this phase instance (pairs with the end).
        started_at: ISO-8601 UTC server timestamp of the phase start.
        detail: Optional human-readable qualifier.
        parent_id: Parent phase id when this is a concurrent child.
    """
    if not session_id:
        return
    # Register first (synchronous, authoritative) so the snapshot reflects this start even if
    # the delta emit below fails; a session rejected at the cap degrades to delta-only (FRE-986).
    tracked = _phase_registry_add(
        session_id,
        phase=phase,
        phase_id=phase_id,
        started_at=started_at,
        detail=detail,
        parent_id=parent_id,
    )
    try:
        await _push_event(
            PhaseStartEvent(
                phase=phase,
                phase_id=phase_id,
                session_id=session_id,
                started_at=started_at,
                detail=detail,
                parent_id=parent_id,
            ),
            session_id,
        )
    except Exception:
        log.exception(
            "transport.phase_start_emit_failed",
            session_id=session_id,
            phase=phase.value,
            phase_id=phase_id,
        )
    if tracked:
        await _emit_phase_snapshot_best_effort(session_id, phase_id)


async def emit_phase_end(
    *,
    session_id: str | None,
    phase: Phase,
    phase_id: str,
    parent_id: str | None = None,
    ok: bool = True,
) -> None:
    """Persist + enqueue a ``PHASE_END`` event (ADR-0123 §2).

    Same best-effort posture as :func:`emit_phase_start` (AC-6).

    Args:
        session_id: Target session identifier, or ``None`` to skip emission.
        phase: Which phase ended.
        phase_id: Id of the phase instance that ended (pairs with its start).
        parent_id: Parent phase id when this ended a concurrent child.
        ok: ``False`` when the phase ended because the wrapped work raised
            (FRE-936 / AC-9(b)); see :class:`~personal_agent.transport.events.PhaseEndEvent`.
    """
    if not session_id:
        return
    # Capture tracked-ness before removal: an untracked (rejected) session must not emit a
    # snapshot at all, else it would assert a false authoritative empty state (FRE-986).
    was_tracked = session_id in _phase_registry
    _phase_registry_remove(session_id, phase_id)  # authoritative even if the delta emit fails
    try:
        await _push_event(
            PhaseEndEvent(
                phase=phase,
                phase_id=phase_id,
                session_id=session_id,
                parent_id=parent_id,
                ok=ok,
            ),
            session_id,
        )
    except Exception:
        log.exception(
            "transport.phase_end_emit_failed",
            session_id=session_id,
            phase=phase.value,
            phase_id=phase_id,
        )
    if was_tracked:
        await _emit_phase_snapshot_best_effort(session_id, phase_id)


@asynccontextmanager
async def phase_span(
    *,
    session_id: str | None,
    phase: Phase,
    detail: str | None = None,
    parent_id: str | None = None,
) -> AsyncIterator[str | None]:
    """Emit a ``PHASE_START`` on enter and a paired ``PHASE_END`` on exit.

    Guarantees pairing across every exit path — normal return, early return, or
    exception. A no-op when ``session_id`` is falsy (yields ``None``). The
    generated ``phase_id`` is yielded so a concurrent child can reference it as
    its ``parent_id`` (AC-8): the end only fires after the ``async with`` body
    completes, so a parent span wrapping ``asyncio.gather`` ends strictly after
    its last child (ADR §1).

    FRE-936 / AC-9(b): the paired end reports ``ok=False`` when the body raised
    (any ``BaseException``, including ``asyncio.CancelledError``) so the client
    can tell a failed phase from a successful one — the exception always
    re-raises afterward, so cancellation still propagates.

    Args:
        session_id: Target session identifier, or ``None`` to skip emission.
        phase: Which phase this span represents.
        detail: Optional human-readable qualifier.
        parent_id: Parent phase id when this span is a concurrent child.

    Yields:
        The generated ``phase_id`` for children to reference, or ``None`` when
        emission is skipped.
    """
    if not session_id:
        yield None
        return
    phase_id = uuid4().hex
    await emit_phase_start(
        session_id=session_id,
        phase=phase,
        phase_id=phase_id,
        started_at=datetime.now(UTC).isoformat(),
        detail=detail,
        parent_id=parent_id,
    )
    try:
        yield phase_id
    except BaseException:
        await emit_phase_end(
            session_id=session_id,
            phase=phase,
            phase_id=phase_id,
            parent_id=parent_id,
            ok=False,
        )
        raise
    else:
        await emit_phase_end(
            session_id=session_id,
            phase=phase,
            phase_id=phase_id,
            parent_id=parent_id,
            ok=True,
        )


async def emit_session_selection(*, session_id: str, role: str, deployment_key: str) -> None:
    """Persist + enqueue a ``session_selection`` STATE_DELTA event (ADR-0121 §4).

    Best-effort live notification to the single active client (ADR-0075) that the
    session's server-owned model selection for ``role`` changed. Correctness does
    not depend on delivery — other clients converge via hydration on the session
    GET / WS reconnect. See ADR-0079 §5-6 (invariants inherited).

    Args:
        session_id: Target session identifier.
        role: The role whose selection changed (e.g. ``"primary"``).
        deployment_key: The newly selected catalog deployment key.
    """
    await _push_event(
        StateUpdateEvent(
            key="session_selection",
            value={"role": role, "deployment_key": deployment_key},
            session_id=session_id,
        ),
        session_id,
    )


class AGUITransport:
    """AG-UI streaming transport via WebSocket.

    Satisfies ``UITransportProtocol`` (structural typing).  Pushes typed
    internal events through the sequenced dual-write path: Postgres for
    durability, bounded asyncio.Queue for real-time delivery.

    Decision round-trips (tool approvals, constraint pauses, HITL interrupts)
    use the per-connection waiter registry in
    :mod:`personal_agent.transport.agui.ws_endpoint` instead of the retired
    Future registry.
    """

    async def send_text_delta(self, text: str, session_id: str) -> None:
        """Stream an incremental text chunk to the UI.

        Args:
            text: Partial text token or chunk to deliver.
            session_id: Target session identifier.
        """
        await _push_event(TextDeltaEvent(text=text, session_id=session_id), session_id)

    async def send_tool_event(
        self, event: ToolStartEvent | ToolEndEvent | dict[str, Any], session_id: str
    ) -> None:
        """Deliver a tool lifecycle event to the UI (start or end).

        Args:
            event: Tool event payload — either a typed event or a dict with
                ``tool_name`` and optional ``args``/``result_summary``.
            session_id: Target session identifier.
        """
        if isinstance(event, (ToolStartEvent, ToolEndEvent)):
            await _push_event(event, session_id)
        elif isinstance(event, dict):
            tool_name = str(event.get("tool_name", "unknown"))
            if "result_summary" in event:
                await _push_event(
                    ToolEndEvent(
                        tool_name=tool_name,
                        result_summary=str(event["result_summary"]),
                        session_id=session_id,
                    ),
                    session_id,
                )
            else:
                await _push_event(
                    ToolStartEvent(
                        tool_name=tool_name,
                        args=event.get("args", {}),
                        session_id=session_id,
                    ),
                    session_id,
                )
        else:
            log.warning(
                "transport.send_tool_event_unknown_type",
                session_id=session_id,
                event_type=type(event).__name__,
            )

    async def send_state(self, state: Mapping[str, Any], session_id: str) -> None:
        """Push agent state key-value pairs to the UI.

        Args:
            state: JSON-serialisable state mapping.
            session_id: Target session identifier.
        """
        for key, value in state.items():
            await _push_event(
                StateUpdateEvent(key=key, value=value, session_id=session_id),
                session_id,
            )

    async def send_interrupt(self, context: Any, session_id: str) -> Any:
        """Push an interrupt event to the UI.

        Args:
            context: Either an InterruptEvent or a value to wrap.
            session_id: Target session identifier.

        Returns:
            None — response handling via WS is implemented in request_tool_approval.
        """
        if isinstance(context, InterruptEvent):
            await _push_event(context, session_id)
        else:
            await _push_event(
                InterruptEvent(
                    context=str(context),
                    options=["approve", "reject"],
                    session_id=session_id,
                ),
                session_id,
            )
        return None

    async def request_tool_approval(
        self,
        *,
        request_id: str,
        trace_id: str,
        session_id: str,
        tool: str,
        args: Mapping[str, Any],
        risk_level: Literal["low", "medium", "high"],
        reason: str,
        timeout_seconds: float = 60.0,
    ) -> ApprovalDecision:
        """Push an approval request event and await the human's decision.

        Pushes a ToolApprovalRequestEvent through the dual-write path so
        the PWA renders an approval card, then blocks on the per-connection
        waiter registry until the client sends an APPROVAL_DECISION message
        or the timeout elapses.

        Args:
            request_id: Unique identifier for this round-trip (UUID string).
            trace_id: Trace context identifier for telemetry correlation.
            session_id: Target session identifier.
            tool: Name of the tool awaiting approval.
            args: Arguments that will be passed to the tool if approved.
            risk_level: Qualitative risk label for the PWA approval card.
            reason: Human-readable explanation of why approval is required.
            timeout_seconds: Seconds before auto-returning a timeout decision.

        Returns:
            ApprovalDecision with the human's verdict or a timeout/disconnect.
        """
        expires_at = (datetime.now(UTC) + timedelta(seconds=timeout_seconds)).isoformat()

        async def _push() -> None:
            await _push_event(
                ToolApprovalRequestEvent(
                    request_id=request_id,
                    trace_id=trace_id,
                    session_id=session_id,
                    tool=tool,
                    args=args,
                    risk_level=risk_level,
                    reason=reason,
                    expires_at=expires_at,
                ),
                session_id,
            )
            log.info(
                "transport.approval_request_queued",
                request_id=request_id,
                session_id=session_id,
                trace_id=trace_id,
                tool=tool,
                risk_level=risk_level,
                timeout_seconds=timeout_seconds,
            )

        # Register the waiter BEFORE pushing the event so a fast client reply
        # can never arrive before the waiter exists (ADR-0076 race fix).
        decision = await register_waiter(
            session_id=session_id,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
            on_registered=_push,
        )
        log.info(
            "transport.approval_decision_received",
            request_id=request_id,
            session_id=session_id,
            trace_id=trace_id,
            tool=tool,
            decision=decision.decision,
        )
        return decision
