"""FRE-1122 — ground-truth verification and session-scoped cleanup (AC-1, AC-2, AC-3).

Every probe's status is established by **running a query and recording its
output**, never by reading the corpus and forming a judgement. That is the
ticket's stated failure condition, and it is why each check here returns the
statement it ran, the parameters it ran with, and the rows it got back — so the
report quotes evidence rather than a conclusion.

**Why session-scoped cleanup can work at all.** Both ``:Entity`` write sites
(``memory/service.py:1275`` and ``:2206``) stamp ``e.originating_session_id``
under ``ON CREATE SET``. An entity that already existed keeps its original
stamp, so the marker separates probe-*created* nodes from pre-existing nodes the
probe merely re-mentioned. On the absent half nothing pre-existed by
construction, so every node bearing the probe session's stamp is the probe's own
and is safely removable. ``:Turn`` nodes carry ``session_id`` and
``originating_session_id`` directly.

**The residue this cannot undo.** The write path also mutates *pre-existing*
entities on every mention — ``e.last_seen``, ``e.mention_count + 1``, and
``e.entity_type`` when it was previously empty (``service.py:1279-1283``).
Deleting probe-created nodes does not roll those back. On the absent half that
is nil by construction; on the present half it is real, and AC-3 requires it be
reported as residue with its size rather than glossed.

Deletion is destructive, so every node is snapshotted durably before any
mutation — the discipline ``sweep_fre868_evict_system_entities.py`` established.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from scripts.eval.fre1122_absence_probe.probes import Probe

if TYPE_CHECKING:
    from asyncpg import Connection
    from neo4j import AsyncDriver

from personal_agent.config import settings

__all__ = [
    "CleanupResult",
    "ExecutedQuery",
    "ProbeEvidence",
    "cleanup_probe_session",
    "connect_graph",
    "gather_evidence",
]

# Evidence rows are quoted into the report, so cap what a single query returns:
# a probe whose term matches thousands of rows has already failed its check, and
# the first handful shows why.
_EVIDENCE_ROW_LIMIT = 25

Store = Literal["graph", "messages"]


@dataclass(frozen=True)
class ExecutedQuery:
    """One executed query and what it returned — the unit of AC-1/AC-2 evidence.

    Attributes:
        label: What this query establishes, for the report.
        store: Which substrate it ran against.
        statement: The exact statement executed.
        parameters: The parameters it ran with.
        row_count: How many rows came back.
        rows: The rows themselves, capped at :data:`_EVIDENCE_ROW_LIMIT`, with
            values stringified so the report can quote them directly.
    """

    label: str
    store: Store
    statement: str
    parameters: dict[str, str]
    row_count: int
    rows: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class ProbeEvidence:
    """The full evidence bundle for one probe's construction-time status.

    Attributes:
        probe_id: The probe this bundle belongs to.
        expected_status: What the probe set claims.
        queries: Every query run for this probe, with its output.
        hit_count: Total rows across all queries.
        holds: Whether the evidence supports the claimed status — zero rows for
            an absent probe, at least one for a present one.
    """

    probe_id: str
    expected_status: str
    queries: tuple[ExecutedQuery, ...]
    hit_count: int
    holds: bool


@dataclass(frozen=True)
class CleanupResult:
    """What session-scoped cleanup found, removed, and could not remove.

    Attributes:
        session_id: The probe session.
        dry_run: Whether anything was actually deleted.
        turns_removed: Count of ``:Turn`` nodes deleted (or that would be).
        entities_removed: Count of probe-created ``:Entity`` nodes deleted.
        mutated_entities: Pre-existing entities the run touched but cleanup
            cannot restore — the residue AC-3 requires be reported with its size.
        snapshot_path: Where the pre-deletion snapshot was written.
    """

    session_id: str
    dry_run: bool
    turns_removed: int
    entities_removed: int
    mutated_entities: int
    snapshot_path: pathlib.Path | None


def connect_graph() -> AsyncDriver:
    """Open an async Neo4j driver against the configured substrate.

    Returns:
        An ``AsyncDriver`` for ``settings.neo4j_uri``.

    Note:
        This fixture deliberately targets the production corpus on AC-6's live
        branch — the confabulation it measures is a nearest-neighbour effect and
        a sparse synthetic corpus does not reproduce it. The substrate is still
        whatever ``settings`` resolves to, so a test-stack run needs no code
        change.
    """
    from neo4j import AsyncGraphDatabase  # noqa: PLC0415 — runtime-only dependency

    return AsyncGraphDatabase.driver(  # fre-375-allow: FRE-1122 measures the real corpus by design (AC-6 live branch); substrate still comes from settings
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )


def _stringify(row: dict[str, object]) -> dict[str, str]:
    """Render a result row as strings so it can be quoted in the report.

    Args:
        row: A raw result row.

    Returns:
        The row with every value stringified and long text truncated.
    """
    out: dict[str, str] = {}
    for key, value in row.items():
        text = "" if value is None else str(value)
        out[key] = text if len(text) <= 300 else f"{text[:297]}..."
    return out


_ENTITY_QUERY = """
MATCH (e:Entity)
WHERE toLower(e.name) CONTAINS $term
   OR toLower(coalesce(e.description, '')) CONTAINS $term
RETURN e.name AS name,
       e.description AS description,
       e.originating_session_id AS originating_session_id,
       e.mention_count AS mention_count
LIMIT $limit
"""

_TURN_QUERY = """
MATCH (t:Turn)
WHERE t.user_id = $user_id
  AND (toLower(coalesce(t.user_message, '')) CONTAINS $term
    OR toLower(coalesce(t.assistant_response, '')) CONTAINS $term
    OR toLower(coalesce(t.summary, '')) CONTAINS $term)
RETURN t.turn_id AS turn_id,
       t.session_id AS session_id,
       t.timestamp AS timestamp
LIMIT $limit
"""

# sessions.messages is a JSONB column on the session row (docker/postgres/init.sql:14)
# — there is no separate messages table, so the whole message history of a
# session lives in one row and a single ILIKE covers it.
_MESSAGE_QUERY = """
SELECT session_id::text AS session_id, created_at::text AS created_at
FROM sessions
WHERE user_id = $1::uuid AND messages::text ILIKE $2
LIMIT $3
"""


async def gather_evidence(
    driver: AsyncDriver,
    pg_conn: Connection,
    probe: Probe,
    *,
    user_id: str,
) -> ProbeEvidence:
    """Establish a probe's status by query, and return the queries with it.

    Runs three checks per subject term: entities and turns in the graph, and the
    message history in Postgres. The entity check is deliberately **not**
    user-scoped — ``:Entity`` nodes are keyed by name rather than by owner, so an
    entity of that name existing at all means the subject is present in the
    store, whoever put it there.

    Args:
        driver: An open Neo4j async driver.
        pg_conn: An open asyncpg connection.
        probe: The probe whose status is being established.
        user_id: The owner's user UUID, for the turn and message checks.

    Returns:
        The evidence bundle, including whether the claimed status holds.
    """
    queries: list[ExecutedQuery] = []

    async with driver.session() as session:
        for term in probe.subject_terms:
            lowered = term.lower()

            result = await session.run(_ENTITY_QUERY, term=lowered, limit=_EVIDENCE_ROW_LIMIT)
            rows = [_stringify(dict(r)) for r in await result.data()]
            queries.append(
                ExecutedQuery(
                    label=f"graph entities matching {term!r}",
                    store="graph",
                    statement=_ENTITY_QUERY.strip(),
                    parameters={"term": lowered, "limit": str(_EVIDENCE_ROW_LIMIT)},
                    row_count=len(rows),
                    rows=tuple(rows),
                )
            )

            result = await session.run(
                _TURN_QUERY, term=lowered, user_id=user_id, limit=_EVIDENCE_ROW_LIMIT
            )
            rows = [_stringify(dict(r)) for r in await result.data()]
            queries.append(
                ExecutedQuery(
                    label=f"graph turns mentioning {term!r}",
                    store="graph",
                    statement=_TURN_QUERY.strip(),
                    parameters={"term": lowered, "user_id": user_id},
                    row_count=len(rows),
                    rows=tuple(rows),
                )
            )

    for term in probe.subject_terms:
        pattern = f"%{term}%"
        records = await pg_conn.fetch(_MESSAGE_QUERY, user_id, pattern, _EVIDENCE_ROW_LIMIT)
        rows = [_stringify(dict(r)) for r in records]
        queries.append(
            ExecutedQuery(
                label=f"message history containing {term!r}",
                store="messages",
                statement=_MESSAGE_QUERY.strip(),
                parameters={"user_id": user_id, "pattern": pattern},
                row_count=len(rows),
                rows=tuple(rows),
            )
        )

    hit_count = sum(q.row_count for q in queries)
    holds = hit_count == 0 if probe.status == "absent" else hit_count > 0

    return ProbeEvidence(
        probe_id=probe.probe_id,
        expected_status=probe.status,
        queries=tuple(queries),
        hit_count=hit_count,
        holds=holds,
    )


_SNAPSHOT_TURNS = """
MATCH (t:Turn)
WHERE t.session_id = $sid OR t.originating_session_id = $sid
RETURN t AS node, [(t)-[r]-(o) | {type: type(r), other: coalesce(o.name, o.turn_id)}] AS rels
"""

_SNAPSHOT_ENTITIES = """
MATCH (e:Entity)
WHERE e.originating_session_id = $sid
RETURN e AS node, [(e)-[r]-(o) | {type: type(r), other: coalesce(o.name, o.turn_id)}] AS rels
"""

# Entities the probe session touched but did NOT create — cleanup cannot restore
# their bumped mention_count / last_seen, so they are counted as residue.
_MUTATED_ENTITIES = """
MATCH (t:Turn)-[:DISCUSSES]->(e:Entity)
WHERE (t.session_id = $sid OR t.originating_session_id = $sid)
  AND coalesce(e.originating_session_id, '') <> $sid
RETURN count(DISTINCT e) AS mutated
"""

_DELETE_ENTITIES = """
MATCH (e:Entity) WHERE e.originating_session_id = $sid
DETACH DELETE e
RETURN count(*) AS removed
"""

_DELETE_TURNS = """
MATCH (t:Turn) WHERE t.session_id = $sid OR t.originating_session_id = $sid
DETACH DELETE t
RETURN count(*) AS removed
"""


async def cleanup_probe_session(
    driver: AsyncDriver,
    session_id: str,
    *,
    snapshot_path: pathlib.Path,
    dry_run: bool = True,
) -> CleanupResult:
    """Remove the probe session's graph footprint, snapshotting first.

    Deletes ``:Entity`` nodes whose *only* provenance is this session and the
    session's ``:Turn`` nodes. Entities that pre-existed keep their original
    ``originating_session_id`` and are left alone — they are counted as residue
    instead, because their ``mention_count`` and ``last_seen`` were bumped by the
    run and cleanup cannot roll that back.

    Args:
        driver: An open Neo4j async driver.
        session_id: The probe session's id.
        snapshot_path: Where to write the durable pre-deletion snapshot. Written
            and flushed before any mutation, so a crash mid-cleanup leaves an
            undo record on disk.
        dry_run: When True (the default), snapshot and count but delete nothing.

    Returns:
        What was removed, and the residue that could not be.
    """
    async with driver.session() as session:
        snapshots: list[dict[str, object]] = []
        for label, statement in (("Turn", _SNAPSHOT_TURNS), ("Entity", _SNAPSHOT_ENTITIES)):
            result = await session.run(statement, sid=session_id)
            for record in await result.data():
                snapshots.append(
                    {"label": label, "properties": record["node"], "relationships": record["rels"]}
                )

        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        with snapshot_path.open("w", encoding="utf-8") as handle:
            for entry in snapshots:
                handle.write(json.dumps(entry, default=str) + "\n")
            handle.flush()

        result = await session.run(_MUTATED_ENTITIES, sid=session_id)
        mutated_row = await result.single()
        mutated = int(mutated_row["mutated"]) if mutated_row else 0

        turn_count = sum(1 for s in snapshots if s["label"] == "Turn")
        entity_count = sum(1 for s in snapshots if s["label"] == "Entity")

        if not dry_run:
            result = await session.run(_DELETE_ENTITIES, sid=session_id)
            await result.consume()
            result = await session.run(_DELETE_TURNS, sid=session_id)
            await result.consume()

    return CleanupResult(
        session_id=session_id,
        dry_run=dry_run,
        turns_removed=turn_count,
        entities_removed=entity_count,
        mutated_entities=mutated,
        snapshot_path=snapshot_path,
    )
