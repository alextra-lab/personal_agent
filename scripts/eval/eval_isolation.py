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
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from scripts.eval.fre1337_intent_probe.behavioral import (
    EXTRACTION_SETTLE_TIMEOUT_S,
    SIGNAL_SETTLE_TIMEOUT_S,
    wait_for_event_settle,
)
from scripts.eval.fre1337_intent_probe.substrate import (
    EVAL_ARMS,
    EVAL_NEO4J_URI,
    assert_eval_chat_url,
    wipe_eval_graph,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ArmTurnResult:
    """One ``IsolatedArmRunner.run_turn()`` call's outcome.

    Attributes:
        session_id: The turn's session id.
        trace_id: The turn's trace id.
        arm: Which eval gateway served it (an ``EVAL_ARMS`` key).
        extraction_settled: Whether ``entity_extraction_completed`` stabilized at a
            nonzero count before timeout. Waited out with ``require_nonzero=True``
            (unlike ``behavioral.py``'s own reporting-only use of this same settle
            wait): an early "stable at zero" exit here would let the *next* call's
            wipe race a turn's extraction that simply hadn't started yet within the
            first couple of polls, reproducing FRE-1338's leak instead of preventing
            it. ``False`` means the full timeout elapsed with nothing landing — a
            true negative, not an early guess — and costs the full
            ``EXTRACTION_SETTLE_TIMEOUT_S`` wait every time, deliberately: safety
            over speed for a gate in front of a graph wipe.
    """

    session_id: str
    trace_id: str
    arm: str
    extraction_settled: bool


@dataclass
class IsolatedArmRunner:
    """Drives eval-gateway turns with automatic, structural cross-arm isolation.

    Every ``run_turn()`` call — including the first — wipes ``neo4j-eval`` before
    driving its turn, then replays ``reseed`` if one was given. A caller supplies no
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
    ) -> ArmTurnResult:
        """Wipe, reseed, then drive one turn — isolated from every earlier turn.

        Args:
            http: Async client for POSTing to the eval gateway.
            es: Async client for polling ``elasticsearch-eval``'s settle events.
            message: The turn's user message.
            arm: Which eval gateway to drive (an ``EVAL_ARMS`` key).

        Returns:
            The turn's identifiers and whether its entity extraction settled.

        Raises:
            KeyError: If ``arm`` is not a key in ``EVAL_ARMS`` — checked before the
                wipe, so an invalid ``arm`` never wipes ``neo4j-eval`` or runs
                ``reseed`` on its way to raising.
            SubstrateGuardError: If the resolved URL is somehow outside ``EVAL_ARMS``
                (defense in depth — ``EVAL_ARMS[arm]`` already guarantees this).
        """
        base_url = EVAL_ARMS[arm]
        assert_eval_chat_url(base_url)

        await wipe_eval_graph(self.driver, uri=EVAL_NEO4J_URI)
        if self.reseed is not None:
            await self.reseed()
        self._turn_count += 1

        resp = await http.post(
            f"{base_url}/chat", params={"message": message, "channel": "EVAL"}, timeout=1200.0
        )
        resp.raise_for_status()
        data = resp.json()
        session_id, trace_id = str(data["session_id"]), str(data["trace_id"])

        await wait_for_event_settle(
            es, trace_id, "model_call_completed", timeout_s=SIGNAL_SETTLE_TIMEOUT_S
        )
        extraction_settled = await wait_for_event_settle(
            es,
            trace_id,
            "entity_extraction_completed",
            timeout_s=EXTRACTION_SETTLE_TIMEOUT_S,
            require_nonzero=True,
        )
        log.info(
            "fre1372_arm_turn",
            arm=arm,
            turn=self._turn_count,
            session_id=session_id,
            extraction_settled=extraction_settled,
        )
        return ArmTurnResult(
            session_id=session_id,
            trace_id=trace_id,
            arm=arm,
            extraction_settled=extraction_settled,
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
