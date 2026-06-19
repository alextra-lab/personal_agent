"""The ADR-0088 live turn-observation projector (D4 / FRE-513).

A single bus consumer on ``stream:turn.observed`` that maintains a per-trace
:class:`TurnObservation` and is the **sole** emitter of ``turn_status`` (the ADR-0076
STATE_DELTA sink). Topologies report through the seam (events); the projector projects.

Because ``turn_status`` is a full-state replacement keyed by session, the live path is
naturally idempotent (ADR-0088 D4): a duplicate or replayed event simply re-sets the same
state, and a missed event self-corrects on the next one. The live cost meter accumulates
``turn.model_call_completed`` events (topology-independent, since they originate at the
hard-enforced cost boundary) and reconciles to the authoritative sum at ``turn.completed``.

This is a **live-only** consumer (ADR-0088 D6 sink 2): durability lives in the seam's
direct route-trace write + ``api_costs``. Every ``emit_turn_status`` call is best-effort so
a transport failure can never break the consumer loop.

Bus-down behaviour (FRE-507): under ``NoOpBus`` (Redis down or the flag off) the live meter
goes **dark** — publishes are discarded and this consumer is not even wired (``service/app.py``
only subscribes it on the ``RedisStreamBus`` branch). That is accepted graceful degradation,
*not* a data risk: the durable cost path is decoupled from the bus (``cost_tracker`` writes the
``api_costs`` row before the best-effort publish, authoritative cost == ``SUM(api_costs)``, and
the seam's route-trace write is bus-independent — D8), so a dark meter loses only the live
cosmetic cadence, never durable data. The in-band fallback (a direct ``emit_turn_status`` when
the bus is a ``NoOpBus``) is *declined* — not because it could not deliver (the WS carrier is
Redis-independent) but because it would re-introduce a **second** ``turn_status`` writer at the
cost boundary, breaking this projector's sole-emitter contract (the scattered in-band emits
ADR-0088 removed, FRE-501). A degraded-mode cosmetic gain is not worth forking that invariant.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from personal_agent.captains_log.es_indexer import schedule_es_index
from personal_agent.events.models import (
    EventBase,
    ModelCallCompletedEvent,
    SubAgentProgressEvent,
    TopologyEnteredEvent,
    TurnCompletedEvent,
    TurnDegradedEvent,
    TurnProgressEvent,
)
from personal_agent.transport.agui.transport import emit_turn_status

log = structlog.get_logger(__name__)

# Defensive bound on retained per-trace state: a turn that never emits ``turn.completed``
# (process crash mid-turn) would otherwise leak an entry. Evicting the oldest beyond this
# cap keeps the projector memory-stable without a TTL sweeper.
_MAX_TRACKED_TRACES = 2000

# ADR-0092 §D4: parallel bound for the session-aggregate map.  Active sessions rarely
# exceed a handful; 2000 matches the per-trace cap so eviction is consistent.
_MAX_TRACKED_SESSIONS = 2000

# FRE-557 projector-health rolling counter cadence: emit a rolling snapshot every N events
# OR every T seconds of activity (whichever first), so low-volume instances still heartbeat.
# Process-local + reset on restart — an operational gauge, not a durable counter.
_ROLLING_EMIT_EVERY = 1000
_ROLLING_EMIT_SECONDS = 300.0

# FRE-557 dedicated per-trace bus-delivery health index (one doc per trace at completion).
_HEALTH_INDEX_PREFIX = "agent-monitors-projector-health"

# ADR-0092 §D4: injected async callable that hydrates a session's historical cost map.
# ``session_id -> {trace_id_str: cost_usd}``.  ``None`` means carry-only (no read).
SessionCostHydrator = Callable[[str], Awaitable[dict[str, float]]]


@dataclass
class SessionAggregate:
    """Per-session state the projector carries across turns (ADR-0092 §D2/§D3/§D4).

    Attributes:
        session_id: Owning session identifier.
        costs: Idempotent ``{trace_id_str: authoritative_cost_usd}`` map (set, never ``+=``).
            The surfaced ``session_cost_usd`` is ``sum(costs.values())``.
        context_tokens: Latest ``context_tokens`` seen for this session; carried across turns
            so the session lane never resets to zero on new user input (D3).
        hydrated: ``True`` once the one-per-session substrate hydration has run.
    """

    session_id: str
    costs: dict[str, float] = field(default_factory=dict)
    context_tokens: int = 0
    hydrated: bool = False


@dataclass
class TurnObservation:
    """Per-trace live observation the projector maintains and projects (ADR-0088 D4).

    Attributes:
        trace_id: Turn trace identifier (join key).
        session_id: Session the ``turn_status`` STATE_DELTA is keyed by.
        topology: Active execution-topology label.
        phase: Coarse lifecycle phase (``running`` / ``completed``).
        tool_iteration: Latest primary tool-execution iteration reported.
        tool_iteration_max: Resolved per-turn primary tool-iteration cap.
        sub_agent_iterations: Per-``task_id`` latest sub-agent iteration (FRE-553); summed
            into the surfaced meter so concurrent sub-agents never clobber one counter.
        sub_agent_iteration_max: Per-``task_id`` sub-agent cap (FRE-553); summed into the
            surfaced max.
        context_tokens: Latest estimated context-window occupancy.
        context_max: Resolved context-window token budget.
        live_cost_usd: Accumulated live cost from model-call events.
        input_tokens: Accumulated prompt tokens from model-call events.
        output_tokens: Accumulated completion tokens from model-call events.
        degradations: Human-readable degradation markers raised this turn.
        degraded: Whether any degradation has been reported.
        events_received: Count of ``stream:turn.observed`` events the projector received for
            this trace (FRE-557 bus-delivery health).
        model_calls_received: Count of ``ModelCallCompletedEvent``s received for this trace —
            compared offline to ``COUNT(api_costs WHERE trace_id)`` to detect delivery loss.
    """

    trace_id: str
    session_id: str
    topology: str = "primary"
    phase: str = "running"
    tool_iteration: int = 0
    tool_iteration_max: int = 0
    sub_agent_iterations: dict[str, int] = field(default_factory=dict)
    sub_agent_iteration_max: dict[str, int] = field(default_factory=dict)
    context_tokens: int = 0
    context_max: int = 0
    live_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    degradations: list[str] = field(default_factory=list)
    degraded: bool = False
    events_received: int = 0
    model_calls_received: int = 0


class TurnObservationProjector:
    """Consumes ``stream:turn.observed`` and emits ``turn_status`` (ADR-0088 D4)."""

    def __init__(self, hydration_source: SessionCostHydrator | None = None) -> None:
        """Initialise observation maps and process-local counters.

        Args:
            hydration_source: Optional async callable ``(session_id) -> {trace_id: cost}``
                used to restore historical session cost on first touch (ADR-0092 §D4).
                ``None`` means carry-only — the session aggregate starts empty and grows
                only from live ``turn.completed`` events.
        """
        self._by_trace: dict[str, TurnObservation] = {}
        # ADR-0092 §D4: session-scoped aggregate map (persists across turns).
        self._by_session: dict[str, SessionAggregate] = {}
        self._hydration_source = hydration_source
        # FRE-557 global rolling counters (process-local; reset on restart).
        self._events_received_total: int = 0
        self._events_by_type: dict[str, int] = {}
        self._last_rolling_emit: float = time.monotonic()

    async def _ensure_session(self, session_id: str) -> SessionAggregate:
        """Return (creating if needed) the session aggregate, hydrating on first touch.

        Hydration runs at most once per session per process lifetime.  A failing hydration
        source is swallowed (best-effort) so the projector continues in carry-only mode.
        LRU-evicts the oldest session beyond ``_MAX_TRACKED_SESSIONS``; cost is recoverable
        (re-hydrated on next touch), ``context_tokens`` is process-local and resets to 0.

        Args:
            session_id: The session identifier string.

        Returns:
            The ``SessionAggregate`` for this session.
        """
        sess = self._by_session.get(session_id)
        if sess is None:
            if len(self._by_session) >= _MAX_TRACKED_SESSIONS:
                oldest_key = next(iter(self._by_session))
                self._by_session.pop(oldest_key)
                log.debug("projector_evicted_session", session_id=oldest_key)
            sess = SessionAggregate(session_id=session_id)
            self._by_session[session_id] = sess
            await self._hydrate(sess)
        return sess

    async def _hydrate(self, sess: SessionAggregate) -> None:
        """Populate ``sess.costs`` from the hydration source (once per session).

        Uses ``setdefault`` so a trace already written by a live ``turn.completed`` in this
        process (possible only if the projector created the session entry and then
        immediately re-hydrated, which cannot happen given single-consumer ordering) is
        never overwritten.  In practice the first-touch path always calls this on a fresh
        ``SessionAggregate`` with an empty ``costs`` dict.

        Args:
            sess: The freshly-created ``SessionAggregate`` to populate.
        """
        sess.hydrated = True
        if self._hydration_source is None:
            return
        try:
            historical = await self._hydration_source(sess.session_id)
            for tid, cost in historical.items():
                sess.costs.setdefault(tid, cost)
        except Exception:
            log.debug("projector_hydration_failed", session_id=sess.session_id)

    def _observation(self, trace_id: str, session_id: str) -> TurnObservation:
        """Return (creating if needed) the observation for a trace."""
        obs = self._by_trace.get(trace_id)
        if obs is None:
            if len(self._by_trace) >= _MAX_TRACKED_TRACES:
                # Evict the oldest tracked trace (insertion order) to stay bounded.
                oldest = next(iter(self._by_trace))
                evicted = self._by_trace.pop(oldest)
                # FRE-557: a mid-turn eviction of an *active* trace would later produce a
                # zero-counter health doc with no signal of loss — make it loud.
                if evicted.events_received > 0:
                    log.warning(
                        "projector_evicted_active_trace",
                        trace_id=oldest,
                        events_received=evicted.events_received,
                    )
                else:
                    log.debug("turn_projector_evicted_stale_trace", trace_id=oldest)
            obs = TurnObservation(trace_id=trace_id, session_id=session_id)
            self._by_trace[trace_id] = obs
        return obs

    async def handle(self, event: EventBase) -> None:
        """Dispatch a ``stream:turn.observed`` event and emit the live ``turn_status``.

        Args:
            event: A parsed turn-observed event. Unknown event types are ignored (the
                stream is single-purpose, but the consumer tolerates additions).
        """
        # FRE-557: count every received event (incl. unknown types) for the rolling
        # bus-delivery gauge, before per-type dispatch.
        self._events_received_total += 1
        name = type(event).__name__
        self._events_by_type[name] = self._events_by_type.get(name, 0) + 1
        self._maybe_emit_rolling()

        if isinstance(event, TopologyEnteredEvent):
            sess = await self._ensure_session(event.session_id)
            obs = self._observation(event.trace_id, event.session_id)
            obs.events_received += 1
            obs.topology = event.topology
        elif isinstance(event, TurnProgressEvent):
            sess = await self._ensure_session(event.session_id)
            obs = self._observation(event.trace_id, event.session_id)
            obs.events_received += 1
            obs.tool_iteration = event.tool_iteration
            obs.tool_iteration_max = event.tool_iteration_max
            obs.context_tokens = event.context_tokens
            obs.context_max = event.context_max
            # ADR-0092 §D3: carry the latest context occupancy across turns (no reset-to-0).
            sess.context_tokens = event.context_tokens
            if event.topology is not None:
                obs.topology = event.topology
        elif isinstance(event, SubAgentProgressEvent):
            # FRE-553: track each sub-agent's latest iteration per task_id, max-wins so a
            # stale/reordered best-effort tick can never drop the surfaced count. Entries
            # persist until TurnCompletedEvent pops the whole trace (do not pop per sub-agent
            # — removing both numerator and denominator would mask completed work).
            sess = await self._ensure_session(event.session_id)
            obs = self._observation(event.trace_id, event.session_id)
            obs.events_received += 1
            obs.sub_agent_iterations[event.task_id] = max(
                obs.sub_agent_iterations.get(event.task_id, 0), event.iteration
            )
            obs.sub_agent_iteration_max[event.task_id] = max(
                obs.sub_agent_iteration_max.get(event.task_id, 0), event.iteration_max
            )
        elif isinstance(event, ModelCallCompletedEvent):
            sess = await self._ensure_session(event.session_id)
            obs = self._observation(event.trace_id, event.session_id)
            obs.events_received += 1
            obs.model_calls_received += 1
            obs.live_cost_usd += event.cost_usd
            obs.input_tokens += event.input_tokens
            obs.output_tokens += event.output_tokens
            if event.topology is not None:
                obs.topology = event.topology
        elif isinstance(event, TurnDegradedEvent):
            sess = await self._ensure_session(event.session_id)
            obs = self._observation(event.trace_id, event.session_id)
            obs.events_received += 1
            obs.degraded = True
            obs.degradations.append(f"{event.where}: {event.reason}")
        elif isinstance(event, TurnCompletedEvent):
            # FRE-557: was the full lifecycle observed, or is this obs about to be freshly
            # created (evicted mid-turn / never-seen-until-completion)? Captured before
            # _observation so the health doc can flag untrustworthy counters.
            observation_complete = event.trace_id in self._by_trace
            sess = await self._ensure_session(event.session_id)
            obs = self._observation(event.trace_id, event.session_id)
            obs.events_received += 1
            obs.topology = event.topology
            obs.phase = "completed"
            # FRE-557: capture the bus-accumulated live cost BEFORE the authoritative
            # overwrite — that is what the UI meter actually showed.
            projector_live_cost_usd = obs.live_cost_usd
            self._emit_turn_health(
                obs,
                projector_live_cost_usd=projector_live_cost_usd,
                cost_authoritative_usd=event.cost_authoritative_usd,
                observation_complete=observation_complete,
            )
            # Authoritative wins (ADR-0088 D3): reconcile the live meter to SUM(api_costs).
            obs.live_cost_usd = event.cost_authoritative_usd
            # ADR-0092 §D2: idempotent session cost roll-up (set, never +=).
            sess.costs[event.trace_id] = event.cost_authoritative_usd
            await self._emit(obs)
            self._by_trace.pop(event.trace_id, None)
            return
        else:
            return

        await self._emit(obs)

    async def _emit(self, obs: TurnObservation) -> None:
        """Emit the full-state ``turn_status`` STATE_DELTA (best-effort)."""
        # FRE-553: surface the aggregate (primary + Σ sub-agent) so the meter climbs live
        # through a decomposed turn's expansion window. Raw fields are kept separate and
        # summed only here. With no sub-agent ticks the dicts are empty → primary values.
        tool_iteration = obs.tool_iteration + sum(obs.sub_agent_iterations.values())
        tool_iteration_max = obs.tool_iteration_max + sum(obs.sub_agent_iteration_max.values())
        # ADR-0092 §D2/§D3: session-lane fields (zero when no aggregate yet — shouldn't
        # occur in practice since _ensure_session always precedes _emit).
        sess = self._by_session.get(obs.session_id)
        session_cost_usd = round(sum(sess.costs.values()), 6) if sess else 0.0
        session_context_tokens = sess.context_tokens if sess else 0
        try:
            await emit_turn_status(
                session_id=obs.session_id,
                value={
                    "context_tokens": obs.context_tokens,
                    "context_max": obs.context_max,
                    "tool_iteration": tool_iteration,
                    "tool_iteration_max": tool_iteration_max,
                    "turn_cost_usd": round(obs.live_cost_usd, 6),
                    # FRE-407: the client stamps trace_id onto the assistant message so the
                    # rating control (which joins on trace_id) can render after DONE.
                    "trace_id": obs.trace_id,
                    "topology": obs.topology,
                    "degraded": obs.degraded,
                    "degradations": list(obs.degradations),
                    # ADR-0092 §D2/§D3 session lane.
                    "session_cost_usd": session_cost_usd,
                    "session_context_tokens": session_context_tokens,
                },
            )
        except Exception:
            log.debug(
                "turn_status_emit_failed",
                trace_id=obs.trace_id,
                session_id=obs.session_id,
            )

    def _maybe_emit_rolling(self) -> None:
        """Emit the rolling bus-delivery gauge every N events or T seconds (FRE-557).

        Event-driven (no background task): fires when the event count crosses
        ``_ROLLING_EMIT_EVERY`` **or** ``_ROLLING_EMIT_SECONDS`` have elapsed since the last
        emit — so a low-volume instance still heartbeats on its next event. Process-local and
        reset on restart; this is an operational gauge for systemic delivery loss, not a
        durable per-trace counter (a trace seen *zero* times is invisible here — see the
        per-trace health doc + the offline reconciliation query).
        """
        now = time.monotonic()
        if (
            self._events_received_total % _ROLLING_EMIT_EVERY == 0
            or now - self._last_rolling_emit >= _ROLLING_EMIT_SECONDS
        ):
            self._last_rolling_emit = now
            log.info(
                "projector_events_rolling",
                events_total=self._events_received_total,
                by_type=dict(self._events_by_type),
                tracked_traces=len(self._by_trace),
            )

    def _emit_turn_health(
        self,
        obs: TurnObservation,
        *,
        projector_live_cost_usd: float,
        cost_authoritative_usd: float,
        observation_complete: bool,
    ) -> None:
        """Project per-trace bus-delivery health to ``agent-monitors-projector-health-*``.

        Non-blocking + best-effort (the whole body is guarded — ``schedule_es_index`` only
        guards the scheduled write, not the synchronous doc build). Idempotent on
        ``doc_id = trace_id``. Carries the projector's bus-accumulated live cost (pre-reconcile)
        alongside the authoritative sum so an undercount is attributable to delivery loss
        (``model_calls_received`` < ``COUNT(api_costs WHERE trace_id)``) rather than the
        ledger's accumulator drift (``cost_reconciled = FALSE`` — a separate, orthogonal axis).

        Args:
            obs: The completed turn's observation.
            projector_live_cost_usd: Bus-accumulated live cost captured before the
                authoritative reconcile-overwrite.
            cost_authoritative_usd: ``SUM(api_costs)`` carried on the completion event.
            observation_complete: ``False`` when the obs was freshly created at completion
                (evicted mid-turn / never-seen-until-completion) → counters untrustworthy.
        """
        try:
            ts = datetime.now(timezone.utc).isoformat()
            doc: dict[str, Any] = {
                "@timestamp": ts,
                "trace_id": obs.trace_id,
                "session_id": obs.session_id,
                "topology": obs.topology,
                "events_received": obs.events_received,
                "model_calls_received": obs.model_calls_received,
                "projector_live_cost_usd": float(projector_live_cost_usd),
                "cost_authoritative_usd": float(cost_authoritative_usd),
                "cost_delta_usd": round(
                    float(projector_live_cost_usd) - float(cost_authoritative_usd), 6
                ),
                "observation_complete": observation_complete,
            }
            index_name = f"{_HEALTH_INDEX_PREFIX}-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
            schedule_es_index(index_name, doc, doc_id=obs.trace_id)
        except Exception as e:
            log.warning(
                "projector_health_emit_failed",
                trace_id=obs.trace_id,
                error=str(e),
            )
