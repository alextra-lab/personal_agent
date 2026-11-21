"""Tests for the search_logs allowlist drift check (FRE-1777, AC-2).

No test talks to Elasticsearch: all use ``httpx.MockTransport`` (tests/CLAUDE.md, FRE-375).
"""

from __future__ import annotations

import json

import httpx
import pytest
from scripts.monitors.search_logs_field_drift import (
    counts_from,
    exists_body,
    family_drift,
    leaf_fields,
    measure,
    render,
)

from personal_agent.tools.telemetry_query import ALLOWED_FIELDS

_MAPPING = {
    "agent-logs-2026-10": {
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "event_type": {"type": "keyword"},
                "old_only": {"type": "keyword"},
            }
        }
    },
    "agent-logs-2026-11": {
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "unlisted_note": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "probe": {"properties": {"ms": {"type": "long"}, "ok": {"type": "boolean"}}},
                "empty_object": {"type": "object"},
            }
        }
    },
}


def test_leaf_fields_unites_indices_walks_objects_and_skips_multi_fields() -> None:
    assert leaf_fields(_MAPPING) == {
        "@timestamp",
        "event_type",
        "old_only",
        "unlisted_note",
        "probe.ms",
        "probe.ok",
    }


def test_exists_body_is_one_bounded_count_only_search() -> None:
    body = exists_body(["a", "b"], "@timestamp", "7d")
    assert body["size"] == 0
    assert body["query"] == {"range": {"@timestamp": {"gte": "now-7d"}}}
    assert body["aggs"] == {
        "fields": {
            "filters": {
                "filters": {"a": {"exists": {"field": "a"}}, "b": {"exists": {"field": "b"}}}
            }
        }
    }


def test_family_drift_reports_both_lists() -> None:
    drift = family_drift(
        "agent-logs",
        frozenset({"@timestamp", "event_type", "gone"}),
        {"@timestamp", "event_type", "unlisted_note", "probe.ms", "quiet"},
        {"unlisted_note": 50, "probe.ms": 7, "quiet": 0},
        checked=3,
        top=1,
    )
    assert drift.listed_missing == ("gone",)
    assert drift.unlisted_with_data == (("unlisted_note", 50),)
    assert drift.with_data == 2, "the total counts every unlisted field with data, not the top N"
    assert drift.unlisted_mapped == 3


def test_render_prints_names_and_counts_only() -> None:
    drift = family_drift(
        "agent-logs", frozenset({"gone"}), {"unlisted_note"}, {"unlisted_note": 5}, checked=1, top=5
    )
    text = render(drift, since="7d")
    assert "Listed fields that do not exist (1):\n  gone" in text
    assert "  unlisted_note  5" in text


@pytest.mark.asyncio
async def test_measure_sends_one_mapping_read_and_one_search() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/_mapping"):
            return httpx.Response(200, json=_MAPPING)
        names = json.loads(request.content)["aggs"]["fields"]["filters"]["filters"]
        buckets = {name: {"doc_count": 3 if name == "unlisted_note" else 0} for name in names}
        return httpx.Response(200, json={"aggregations": {"fields": {"buckets": buckets}}})

    async with httpx.AsyncClient(
        base_url="http://es.test", transport=httpx.MockTransport(handler)
    ) as client:
        drift = await measure(client, "agent-logs", since="7d", top=10)

    assert [(r.method, r.url.path) for r in requests] == [
        ("GET", "/agent-logs-*/_mapping"),
        ("POST", "/agent-logs-*/_search"),
    ]
    sent = json.loads(requests[1].content)["aggs"]["fields"]["filters"]["filters"]
    assert not set(sent) & ALLOWED_FIELDS["agent-logs"], "only unlisted fields are counted"
    assert drift.unlisted_with_data == (("unlisted_note", 3),)
    assert "@timestamp" not in drift.listed_missing
    assert "event_type" not in drift.listed_missing


def test_counts_from_tolerates_a_reply_without_aggregations() -> None:
    assert counts_from({}) == {}
