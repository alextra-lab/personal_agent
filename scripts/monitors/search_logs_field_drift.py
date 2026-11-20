"""Report drift between ``search_logs``'s field allowlist and the live mappings (FRE-1777).

``ALLOWED_FIELDS`` in ``personal_agent.tools.telemetry_query`` is a static positive list
(ADR-0156 A5.3, "allow, never deny"). New logged fields stay unreadable until a person adds
them, and a renamed field leaves a dead name on the list. This script reports both, per
index family:

* listed fields that no index of the family maps;
* fields that hold data in the window but are not listed, top N by document count.

It never changes the list: a person reviews each name (and its values, for personal data)
before adding it. Read-only, and bounded: per family, one mapping read and one search with
``size: 0`` and one ``exists`` filter per unlisted field. It prints field names and document
counts only, never values.

Usage::

    uv run python -m scripts.monitors.search_logs_field_drift
    uv run python -m scripts.monitors.search_logs_field_drift --family agent-logs --top 40
    uv run python -m scripts.monitors.search_logs_field_drift --since 7d --es-url http://localhost:9200
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx

from personal_agent.config import settings
from personal_agent.tools.telemetry_query import ALLOWED_FIELDS, FAMILY_TIME_FIELD

_TIMEOUT_S = 60.0
_ES_TIMEOUT = "30s"
# A bound on the filters of the one aggregation. agent-logs mapped 614 fields with data in
# 7 days on 2026-11-12; past this bound the report says that it is partial.
_MAX_FILTERS = 2000
_PARAMS = {"ignore_unavailable": "true", "allow_no_indices": "true"}


@dataclass(frozen=True)
class FamilyDrift:
    """The drift of one index family.

    Attributes:
        family: The index family.
        listed_missing: Listed fields that no index of the family maps, sorted.
        unlisted_with_data: ``(field, doc count)`` of unlisted fields with data, most first.
        with_data: How many unlisted fields hold data in the window.
        unlisted_mapped: How many unlisted fields the mapping holds.
        checked: How many unlisted fields the aggregation counted (at most ``_MAX_FILTERS``).
    """

    family: str
    listed_missing: tuple[str, ...]
    unlisted_with_data: tuple[tuple[str, int], ...]
    with_data: int
    unlisted_mapped: int
    checked: int


def leaf_fields(mapping_reply: Mapping[str, object]) -> set[str]:
    """Collect the leaf field paths of every index in a ``GET <pattern>/_mapping`` reply.

    Multi-fields (``x.keyword``) are left out: the allowlist names the base field and accepts
    its ``.keyword`` form. Object fields are walked into; only fields with a ``type`` count.

    Args:
        mapping_reply: The decoded reply, index name -> ``{"mappings": {...}}``.

    Returns:
        The dotted paths of the mapped leaf fields, over all indices.
    """
    found: set[str] = set()

    def walk(properties: object, prefix: str) -> None:
        if not isinstance(properties, dict):
            return
        for name, spec in properties.items():
            if not isinstance(spec, dict):
                continue
            path = f"{prefix}{name}"
            if "properties" in spec:
                walk(spec["properties"], f"{path}.")
            elif "type" in spec and spec["type"] != "object":
                found.add(path)

    for entry in mapping_reply.values():
        mappings = entry.get("mappings") if isinstance(entry, dict) else None
        if isinstance(mappings, dict):
            walk(mappings.get("properties"), "")
    return found


def exists_body(fields: Sequence[str], time_field: str, since: str) -> dict[str, object]:
    """Build the one bounded search that counts, per field, the documents that hold it.

    Args:
        fields: The field names to count.
        time_field: The family's date field.
        since: The window, e.g. ``7d``.

    Returns:
        A ``size: 0`` search body with one ``filters`` aggregation.
    """
    return {
        "size": 0,
        "track_total_hits": False,
        "timeout": _ES_TIMEOUT,
        "query": {"range": {time_field: {"gte": f"now-{since}"}}},
        "aggs": {
            "fields": {
                "filters": {"filters": {name: {"exists": {"field": name}} for name in fields}}
            }
        },
    }


def counts_from(search_reply: Mapping[str, object]) -> dict[str, int]:
    """Read the per-field document counts from the search reply.

    Args:
        search_reply: The decoded reply of the ``exists_body`` search.

    Returns:
        Field name -> document count.
    """
    aggregations = search_reply.get("aggregations")
    buckets = (
        aggregations.get("fields", {}).get("buckets", {}) if isinstance(aggregations, dict) else {}
    )
    return {
        name: int(bucket.get("doc_count", 0))
        for name, bucket in (buckets.items() if isinstance(buckets, dict) else [])
        if isinstance(bucket, dict)
    }


def family_drift(
    family: str,
    allowed: frozenset[str],
    mapped: set[str],
    counts: Mapping[str, int],
    *,
    checked: int,
    top: int,
) -> FamilyDrift:
    """Compare one family's allowlist with its mapping and its per-field counts.

    Args:
        family: The index family.
        allowed: The family's ``ALLOWED_FIELDS`` entry.
        mapped: The family's mapped leaf fields.
        counts: Document counts of unlisted fields in the window.
        checked: How many unlisted fields were counted.
        top: How many unlisted fields with data to keep.

    Returns:
        The drift.
    """
    unlisted = mapped - allowed
    with_data = sorted(
        ((name, n) for name, n in counts.items() if n > 0 and name in unlisted),
        key=lambda item: (-item[1], item[0]),
    )
    return FamilyDrift(
        family=family,
        listed_missing=tuple(sorted(allowed - mapped)),
        unlisted_with_data=tuple(with_data[:top]),
        with_data=len(with_data),
        unlisted_mapped=len(unlisted),
        checked=checked,
    )


async def measure(client: httpx.AsyncClient, family: str, *, since: str, top: int) -> FamilyDrift:
    """Read one family's mapping and count its unlisted fields in the window.

    Args:
        client: A client whose base URL is Elasticsearch.
        family: The index family.
        since: The window, e.g. ``7d``.
        top: How many unlisted fields with data to report.

    Returns:
        The family's drift.

    Raises:
        httpx.HTTPError: Elasticsearch cannot be reached or answers with an error.
    """
    mapping = await client.get(f"/{family}-*/_mapping", params=_PARAMS)
    mapping.raise_for_status()
    allowed = ALLOWED_FIELDS[family]
    mapped = leaf_fields(mapping.json())
    candidates = sorted(mapped - allowed)[:_MAX_FILTERS]
    counts: dict[str, int] = {}
    if candidates:
        search = await client.post(
            f"/{family}-*/_search",
            params=_PARAMS,
            json=exists_body(candidates, FAMILY_TIME_FIELD[family], since),
        )
        search.raise_for_status()
        counts = counts_from(search.json())
    return family_drift(family, allowed, mapped, counts, checked=len(candidates), top=top)


def render(drift: FamilyDrift, *, since: str) -> str:
    """Render one family's drift as plain text: names and counts, never values.

    Args:
        drift: The family's drift.
        since: The window the counts cover.

    Returns:
        The report section.
    """
    lines = [f"== {drift.family}"]
    lines.append(f"Listed fields that do not exist ({len(drift.listed_missing)}):")
    lines.extend(f"  {name}" for name in drift.listed_missing)
    if not drift.listed_missing:
        lines.append("  (none)")
    lines.append(
        f"Unlisted fields with data in the last {since}: {drift.with_data} "
        f"({drift.unlisted_mapped} unlisted fields mapped, {drift.checked} counted). "
        f"Top {len(drift.unlisted_with_data)} by document count:"
    )
    lines.extend(f"  {name}  {count}" for name, count in drift.unlisted_with_data)
    if not drift.unlisted_with_data:
        lines.append("  (none)")
    if drift.checked < drift.unlisted_mapped:
        lines.append(f"  partial: only the first {drift.checked} unlisted fields were counted")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="search_logs_field_drift",
        description="search_logs allowlist vs live mapping drift (FRE-1777).",
    )
    parser.add_argument(
        "--family",
        choices=list(ALLOWED_FIELDS),
        action="append",
        help="Index family to check; repeat for several (default: all).",
    )
    parser.add_argument("--since", default="7d", help="Window for the counts (default: 7d).")
    parser.add_argument(
        "--top", type=int, default=25, help="Unlisted fields with data to list (default: 25)."
    )
    parser.add_argument("--es-url", default=None, help="Elasticsearch URL (default: settings).")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    base = (args.es_url or settings.elasticsearch_url).rstrip("/")
    families = args.family or list(ALLOWED_FIELDS)
    async with httpx.AsyncClient(base_url=base, timeout=_TIMEOUT_S) as client:
        for family in families:
            drift = await measure(client, family, since=args.since, top=args.top)
            sys.stdout.write(render(drift, since=args.since) + "\n\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
