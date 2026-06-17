"""End-to-end wire test for the ADR-0088 live cost meter (FRE-507 Deliverable A).

This is the close-out proof for FRE-507: drive a decomposed (fan-out) turn through a **real
(non-NoOp) bus** — the genuine ``RedisStreamBus`` + ``ConsumerRunner`` publish→XADD→XREADGROUP→
``parse_stream_event``→``projector.handle``→``emit_turn_status`` path — and assert the live
``turn_status`` cost meter (i) **climbs across ≥2 emits during the expansion window** (not a single
end-of-turn jump) and (ii) reconciles to the authoritative ``SUM(api_costs)`` at completion.

The Redis *client* is an in-memory fake (the project idiom — ``tests/personal_agent/events/
test_consumer.py`` mocks the client, not the bus), so this runs in ``make test`` with no live
infra and stays in the default regression gate. Unit tests in ``test_projector.py`` already prove
the projector arithmetic; this proves the same invariant on the wire.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest

from personal_agent.config import settings
from personal_agent.events.bus import NoOpBus
from personal_agent.events.consumer import ConsumerRunner
from personal_agent.events.models import (
    CG_TURN_PROJECTOR,
    STREAM_TURN_OBSERVED,
    EventBase,
    ModelCallCompletedEvent,
    SubAgentProgressEvent,
    TopologyEnteredEvent,
    TurnCompletedEvent,
)
from personal_agent.events.redis_backend import RedisStreamBus
from personal_agent.observability.topology import projector as projector_mod
from personal_agent.observability.topology.projector import TurnObservationProjector

pytestmark = pytest.mark.asyncio

_TRACE = "trace-507"
_SESSION = "sess-507"


class _InMemoryStreamRedis:
    """Minimal in-memory stand-in for ``redis.asyncio.Redis`` streams.

    Implements only the calls ``RedisStreamBus`` + ``ConsumerRunner`` make: ``xgroup_create``,
    ``xadd``, ``xreadgroup`` (consumer-group ``>`` semantics via a per-group cursor), ``xack``,
    and ``aclose``. It makes ``bus.publish()`` genuinely serialize and the runner genuinely
    deserialize what was published — a true round-trip.

    SCOPED TO THE HAPPY PATH: there is no PEL / pending-entries-list, redelivery, or NOACK
    modelling (the runner ACKs after a successful handler, advancing nothing that must be
    replayed). Do NOT reuse this fake to validate retry / dead-letter behaviour (FRE-507 codex
    Q3) — ``tests/personal_agent/events/test_consumer.py`` covers those with explicit mocks.
    """

    def __init__(self) -> None:
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._cursors: dict[tuple[str, str], int] = {}
        self._seq = 0
        self.acked: list[tuple[str, str, str]] = []

    async def xgroup_create(
        self, stream: str, group: str, id: str = "0", mkstream: bool = False
    ) -> bool:
        self._streams.setdefault(stream, [])
        self._cursors.setdefault((stream, group), 0)
        return True

    async def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> str:
        self._seq += 1
        message_id = f"{self._seq}-0"
        self._streams.setdefault(stream, []).append((message_id, fields))
        return message_id

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int = 10,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        out: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        for stream in streams:  # only ">" (new) is ever requested by the runner
            entries = self._streams.get(stream, [])
            cursor = self._cursors.get((stream, groupname), 0)
            batch = entries[cursor : cursor + count]
            if batch:
                self._cursors[(stream, groupname)] = cursor + len(batch)
                out.append((stream, batch))
        if not out:
            # Nothing new: yield control with a short sleep so the runner's read loop polls
            # instead of spinning, mirroring a real XREADGROUP BLOCK that times out.
            await asyncio.sleep(0.005)
        return out

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        self.acked.append((stream, group, message_id))
        return 1

    async def aclose(self) -> None:
        return None


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch ``emit_turn_status`` and capture each on-the-wire ``turn_status`` value payload."""
    emitted: list[dict[str, Any]] = []

    async def _fake_emit(*, session_id: str, value: dict[str, Any]) -> None:
        emitted.append({"session_id": session_id, **value})

    monkeypatch.setattr(projector_mod, "emit_turn_status", _fake_emit)
    return emitted


async def _drain_until(pred: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Poll until ``pred()`` holds or fail the test on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for projector emit predicate")


async def _publish(bus: RedisStreamBus, event: EventBase) -> None:
    await bus.publish(STREAM_TURN_OBSERVED, event, maxlen=settings.turn_observed_stream_maxlen)


async def test_decomposed_turn_meter_climbs_live_and_reconciles_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fan-out turn's live meter climbs mid-expansion and reconciles to SUM(api_costs).

    The runner is started BEFORE any model-call event is published, then events are published
    one at a time — so the captured climb is a genuine mid-flight cadence on the wire, not a
    pre-queued batch replayed in order.
    """
    emitted = _capture(monkeypatch)
    bus = RedisStreamBus(_InMemoryStreamRedis())  # type: ignore[arg-type]
    proj = TurnObservationProjector()
    await bus.subscribe(
        stream=STREAM_TURN_OBSERVED,
        group=CG_TURN_PROJECTOR,
        consumer_name="turn-projector-0",
        handler=proj.handle,
    )
    runner = ConsumerRunner(bus)
    await runner.start()
    try:
        # Enter the fan-out topology; the projector should emit once it is observed.
        await _publish(
            bus,
            TopologyEnteredEvent(trace_id=_TRACE, session_id=_SESSION, topology="hybrid_fanout"),
        )
        await _drain_until(lambda: len(emitted) >= 1)
        assert emitted[-1]["topology"] == "hybrid_fanout"

        # Publish each model-call event live and capture the meter value AFTER each is observed.
        mid_values: list[float] = []
        for cost, expected, role in [
            (0.05, 0.05, "sub_agent"),
            (0.07, 0.12, "sub_agent"),
            (0.11, 0.23, "sub_agent"),
            (0.09, 0.32, "primary"),
        ]:
            if role == "sub_agent":
                # interleave a sub-agent progress tick to mirror a real expansion window
                await _publish(
                    bus,
                    SubAgentProgressEvent(
                        trace_id=_TRACE,
                        session_id=_SESSION,
                        task_id="a",
                        iteration=1,
                        iteration_max=10,
                    ),
                )
            await _publish(
                bus,
                ModelCallCompletedEvent(
                    trace_id=_TRACE,
                    session_id=_SESSION,
                    cost_usd=cost,
                    input_tokens=100,
                    output_tokens=50,
                    model_role=role,
                ),
            )
            await _drain_until(lambda e=expected: emitted[-1]["turn_cost_usd"] == pytest.approx(e))
            mid_values.append(emitted[-1]["turn_cost_usd"])

        # (i) The meter climbed across ≥2 emits DURING expansion (captured between publishes).
        assert len(mid_values) >= 2
        assert all(a < b for a, b in zip(mid_values, mid_values[1:])), mid_values
        # …and it was not a single end-of-turn jump: an intermediate value sits strictly between.
        assert any(0 < v < 0.32 for v in mid_values), mid_values

        # Complete the turn; authoritative SUM(api_costs) == accumulated live cost here (0.32).
        await _publish(
            bus,
            TurnCompletedEvent(
                trace_id=_TRACE,
                session_id=_SESSION,
                topology="hybrid_fanout",
                cost_authoritative_usd=0.32,
            ),
        )
        await _drain_until(lambda: _TRACE not in proj._by_trace)

        # (ii) Final emitted meter == authoritative sum; topology carried through.
        assert emitted[-1]["turn_cost_usd"] == pytest.approx(0.32)
        assert emitted[-1]["topology"] == "hybrid_fanout"
    finally:
        await runner.stop()


async def test_noop_bus_meter_is_dark_and_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRE-507 Deliverable B: under NoOpBus the live meter goes dark, never raising.

    Documented graceful degradation (ADR-0088 D6): publishes are discarded and the projector
    consumer is not wired, so no ``turn_status`` flows. Durable ``api_costs`` / route-trace
    writes are bus-independent and unaffected — see the projector module docstring.
    """
    emitted = _capture(monkeypatch)
    bus = NoOpBus()

    for event in [
        TopologyEnteredEvent(trace_id=_TRACE, session_id=_SESSION, topology="hybrid_fanout"),
        ModelCallCompletedEvent(
            trace_id=_TRACE,
            session_id=_SESSION,
            cost_usd=0.05,
            input_tokens=1,
            output_tokens=1,
            model_role="sub_agent",
        ),
        TurnCompletedEvent(
            trace_id=_TRACE,
            session_id=_SESSION,
            topology="hybrid_fanout",
            cost_authoritative_usd=0.05,
        ),
    ]:
        # Must not raise; the durable cost path does not depend on this publish.
        await bus.publish(STREAM_TURN_OBSERVED, event)

    # Meter is dark: nothing was emitted because no projector consumes a NoOpBus.
    assert emitted == []
