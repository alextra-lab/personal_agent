#!/usr/bin/env python3
"""FRE-1021 — entity-candidate participation census.

Read-only audit tool backing the FRE-1021 measurement. ADR-0126 D2 and the FRE-1021
ticket both assert a *mechanism* — entities and turn/episode candidates compete in one
ranked, capped, fused pool, so a topic's own recent turns can displace its entities —
observed directly on two live turns (12:04 / 18:32, 2026-07-27) but not yet measured as
a rate. This script measures the rate, over every turn the ADR-0125 evidence contract
has recorded (``recall_admission`` on ``agent-captains-captures-*``), by reading the
``kind`` field of each recall candidate directly from the admitted-recall record — the
same record ADR-0126's own acceptance criteria require any consumer to check.

It reports, per turn with at least one recall candidate:
  * whether ANY candidate of kind ``entity`` was offered at all (participation), and
  * whether an ``entity``-kind candidate was ever *admitted* (reached the model).

**FRE-1060 changed what "offered" means, mid-corpus.** Before that deploy the record held
only the candidates that survived the proactive path's own caps and budgets, so this
script's participation rate was a rate over *post-selection survivors* presented as one
over recall. Records written after it name the whole offered population and declare
``recall_admission.candidate_population == "offered"``; earlier records omit the field and
are ``post_selection``. **Figures either side of that boundary are not comparable** — a
post-deploy participation rate will be legitimately higher without anything about recall
having changed. Filter on ``candidate_population`` before comparing across it. This script
deliberately does not filter: it reports whatever the window holds, and the caveat is the
reader's to apply.

Candidate order within a record is *rank order per group* — admitted items first, then the
discarded ones — not one globally ranked list. Every item carries its ``score``, so true
global rank is recovered by sorting. Nothing below depends on order.

This is a plain read-only ``_search`` scroll — nothing is written, no LLM is invoked,
no live gateway turn is fired.

Usage::

    python scripts/audit/fre1021_entity_participation_census.py
    python scripts/audit/fre1021_entity_participation_census.py --since 2026-07-20 --json
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from personal_agent.config import settings

SCROLL_PAGE = 500
SCROLL_TTL = "2m"


@dataclass(frozen=True)
class TurnRecallRecord:
    """One turn's admitted-recall record.

    Attributes:
        trace_id: The turn's trace id.
        timestamp: ISO-8601 capture time.
        candidate_kinds: ``kind`` of every offered candidate. Three groups in construction
            order — offered, producer-discarded, session facts — each internally rank
            ordered (FRE-1060); not one globally ranked list, and not admitted-first.
            Order is not relied on here.
        admitted_kinds: ``kind`` of every *admitted* candidate (reached the model).
        population: ``recall_admission.candidate_population`` — ``"offered"`` when the
            record names the whole population, ``"post_selection"`` when it names only
            survivors. Absent on every pre-FRE-1060 capture, which are survivors-only.
    """

    trace_id: str
    timestamp: str
    candidate_kinds: list[str] = field(default_factory=list)
    admitted_kinds: list[str] = field(default_factory=list)
    population: str = "post_selection"

    @property
    def entity_offered(self) -> bool:
        """Whether any candidate of kind ``entity`` was offered."""
        return "entity" in self.candidate_kinds

    @property
    def entity_admitted(self) -> bool:
        """Whether any candidate of kind ``entity`` was admitted."""
        return "entity" in self.admitted_kinds


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


def collect_recall_records(
    reader: ElasticsearchReader, since: str | None
) -> list[TurnRecallRecord]:
    """Return one :class:`TurnRecallRecord` per capture carrying a non-empty recall_admission.

    Args:
        reader: Cluster reader.
        since: Optional inclusive ISO date lower bound on the capture timestamp.

    Returns:
        Records in index order. Captures with no ``recall_admission`` (no candidates
        offered that turn) are skipped entirely — they are not zero-participation
        turns, they are turns where recall never ran.
    """
    query: dict[str, Any] = (
        {
            "bool": {
                "must": [{"exists": {"field": "recall_admission"}}],
                "filter": [{"range": {"timestamp": {"gte": since}}}],
            }
        }
        if since
        else {"exists": {"field": "recall_admission"}}
    )
    records: list[TurnRecallRecord] = []
    for src in reader.scroll(
        "agent-captains-captures-*", query, ["timestamp", "trace_id", "recall_admission"]
    ):
        admission = src.get("recall_admission") or {}
        items = admission.get("items") or []
        if not items:
            continue
        records.append(
            TurnRecallRecord(
                trace_id=str(src.get("trace_id") or ""),
                timestamp=str(src.get("timestamp") or ""),
                candidate_kinds=[str(it.get("kind") or "") for it in items],
                admitted_kinds=[str(it.get("kind") or "") for it in items if it.get("admitted")],
                population=str(admission.get("candidate_population") or "post_selection"),
            )
        )
    return records


def report(records: Sequence[TurnRecallRecord], since: str | None) -> dict[str, object]:
    """Summarise entity participation and print the decision table.

    Args:
        records: Every collected turn recall record with ≥1 candidate.
        since: The lower bound applied, for the header line.

    Returns:
        The same figures as a mapping, for ``--json`` consumers.
    """
    scope = f"since {since}" if since else "all time"
    print(
        f"\n=== entity-candidate participation, {scope} ({len(records)} turns with candidates) ==="
    )
    if not records:
        print("no turns with recall candidates found")
        return {"turns": 0}

    offered = [r for r in records if r.entity_offered]
    admitted = [r for r in records if r.entity_admitted]
    all_kinds = Counter(k for r in records for k in r.candidate_kinds)
    candidate_counts = [len(r.candidate_kinds) for r in records]

    # FRE-1060: printed, not merely docstring'd. `candidate_count` changed meaning at that
    # deploy, so a window straddling it mixes two populations and the participation rate
    # rises for that reason alone. A caveat only a code reader sees is one the operator
    # pasting this number into a ticket never does.
    populations = Counter(r.population for r in records)
    if len(populations) > 1:
        print(
            "WARNING: this window mixes two candidate populations "
            f"({dict(populations)}) — it straddles the FRE-1060 deploy. "
            "'post_selection' records name survivors only, 'offered' name the whole "
            "population, so the figures below are NOT comparable across that boundary. "
            "Re-run with --since after the deploy for a single-population view."
        )
    else:
        print(f"candidate population (uniform): {next(iter(populations))}")

    pct = 100.0 / len(records)
    print(f"kind distribution across all offered candidates: {dict(all_kinds)}")
    print(
        f"candidates per turn min/median/max: "
        f"{min(candidate_counts)} / {sorted(candidate_counts)[len(candidate_counts) // 2]} / "
        f"{max(candidate_counts)}"
    )
    print(
        f"turns with >=1 entity candidate OFFERED : {len(offered):>4} / {len(records)} ({len(offered) * pct:.1f}%)"
    )
    print(
        f"turns with >=1 entity candidate ADMITTED: {len(admitted):>4} / {len(records)} ({len(admitted) * pct:.1f}%)"
    )

    zero_entity = [r for r in records if not r.entity_offered]
    if zero_entity:
        print(f"\n{len(zero_entity)} turns offered ZERO entity candidates. First 5 trace_ids:")
        for r in zero_entity[:5]:
            print(f"  {r.timestamp[:19]}  {r.trace_id}  kinds={r.candidate_kinds}")

    return {
        "turns_with_candidates": len(records),
        "kind_distribution": dict(all_kinds),
        "entity_offered_turns": len(offered),
        "entity_offered_rate": len(offered) / len(records),
        "entity_admitted_turns": len(admitted),
        "entity_admitted_rate": len(admitted) / len(records),
        # FRE-1060: emitted so a JSON consumer can see the same caveat the printed report
        # carries. `mixed_populations` true means the rates above span the deploy boundary
        # and are not a single measurement.
        "candidate_populations": dict(populations),
        "mixed_populations": len(populations) > 1,
    }


def main() -> int:
    """Run the census and print the decision table.

    Returns:
        Process exit code — ``0`` on success, ``1`` when the cluster is unreachable.
    """
    parser = argparse.ArgumentParser(description="FRE-1021 entity-candidate participation census")
    parser.add_argument(
        "--es-url",
        default=settings.elasticsearch_url,
        help="Elasticsearch base URL (defaults to the configured cluster)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO date lower bound, e.g. 2026-07-20",
    )
    parser.add_argument("--json", action="store_true", help="also emit the figures as JSON")
    args = parser.parse_args()

    reader = ElasticsearchReader(args.es_url)
    try:
        figures = report(collect_recall_records(reader, args.since), args.since)
    except (urllib.error.URLError, OSError) as exc:
        print(f"elasticsearch unreachable at {args.es_url}: {exc}")
        return 1

    if args.json:
        print("\n" + json.dumps(figures, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
