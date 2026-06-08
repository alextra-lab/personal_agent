#!/usr/bin/env python3
"""FRE-535 (B1) — Kibana panel live-verification harness.

Read-only gate for the "no silent-empty panels" acceptance criterion. For every
visualisation in the repo Kibana NDJSON saved objects (``config/kibana/dashboards``)
it reproduces the panel's *own* query context — its ``searchSourceJSON`` kuery filter
plus a long ``@timestamp`` lookback — and checks against live Elasticsearch that:

1. the panel's filter matches at least one document (catches filter-dead panels such
   as a query on an ``event_type`` that is never emitted), and
2. every **bucket** field (terms / split aggregations, not metric aggregations)
   resolves to at least one aggregation bucket under that filter (catches the
   ``.keyword``-on-bare-keyword and text-as-terms traps that silently produce empty
   panels).

Panels intentionally retired by FRE-535 are listed in ``RETIRED`` and reported as
``retired`` rather than failures. The script exits non-zero if any non-retired panel
field is empty, so it works red-before / green-after the triage edits.

Nothing is written to Elasticsearch or Kibana.

Usage::

    uv run python scripts/audit/verify_fre535_panels.py
    uv run python scripts/audit/verify_fre535_panels.py --es http://localhost:9200
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DASH_DIR = REPO / "config" / "kibana" / "dashboards"
DEFAULT_ES = "http://localhost:9200"

# FRE-535 retired these panels by removing them from the NDJSON entirely (not kept
# as expected-empty), so the gate now fails on ANY empty panel. Retired for the
# record: delegation_outcomes "Rounds needed trend" / "Delegation satisfaction
# distribution" / "Delegation success rate" (dead `delegation_outcome_recorded`
# event); insights_engine "Weekly proposals created" (`record_type:weekly_summary`
# never emitted); task_analytics "Routing Decisions" (`routing_decision` never
# emitted — see FRE-545); request_timing "Avg Duration by Phase" / "Request Phase
# Details" (dead `request_timing_phase`; covered by Request Traces); and the whole
# `request_latency.ndjson` dashboard (dead `request_latency_*`, superseded).
RETIRED: frozenset[tuple[str, str]] = frozenset()

# Time-axis fields — verified via the panel doc-count, not a terms aggregation.
DATE_FIELDS: frozenset[str] = frozenset({"@timestamp", "timestamp"})

_KUERY_AND = re.compile(r"\band\b", re.I)
_KUERY_OR = re.compile(r"\bor\b", re.I)


# ---------------------------------------------------------------------------
# HTTP (stdlib only; localhost POST _search with size:0)
# ---------------------------------------------------------------------------


def _post_json(url: str, body: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    """POST a JSON body and parse the JSON response.

    Args:
        url: Target URL.
        body: Request body serialised to JSON.
        timeout: Socket timeout in seconds.

    Returns:
        Parsed JSON object. On an HTTP error the parsed error body is returned so
        callers can inspect the Elasticsearch ``error`` field.
    """
    data = json.dumps(body).encode()
    req = urllib.request.Request(  # noqa: S310 (localhost only)
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            parsed: dict[str, Any] = json.loads(resp.read().decode())
            return parsed
    except urllib.error.HTTPError as exc:
        try:
            err_body: dict[str, Any] = json.loads(exc.read().decode())
            return err_body
        except json.JSONDecodeError:
            return {"error": {"type": f"http_{exc.code}"}}


def _kuery_query(kuery: str) -> dict[str, Any]:
    """Translate a (simple) Kibana kuery string to an Elasticsearch query clause.

    Handles the field:value / quoted-value / lowercase ``and``/``or`` forms used by
    the repo dashboards. An empty kuery becomes ``match_all``.

    Args:
        kuery: The panel's ``searchSourceJSON.query.query`` string.

    Returns:
        An Elasticsearch query clause.
    """
    kuery = (kuery or "").strip()
    if not kuery:
        return {"match_all": {}}
    normalised = _KUERY_OR.sub("OR", _KUERY_AND.sub("AND", kuery))
    return {"query_string": {"query": normalised}}


# ---------------------------------------------------------------------------
# Saved-object parsing
# ---------------------------------------------------------------------------


def _iter_saved_objects() -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield ``(ndjson_filename, saved_object)`` for every repo dashboard object."""
    for ndjson in sorted(DASH_DIR.glob("*.ndjson")):
        for line in ndjson.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield ndjson.name, json.loads(line)
            except json.JSONDecodeError:
                continue


def load_index_patterns() -> dict[str, str]:
    """Build ``index-pattern id -> title`` across all repo NDJSON files."""
    patterns: dict[str, str] = {}
    for _fname, so in _iter_saved_objects():
        if so.get("type") == "index-pattern":
            title = (so.get("attributes") or {}).get("title")
            if isinstance(title, str):
                patterns[str(so.get("id"))] = title
    return patterns


def _index_ref(so: dict[str, Any]) -> str | None:
    """Return the index-pattern id this saved object's searchSource references."""
    for ref in so.get("references") or []:
        if ref.get("type") == "index-pattern":
            return str(ref.get("id"))
    return None


def _bucket_fields_classic(vis_state: dict[str, Any]) -> list[str]:
    """Collect bucket (non-metric) aggregation fields from a classic visState."""
    fields: list[str] = []
    for agg in vis_state.get("aggs") or []:
        if not isinstance(agg, dict):
            continue
        if agg.get("schema") == "metric":
            continue
        field = (agg.get("params") or {}).get("field")
        if isinstance(field, str) and field:
            fields.append(field)
    return fields


def _bucket_fields_lens(state: dict[str, Any]) -> list[str]:
    """Collect terms-bucket source fields from a Lens ``state`` blob."""
    fields: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if obj.get("operationType") == "terms" and isinstance(obj.get("sourceField"), str):
                fields.append(obj["sourceField"])
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(state)
    return fields


def _panel_query(so: dict[str, Any]) -> str:
    """Extract the panel's kuery query string from its searchSourceJSON."""
    meta = (so.get("attributes") or {}).get("kibanaSavedObjectMeta") or {}
    blob = meta.get("searchSourceJSON")
    if isinstance(blob, str):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            return ""
        return str((parsed.get("query") or {}).get("query") or "")
    return ""


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify(es_url: str) -> int:
    """Run the live verification and print a per-panel report.

    Args:
        es_url: Base Elasticsearch URL.

    Returns:
        Process exit code — 0 if every non-retired panel field is non-empty, else 1.
    """
    patterns = load_index_patterns()
    rows: list[tuple[str, str, str, str, str]] = []
    failures = 0

    for fname, so in _iter_saved_objects():
        if so.get("type") not in {"visualization", "lens"}:
            continue
        attrs = so.get("attributes") or {}
        title = str(attrs.get("title") or so.get("id"))
        retired = (fname, title) in RETIRED

        index = patterns.get(_index_ref(so) or "", "")
        if not index:
            rows.append((fname, title, "(no index-pattern ref)", "-", "SKIP"))
            continue

        # Bucket fields: classic visState aggs + Lens terms columns.
        bucket_fields: list[str] = []
        vs_blob = attrs.get("visState")
        if isinstance(vs_blob, str):
            try:
                bucket_fields += _bucket_fields_classic(json.loads(vs_blob))
            except json.JSONDecodeError:
                pass
        if isinstance(attrs.get("state"), dict):
            bucket_fields += _bucket_fields_lens(attrs["state"])
        bucket_fields = [f for f in dict.fromkeys(bucket_fields) if f not in DATE_FIELDS]

        # All-time (no @timestamp range): we query ES directly, so the most-permissive
        # gate is "does this panel's filter ever match data?". Avoids the time-field
        # trap (insights/reflections key on ``timestamp``, not ``@timestamp``).
        query = _kuery_query(_panel_query(so))
        search_url = f"{es_url}/{index}/_search"

        # 1) Does the panel's filter match anything?
        count_resp = _post_json(search_url, {"size": 0, "query": query})
        total = (count_resp.get("hits") or {}).get("total") or {}
        doc_count = total.get("value", 0) if isinstance(total, dict) else 0
        if doc_count == 0:
            status = "retired" if retired else "FAIL(filter-empty)"
            rows.append((fname, title, _panel_query(so) or "*", index, status))
            if not retired:
                failures += 1
            continue

        # 2) Does every bucket field resolve to a non-empty terms aggregation?
        empty_fields: list[str] = []
        for field in bucket_fields:
            agg_resp = _post_json(
                search_url,
                {"size": 0, "query": query, "aggs": {"a": {"terms": {"field": field}}}},
            )
            if "error" in agg_resp:
                empty_fields.append(f"{field}!ERR")
                continue
            buckets = ((agg_resp.get("aggregations") or {}).get("a") or {}).get("buckets") or []
            if not buckets:
                empty_fields.append(field)

        if empty_fields:
            status = "retired" if retired else "FAIL(empty-bucket)"
            rows.append((fname, title, ", ".join(empty_fields), index, status))
            if not retired:
                failures += 1
        else:
            detail = ", ".join(bucket_fields) if bucket_fields else f"{doc_count} docs"
            rows.append((fname, title, detail, index, "ok"))

    _print_report(rows, failures)
    return 1 if failures else 0


def _print_report(rows: list[tuple[str, str, str, str, str]], failures: int) -> None:
    """Print the per-panel verification table and a summary line."""
    width = max((len(r[1]) for r in rows), default=10)
    current = ""
    for fname, title, detail, index, status in sorted(rows):
        if fname != current:
            print(f"\n## {fname}")
            current = fname
        marker = {"ok": "  ok ", "retired": "  -- ", "SKIP": " skip"}.get(status, "FAIL ")
        print(f"  [{marker}] {title:<{width}}  {status:<18} {detail}  ({index})")
    print(f"\n{'FAIL' if failures else 'PASS'}: {failures} non-retired panel(s) with empty fields.")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="FRE-535 panel live-verification gate.")
    parser.add_argument("--es", default=DEFAULT_ES, help="Elasticsearch base URL.")
    args = parser.parse_args()
    sys.exit(verify(args.es))


if __name__ == "__main__":
    main()
