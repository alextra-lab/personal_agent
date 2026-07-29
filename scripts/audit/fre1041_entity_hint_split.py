#!/usr/bin/env python3
"""FRE-1041 — entity-hint source split census, and the replacement's before/after.

Read-only audit tool backing the FRE-1041 measurement. The ticket asks one question
before any fix may be chosen: on the live proactive path, how much of the entity signal
comes from the capitalisation heuristic in ``request_gateway/context.py`` versus from the
database-derived session entities the protocol adapter merges it with?

The census replays real user turns from the capture store (``agent-captains-captures-*``)
and, per turn, reconstructs both sources the live merge in
``protocol_adapter.suggest_relevant`` unions, plus the proposed replacement:

  * **H** — the capitalisation heuristic. Frozen locally (see
    :func:`capitalized_entity_hints_frozen`) so this "before" arm keeps measuring the
    same thing after the shipped function is replaced.
  * **D** — ``fetch_session_discussed_entity_names``: Entity names DISCUSSES-linked to
    turns of the same session. Reconstructed *point-in-time* (only turns strictly
    earlier than the measured turn — later turns do not exist at request time) and under
    the **same visibility filter** the live query applies, using each capture's own
    ``user_id``. Still an upper bound in one respect: entity extraction runs after a turn
    completes, so some earlier turns may not have been extracted yet at request time.
  * **R** — the graph-anchored resolver: retrieve entity hits from the *existing*
    ``turn_entity_fulltext`` index, then keep only those whose name occurs as a
    contiguous token run in the message ("lexical retrieve, literal verify").

"Usable" is judged with the same semantics ``_overlap_subscore`` uses — an **exact,
case-sensitive** match against a name the graph holds — because a hint that cannot
appear in that intersection contributes exactly nothing to the live score.

This is a plain read-only census — ES ``_search`` scrolls and Neo4j reads. Nothing is
written, no LLM is invoked, no live gateway turn is fired.

Usage::

    python scripts/audit/fre1041_entity_hint_split.py --since 2026-07-20
    python scripts/audit/fre1041_entity_hint_split.py --since 2026-07-20 --json-out out.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from neo4j import GraphDatabase

from personal_agent.config import settings
from personal_agent.memory.entity_mentions import tokenize, verify_mentions
from personal_agent.memory.service import (
    MESSAGE_ENTITY_HINT_LIMIT,
    _escape_lucene_query,
)

SCROLL_PAGE = 500
SCROLL_TTL = "2m"
FULLTEXT_TOP_K = 50


def capitalized_entity_hints_frozen(user_message: str) -> list[str]:
    """The pre-FRE-1041 capitalisation heuristic, frozen for the "before" arm.

    A verbatim copy of ``request_gateway/context.py:_capitalized_entity_hints`` as it
    stood before FRE-1041 replaced it. Frozen deliberately, exactly as
    ``scripts/study/baseline_harness.py`` freezes it: a census whose baseline arm moves
    when the shipped code moves cannot show a before/after at all.

    Args:
        user_message: The verbatim user message.

    Returns:
        Capitalised words longer than three characters, capped at ten.
    """
    words = user_message.split()
    return [w.strip('",.:;!?') for w in words if len(w) > 3 and w[0].isupper()][:10]


@dataclass(frozen=True)
class TurnHints:
    """One real turn's reconstructed entity-hint sources.

    Attributes:
        trace_id: The turn's trace id.
        timestamp: ISO-8601 capture time.
        session_id: Session the turn belongs to.
        user_message: The verbatim user message.
        heuristic: Names the capitalisation heuristic produced.
        db_entities: Names the point-in-time, visibility-scoped session query returns.
        resolved: Names the graph-anchored resolver produces.
        graph_names: Exact graph entity names, for usability classification.
    """

    trace_id: str
    timestamp: str
    session_id: str
    user_message: str
    heuristic: frozenset[str]
    db_entities: frozenset[str]
    resolved: frozenset[str]
    graph_names: frozenset[str]

    @property
    def usable_heuristic(self) -> frozenset[str]:
        """Heuristic names that exactly match a graph entity name.

        Exact and case-sensitive, matching ``_overlap_subscore``'s set intersection: a
        name that cannot land in that intersection contributes nothing to the live score.
        """
        return frozenset(h for h in self.heuristic if h in self.graph_names)

    @property
    def inert_heuristic(self) -> frozenset[str]:
        """Heuristic names that match no entity — they cannot score."""
        return self.heuristic - self.usable_heuristic

    @property
    def unique_usable_heuristic(self) -> frozenset[str]:
        """Usable heuristic names the database source did not already supply."""
        return self.usable_heuristic - self.db_entities

    @property
    def merged_today(self) -> frozenset[str]:
        """The union the live adapter forms today."""
        return self.heuristic | self.db_entities

    @property
    def merged_after(self) -> frozenset[str]:
        """The union the adapter would form with the resolver in place."""
        return self.resolved | self.db_entities


class ElasticsearchReader:
    """Minimal read-only scrolling reader.

    Deliberately not the project's async ES client: this is an offline audit tool that
    must run against any reachable cluster without importing the service's async stack.
    """

    def __init__(self, base_url: str) -> None:
        """Store the cluster base URL.

        Args:
            base_url: Cluster root, e.g. ``http://localhost:9200``.
        """
        self._base_url = base_url.rstrip("/")

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            parsed: dict[str, Any] = json.load(response)
        return parsed

    def scroll(
        self, index: str, query: dict[str, Any], source: Sequence[str]
    ) -> Iterator[dict[str, Any]]:
        """Yield every ``_source`` matching *query*, paging via the scroll API.

        Args:
            index: Index pattern, e.g. ``agent-captains-captures-*``.
            query: Elasticsearch query DSL fragment.
            source: Fields to return.

        Yields:
            One ``_source`` mapping per matching document.
        """
        page = self._post(
            f"/{index}/_search?scroll={SCROLL_TTL}",
            {"size": SCROLL_PAGE, "_source": source, "query": query, "sort": [{"_doc": "asc"}]},
        )
        scroll_id = page.get("_scroll_id")
        hits = page["hits"]["hits"]
        while hits:
            for hit in hits:
                yield hit["_source"]
            page = self._post("/_search/scroll", {"scroll": SCROLL_TTL, "scroll_id": scroll_id})
            scroll_id = page.get("_scroll_id")
            hits = page["hits"]["hits"]


def _visibility_fragment(alias: str) -> str:
    """Return the same visibility WHERE fragment the memory service applies (FRE-229).

    Replicated rather than imported because the service's helper is async-stack-bound;
    it is kept character-identical to ``memory/service.py:_build_visibility_filter`` so
    the census reconstructs what the live query actually returned.

    Args:
        alias: Cypher node alias to qualify.

    Returns:
        A Cypher boolean fragment referencing ``$vis_authenticated`` / ``$vis_user_id``.
    """
    return (
        f"({alias}.visibility IS NULL "
        f"OR {alias}.visibility = 'public' "
        f"OR ({alias}.visibility = 'group' AND $vis_authenticated = true) "
        f"OR {alias}.visibility = 'private:' + $vis_user_id)"
    )


class GraphReader:
    """Read-only Neo4j access for entity names, session history and lexical retrieval."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        """Open a driver against the live knowledge graph.

        Args:
            uri: Bolt URI.
            user: Neo4j username.
            password: Neo4j password.
        """
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self.fulltext_ms: list[float] = []

    def close(self) -> None:
        """Release the driver."""
        self._driver.close()

    def entity_names(self) -> frozenset[str]:
        """Return every Entity name in the graph, exactly as stored.

        Returns:
            The graph's entity names.
        """
        with self._driver.session() as session:
            return frozenset(
                str(record["n"])
                for record in session.run("MATCH (e:Entity) RETURN e.name AS n")
                if record["n"]
            )

    def session_entity_timeline(self, session_id: str, user_id: str) -> list[tuple[str, str]]:
        """Return (turn timestamp, entity name) pairs for one session, visibility-scoped.

        Args:
            session_id: Session identifier.
            user_id: The capture's user id, driving the visibility filter.

        Returns:
            Pairs the caller filters by timestamp to reconstruct the point-in-time set.
        """
        with self._driver.session() as session:
            result = session.run(
                f"""
                MATCH (t:Turn {{session_id: $session_id}})-[:DISCUSSES]->(e:Entity)
                WHERE {_visibility_fragment("t")}
                RETURN t.timestamp AS ts, e.name AS name
                """,
                session_id=session_id,
                vis_authenticated=bool(user_id),
                vis_user_id=user_id,
            )
            return [
                (str(record["ts"] or ""), str(record["name"]))
                for record in result
                if record["name"]
            ]

    def resolve(self, message: str, user_id: str) -> frozenset[str]:
        """Run the resolver: lexical retrieve, then literal verify.

        The retrieval query and the verification step are the ones
        ``MemoryService.resolve_message_entity_names`` ships — the verifier is imported
        rather than reimplemented, so this "after" arm measures the deployed behaviour
        and cannot drift from it. Only the async driver differs.

        Args:
            message: The verbatim user message.
            user_id: The capture's user id, driving the visibility filter.

        Returns:
            Graph-canonical entity names the message literally mentions.
        """
        with self._driver.session() as session:
            started = time.perf_counter()
            result = session.run(
                f"""
                CALL db.index.fulltext.queryNodes('turn_entity_fulltext', $query_text)
                YIELD node, score
                WITH node, score WHERE node:Entity AND {_visibility_fragment("node")}
                RETURN node.name AS name ORDER BY score DESC LIMIT $top_k
                """,
                query_text=_escape_lucene_query(message),
                top_k=FULLTEXT_TOP_K,
                vis_authenticated=bool(user_id),
                vis_user_id=user_id,
            )
            names = [str(record["name"]) for record in result if record["name"]]
            self.fulltext_ms.append((time.perf_counter() - started) * 1000.0)
        return frozenset(verify_mentions(message, names)[:MESSAGE_ENTITY_HINT_LIMIT])


def collect_turns(
    reader: ElasticsearchReader, graph: GraphReader, since: str | None, limit: int | None
) -> list[TurnHints]:
    """Replay real turns and reconstruct every entity-hint source for each.

    Args:
        reader: Capture-store reader.
        graph: Knowledge-graph reader.
        since: Optional inclusive ISO date lower bound on the capture timestamp.
        limit: Optional cap on the most recent turns to keep.

    Returns:
        One record per replayed turn, oldest first.
    """
    query: dict[str, Any] = (
        {
            "bool": {
                "must": [{"exists": {"field": "user_message"}}],
                "filter": [{"range": {"timestamp": {"gte": since}}}],
            }
        }
        if since
        else {"exists": {"field": "user_message"}}
    )
    raw = [
        src
        for src in reader.scroll(
            "agent-captains-captures-*",
            query,
            ["timestamp", "trace_id", "session_id", "user_message", "user_id"],
        )
        if str(src.get("user_message") or "").strip()
    ]
    raw.sort(key=lambda s: str(s.get("timestamp") or ""))
    if limit is not None:
        raw = raw[-limit:]

    graph_names = graph.entity_names()
    timelines: dict[tuple[str, str], list[tuple[str, str]]] = {}
    turns: list[TurnHints] = []
    for src in raw:
        message = str(src["user_message"])
        timestamp = str(src.get("timestamp") or "")
        session_id = str(src.get("session_id") or "")
        user_id = str(src.get("user_id") or "")

        key = (session_id, user_id)
        if key not in timelines:
            timelines[key] = (
                graph.session_entity_timeline(session_id, user_id) if session_id else []
            )

        turns.append(
            TurnHints(
                trace_id=str(src.get("trace_id") or ""),
                timestamp=timestamp,
                session_id=session_id,
                user_message=message,
                heuristic=frozenset(
                    h for h in capitalized_entity_hints_frozen(message) if h.strip()
                ),
                db_entities=frozenset(name for ts, name in timelines[key] if ts and ts < timestamp),
                resolved=graph.resolve(message, user_id),
                graph_names=graph_names,
            )
        )
    return turns


def report(
    turns: Sequence[TurnHints], since: str | None, fulltext_ms: Sequence[float]
) -> dict[str, object]:
    """Print the source-split and before/after decision tables.

    Args:
        turns: Every replayed turn.
        since: The lower bound applied, for the header line.
        fulltext_ms: Per-turn resolver query latencies.

    Returns:
        The same figures as a mapping, for JSON consumers.
    """
    scope = f"since {since}" if since else "all time"
    print(f"\n=== FRE-1041 entity-hint sources, {scope} ({len(turns)} real turns) ===")
    if not turns:
        print("no turns found")
        return {"turns": 0}

    total = len(turns)
    pct = 100.0 / total
    figures: dict[str, object] = {"turns": total, "scope": scope}

    def table(title: str, rows: Sequence[tuple[str, Any]]) -> None:
        print(f"\n-- {title} --")
        print(f"{'':<60}{'turns':>7}{'rate':>8}")
        for label, predicate in rows:
            count = sum(1 for t in turns if predicate(t))
            print(f"{label:<60}{count:>7}{count * pct:>7.1f}%")
            figures[label] = {"turns": count, "rate": round(count / total, 4)}

    table(
        "the split the ticket asked for (today's live merge)",
        [
            ("heuristic H empty", lambda t: not t.heuristic),
            (
                "heuristic H non-empty but entirely inert",
                lambda t: bool(t.heuristic) and not t.usable_heuristic,
            ),
            ("heuristic H has >=1 usable name", lambda t: bool(t.usable_heuristic)),
            ("database D empty (point-in-time, visibility-scoped)", lambda t: not t.db_entities),
            ("merged H|D empty — no entity signal at all", lambda t: not t.merged_today),
            (
                "H contributes >=1 usable name D did not have",
                lambda t: bool(t.unique_usable_heuristic),
            ),
            (
                "D supplies the entire usable signal",
                lambda t: bool(t.db_entities) and not t.unique_usable_heuristic,
            ),
        ],
    )

    table(
        "before/after: turns yielding no usable hint from the message",
        [
            ("BEFORE — no usable name from the heuristic", lambda t: not t.usable_heuristic),
            ("AFTER  — no name from the graph-anchored resolver", lambda t: not t.resolved),
            (
                "resolver finds >=1 name the heuristic missed",
                lambda t: bool(t.resolved - t.usable_heuristic),
            ),
            (
                "resolver LOSES a usable name the heuristic had",
                lambda t: bool(t.usable_heuristic - t.resolved),
            ),
        ],
    )

    stopword_class = {
        "What",
        "Only",
        "Which",
        "Provide",
        "Include",
        "Good",
        "Please",
        "This",
        "Build",
        "Does",
        "Can",
        "How",
        "Why",
        "The",
        "Keep",
        "Mais",
        "Breakdown",
    }
    table(
        "the stopword guard (ticket's AC-3)",
        [
            (
                "BEFORE — a sentence-initial stopword reaches recall",
                lambda t: bool(t.heuristic & stopword_class),
            ),
            (
                "AFTER  — a sentence-initial stopword reaches recall",
                lambda t: bool(t.resolved & stopword_class),
            ),
        ],
    )

    inert = Counter(name for t in turns for name in t.inert_heuristic)
    print(f"\nmost frequent INERT heuristic hints: {inert.most_common(10)}")
    figures["top_inert_hints"] = inert.most_common(10)

    gained = Counter(name for t in turns for name in (t.resolved - t.usable_heuristic))
    print(f"most frequent names the resolver RECOVERS: {gained.most_common(10)}")
    figures["top_recovered"] = gained.most_common(10)

    if fulltext_ms:
        ordered = sorted(fulltext_ms)
        p50 = ordered[len(ordered) // 2]
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(
            f"\nresolver full-text query latency: p50 {p50:.1f} ms · p95 {p95:.1f} ms · max {ordered[-1]:.1f} ms"
        )
        figures["resolver_latency_ms"] = {
            "p50": round(p50, 1),
            "p95": round(p95, 1),
            "max": round(ordered[-1], 1),
        }

    melon = [t for t in turns if "melon" in t.user_message.lower()]
    if melon:
        print(f"\n--- the decisive case: {len(melon)} turn(s) mentioning melon ---")
        for t in melon:
            print(f"  {t.timestamp[:19]}  {t.user_message[:70]!r}")
            print(f"     BEFORE H={sorted(t.heuristic)} usable={sorted(t.usable_heuristic)}")
            print(f"     AFTER  R={sorted(t.resolved)}")
        figures["melon_turns"] = [
            {
                "timestamp": t.timestamp,
                "user_message": t.user_message,
                "heuristic": sorted(t.heuristic),
                "usable_heuristic": sorted(t.usable_heuristic),
                "resolved": sorted(t.resolved),
            }
            for t in melon
        ]
    return figures


def main() -> int:
    """Run the census and print the decision tables.

    Returns:
        Process exit code — ``0`` on success, ``1`` when a substrate is unreachable.
    """
    parser = argparse.ArgumentParser(description="FRE-1041 entity-hint source split census")
    parser.add_argument(
        "--es-url", default=settings.elasticsearch_url, help="Elasticsearch base URL"
    )
    parser.add_argument("--since", default=None, help="ISO date lower bound, e.g. 2026-07-20")
    parser.add_argument("--limit", type=int, default=None, help="Keep only the N most recent turns")
    parser.add_argument("--json", action="store_true", help="also emit the figures as JSON")
    parser.add_argument("--json-out", default=None, help="write the figures to this path")
    args = parser.parse_args()

    graph = GraphReader(
        os.environ.get("AGENT_NEO4J_URI") or settings.neo4j_uri,
        os.environ.get("AGENT_NEO4J_USER") or settings.neo4j_user,
        os.environ.get("AGENT_NEO4J_PASSWORD") or settings.neo4j_password,
    )
    try:
        turns = collect_turns(ElasticsearchReader(args.es_url), graph, args.since, args.limit)
        figures = report(turns, args.since, graph.fulltext_ms)
    except (urllib.error.URLError, OSError) as exc:
        print(f"substrate unreachable: {exc}")
        return 1
    finally:
        graph.close()

    if args.json:
        print("\n" + json.dumps(figures, indent=2, default=str))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(figures, handle, indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
