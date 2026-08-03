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

**The residue this cannot undo**, in three measured classes. The write path
mutates *pre-existing* entities on every mention — ``e.last_seen``,
``e.mention_count + 1``, and ``e.entity_type`` when previously empty
(``service.py:1279-1283``) — and consolidation can additionally *fill* an empty
description or *overwrite* an existing one (FRE-711, ``service.py:2201-2230``).
Deleting probe-created nodes rolls none of that back. On the absent half it is
nil by construction; on the present half it is real, and AC-3 requires it be
reported with its size rather than glossed, which is what
:class:`CleanupResult`'s three residue counts are for.

**One protection worth stating precisely**, because the ticket understates it:
eval mode does more than suppress Linear promotion. FRE-711's *correction* arm
carries ``AND NOT ($eval_mode AND coalesce(_old_eval, false) = false)``, so an
eval-mode description can never overwrite a non-eval one — and the runner fires
every probe on ``channel="EVAL"``, which sets ``eval_mode`` (``app.py:2111``).
The *fill* arm has no such guard, so a previously-empty description can still be
populated by a probe turn. Given FRE-1115 measured 18.7% of the corpus as
empty-description, that is the residue class most likely to be non-zero.

Deletion is destructive, so every node is snapshotted durably before any
mutation — the discipline ``sweep_fre868_evict_system_entities.py`` established.
"""

from __future__ import annotations

import json
import os
import pathlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from scripts.eval.fre1122_absence_probe.probes import Probe

if TYPE_CHECKING:
    from asyncpg import Connection
    from neo4j import AsyncDriver

from personal_agent.config import settings

__all__ = [
    "CleanupRefused",
    "CleanupResult",
    "ExecutedQuery",
    "ProbeEvidence",
    "cleanup_probe_session",
    "connect_graph",
    "gather_evidence",
    "verify_expected_source",
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
        entities_removed: Count of probe-created ``:Entity`` nodes deleted —
            only those never referenced from outside the probe session.
        claims_removed: Count of ``:Claim`` nodes the run asserted and cleanup
            deleted. Consolidation writes claims on every turn, so without this
            the run's own facts survive and the absent half never returns to
            zero rows.
        mutated_entities: Pre-existing entities the run touched but cleanup
            cannot restore — the residue AC-3 requires be reported with its size.
        descriptions_filled: Pre-existing entities whose empty description the
            run populated. Not undone by cleanup.
        descriptions_rewritten: Pre-existing descriptions the run overwrote.
            Recoverable — FRE-711 archives the prior text to an
            ``:EntityDescriptionVersion`` node — but not restored by cleanup.
        claims_superseded: Pre-existing owner claims the run invalidated by
            asserting over them. Not residue but **data loss** if left — the
            write path sets valid_to, invalid_at and superseded_by on the claim
            it replaces, so deleting the run's claim would strand a real fact
            as invalid with a dangling pointer.
        claims_restored: How many of those were restored to current. Zero on a
            dry run, which is why a dry run cannot attest to restoration.
        adopted_entities_retained: Probe-created entities a later, unrelated turn
            adopted. Deliberately NOT deleted — another session now depends on
            them — and named here so the residue is visible rather than either
            silently destroyed or silently ignored.
        snapshot_path: Where the pre-deletion snapshot was written.
    """

    session_id: str
    dry_run: bool
    turns_removed: int
    entities_removed: int
    claims_removed: int
    claims_superseded: int
    claims_restored: int
    mutated_entities: int
    descriptions_filled: int
    descriptions_rewritten: int
    adopted_entities_retained: tuple[str, ...]
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

# Owner-scoped CURRENT claims. This surface is not reachable from :Entity or
# :Turn — a claim carries its own ``content`` text, is written by consolidation
# on every turn (consolidator.py:876 -> assert_claim), and is read back by
# search_memory (service.py:2814). Omitting it was a false-zero hole: a fact
# retrievable by the authenticated gateway would have been reported absent, and
# that silently invalidates the entire absent half (Codex round 1, finding 3).
_CLAIM_QUERY = """
MATCH (:Person {user_id: $user_id})-[:HAS_FACT]->(cl:Claim)
WHERE cl.valid_to IS NULL AND cl.invalid_at IS NULL
  AND toLower(coalesce(cl.content, '')) CONTAINS $term
RETURN cl.claim_id AS claim_id,
       cl.content AS content,
       cl.session_id AS session_id
LIMIT $limit
"""

# Counting counterparts. The evidence queries carry LIMIT so the report can quote
# rows, but a capped count understates AC-3's pollution size — "at most 25" is
# not a size (Codex round 1, non-blocking 3). Zero/non-zero is unaffected; the
# magnitude is what these recover.
_ENTITY_COUNT = """
MATCH (e:Entity)
WHERE toLower(e.name) CONTAINS $term
   OR toLower(coalesce(e.description, '')) CONTAINS $term
RETURN count(e) AS total
"""

_TURN_COUNT = """
MATCH (t:Turn)
WHERE t.user_id = $user_id
  AND (toLower(coalesce(t.user_message, '')) CONTAINS $term
    OR toLower(coalesce(t.assistant_response, '')) CONTAINS $term
    OR toLower(coalesce(t.summary, '')) CONTAINS $term)
RETURN count(t) AS total
"""

_CLAIM_COUNT = """
MATCH (:Person {user_id: $user_id})-[:HAS_FACT]->(cl:Claim)
WHERE cl.valid_to IS NULL AND cl.invalid_at IS NULL
  AND toLower(coalesce(cl.content, '')) CONTAINS $term
RETURN count(cl) AS total
"""

# sessions.messages is a JSONB column on the session row (docker/postgres/init.sql:14)
# — there is no separate messages table, so the whole message history of a
# session lives in one row and a single ILIKE covers it.
#
# ESCAPE '\' is not decoration: a subject term containing % or _ would otherwise
# become a wildcard and manufacture false hits (Codex round 1, non-blocking 2).
_MESSAGE_QUERY = r"""
SELECT session_id::text AS session_id, created_at::text AS created_at
FROM sessions
WHERE user_id = $1::uuid AND messages::text ILIKE $2 ESCAPE '\'
LIMIT $3
"""

_MESSAGE_COUNT = r"""
SELECT count(*) AS total
FROM sessions
WHERE user_id = $1::uuid AND messages::text ILIKE $2 ESCAPE '\'
"""


def _ilike_pattern(term: str) -> str:
    r"""Build an ILIKE pattern with SQL wildcards in ``term`` escaped.

    Args:
        term: A probe subject term, authored by a human and never sanitised
            upstream.

    Returns:
        A ``%term%`` pattern in which any literal ``%``, ``_`` or ``\`` from the
        term matches itself rather than acting as a wildcard.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_SOURCE_TURN = """
MATCH (t:Turn {turn_id: $row_id})
WHERE t.user_id = $user_id
RETURN t.turn_id AS turn_id,
       coalesce(t.user_message, '') + ' ' + coalesce(t.assistant_response, '')
         + ' ' + coalesce(t.summary, '') AS content
"""

_SOURCE_CLAIM = """
MATCH (:Person {user_id: $user_id})-[:HAS_FACT]->(cl:Claim {claim_id: $row_id})
WHERE cl.valid_to IS NULL AND cl.invalid_at IS NULL
RETURN cl.claim_id AS claim_id, cl.content AS content
"""

_SOURCE_ENTITY = """
MATCH (e:Entity {name: $row_id})
RETURN e.name AS name, coalesce(e.description, '') AS content
"""

_SOURCE_KINDS: dict[str, str] = {
    "turn": _SOURCE_TURN,
    "claim": _SOURCE_CLAIM,
    "entity": _SOURCE_ENTITY,
}


async def verify_expected_source(
    driver: AsyncDriver,
    pg_conn: Connection,
    probe: Probe,
    *,
    user_id: str,
) -> ExecutedQuery:
    """Fetch the stored row a present probe names, and check it holds the fact.

    This is what makes AC-2 mean what it says. Establishing "present" from any
    lexical hit on a subject term let an unrelated row vouch for a fact that was
    never stored — a probe naming ``Turn:does-not-exist`` still passed as long as
    some entity somewhere matched one of its terms (Codex round 1, finding 4).

    A row satisfies the check only when it exists, is visible to this owner, and
    its stored text contains **every** expected token. Anything less returns zero
    rows, which fails the probe rather than warning about it.

    Args:
        driver: An open Neo4j async driver.
        pg_conn: An open asyncpg connection (reserved for message-backed sources).
        probe: The present probe whose ``expected_source`` is being verified.
        user_id: The owner's user UUID.

    Returns:
        The executed query and its output, with ``row_count`` 1 when the named
        row exists *and* reproduces every expected token, else 0.
    """
    raw = probe.expected_source or ""
    kind, _, row_id = raw.partition(":")
    statement = _SOURCE_KINDS.get(kind.strip().lower(), "")
    label = f"AC-2 stored row {raw!r} for {probe.probe_id}"

    if not statement or not row_id.strip():
        return ExecutedQuery(
            label=label,
            store="graph",
            statement="(none — unparseable expected_source)",
            parameters={"expected_source": raw},
            row_count=0,
            rows=(
                {
                    "error": (
                        f"expected_source must be one of {sorted(_SOURCE_KINDS)} "
                        f"followed by ':<id>'; got {raw!r}"
                    )
                },
            ),
        )

    async with driver.session() as session:
        result = await session.run(statement, row_id=row_id.strip(), user_id=user_id)
        found = await result.data()

    if not found:
        return ExecutedQuery(
            label=label,
            store="graph",
            statement=statement.strip(),
            parameters={"row_id": row_id.strip(), "user_id": user_id},
            row_count=0,
            rows=({"error": "the named row does not exist or is not visible to this owner"},),
        )

    content = _normalise_text(str(found[0].get("content", "")))
    missing = [t for t in probe.expected_tokens if _normalise_text(t) not in content]

    return ExecutedQuery(
        label=label,
        store="graph",
        statement=statement.strip(),
        parameters={"row_id": row_id.strip(), "user_id": user_id},
        row_count=0 if missing else 1,
        rows=(
            _stringify(found[0])
            if not missing
            else {"error": f"stored row found but missing expected token(s): {missing!r}"},
        ),
    )


def _normalise_text(text: str) -> str:
    """Lowercase and collapse whitespace, for token containment checks.

    Args:
        text: Raw stored text or an expected token.

    Returns:
        The comparable form.
    """
    return " ".join(text.split()).lower()


async def gather_evidence(
    driver: AsyncDriver,
    pg_conn: Connection,
    probe: Probe,
    *,
    user_id: str,
) -> ProbeEvidence:
    """Establish a probe's status by query, and return the queries with it.

    Runs four checks per subject term — entities, turns and owner-scoped current
    claims in the graph, and the message history in Postgres. The claim check
    matters disproportionately: a ``:Claim`` carries its own ``content``, is
    written by consolidation on every turn and read back by ``search_memory``, so
    a subject present only there would otherwise be reported absent.

    The entity check is deliberately **not** user-scoped — ``:Entity`` nodes are
    keyed by name rather than by owner, so an entity of that name existing at all
    means the subject is present, whoever put it there. Stances reach the same
    node (``(:Person)-[:HAS_STANCE]->(:Entity {name})``) and are covered by it.

    For a **present** probe, subject-term hits are supporting diagnostics only.
    Its status is decided by :func:`verify_expected_source`, which fetches the
    row the probe names and checks the expected tokens are in it — a lexical hit
    on an unrelated row proves nothing about the fact being recallable (AC-2).

    Args:
        driver: An open Neo4j async driver.
        pg_conn: An open asyncpg connection.
        probe: The probe whose status is being established.
        user_id: The owner's user UUID, for the turn, claim and message checks.

    Returns:
        The evidence bundle, including whether the claimed status holds.
    """
    queries: list[ExecutedQuery] = []

    async with driver.session() as session:
        for term in probe.subject_terms:
            lowered = term.lower()

            for label, evidence_q, count_q, params in (
                ("entities", _ENTITY_QUERY, _ENTITY_COUNT, {"term": lowered}),
                ("turns", _TURN_QUERY, _TURN_COUNT, {"term": lowered, "user_id": user_id}),
                (
                    "current claims",
                    _CLAIM_QUERY,
                    _CLAIM_COUNT,
                    {"term": lowered, "user_id": user_id},
                ),
            ):
                query_params = {**params, "limit": _EVIDENCE_ROW_LIMIT}
                result = await session.run(evidence_q, query_params)
                rows = [_stringify(dict(r)) for r in await result.data()]
                total = await _count(session, count_q, "total", **params)
                queries.append(
                    ExecutedQuery(
                        label=f"graph {label} matching {term!r}",
                        store="graph",
                        statement=evidence_q.strip(),
                        parameters={k: str(v) for k, v in params.items()},
                        row_count=total,
                        rows=tuple(rows),
                    )
                )

    for term in probe.subject_terms:
        pattern = _ilike_pattern(term)
        records = await pg_conn.fetch(_MESSAGE_QUERY, user_id, pattern, _EVIDENCE_ROW_LIMIT)
        rows = [_stringify(dict(r)) for r in records]
        total_row = await pg_conn.fetchrow(_MESSAGE_COUNT, user_id, pattern)
        queries.append(
            ExecutedQuery(
                label=f"message history containing {term!r}",
                store="messages",
                statement=_MESSAGE_QUERY.strip(),
                parameters={"user_id": user_id, "pattern": pattern},
                row_count=int(total_row["total"]) if total_row else 0,
                rows=tuple(rows),
            )
        )

    hit_count = sum(q.row_count for q in queries)

    if probe.status == "absent":
        holds = hit_count == 0
    else:
        source = await verify_expected_source(driver, pg_conn, probe, user_id=user_id)
        queries.append(source)
        holds = source.row_count > 0

    return ProbeEvidence(
        probe_id=probe.probe_id,
        expected_status=probe.status,
        queries=tuple(queries),
        hit_count=hit_count,
        holds=holds,
    )


# Snapshots carry stable element ids, relationship direction and relationship
# properties. The first draft stored only the type and a lossy
# coalesce(o.name, o.turn_id), which could not reconstruct what DETACH DELETE
# destroyed (Codex round 1, finding 8).
_SNAPSHOT_TURNS = """
MATCH (t:Turn)
WHERE t.session_id = $sid OR t.originating_session_id = $sid
RETURN elementId(t) AS element_id, labels(t) AS labels, properties(t) AS node,
       [(t)-[r]->(o) | {type: type(r), direction: 'out', properties: properties(r),
                        other_element_id: elementId(o), other_labels: labels(o),
                        other_key: coalesce(o.name, o.turn_id, o.claim_id)}] AS rels_out,
       [(t)<-[r]-(o) | {type: type(r), direction: 'in', properties: properties(r),
                        other_element_id: elementId(o), other_labels: labels(o),
                        other_key: coalesce(o.name, o.turn_id, o.claim_id)}] AS rels_in
"""

# Only entities created by this session AND never referenced from outside it.
# The originating_session_id stamp alone was not enough: an entity the probe
# created can afterwards be mentioned by a real turn, and DETACH DELETE would
# then destroy a node another session depends on along with that turn's edge
# (Codex round 1, finding 7).
_SNAPSHOT_ENTITIES = """
MATCH (e:Entity)
WHERE e.originating_session_id = $sid
  AND NOT EXISTS {
      MATCH (t:Turn)-[:DISCUSSES]->(e)
      WHERE coalesce(t.session_id, '') <> $sid
        AND coalesce(t.originating_session_id, '') <> $sid
  }
RETURN elementId(e) AS element_id, labels(e) AS labels, properties(e) AS node,
       [(e)-[r]->(o) | {type: type(r), direction: 'out', properties: properties(r),
                        other_element_id: elementId(o), other_labels: labels(o),
                        other_key: coalesce(o.name, o.turn_id, o.claim_id)}] AS rels_out,
       [(e)<-[r]-(o) | {type: type(r), direction: 'in', properties: properties(r),
                        other_element_id: elementId(o), other_labels: labels(o),
                        other_key: coalesce(o.name, o.turn_id, o.claim_id)}] AS rels_in
"""

# Probe-created entities that a LATER, unrelated turn adopted. Not deleted —
# reported as retained, with their names, so the residue is visible rather than
# silently destroyed or silently ignored.
_ADOPTED_ENTITIES = """
MATCH (e:Entity)
WHERE e.originating_session_id = $sid
  AND EXISTS {
      MATCH (t:Turn)-[:DISCUSSES]->(e)
      WHERE coalesce(t.session_id, '') <> $sid
        AND coalesce(t.originating_session_id, '') <> $sid
  }
RETURN e.name AS name
"""

# Binds the session to this run before anything destructive happens: it must
# belong to the expected owner and contain the trace ids the run recorded. A
# stale or hand-edited run artifact naming a real production session would
# otherwise have its turns and entities deleted (Codex round 1, finding 7).
_VERIFY_SESSION_BINDING = """
MATCH (t:Turn)
WHERE t.session_id = $sid OR t.originating_session_id = $sid
RETURN count(t) AS turns,
       count(CASE WHEN t.user_id = $user_id THEN 1 END) AS owned,
       count(CASE WHEN t.trace_id IN $trace_ids THEN 1 END) AS matching_traces
"""


# The graph carries its own :Session node (service.py:1369), distinct from the
# Postgres row. It survived an otherwise "zero residue" cleanup (Codex round 2).
_SNAPSHOT_SESSION = """
MATCH (s:Session {session_id: $sid})
RETURN elementId(s) AS element_id, labels(s) AS labels, properties(s) AS node,
       [(s)-[r]->(o) | {type: type(r), direction: 'out', properties: properties(r),
                        other_element_id: elementId(o), other_labels: labels(o),
                        other_key: coalesce(o.name, o.turn_id, o.claim_id)}] AS rels_out,
       [(s)<-[r]-(o) | {type: type(r), direction: 'in', properties: properties(r),
                        other_element_id: elementId(o), other_labels: labels(o),
                        other_key: coalesce(o.name, o.turn_id, o.claim_id)}] AS rels_in
"""

_DELETE_SESSION = """
MATCH (s:Session {session_id: $sid})
DETACH DELETE s
"""

# Pre-existing claims the run SUPERSEDED. assert_claim sets valid_to, invalid_at
# and superseded_by on the claim it replaces (service.py:2677-2693), so deleting
# the run's own claim leaves a real owner fact invalidated with a dangling
# pointer. That is data loss, not residue, and it is restored rather than
# counted — the probe's supersession is the only thing that set those fields.
_SUPERSEDED_BY_RUN = """
MATCH (o:Person)-[:HAS_FACT]->(old:Claim)
WHERE old.superseded_by IN $claim_ids
RETURN elementId(old) AS element_id, labels(old) AS labels, properties(old) AS node,
       [] AS rels_out, [] AS rels_in
"""

_RESTORE_SUPERSEDED = """
MATCH (o:Person)-[:HAS_FACT]->(old:Claim)
WHERE old.superseded_by IN $claim_ids
SET old.valid_to = null, old.invalid_at = null,
    old.superseded_by = null, old.supersession_reason = null
RETURN count(old) AS restored
"""

_RUN_CLAIM_IDS = """
MATCH (:Person)-[:HAS_FACT]->(cl:Claim {session_id: $sid})
RETURN collect(cl.claim_id) AS claim_ids
"""


class CleanupRefused(RuntimeError):
    """Cleanup was asked to delete something it could not prove belongs to the run."""


# Entities the probe session touched but did NOT create — cleanup cannot restore
# their bumped mention_count / last_seen, so they are counted as residue.
_MUTATED_ENTITIES = """
MATCH (t:Turn)-[:DISCUSSES]->(e:Entity)
WHERE (t.session_id = $sid OR t.originating_session_id = $sid)
  AND coalesce(e.originating_session_id, '') <> $sid
RETURN count(DISTINCT e) AS mutated
"""

# Pre-existing entities whose EMPTY description the run filled. FRE-711's
# _do_fill arm carries no eval-mode guard, so an eval turn can populate a
# previously-empty description — and given FRE-1115 measured 18.7% of the corpus
# as empty-description, this is the residue class most likely to be non-zero.
_FILLED_DESCRIPTIONS = """
MATCH (t:Turn)-[:DISCUSSES]->(e:Entity)
WHERE (t.session_id = $sid OR t.originating_session_id = $sid)
  AND coalesce(e.originating_session_id, '') <> $sid
  AND coalesce(e.description_eval_mode, false) = true
  AND coalesce(e.description, '') <> ''
RETURN count(DISTINCT e) AS filled
"""

# Pre-existing descriptions the run OVERWROTE. FRE-711 archives the prior text to
# a :HAD_DESCRIPTION -> :EntityDescriptionVersion node stamped with the trace
# that caused it, so this residue is both countable and — unlike a mention_count
# bump — recoverable from the archived version.
_REWRITTEN_DESCRIPTIONS = """
MATCH (e:Entity)-[:HAD_DESCRIPTION]->(v:EntityDescriptionVersion)
WHERE v.source_trace_id IN $trace_ids
RETURN count(v) AS rewritten
"""

# Claims the run asserted. Consolidation writes :Claim on every turn
# (consolidator.py:876), so without this the run's own facts survive cleanup and
# the absent half never returns to zero rows (Codex round 1, finding 5).
_SNAPSHOT_CLAIMS = """
MATCH (p:Person)-[:HAS_FACT]->(cl:Claim {session_id: $sid})
RETURN elementId(cl) AS element_id, labels(cl) AS labels, properties(cl) AS node,
       [(cl)<-[r]-(o) | {type: type(r), direction: 'in', properties: properties(r),
                         other_element_id: elementId(o), other_labels: labels(o),
                         other_key: coalesce(o.name, o.turn_id, o.user_id)}] AS rels_in,
       [] AS rels_out
"""

_DELETE_CLAIMS = """
MATCH (:Person)-[:HAS_FACT]->(cl:Claim {session_id: $sid})
DETACH DELETE cl
"""

# Deletion mirrors the snapshot predicate exactly. If the two ever diverge, the
# run deletes something it did not record an undo for — so they are asserted
# equal by the delete-scope test rather than left to review.
_DELETE_ENTITIES = """
MATCH (e:Entity)
WHERE e.originating_session_id = $sid
  AND NOT EXISTS {
      MATCH (t:Turn)-[:DISCUSSES]->(e)
      WHERE coalesce(t.session_id, '') <> $sid
        AND coalesce(t.originating_session_id, '') <> $sid
  }
DETACH DELETE e
"""

_DELETE_TURNS = """
MATCH (t:Turn) WHERE t.session_id = $sid OR t.originating_session_id = $sid
DETACH DELETE t
"""


async def _count(session, statement: str, key: str, **params: object) -> int:  # type: ignore[no-untyped-def]
    """Run a single-row counting query and return the count.

    Args:
        session: An open Neo4j async session.
        statement: A Cypher statement returning one row with ``key``.
        key: The returned column name.
        **params: Query parameters.

    Returns:
        The count, or 0 when the query returned no row.
    """
    result = await session.run(statement, **params)
    row = await result.single()
    return int(row[key]) if row else 0


async def cleanup_probe_session(
    driver: AsyncDriver,
    session_id: str,
    *,
    user_id: str,
    snapshot_path: pathlib.Path,
    trace_ids: Sequence[str] = (),
    dry_run: bool = True,
    restore_superseded: bool = False,
) -> CleanupResult:
    """Remove the probe session's graph footprint, snapshotting first.

    Deletes ``:Entity`` nodes whose *only* provenance is this session and the
    session's ``:Turn`` nodes. Entities that pre-existed keep their original
    ``originating_session_id`` and are left alone — they are counted as residue
    instead, in three classes cleanup cannot roll back:

    * ``mention_count`` / ``last_seen`` bumps on every entity the run mentioned;
    * an empty description the run *filled* — FRE-711's fill arm carries no
      eval-mode guard, and FRE-1115 measured 18.7% of the corpus as
      empty-description, so this is the class most likely to be non-zero;
    * a description the run *overwrote* — recoverable, since FRE-711 archives the
      prior text to an ``:EntityDescriptionVersion``, but not restored here.

    A pre-existing description cannot be silently *corrected* by this run:
    FRE-711's correction arm excludes an eval-mode description overwriting a
    non-eval one, and the runner fires every probe on ``channel="EVAL"``.

    Args:
        driver: An open Neo4j async driver.
        session_id: The probe session's id.
        user_id: The owner the session must belong to. Checked before any
            deletion; a session whose turns are not all this owner's is refused.
        snapshot_path: Where to write the durable pre-deletion snapshot. Written,
            flushed and fsynced before any mutation, so a crash mid-cleanup
            leaves a complete undo record on disk.
        trace_ids: The run's trace ids. Required for a real delete — they bind
            the session to this run. Also drives the description-rewrite count.
        dry_run: When True (the default), snapshot and count but delete nothing.
        restore_superseded: Whether to restore pre-existing owner claims the run
            invalidated. **Defaults to False, deliberately.** The restore is an
            inference — it assumes every claim pointing at one of the run's
            claims was current immediately before the run — and that assumption
            cannot be proven from the graph after the fact. ``assert_claim``
            selects supersession candidates and mutates them in separate
            transactions with no compare-and-set, so a real owner assertion
            racing a probe turn could leave a predecessor whose resurrection
            would itself be corruption. Off by default, the run *reports* what
            it invalidated and the owner decides; on, it repairs the common
            uncontended case. Either way the pre-restore state is snapshotted.

    Returns:
        What was removed, and the residue that could not be.

    Raises:
        CleanupRefused: If a real delete is requested for a session that cannot
            be proven to belong to this run and this owner.
    """
    async with driver.session() as session:
        # Prove the session belongs to this run BEFORE anything destructive. A
        # stale or hand-edited run artifact naming a real production session
        # would otherwise have its turns and entities deleted.
        if not dry_run:
            if not trace_ids:
                raise CleanupRefused(
                    "refusing to delete: no trace ids were supplied, so the session "
                    "cannot be bound to this run"
                )
            result = await session.run(
                _VERIFY_SESSION_BINDING,
                sid=session_id,
                user_id=user_id,
                trace_ids=list(trace_ids),
            )
            binding = await result.single()
            turns = int(binding["turns"]) if binding else 0
            owned = int(binding["owned"]) if binding else 0
            matching = int(binding["matching_traces"]) if binding else 0
            # Every turn must be this owner's AND carry a trace id the run
            # recorded. "at least one matches" would have let a session that
            # merely overlaps the run be deleted wholesale (Codex round 2).
            if turns == 0 or owned != turns or matching != turns:
                raise CleanupRefused(
                    f"refusing to delete session {session_id}: {turns} turn(s), "
                    f"{owned} owned by {user_id}, {matching} carrying a recorded "
                    "trace id — every turn must be both, or the session is not "
                    "provably this run's alone"
                )

        # Claim ids first: the superseded-claim snapshot keys off them, and it
        # has to be captured while the run's claims still exist.
        result = await session.run(_RUN_CLAIM_IDS, sid=session_id)
        claim_id_row = await result.single()
        run_claim_ids = list(claim_id_row["claim_ids"]) if claim_id_row else []

        snapshots: list[dict[str, object]] = []
        for label, statement in (
            ("Turn", _SNAPSHOT_TURNS),
            ("Entity", _SNAPSHOT_ENTITIES),
            ("Claim", _SNAPSHOT_CLAIMS),
            ("Session", _SNAPSHOT_SESSION),
        ):
            result = await session.run(statement, sid=session_id)
            for record in await result.data():
                snapshots.append(
                    {
                        "label": label,
                        "element_id": record["element_id"],
                        "labels": record["labels"],
                        "properties": record["node"],
                        "relationships": list(record["rels_out"]) + list(record["rels_in"]),
                    }
                )

        # Collected BEFORE the file is written. An earlier draft appended these
        # after the fsync and never rewrote the file, so the durable snapshot
        # held zero SupersededClaim records — the undo log for the one operation
        # that mutates pre-existing owner data was empty (Codex round 3).
        if run_claim_ids:
            result = await session.run(_SUPERSEDED_BY_RUN, claim_ids=run_claim_ids)
            for record in await result.data():
                snapshots.append(
                    {
                        "label": "SupersededClaim",
                        "element_id": record["element_id"],
                        "labels": record["labels"],
                        "properties": record["node"],
                        "relationships": [],
                    }
                )

        # Durable before destructive: flush() alone only reaches the OS buffer,
        # so a host crash could lose the undo record while the delete survived.
        # The containing directory is fsynced too, or the file's own directory
        # entry may not survive the same crash.
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        with snapshot_path.open("w", encoding="utf-8") as handle:
            for entry in snapshots:
                handle.write(json.dumps(entry, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        dir_fd = os.open(snapshot_path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        mutated = await _count(session, _MUTATED_ENTITIES, "mutated", sid=session_id)
        filled = await _count(session, _FILLED_DESCRIPTIONS, "filled", sid=session_id)
        rewritten = (
            await _count(session, _REWRITTEN_DESCRIPTIONS, "rewritten", trace_ids=list(trace_ids))
            if trace_ids
            else 0
        )

        result = await session.run(_ADOPTED_ENTITIES, sid=session_id)
        adopted = tuple(str(r["name"]) for r in await result.data())

        turn_count = sum(1 for s in snapshots if s["label"] == "Turn")
        entity_count = sum(1 for s in snapshots if s["label"] == "Entity")
        claim_count = sum(1 for s in snapshots if s["label"] == "Claim")
        superseded = sum(1 for s in snapshots if s["label"] == "SupersededClaim")

        restored = 0
        if not dry_run:
            for statement in (_DELETE_CLAIMS, _DELETE_ENTITIES, _DELETE_TURNS, _DELETE_SESSION):
                result = await session.run(statement, sid=session_id)
                await result.consume()

            # Delete FIRST, restore after. Restoring first opened a window where
            # a crash left the restored predecessor AND the probe's claim both
            # current, violating the one-current-claim invariant — strictly
            # worse than the pre-cleanup state. Deleting first cannot lose the
            # pointers, because run_claim_ids is already in memory; a crash
            # after the delete simply leaves the predecessor invalidated, which
            # is the state as if restore had never been attempted (Codex round 3).
            if run_claim_ids and restore_superseded:
                restored = await _count(
                    session, _RESTORE_SUPERSEDED, "restored", claim_ids=run_claim_ids
                )

    return CleanupResult(
        session_id=session_id,
        dry_run=dry_run,
        turns_removed=turn_count,
        entities_removed=entity_count,
        claims_removed=claim_count,
        claims_superseded=superseded,
        claims_restored=restored,
        mutated_entities=mutated,
        descriptions_filled=filled,
        descriptions_rewritten=rewritten,
        adopted_entities_retained=adopted,
        snapshot_path=snapshot_path,
    )
