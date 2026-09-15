"""Structural cross-arm isolation for eval scripts (FRE-1372, carries FRE-1338 AC-3).

FRE-1338's incident: a behavioral turn's entity extraction writes to ``neo4j-eval``
asynchronously, and a *later* turn's ``search_memory`` picks those entities up as
ordinary recall — a same-run KG leak, 31 seconds start to finish in the measured
instance. ``fre1337_intent_probe/behavioral.py`` originally closed this with an explicit
``wipe_eval_graph()`` call the harness made itself, before every fixture — a control that
held only as long as every eval script remembered to call it. A master review of FRE-1372
caught that this was exactly the failure AC-3 rules out ("isolation depends on each
script remembering to call a reset helper") and that behavioral.py was the live
counter-example: two ways to drive a turn, only one isolated. behavioral.py is migrated
onto this module as of FRE-1372 — see its own module docstring — so this is now the
*only* way any eval script drives a turn against the isolated eval gateway, and
``scripts/check_no_direct_substrate_in_tests.py`` enforces that structurally: a
``scripts/eval/`` file referencing ``EVAL_ARMS``/``EVAL_CHAT_BASE_URL`` without also
referencing ``IsolatedArmRunner`` fails pre-commit.

``IsolatedArmRunner`` is the shared, sanctioned way an eval script drives a turn
against the isolated eval gateway. A caller writes no wipe/restore code of its own —
isolation is a side effect of using this class to reach the gateway at all.

**Why a full wipe, not a selective restore.** The first design considered here kept a
baseline set of pre-run node ids (captured via ``elementId()``) and deleted only
newer nodes between arms, to avoid disturbing a fixture memory seeded before the run
(AC-2). Two things ruled that out:

1. A *selective* delete against this exact graph was already tried and rejected —
   ``test_fre1337_substrate_guard.py``'s ``test_wipe_cypher_is_unscoped_full_wipe``
   documents a prior plan review flagging that a scoped delete misses ``:Session``-only
   nodes and risks orphaning cross-session-adopted entities. An unscoped
   ``wipe_eval_graph()`` is the only delete shape proven safe against this substrate.
2. ``elementId()`` is documented stable only within a single transaction (Cypher
   manual, "Outside of the scope of a single transaction, no guarantees are given
   about the mapping between ID values and elements") — retaining ids captured in one
   session and matching them in a later one, as this harness's calls necessarily do,
   is outside that guarantee.

So this class never tries to preserve state *through* a wipe. Instead it treats
"what should exist at the start of an arm" as a **replayable action**: an optional
``reseed`` coroutine, supplied by the caller and invoked after every wipe (including
before the first turn, so no run silently inherits whatever was left in ``neo4j-eval``
by an earlier script). Because ``reseed`` goes through the caller's own production
write path, a fixture it creates carries the entitlement production would compute for
it — AC-2's guarantee, met by reuse rather than by preservation.

**Client timeout and cross-fixture overlap (FRE-1503).** A fan-out turn can legitimately
run for most of ``settings.orchestrator_turn_lifetime_seconds`` — the gateway's own
absolute wall-clock cap. The old hardcoded ``timeout=1200.0`` was below that cap: on
2026-09-12/13 ten FRE-1498 fan-out turns outlived the client, ``httpx`` raised
``ReadTimeout``, the gateway kept running each turn to completion, and the *next*
fixture's ``run_turn`` started on top of it — two turns sharing the single local
backend until it saturated. Two fixes, both here so every caller inherits them
(the FRE-1372 principle: isolation is not something a caller opts into per script):
the client timeout is read from ``settings.orchestrator_turn_lifetime_seconds`` plus
:data:`_CLIENT_TIMEOUT_BUFFER_S` rather than hardcoded, and every ``run_turn`` call
first waits for :func:`wait_for_gateway_idle` — no new turn starts while the eval
gateway still shows recent model/tool activity from a prior one.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from scripts.eval.fre1337_intent_probe.behavioral import (
    CONSOLIDATION_SETTLE_TIMEOUT_S,
    EVAL_ES_INDEX,
    EVAL_ES_URL,
    EXTRACTION_SETTLE_TIMEOUT_S,
    SIGNAL_SETTLE_TIMEOUT_S,
    fetch_latest_event,
    wait_for_event_settle,
)
from scripts.eval.fre1337_intent_probe.substrate import (
    EVAL_ARMS,
    EVAL_NEO4J_URI,
    assert_eval_chat_url,
    wipe_eval_graph,
)

from personal_agent.config import settings

log = structlog.get_logger(__name__)

#: Added to ``settings.orchestrator_turn_lifetime_seconds`` for the ``/chat`` client
#: timeout, so the client always outlives the gateway's own absolute cap instead of
#: racing it (FRE-1503).
_CLIENT_TIMEOUT_BUFFER_S = 100.0

#: Event types that mark the eval gateway as doing model/tool work on *any* trace —
#: deliberately not scoped to one trace_id, since a stray concurrent turn's activity
#: must also hold off the next fixture.
_GATEWAY_ACTIVITY_EVENT_TYPES = (
    "model_call_started",
    "model_call_completed",
    "tool_call_started",
    "tool_call_completed",
)

#: How long the gateway must show no activity before it counts as idle.
_GATEWAY_IDLE_QUIET_S = 150.0

#: Hard cap on how long a single ``wait_for_gateway_idle`` call polls.
_GATEWAY_IDLE_MAX_WAIT_S = 3700.0

_GATEWAY_IDLE_POLL_INTERVAL_S = 15.0


async def wait_for_gateway_idle(
    es: httpx.AsyncClient,
    *,
    quiet_s: float = _GATEWAY_IDLE_QUIET_S,
    max_wait_s: float = _GATEWAY_IDLE_MAX_WAIT_S,
) -> float:
    """Block until the eval gateway has had no model/tool activity for ``quiet_s``.

    Args:
        es: Async HTTP client pointed at ``elasticsearch-eval``.
        quiet_s: Seconds of silence required before the gateway counts as idle.
        max_wait_s: Hard polling timeout — a stuck gateway does not hang the caller
            forever.

    Returns:
        Seconds actually waited.
    """
    deadline = asyncio.get_event_loop().time() + max_wait_s
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() < deadline:
        resp = await es.post(
            f"{EVAL_ES_URL}/{EVAL_ES_INDEX}/_search",
            json={
                "size": 1,
                "query": {"terms": {"event_type": list(_GATEWAY_ACTIVITY_EVENT_TYPES)}},
                "sort": [{"@timestamp": {"order": "desc"}}],
                "_source": ["@timestamp"],
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        hits = resp.json()["hits"]["hits"]
        if not hits:
            return round(asyncio.get_event_loop().time() - t0, 1)
        last = datetime.fromisoformat(hits[0]["_source"]["@timestamp"].replace("Z", "+00:00"))
        if (datetime.now(UTC) - last).total_seconds() >= quiet_s:
            return round(asyncio.get_event_loop().time() - t0, 1)
        await asyncio.sleep(_GATEWAY_IDLE_POLL_INTERVAL_S)
    return round(asyncio.get_event_loop().time() - t0, 1)


#: FRE-1517 (master, 2026-09-14): between turns of one session, the next turn waits only until
#: no local model call is in flight, then this fixed gap. The owner's next turn never waits for
#: global quiet, so a turn that starts before the previous turn's extraction settles is the
#: production condition, not a defect.
_IN_SESSION_GAP_S = 10.0

#: A started local call older than this does not count as in flight. It bounds the wait when a
#: completed event is lost — Elasticsearch counts are provisional (FRE-1051).
_IN_FLIGHT_LOOKBACK_S = 3600.0

#: Hard cap on one in-session wait.
_IN_SESSION_MAX_WAIT_S = 1800.0

_IN_SESSION_POLL_INTERVAL_S = 5.0

#: ``max_result_window`` default; a filled page would under-report what is in flight.
_IN_FLIGHT_PAGE = 10000


async def local_model_calls_in_flight(
    es: httpx.AsyncClient, *, lookback_s: float = _IN_FLIGHT_LOOKBACK_S
) -> list[str]:
    """Span ids of ``slm_local`` model calls that started and have not ended.

    A call ends with ``model_call_completed`` or ``model_call_error`` on the same ``span_id``
    (``llm_client/telemetry.py`` and ``litellm_client.py`` both stamp ``provider`` and
    ``span_id`` on all three events).

    Args:
        es: Async HTTP client pointed at ``elasticsearch-eval``.
        lookback_s: Only calls started within this many seconds are considered.

    Returns:
        The span ids still open, in no particular order.

    Raises:
        RuntimeError: If the page is full, because the list would then be incomplete.
    """
    since = (datetime.now(UTC) - timedelta(seconds=lookback_s)).isoformat()
    resp = await es.post(
        f"{EVAL_ES_URL}/{EVAL_ES_INDEX}/_search",
        json={
            "size": _IN_FLIGHT_PAGE,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"provider": "slm_local"}},
                        {
                            "terms": {
                                "event_type": [
                                    "model_call_started",
                                    "model_call_completed",
                                    "model_call_error",
                                ]
                            }
                        },
                        {"range": {"@timestamp": {"gte": since}}},
                    ]
                }
            },
            "_source": ["event_type", "span_id"],
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    hits = resp.json()["hits"]["hits"]
    if len(hits) >= _IN_FLIGHT_PAGE:
        raise RuntimeError("in-flight query filled its page; the open-call list is incomplete")
    started: set[str] = set()
    ended: set[str] = set()
    for hit in hits:
        source = hit["_source"]
        span_id = source.get("span_id")
        if not span_id:
            continue
        if source.get("event_type") == "model_call_started":
            started.add(span_id)
        else:
            ended.add(span_id)
    return sorted(started - ended)


async def wait_for_local_model_calls(
    es: httpx.AsyncClient,
    *,
    gap_s: float = _IN_SESSION_GAP_S,
    max_wait_s: float = _IN_SESSION_MAX_WAIT_S,
) -> float:
    """Block until no ``slm_local`` model call is in flight, then wait a fixed gap.

    Args:
        es: Async HTTP client pointed at ``elasticsearch-eval``.
        gap_s: Fixed seconds to wait once nothing is in flight.
        max_wait_s: Hard polling timeout before the gap.

    Returns:
        Seconds actually waited, gap included.
    """
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    deadline = t0 + max_wait_s
    while loop.time() < deadline and await local_model_calls_in_flight(es):
        await asyncio.sleep(_IN_SESSION_POLL_INTERVAL_S)
    await asyncio.sleep(gap_s)
    return round(loop.time() - t0, 1)


@dataclass(frozen=True)
class ArmTurnResult:
    """One ``IsolatedArmRunner.run_turn()`` call's outcome.

    Attributes:
        session_id: The turn's session id.
        trace_id: The turn's trace id.
        arm: Which eval gateway served it (an ``EVAL_ARMS`` key).
        extraction_settled: Whether the turn's *entire* consolidation write pipeline
            settled before the next call's wipe — ``entity_extraction_completed``
            AND, once that lands, its batch's own ``consolidation_completed`` (the
            event marking the actual graph write: Turn/Entity MERGE, embeddings,
            session nodes, promotion). Waited out with ``require_nonzero=True``
            throughout: an early "stable at zero" exit here would let the *next*
            call's wipe race a write that simply hadn't landed yet, reproducing
            FRE-1338's leak instead of preventing it. FRE-1372 (live-verified
            2026-09-14): waiting on extraction alone was not enough —
            ``entity_extraction_completed`` marks the LLM call returning, not the
            graph write, and a measured run showed a real cross-arm leak from that
            gap. ``False`` means a full timeout elapsed somewhere in the chain with
            nothing landing — a true negative, not an early guess — and costs the
            full wait every time, deliberately: safety over speed for a gate in
            front of a graph wipe. ``None`` means the caller passed ``wait_settle=False``
            and nothing was waited for (FRE-1517).
    """

    session_id: str
    trace_id: str
    arm: str
    extraction_settled: bool | None


@dataclass
class IsolatedArmRunner:
    """Drives eval-gateway turns with automatic, structural cross-arm isolation.

    Every ``run_turn()`` call that starts a session — including the first — wipes
    ``neo4j-eval`` before driving its turn, then replays ``reseed`` if one was given. A call
    that continues a session (``session_id`` given) does neither (FRE-1517 A1). A caller supplies no
    wipe/restore logic of its own; see the module docstring for why a full wipe is
    the only delete shape used, and why "reseed" replaces "preserve".

    Attributes:
        driver: A connected ``neo4j.AsyncDriver`` for ``EVAL_NEO4J_URI`` — every wipe
            this class issues goes through ``wipe_eval_graph()``'s URI-equality guard.
        reseed: Optional coroutine invoked after every wipe, before the turn's HTTP
            call — the caller's own way of recreating any fixture state (AC-2). Left
            ``None`` when a script needs no fixture.
    """

    driver: Any
    reseed: Callable[[], Awaitable[None]] | None = None
    _turn_count: int = field(default=0, init=False, repr=False)

    async def run_turn(
        self,
        http: httpx.AsyncClient,
        es: httpx.AsyncClient,
        message: str,
        *,
        arm: str = "control",
        session_id: str | None = None,
        model: str | None = None,
        wait_settle: bool = True,
    ) -> ArmTurnResult:
        """Wipe, reseed, then drive one turn — isolated from every earlier session.

        Args:
            http: Async client for POSTing to the eval gateway.
            es: Async client for polling ``elasticsearch-eval``'s settle events.
            message: The turn's user message.
            arm: Which eval gateway to drive (an ``EVAL_ARMS`` key).
            session_id: Continue this session instead of starting one (FRE-1517 A1). Only a
                new session (``None``) wipes and reseeds: the session is the isolation unit,
                because the memory its own earlier turns wrote is part of what a continuing
                turn measures. A wipe per turn would leave a long session nothing beyond
                its history slice to recall.
            model: Optional ``primary`` deployment key for ``/chat``, which stores it on the
                session (ADR-0121 §4).
            wait_settle: Wait for the turn's extraction and consolidation before returning.
                A caller that continues the session passes ``False`` and settles every
                turn itself before the next wipe. A new session waits for full gateway
                quiet first. A continuing turn waits only until no local model call is in
                flight, plus a fixed gap (FRE-1517, master 2026-09-14).

        Returns:
            The turn's identifiers and whether its consolidation write pipeline settled.

        Raises:
            KeyError: If ``arm`` is not a key in ``EVAL_ARMS`` — checked before the
                wipe, so an invalid ``arm`` never wipes ``neo4j-eval`` or runs
                ``reseed`` on its way to raising.
            SubstrateGuardError: If the resolved URL is somehow outside ``EVAL_ARMS``
                (defense in depth — ``EVAL_ARMS[arm]`` already guarantees this).
        """
        base_url = EVAL_ARMS[arm]
        assert_eval_chat_url(base_url)

        if session_id is None:
            await wait_for_gateway_idle(es)
            await wipe_eval_graph(self.driver, uri=EVAL_NEO4J_URI)
            if self.reseed is not None:
                await self.reseed()
        else:
            await wait_for_local_model_calls(es)
        self._turn_count += 1

        params = {"message": message, "channel": "EVAL"}
        if session_id is not None:
            params["session_id"] = session_id
        if model:
            params["model"] = model
        client_timeout = settings.orchestrator_turn_lifetime_seconds + _CLIENT_TIMEOUT_BUFFER_S
        resp = await http.post(f"{base_url}/chat", params=params, timeout=client_timeout)
        resp.raise_for_status()
        data = resp.json()
        session_id, trace_id = str(data["session_id"]), str(data["trace_id"])

        await wait_for_event_settle(
            es, trace_id, "model_call_completed", timeout_s=SIGNAL_SETTLE_TIMEOUT_S
        )
        if not wait_settle:
            return ArmTurnResult(
                session_id=session_id, trace_id=trace_id, arm=arm, extraction_settled=None
            )
        extraction_settled = await wait_for_event_settle(
            es,
            trace_id,
            "entity_extraction_completed",
            timeout_s=EXTRACTION_SETTLE_TIMEOUT_S,
            require_nonzero=True,
            # FRE-1372, live-verified 2026-09-14: when consolidation batches several
            # pending captures into one sweep, entity_extraction_completed's own
            # trace_id is the batch's newly minted id, not this request's — only
            # capture_trace_id still equals it. See wait_for_event_settle's docstring.
            field="capture_trace_id",
        )
        # FRE-1372, live-verified 2026-09-14: entity_extraction_completed marks only
        # the LLM call returning, not the graph write. A measured run showed a 57s
        # gap to the resulting entity_created — waiting on extraction alone let a
        # later wipe fire before that write landed, so it appeared in the *next*
        # arm's freshly-wiped graph: a real cross-arm leak. The batch's own
        # consolidation_completed (Turn/Entity MERGE, embeddings, session nodes,
        # promotion — the actual graph write) is the true "safe to wipe" signal.
        consolidation_settled = False
        if extraction_settled:
            extraction_event = await fetch_latest_event(
                es, trace_id, "entity_extraction_completed", field="capture_trace_id"
            )
            batch_trace_id = str(extraction_event["trace_id"]) if extraction_event else ""
            if batch_trace_id:
                consolidation_settled = await wait_for_event_settle(
                    es,
                    batch_trace_id,
                    "consolidation_completed",
                    timeout_s=CONSOLIDATION_SETTLE_TIMEOUT_S,
                    require_nonzero=True,
                )
        settled = extraction_settled and consolidation_settled
        log.info(
            "fre1372_arm_turn",
            arm=arm,
            turn=self._turn_count,
            session_id=session_id,
            extraction_settled=extraction_settled,
            consolidation_settled=consolidation_settled,
        )
        return ArmTurnResult(
            session_id=session_id,
            trace_id=trace_id,
            arm=arm,
            extraction_settled=settled,
        )


def eval_neo4j_password() -> str:
    """The ``neo4j-eval`` password, from the same env vars ``behavioral.py`` reads.

    Returns:
        The configured password.

    Raises:
        RuntimeError: If neither env var is set.
    """
    password = os.environ.get("NEO4J_PASSWORD") or os.environ.get("STUDY_NEO4J_PASSWORD")
    if not password:
        raise RuntimeError(
            "NEO4J_PASSWORD (or STUDY_NEO4J_PASSWORD) must be set to run an isolated "
            "arm — it authenticates against neo4j-eval, matching docker-compose.eval.yml."
        )
    return password


def create_eval_driver() -> Any:
    """Build a driver for ``neo4j-eval`` — the only substrate this module ever touches.

    Returns:
        A connected ``neo4j.AsyncDriver`` for ``EVAL_NEO4J_URI``.
    """
    from neo4j import AsyncGraphDatabase

    return AsyncGraphDatabase.driver(  # fre-375-allow: EVAL_NEO4J_URI only, guarded in substrate.py
        EVAL_NEO4J_URI, auth=("neo4j", eval_neo4j_password())
    )
