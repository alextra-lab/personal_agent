"""Measure the entity-match path's live population (FRE-1480 AC-1, ADR-0148 D4).

ADR-0148 D4 leaves FRE-1480 one decision -- *which* relevance value the entity-match path
acquires -- and AC-1 requires that decision to be made against a number rather than a
preference. This script is that number.

**What it reads.** ``agent-captains-captures-*``, field ``recall_admission`` (the ADR-0125
evidence record). Every capture lists each candidate the recall layer produced, its kind,
its score and whether it was admitted. Read-only: no substrate is written, and nothing here
touches Neo4j, Postgres or the gateway.

**How a turn is attributed to a path.** ``recall_admission`` does not name the producing
path, so the path is read off the item shape. Every discriminator below is derived from the
producing code, not guessed:

* **proactive** -- ``context.py`` scores every admitted item through the sibling map built
  from ``suggestions.candidates``, so no admitted item is unscored.
* **entity match** -- ``MemoryService._multipath_query_memory`` caps
  ``conversations + entities`` at ``query.limit`` (5 on this path), scores turns by fused
  rank ``(total - position) / total``, and never scores entities at all.
* **broad recall** -- ``context.py::_format_broad_recall_context`` emits ``entity`` and
  ``session`` items only, never ``episode``, and runs at ``limit=20``. The item budget, not
  the score, is what separates it from the entity-match path: before FRE-1479 its entities
  were unscored too.
* **reached, admitted nothing** -- nothing was admitted, but the proactive path reported
  drops. Proactive therefore ran and returned no candidates, which is exactly the
  fall-through condition into the entity-match path (``context.py``). The path ran; it
  admitted nothing.

``stance`` and ``behavioural_stance`` items are excluded throughout. They are enrichment and
standing injection, not recall admission (ADR-0148 D2, D4).

**Two limits on the result, both stated rather than left to be discovered.** Captures exist
only for completed turns, and Elasticsearch loses events episodically (FRE-1051), so the
absolute turn counts are a lower bound. The ratios and the per-item findings are read off
the item records themselves and do not depend on the corpus being complete.

Run::

    uv run python -m scripts.eval.fre1480_path_population.measure
    uv run python -m scripts.eval.fre1480_path_population.measure --window now-7d --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

#: The live capture corpus, READ-ONLY. AC-1 asks how often this path is reached in live
#: traffic, so the live record is the only corpus that can answer it -- the test stack holds
#: no production turns. Every call below is a ``_search``; nothing here writes, and the
#: script has no write path at all. Overridable so no deployment detail is hard-coded.
DEFAULT_ES_URL = (
    "http://localhost:9200"  # fre-375-allow: READ-ONLY capture read (AC-1); never writes
)
DEFAULT_INDEX = "agent-captains-captures-*"
DEFAULT_WINDOW = "now-30d"

ENTITY_MATCH_LIMIT = 5
"""``query.limit`` the entity-match path passes into ``MemoryRecallQuery``.

``_multipath_query_memory`` breaks its resolution walk once
``len(conversations) + len(entities) >= query.limit``, so the path can never admit more
than this many items in one turn. A turn admitting more came from somewhere else.
"""

RECALL_KINDS = frozenset({"entity", "episode", "session"})
"""Item kinds the recall layer admits. Stance kinds are enrichment and are excluded."""

PATH_PROACTIVE = "proactive"
PATH_ENTITY_MATCH = "entity_match"
PATH_BROAD = "broad_recall"
PATH_REACHED_NO_ADMIT = "entity_match_reached_no_admission"
PATH_NO_RECALL = "no_recall_reported"


@dataclass(frozen=True)
class TurnClassification:
    """One capture, attributed to the path that produced its admission.

    Attributes:
        path: One of the ``PATH_*`` constants.
        admitted: The recall-layer items the turn admitted.
        timestamp: The capture's ISO timestamp.
        user_message: The turn's user message, for spot-checking an attribution by hand.
    """

    path: str
    admitted: tuple[dict[str, Any], ...]
    timestamp: str
    user_message: str


@dataclass
class PathSummary:
    """Aggregates for one path over the window.

    Attributes:
        turns: How many turns this path produced.
        items_per_turn: Admitted item count for each of those turns.
        kinds: Admitted items by kind.
        unscored_by_kind: Admitted items carrying no score, by kind.
    """

    turns: int = 0
    items_per_turn: list[int] = field(default_factory=list)
    kinds: Counter[str] = field(default_factory=Counter)
    unscored_by_kind: Counter[str] = field(default_factory=Counter)


def _search(es_url: str, index: str, window: str, size: int) -> list[dict[str, Any]]:
    """Fetch capture documents carrying a ``recall_admission`` record.

    Args:
        es_url: Elasticsearch base URL.
        index: Index pattern to search.
        window: Elasticsearch date-math lower bound, e.g. ``now-30d``.
        size: Maximum documents to return.

    Returns:
        The ``_source`` of each matching document, oldest first.

    Raises:
        RuntimeError: If Elasticsearch is unreachable or answers with an error. A failed
            read is never returned as an empty population -- a clean zero and an
            unreachable substrate are different facts (FRE-1051).
    """
    body = {
        "query": {
            "bool": {
                "filter": [
                    {"exists": {"field": "recall_admission"}},
                    {"range": {"timestamp": {"gte": window}}},
                ]
            }
        },
        "size": size,
        "sort": [{"timestamp": "asc"}],
        "_source": {"includes": ["trace_id", "timestamp", "user_message", "recall_admission"]},
    }
    request = urllib.request.Request(  # noqa: S310 -- fixed localhost scheme
        f"{es_url}/{index}/_search",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.load(response)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"capture read failed against {es_url}: {exc}") from exc
    if "hits" not in payload:
        raise RuntimeError(f"unexpected search response from {es_url}: {payload}")
    return [hit["_source"] for hit in payload["hits"]["hits"]]


def classify(admission: dict[str, Any]) -> tuple[str, tuple[dict[str, Any], ...]]:
    """Attribute one capture's admission record to the path that produced it.

    Args:
        admission: The capture's ``recall_admission`` record.

    Returns:
        Tuple of (path constant, the admitted recall-layer items).
    """
    items = [i for i in (admission.get("items") or []) if i.get("kind") in RECALL_KINDS]
    admitted = tuple(i for i in items if i.get("admitted"))
    dropped = [i for i in items if not i.get("admitted")]
    if not admitted:
        return (PATH_REACHED_NO_ADMIT if dropped else PATH_NO_RECALL), admitted
    if any(i["kind"] == "session" for i in admitted) or len(admitted) > ENTITY_MATCH_LIMIT:
        return PATH_BROAD, admitted
    if any(i["kind"] == "entity" and i.get("score") is None for i in admitted):
        return PATH_ENTITY_MATCH, admitted
    return PATH_PROACTIVE, admitted


def classify_all(documents: Sequence[dict[str, Any]]) -> list[TurnClassification]:
    """Classify every capture in the window.

    Args:
        documents: Capture ``_source`` dicts.

    Returns:
        One classification per document, in the order given.
    """
    results: list[TurnClassification] = []
    for doc in documents:
        path, admitted = classify(doc.get("recall_admission") or {})
        results.append(
            TurnClassification(
                path=path,
                admitted=admitted,
                timestamp=str(doc.get("timestamp", "")),
                user_message=str(doc.get("user_message", "")),
            )
        )
    return results


def summarize(classifications: Sequence[TurnClassification]) -> dict[str, PathSummary]:
    """Aggregate the classifications per path.

    Args:
        classifications: One entry per capture.

    Returns:
        A summary per path constant that occurred.
    """
    summaries: dict[str, PathSummary] = {}
    for turn in classifications:
        summary = summaries.setdefault(turn.path, PathSummary())
        summary.turns += 1
        summary.items_per_turn.append(len(turn.admitted))
        for item in turn.admitted:
            summary.kinds[item["kind"]] += 1
            if item.get("score") is None:
                summary.unscored_by_kind[item["kind"]] += 1
    return summaries


def report(classifications: Sequence[TurnClassification]) -> dict[str, Any]:
    """Build the AC-1 report.

    Args:
        classifications: One entry per capture.

    Returns:
        The report as a JSON-serialisable mapping.

    Raises:
        ValueError: If the window held no captures. An empty window cannot answer AC-1,
            and reporting zeroes for it would read as a measured population of zero.
    """
    if not classifications:
        raise ValueError("no captures in the window -- AC-1 cannot be answered from this read")
    summaries = summarize(classifications)
    total = len(classifications)
    entity_match = summaries.get(PATH_ENTITY_MATCH, PathSummary())
    reached_no_admit = summaries.get(PATH_REACHED_NO_ADMIT, PathSummary())
    reached = entity_match.turns + reached_no_admit.turns
    per_turn = entity_match.items_per_turn

    return {
        "window": {
            "first_capture": classifications[0].timestamp,
            "last_capture": classifications[-1].timestamp,
            "captures_with_recall_admission": total,
        },
        "entity_match_path": {
            "reached_turns": reached,
            "reached_share": round(reached / total, 4),
            "admitting_turns": entity_match.turns,
            "admitting_share": round(entity_match.turns / total, 4),
            "items_admitted": sum(per_turn),
            "items_by_kind": dict(entity_match.kinds),
            "unscored_items_by_kind": dict(entity_match.unscored_by_kind),
            "items_per_admitting_turn": {
                "mean": round(statistics.fmean(per_turn), 2) if per_turn else 0.0,
                "min": min(per_turn) if per_turn else 0,
                "max": max(per_turn) if per_turn else 0,
            },
        },
        "all_paths": {
            path: {
                "turns": s.turns,
                "share": round(s.turns / total, 4),
                "items_by_kind": dict(s.kinds),
            }
            for path, s in sorted(summaries.items(), key=lambda kv: -kv[1].turns)
        },
    }


def _render(result: dict[str, Any]) -> str:
    """Render the report as plain text.

    Args:
        result: The report mapping.

    Returns:
        The rendered report.
    """
    window = result["window"]
    path = result["entity_match_path"]
    lines = [
        f"window: {window['first_capture'][:10]} .. {window['last_capture'][:10]}",
        f"captures with recall_admission: {window['captures_with_recall_admission']}",
        "",
        "paths:",
    ]
    for name, summary in result["all_paths"].items():
        lines.append(
            f"  {name:34s} {summary['turns']:4d} turns "
            f"({100 * summary['share']:5.1f}%)  items={summary['items_by_kind']}"
        )
    per_turn = path["items_per_admitting_turn"]
    lines += [
        "",
        "entity-match path (AC-1):",
        f"  reached   {path['reached_turns']:4d} turns ({100 * path['reached_share']:.1f}%)",
        f"  admitting {path['admitting_turns']:4d} turns ({100 * path['admitting_share']:.1f}%)",
        f"  items admitted: {path['items_admitted']}  by kind {path['items_by_kind']}",
        f"  carrying NO relevance value: {path['unscored_items_by_kind']}",
        f"  per admitting turn: mean={per_turn['mean']} min={per_turn['min']} "
        f"max={per_turn['max']}",
    ]
    return "\n".join(lines)


def main() -> int:
    """Run the measurement.

    Returns:
        Process exit code: 0 on success, 1 when the read or the window fails.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--es-url", default=DEFAULT_ES_URL, help="Elasticsearch base URL.")
    parser.add_argument("--index", default=DEFAULT_INDEX, help="Capture index pattern.")
    parser.add_argument("--window", default=DEFAULT_WINDOW, help="Date-math lower bound.")
    parser.add_argument("--size", type=int, default=1000, help="Maximum captures to read.")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    args = parser.parse_args()

    try:
        documents = _search(args.es_url, args.index, args.window, args.size)
        result = report(classify_all(documents))
    except (RuntimeError, ValueError) as exc:
        parser.exit(status=1, message=f"{exc}\n")

    # An eval script's stdout is its deliverable, so this is the one place `print` is the
    # right call rather than structlog (`scripts/eval/` convention).
    print(json.dumps(result, indent=2) if args.json else _render(result))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
