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
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from personal_agent.events.models import (
    EventBase,
    ModelCallCompletedEvent,
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


@dataclass
class TurnObservation:
    """Per-trace live observation the projector maintains and projects (ADR-0088 D4).

    Attributes:
        trace_id: Turn trace identifier (join key).
        session_id: Session the ``turn_status`` STATE_DELTA is keyed by.
        topology: Active execution-topology label.
        phase: Coarse lifecycle phase (``running`` / ``completed``).
        tool_iteration: Latest tool-execution iteration reported.
        tool_iteration_max: Resolved per-turn tool-iteration cap.
        context_tokens: Latest estimated context-window occupancy.
        context_max: Resolved context-window token budget.
        live_cost_usd: Accumulated live cost from model-call events.
        input_tokens: Accumulated prompt tokens from model-call events.
        output_tokens: Accumulated completion tokens from model-call events.
        degradations: Human-readable degradation markers raised this turn.
        degraded: Whether any degradation has been reported.
    """

    trace_id: str
    session_id: str
    topology: str = "primary"
    phase: str = "running"
    tool_iteration: int = 0
    tool_iteration_max: int = 0
    context_tokens: int = 0
    context_max: int = 0
    live_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    degradations: list[str] = field(default_factory=list)
    degraded: bool = False


class TurnObservationProjector:
    """Consumes ``stream:turn.observed`` and emits ``turn_status`` (ADR-0088 D4)."""

    def __init__(self) -> None:
        """Initialise an empty per-trace observation map."""
        self._by_trace: dict[str, TurnObservation] = {}

    def _observation(self, trace_id: str, session_id: str) -> TurnObservation:
        """Return (creating if needed) the observation for a trace."""
        obs = self._by_trace.get(trace_id)
        if obs is None:
            if len(self._by_trace) >= _MAX_TRACKED_TRACES:
                # Evict the oldest tracked trace (insertion order) to stay bounded.
                oldest = next(iter(self._by_trace))
                del self._by_trace[oldest]
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
        if isinstance(event, TopologyEnteredEvent):
            obs = self._observation(event.trace_id, event.session_id)
            obs.topology = event.topology
        elif isinstance(event, TurnProgressEvent):
            obs = self._observation(event.trace_id, event.session_id)
            obs.tool_iteration = event.tool_iteration
            obs.tool_iteration_max = event.tool_iteration_max
            obs.context_tokens = event.context_tokens
            obs.context_max = event.context_max
            if event.topology is not None:
                obs.topology = event.topology
        elif isinstance(event, ModelCallCompletedEvent):
            obs = self._observation(event.trace_id, event.session_id)
            obs.live_cost_usd += event.cost_usd
            obs.input_tokens += event.input_tokens
            obs.output_tokens += event.output_tokens
            if event.topology is not None:
                obs.topology = event.topology
        elif isinstance(event, TurnDegradedEvent):
            obs = self._observation(event.trace_id, event.session_id)
            obs.degraded = True
            obs.degradations.append(f"{event.where}: {event.reason}")
        elif isinstance(event, TurnCompletedEvent):
            obs = self._observation(event.trace_id, event.session_id)
            obs.topology = event.topology
            obs.phase = "completed"
            # Authoritative wins (ADR-0088 D3): reconcile the live meter to SUM(api_costs).
            obs.live_cost_usd = event.cost_authoritative_usd
            await self._emit(obs)
            self._by_trace.pop(event.trace_id, None)
            return
        else:
            return

        await self._emit(obs)

    async def _emit(self, obs: TurnObservation) -> None:
        """Emit the full-state ``turn_status`` STATE_DELTA (best-effort)."""
        try:
            await emit_turn_status(
                session_id=obs.session_id,
                value={
                    "context_tokens": obs.context_tokens,
                    "context_max": obs.context_max,
                    "tool_iteration": obs.tool_iteration,
                    "tool_iteration_max": obs.tool_iteration_max,
                    "turn_cost_usd": round(obs.live_cost_usd, 6),
                    # FRE-407: the client stamps trace_id onto the assistant message so the
                    # rating control (which joins on trace_id) can render after DONE.
                    "trace_id": obs.trace_id,
                    "topology": obs.topology,
                    "degraded": obs.degraded,
                    "degradations": list(obs.degradations),
                },
            )
        except Exception:
            log.debug(
                "turn_status_emit_failed",
                trace_id=obs.trace_id,
                session_id=obs.session_id,
            )
