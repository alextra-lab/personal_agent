"""Inventory the free-text surface of ``agent-logs-*`` (FRE-1068).

Every figure in ``docs/research/2026-08-06-fre1068-telemetry-free-text-inventory.md``
is produced by this script. Re-run it and diff rather than trusting committed
numbers.

Usage:
    python3 scripts/audit/fre1068_free_text_inventory.py            # markdown
    python3 scripts/audit/fre1068_free_text_inventory.py --json     # machine-readable
    python3 scripts/audit/fre1068_free_text_inventory.py --max-docs 50000   # bounded

**Why this reads ``_source`` and not ``exists``.** The audit's central finding
is that an ``exists`` query answers a different question than "does this field
carry content". Two mapping features make it lie:

* ``arguments`` is mapped ``dynamic: false``, so ``arguments.command`` is stored
  and retrievable but never indexed — ``exists`` returns **zero** while
  hundreds of documents carry full shell command lines.
* ``command`` is ``keyword`` with ``ignore_above: 1024``, so any value longer
  than that is stored but not indexed — and long values are precisely the ones
  worth auditing.

An inventory built on ``exists`` counts would therefore report both fields
clean. This script counts from ``_source`` and reports the delta between the two
as a first-class column, because that delta is the measure of how wrong the
cheap method is.

**Secret values are never printed.** Detector results are reported as
detector name, field, and document count only.

Exit codes:
    0   inventory produced, field set non-empty
    1   field set empty — a failed audit, not a clean result (FRE-1068 AC-1)
    70  Elasticsearch unreachable, so nothing was measured
"""

from __future__ import annotations

import argparse
import collections
import fnmatch
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

from personal_agent.telemetry.redaction import detect_secrets

ES_URL = os.environ.get("AUDIT_ES_URL", "http://localhost:9200")
INDEX_PATTERN = "agent-logs-*"
TEMPLATE_PATH = "docker/elasticsearch/index-template.json"

EXIT_OK = 0
EXIT_EMPTY_INVENTORY = 1
EXIT_UNREACHABLE = 70

#: Content classes, in match order. The first matching rule wins; anything
#: unmatched is reported as ``unclassified`` rather than silently bucketed, so
#: a new content-bearing field shows up as a gap instead of disappearing.
CONTENT_CLASSES: list[tuple[str, str]] = [
    (
        "conversation",
        r"^(user_message|assistant_response|task|task_name|message_preview|"
        r".*content_preview|response_preview|raw_preview|query_text|query_preview)$",
    ),
    ("agent-action", r"^(command|bad_segment|arguments\..*|checkpoint_.*|last_processed_path)$"),
    ("system-diagnostic", r"^(error|exception|message|hint|reason|.*_error|stderr|stdout)$"),
    (
        "structural",
        r"^(.*_id|.*_path|.*_name|.*_type|phase|phases\..*|level|logger|module|"
        r"function|component|event|tags|@timestamp)$",
    ),
]


def _get(path: str) -> Any:
    """GET a JSON document from Elasticsearch."""
    with urllib.request.urlopen(f"{ES_URL}{path}", timeout=30) as response:
        return json.load(response)


def _post(path: str, body: dict[str, Any]) -> Any:
    """POST a JSON body to Elasticsearch and return the parsed response."""
    request = urllib.request.Request(
        f"{ES_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def _delete(path: str) -> None:
    """Issue a DELETE, ignoring failures (used to release scroll contexts)."""
    request = urllib.request.Request(f"{ES_URL}{path}", method="DELETE")
    try:
        urllib.request.urlopen(request, timeout=30).close()
    except (urllib.error.URLError, urllib.error.HTTPError):
        pass


def classify(field: str) -> str:
    """Return the content class for a field name.

    Args:
        field: Dotted field path.

    Returns:
        One of the CONTENT_CLASSES names, or ``unclassified``.
    """
    for name, pattern in CONTENT_CLASSES:
        if re.match(pattern, field):
            return name
    return "unclassified"


def load_dynamic_templates() -> list[tuple[str, str, str]]:
    """Read the committed index template's dynamic-template match order.

    Elasticsearch templates match either by glob (the default) or by regex
    (``match_pattern: regex``); ``ids_keyword`` uses a glob while ``free_text``
    uses a regex, so the kind must be carried rather than assumed.

    Returns:
        List of (template name, match expression, kind) for the
        string-matching templates, in declaration order — which is the order
        Elasticsearch applies them.
    """
    with open(TEMPLATE_PATH) as handle:
        template = json.load(handle)
    result: list[tuple[str, str, str]] = []
    for entry in template["template"]["mappings"]["dynamic_templates"]:
        for name, spec in entry.items():
            if spec.get("match_mapping_type") in ("string", "*"):
                kind = "regex" if spec.get("match_pattern") == "regex" else "glob"
                result.append((name, spec.get("match", ""), kind))
    return result


def claiming_template(field: str, templates: list[tuple[str, str, str]], explicit: set[str]) -> str:
    """Return which mapping rule claims a field.

    Explicit ``properties`` win over dynamic templates, so they are checked
    first — the same precedence Elasticsearch applies.

    Args:
        field: Dotted field path.
        templates: Output of :func:`load_dynamic_templates`.
        explicit: Field names declared in the template's ``properties``.

    Returns:
        ``explicit``, a dynamic-template name, or ``unmapped``.
    """
    leaf = field.split(".")[0]
    if leaf in explicit:
        return "explicit"
    name = field.split(".")[-1]
    for template_name, pattern, kind in templates:
        if not pattern:
            continue
        matched = re.match(pattern, name) if kind == "regex" else fnmatch.fnmatchcase(name, pattern)
        if matched:
            return template_name
    return "unmapped"


def walk_mapping(properties: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten a mapping properties block into {dotted field: type}."""
    result: dict[str, str] = {}
    for name, spec in properties.items():
        full = f"{prefix}{name}"
        if "properties" in spec:
            result.update(walk_mapping(spec["properties"], f"{full}."))
        else:
            result[full] = spec.get("type", "unknown")
    return result


def flatten(value: Any, prefix: str = "") -> Any:
    """Yield (dotted field, scalar) for every leaf in a document."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from flatten(inner, f"{prefix}{key}.")
    elif isinstance(value, list):
        for item in value:
            yield from flatten(item, prefix)
    else:
        yield prefix.rstrip("."), value


class FieldStats:
    """Running per-field statistics, accumulated without retaining documents."""

    __slots__ = ("docs", "values", "total_length", "max_length", "events")

    def __init__(self) -> None:
        """Initialise empty counters."""
        self.docs = 0
        self.values = 0
        self.total_length = 0
        self.max_length = 0
        self.events: collections.Counter[str] = collections.Counter()

    def observe(self, value: str) -> None:
        """Record one *value* of this field.

        A field inside an array yields one observation per element, so this is
        counted separately from the document count — conflating them inflated
        ``tool_names`` to 13,931 "documents" across 308 real ones and made the
        blind-spot delta meaningless.
        """
        self.values += 1
        self.total_length += len(value)
        self.max_length = max(self.max_length, len(value))

    def observe_document(self, event: str) -> None:
        """Record that one document carried this field, whatever its arity."""
        self.docs += 1
        self.events[event] += 1

    @property
    def mean_length(self) -> int:
        """Mean observed value length."""
        return self.total_length // self.values if self.values else 0


def scan(
    max_docs: int,
) -> tuple[dict[str, FieldStats], collections.Counter[tuple[str, str]], int, bool]:
    """Stream every document, accumulating per-field stats and detector hits.

    Memory is bounded: documents are discarded as they are read.

    Args:
        max_docs: Stop after this many documents; 0 scans everything.

    Returns:
        (field stats, detector hits keyed by (detector, field), documents
        scanned, whether the corpus was exhausted). The completeness flag
        matters: the blind-spot delta compares a ``_source`` count against a
        corpus-wide ``exists`` count, so it is only meaningful on a full scan.
    """
    stats: dict[str, FieldStats] = collections.defaultdict(FieldStats)
    detector_hits: collections.Counter[tuple[str, str]] = collections.Counter()
    scanned = 0
    complete = True

    response = _post(
        f"/{INDEX_PATTERN}/_search?scroll=5m",
        {"size": 2000, "query": {"match_all": {}}, "sort": ["_doc"]},
    )
    scroll_id = response.get("_scroll_id")
    try:
        while True:
            hits = response["hits"]["hits"]
            if not hits:
                break
            for hit in hits:
                scanned += 1
                source = hit["_source"]
                event = str(source.get("event_type") or source.get("event") or "unknown")
                fields_in_doc: set[str] = set()
                detectors_in_doc: set[tuple[str, str]] = set()
                for field, value in flatten(source):
                    if not isinstance(value, str) or not value:
                        continue
                    stats[field].observe(value)
                    fields_in_doc.add(field)
                    for name in detect_secrets(value):
                        detectors_in_doc.add((name, field))
                for field in fields_in_doc:
                    stats[field].observe_document(event)
                for key in detectors_in_doc:
                    detector_hits[key] += 1
            if max_docs and scanned >= max_docs:
                complete = False
                break
            response = _post("/_search/scroll", {"scroll": "5m", "scroll_id": scroll_id})
            scroll_id = response.get("_scroll_id", scroll_id)
    finally:
        if scroll_id:
            _delete(f"/_search/scroll/{scroll_id}")

    return stats, detector_hits, scanned, complete


def exists_count(field: str) -> int:
    """Return the ``exists``-query document count for a field."""
    try:
        return int(
            _post(f"/{INDEX_PATTERN}/_count", {"query": {"exists": {"field": field}}})["count"]
        )
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError):
        return 0


def build_inventory(max_docs: int) -> dict[str, Any]:
    """Run the full audit and return a structured result."""
    mapping = _get(f"/{INDEX_PATTERN}/_mapping")
    mapped_types: dict[str, set[str]] = collections.defaultdict(set)
    for body in mapping.values():
        for field, field_type in walk_mapping(body["mappings"].get("properties", {})).items():
            mapped_types[field].add(field_type)

    with open(TEMPLATE_PATH) as handle:
        explicit = set(json.load(handle)["template"]["mappings"]["properties"])
    templates = load_dynamic_templates()

    stats, detector_hits, scanned, complete = scan(max_docs)

    fields: list[dict[str, Any]] = []
    for field, stat in sorted(stats.items(), key=lambda item: -item[1].max_length):
        source_docs = stat.docs
        indexed_docs = exists_count(field)
        fields.append(
            {
                "field": field,
                "mapping_type": "/".join(sorted(mapped_types.get(field, {"not-in-mapping"}))),
                "claimed_by": claiming_template(field, templates, explicit),
                "content_class": classify(field),
                "source_docs": source_docs,
                "source_values": stat.values,
                "exists_docs": indexed_docs,
                "blind_docs": max(0, source_docs - indexed_docs) if complete else None,
                "max_length": stat.max_length,
                "mean_length": stat.mean_length,
                "top_events": [name for name, _ in stat.events.most_common(3)],
            }
        )

    return {
        "index_pattern": INDEX_PATTERN,
        "documents_scanned": scanned,
        "scan_complete": complete,
        "string_fields_total": len(fields),
        "fields": fields,
        "detector_hits": [
            {"detector": detector, "field": field, "docs": count}
            for (detector, field), count in sorted(detector_hits.items(), key=lambda kv: -kv[1])
        ],
    }


def render_markdown(inventory: dict[str, Any]) -> str:
    """Render the inventory as the committed research artifact's tables."""
    lines = [
        f"Documents scanned: {inventory['documents_scanned']:,}",
        f"String-valued fields found: {inventory['string_fields_total']}",
        f"Full-corpus scan: {'yes' if inventory['scan_complete'] else 'NO — blind column is n/a'}",
        "",
        "| field | class | mapping | claimed by | _source docs | exists docs | blind | values | max len |",
        "|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in inventory["fields"]:
        blind = f"{row['blind_docs']:,}" if row["blind_docs"] is not None else "n/a"
        lines.append(
            f"| `{row['field']}` | {row['content_class']} | {row['mapping_type']} | "
            f"{row['claimed_by']} | {row['source_docs']:,} | {row['exists_docs']:,} | "
            f"{blind} | {row['source_values']:,} | {row['max_length']:,} |"
        )
    lines += ["", "| detector | field | documents |", "|---|---|---:|"]
    for hit in inventory["detector_hits"]:
        lines.append(f"| {hit['detector']} | `{hit['field']}` | {hit['docs']:,} |")
    return "\n".join(lines)


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    parser.add_argument(
        "--out",
        help=(
            "write the payload to this file. Importing the app package "
            "configures logging onto stdout, so piping stdout yields log lines "
            "mixed into the payload; --out is the machine-readable path."
        ),
    )
    parser.add_argument(
        "--max-docs", type=int, default=0, help="stop after N documents (0 = scan everything)"
    )
    args = parser.parse_args()

    try:
        inventory = build_inventory(args.max_docs)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"Elasticsearch unreachable at {ES_URL}: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE

    payload = json.dumps(inventory, indent=2) if args.json else render_markdown(inventory)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(payload + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(payload)

    if not inventory["fields"]:
        print(
            "FAILED AUDIT: no free-text fields found. An inventory that finds "
            "nothing is a failure of the audit, not a pass (FRE-1068 AC-1).",
            file=sys.stderr,
        )
        return EXIT_EMPTY_INVENTORY
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
