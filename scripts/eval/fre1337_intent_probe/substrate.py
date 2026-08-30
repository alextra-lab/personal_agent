"""Arm 3's contamination control (AC-3) — isolated eval graph, wiped between fixtures.

FRE-1338's incident: entity extraction from one behavioral turn wrote nodes that a
*second* turn's ``search_memory`` picked up and cited 31 seconds later, unread. The
control here is a full-graph wipe of ``neo4j-eval`` before every fixture in the
behavioral arm — safe only because it targets nothing but the isolated eval substrate.

Guard is a **hardcoded URI allowlist**, not the ``Environment`` enum: the eval gateway's
``APP_ENV=eval`` (``docker-compose.eval.yml``) resolves to ``Environment.DEVELOPMENT``
per ``env_loader.py``'s fallthrough (there is no ``Environment.EVAL``), so the existing
``fre435_memory_recall`` harness's ``Environment.TEST``-gated ``wipe_substrate()`` doesn't
fit this substrate and isn't reused as-is — a string-equality check on the one URI this
harness is allowed to touch is simpler and cannot be fooled by an environment
misdetection.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: The only Neo4j this harness is ever allowed to mutate. Host-side bolt port for
#: `docker-compose.eval.yml`'s `neo4j-eval` service (127.0.0.1:7689:7687) — never prod's
#: :7687 or the FRE-375 test stack's :7688.
EVAL_NEO4J_URI = "bolt://localhost:7689"

#: The only `/chat` this harness's behavioral arm is ever allowed to drive. Host-side port
#: for `seshat-gateway-control` (127.0.0.1:9002:9001) — never prod's :9001.
EVAL_CHAT_BASE_URL = "http://localhost:9002"

#: Full-graph wipe, same statement `fre435_memory_recall/harness.py`'s `WIPE_CYPHER` uses.
#: Leaves schema (constraints, vector index) intact.
WIPE_CYPHER = "MATCH (n) DETACH DELETE n"


class SubstrateGuardError(RuntimeError):
    """Raised when a caller asks this module to touch anything but the eval substrate."""


def _assert_eval_uri(uri: str) -> None:
    if uri != EVAL_NEO4J_URI:
        raise SubstrateGuardError(
            f"refused: {uri!r} is not the eval Neo4j ({EVAL_NEO4J_URI!r}) — this harness "
            "wipes nothing but the isolated eval substrate."
        )


async def wipe_eval_graph(driver: Any, *, uri: str) -> None:
    """DETACH DELETE every node in the eval graph, for per-fixture isolation.

    Args:
        driver: A connected ``neo4j.AsyncDriver`` for ``uri``.
        uri: The URI the driver is connected to — checked, not trusted, against
            :data:`EVAL_NEO4J_URI` before every wipe.

    Raises:
        SubstrateGuardError: If ``uri`` is anything but the eval Neo4j.
    """
    _assert_eval_uri(uri)
    async with driver.session() as session:
        await session.run(WIPE_CYPHER)
    log.info("fre1337_eval_graph_wiped", uri=uri)


def assert_eval_chat_url(base_url: str) -> None:
    """Refuse to drive anything but the isolated eval gateway.

    Args:
        base_url: The `/chat` base URL the behavioral driver is about to POST to.

    Raises:
        SubstrateGuardError: If ``base_url`` is anything but the eval gateway.
    """
    if base_url != EVAL_CHAT_BASE_URL:
        raise SubstrateGuardError(
            f"refused: {base_url!r} is not the eval gateway ({EVAL_CHAT_BASE_URL!r}) — "
            "this harness's behavioral arm drives nothing but the isolated eval gateway."
        )


async def fetch_originating_session_ids(driver: Any, *, uri: str) -> list[dict[str, Any]]:
    """Read every node's ``originating_session_id`` currently in the eval graph.

    The AC-3 proof's raw material: after a wipe + a later turn, this is what
    :func:`find_cross_session_sources` checks for anything that traces back to a session
    that should have been cleared.

    Args:
        driver: A connected ``neo4j.AsyncDriver`` for ``uri``.
        uri: The URI the driver is connected to — checked against
            :data:`EVAL_NEO4J_URI` before every read, same guard as the wipe.

    Returns:
        One record per node carrying an ``originating_session_id`` property.

    Raises:
        SubstrateGuardError: If ``uri`` is anything but the eval Neo4j.
    """
    _assert_eval_uri(uri)
    async with driver.session() as session:
        result = await session.run(
            "MATCH (n) WHERE n.originating_session_id IS NOT NULL "
            "RETURN n.originating_session_id AS originating_session_id, "
            "labels(n) AS labels, coalesce(n.name, n.id, '') AS name"
        )
        return [dict(record) async for record in result]


def find_cross_session_sources(
    session_sources: list[dict[str, Any]], excluded_session_id: str
) -> list[dict[str, Any]]:
    """AC-3's proof: does a session's source list contain anything from a prior session?

    Args:
        session_sources: Records touched by the *later* run (each carrying
            ``originating_session_id``) — e.g. entities the turn's ``search_memory``
            call surfaced, read back from the eval graph.
        excluded_session_id: The *earlier* run's session id — nothing in
            ``session_sources`` should trace back to it if the wipe between fixtures
            worked.

    Returns:
        The subset of ``session_sources`` that DO originate from
        ``excluded_session_id`` — empty when the control held. Non-empty is the exact
        contamination shape FRE-1338 measured (a source cited but never fetched this
        run, carried over from the prior one).
    """
    return [
        record
        for record in session_sources
        if record.get("originating_session_id") == excluded_session_id
    ]
