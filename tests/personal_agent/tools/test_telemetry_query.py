"""Tests for the native read-only telemetry tool (FRE-1564, AC-1).

The tool is a security boundary: a worker holds it beside private reads. So every refusal
test also asserts that no request left the process (``calls == 0``), and the allowlist
tests pin that no conversation-content field is reachable. No test talks to Elasticsearch;
all use ``httpx.MockTransport`` (tests/CLAUDE.md, FRE-375).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from personal_agent.tools.telemetry_query import (
    ALLOWED_FIELDS,
    FAMILY_TIME_FIELD,
    query_telemetry_executor,
    query_telemetry_tool,
)

from personal_agent.telemetry import TraceContext
from personal_agent.tools.executor import ToolExecutionError

_CTX = TraceContext.new_trace()

# The conversation-derived fields of agent-logs-* (live mapping, 2026-10-10). None may be
# queryable, aggregable or returnable.
_CONTENT_FIELDS = (
    "user_message",
    "message_preview",
    "messages_preview",
    "query_preview",
    "query_text",
    "raw_preview",
    "context_messages",
    "arguments",
    "results",
    "sources",
    "query",
    "query_params",
    "user_id",
    "response_headers",
)


class _Recorder:
    """Stand-in transport: counts requests and returns a canned Elasticsearch reply."""

    def __init__(self, reply: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._reply = reply or (
            lambda _r: httpx.Response(
                200,
                json={"hits": {"total": {"value": 0}, "hits": []}, "timed_out": False},
            )
        )

    @property
    def calls(self) -> int:
        return len(self.requests)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._reply(request)

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


async def _run(rec: _Recorder, **kwargs: Any) -> dict[str, Any]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    with patch(
        "personal_agent.tools.telemetry_query.create_guarded_http_client", return_value=client
    ):
        return await query_telemetry_executor(ctx=_CTX, **kwargs)


async def _refused(rec: _Recorder, **kwargs: Any) -> None:
    with pytest.raises(ToolExecutionError, match="refused"):
        await _run(rec, **kwargs)
    assert rec.calls == 0, "a refused call must send no request"


# ── Definition ────────────────────────────────────────────────────────────────


def test_definition_is_a_low_risk_read_only_tool() -> None:
    assert query_telemetry_tool.name == "query_telemetry"
    assert query_telemetry_tool.category == "read_only"
    assert query_telemetry_tool.risk_level == "low"
    assert query_telemetry_tool.requires_approval is False
    # The primary reads telemetry with bash and curl; this tool is for workers (FRE-1564).
    assert query_telemetry_tool.worker_only is True
    names = {p.name for p in query_telemetry_tool.parameters}
    assert names == {"action", "index", "query", "aggs", "fields", "size", "since"}
    # No parameter can carry a URL, path, header or raw request body.
    assert not names & {"url", "path", "endpoint", "body", "method", "headers"}


def test_the_index_parameter_enumerates_exactly_the_families() -> None:
    index = next(p for p in query_telemetry_tool.parameters if p.name == "index")
    assert index.json_schema is not None
    assert set(index.json_schema["enum"]) == set(FAMILY_TIME_FIELD)


# ── The allowlist ─────────────────────────────────────────────────────────────


def test_no_conversation_content_field_is_allowed_in_any_family() -> None:
    for family, fields in ALLOWED_FIELDS.items():
        for content in _CONTENT_FIELDS:
            assert content not in fields, f"{content} must not be queryable in {family}"
            assert f"{content}.keyword" not in fields


def test_every_family_allows_its_own_time_field() -> None:
    for family, time_field in FAMILY_TIME_FIELD.items():
        assert time_field in ALLOWED_FIELDS[family]


def test_only_the_three_telemetry_families_are_reachable() -> None:
    assert set(FAMILY_TIME_FIELD) == {
        "agent-logs",
        "agent-topology",
        "agent-monitors-slm-health",
    }


# ── What works ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "time_field"),
    [
        ("agent-logs", "@timestamp"),
        ("agent-topology", "@timestamp"),
        ("agent-monitors-slm-health", "probed_at"),
    ],
)
async def test_search_builds_a_fixed_endpoint_and_a_time_bounded_body(
    family: str, time_field: str
) -> None:
    rec = _Recorder()
    await _run(rec, action="search", index=family)

    request = rec.requests[0]
    assert request.method == "POST"
    assert request.url.path == f"/{family}-*/_search"
    body = rec.body()
    assert body["query"]["bool"]["filter"] == [{"range": {time_field: {"gte": "now-24h"}}}]
    assert body["query"]["bool"]["must"] == [{"match_all": {}}]
    assert body["size"] == 20
    assert body["timeout"] == "10s"
    assert set(body["_source"]["includes"]) <= ALLOWED_FIELDS[family]
    assert not set(body["_source"]["includes"]) & set(_CONTENT_FIELDS)


@pytest.mark.asyncio
async def test_count_sends_only_a_query_to_the_count_endpoint() -> None:
    rec = _Recorder(lambda _r: httpx.Response(200, json={"count": 7}))
    out = await _run(
        rec,
        action="count",
        index="agent-topology",
        query={"term": {"planner_decision": "decline"}},
        since="7d",
    )

    assert rec.requests[0].url.path == "/agent-topology-*/_count"
    assert set(rec.body()) == {"query"}
    assert rec.body()["query"]["bool"]["filter"] == [{"range": {"@timestamp": {"gte": "now-7d"}}}]
    assert out == {"count": 7}


@pytest.mark.asyncio
async def test_a_harmless_value_that_looks_like_a_denied_field_is_allowed() -> None:
    """Values are data. Only field positions are checked (the Codex false-positive case)."""
    rec = _Recorder()
    await _run(
        rec,
        action="search",
        index="agent-logs",
        query={"term": {"event_type": "user_message"}},
    )
    assert rec.calls == 1


@pytest.mark.asyncio
async def test_empty_optional_arguments_mean_absent() -> None:
    """``{}`` and ``[]`` are the usual way a model says "no filter"; they are safe to accept."""
    rec = _Recorder()
    await _run(rec, action="search", index="agent-logs", query={}, aggs={}, fields=[])
    body = rec.body()
    assert body["query"]["bool"]["must"] == [{"match_all": {}}]
    assert "aggs" not in body
    assert set(body["_source"]["includes"]) == ALLOWED_FIELDS["agent-logs"]


@pytest.mark.asyncio
async def test_a_realistic_error_query_and_a_latency_aggregation_pass() -> None:
    rec = _Recorder()
    await _run(
        rec,
        action="search",
        index="agent-logs",
        query={
            "bool": {
                "filter": [{"term": {"level": "ERROR"}}],
                "must": [{"match": {"message": "timeout"}}],
                "must_not": [{"exists": {"field": "error.keyword"}}],
            }
        },
        aggs={
            "per_hour": {
                "date_histogram": {"field": "@timestamp", "calendar_interval": "hour"},
                "aggs": {
                    "p": {"percentiles": {"field": "latency_ms", "percents": [50, 95]}},
                    "avg_ms": {"avg": {"field": "duration_ms"}},
                },
            },
            "by_event": {"terms": {"field": "event_type", "size": 20, "order": {"_count": "desc"}}},
        },
        fields=["@timestamp", "event_type", "latency_ms"],
        size=5,
    )
    body = rec.body()
    assert body["size"] == 5
    assert body["_source"]["includes"] == ["@timestamp", "event_type", "latency_ms"]
    assert set(body["aggs"]) == {"per_hour", "by_event"}


@pytest.mark.asyncio
async def test_the_result_carries_total_hits_and_aggregations() -> None:
    rec = _Recorder(
        lambda _r: httpx.Response(
            200,
            json={
                "timed_out": False,
                "hits": {"total": {"value": 2}, "hits": [{"_source": {"level": "ERROR"}}]},
                "aggregations": {"by_event": {"buckets": []}},
            },
        )
    )
    out = await _run(rec, action="search", index="agent-logs")
    assert out["total"] == 2
    assert out["hits"] == [{"level": "ERROR"}]
    assert out["aggregations"] == {"by_event": {"buckets": []}}


# ── Bounds ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("asked", "sent"), [(500, 50), (0, 0), (-3, 0), (None, 20)])
async def test_size_is_capped_and_floored(asked: int | None, sent: int) -> None:
    rec = _Recorder()
    await _run(rec, action="search", index="agent-logs", size=asked)
    assert rec.body()["size"] == sent


@pytest.mark.asyncio
@pytest.mark.parametrize("since", ["90m", "24h", "30d"])
async def test_since_in_bounds_is_applied(since: str) -> None:
    rec = _Recorder()
    await _run(rec, action="search", index="agent-logs", since=since)
    assert rec.body()["query"]["bool"]["filter"][0]["range"]["@timestamp"]["gte"] == f"now-{since}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "since", ["31d", "721h", "43201m", "1y", "now-1h", "24", "h", "-1d", "1d; DROP", ""]
)
async def test_since_out_of_bounds_is_refused(since: str) -> None:
    rec = _Recorder()
    await _refused(rec, action="search", index="agent-logs", since=since)


@pytest.mark.asyncio
async def test_an_oversized_reply_is_cut_and_says_so() -> None:
    big = {"hits": {"total": {"value": 9}, "hits": [{"_source": {"message": "x" * 30000}}]}}
    rec = _Recorder(lambda _r: httpx.Response(200, json=big))
    out = await _run(rec, action="search", index="agent-logs")
    assert out["truncated"] is True
    assert out["total"] == 9
    assert len(json.dumps(out)) < 22000


# ── Refusals: nothing leaves the process ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["", "delete", "index", "update", "bulk", "delete_by_query", "SEARCH ", "msearch"]
)
async def test_an_action_other_than_search_or_count_is_refused(action: str) -> None:
    await _refused(_Recorder(), action=action, index="agent-logs")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "index",
    [
        "",
        "caddy-access",
        "user-turn-ratings",
        "agent-captains-captures",
        "agent-captains-reflections",
        "agent-insights",
        "agent-logs-*",
        "agent-logs,caddy-access",
        "agent-logs-2026-10",
        "_all",
        "*",
        "../_cluster/settings",
        "agent-logs/_delete_by_query",
        "agent-logs?x=1",
        "http://evil.example/agent-logs",
        "AGENT-LOGS",
    ],
)
async def test_an_index_outside_the_families_is_refused(index: str) -> None:
    await _refused(_Recorder(), action="search", index=index)


_BYPASS_QUERIES: list[dict[str, Any]] = [
    {"script": {"script": "1"}},
    {"terms_set": {"event_type": {"terms": ["x"], "minimum_should_match_script": {"source": "1"}}}},
    {"terms": {"event_type": {"index": "other", "id": "1", "path": "p"}}},
    {"more_like_this": {"fields": ["message"], "like": [{"_index": "x", "_id": "1"}]}},
    {"query_string": {"query": "user_message:secret"}},
    {"simple_query_string": {"query": "secret"}},
    {"multi_match": {"query": "secret", "fields": ["*"]}},
    {"wrapper": {"query": "e30="}},
    {"percolate": {"field": "q", "document": {}}},
    {"nested": {"path": "p", "query": {"match_all": {}}}},
    {"has_child": {"type": "t", "query": {"match_all": {}}}},
    {"knn": {"field": "v", "query_vector": [1], "k": 1}},
    {"wildcard": {"message": "*"}},
    {"regexp": {"message": ".*"}},
    {"term": {"user_message": "secret"}},
    {"match": {"user_message.keyword": "secret"}},
    {"term": {"innocent_alias": "x"}},
    {"term": {"event_type": {"index": "x"}}},
    {"term": {"event_type": ["a", "b"]}},
    {"term": {"event_type": None}},
    {"term": {"a": "x", "b": "y"}},
    {"bool": {"filter": {"term": {"user_id": "u"}}}},
    {"bool": {"script": "1"}},
    {"bool": {"must": [{"match_all": {}}], "boost": 2}},
    {"term": {"level": "x" * 201}},
    {"range": {"@timestamp": {"gte": "now-1h", "script": "1"}}},
    {"exists": {"field": "user_message"}},
    {"match_all": {}, "term": {"level": "x"}},
    [],
    "match_all",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", _BYPASS_QUERIES, ids=lambda q: json.dumps(q)[:60])
async def test_a_bypass_query_is_refused_before_any_request(query: Any) -> None:
    await _refused(_Recorder(), action="search", index="agent-logs", query=query)


@pytest.mark.asyncio
async def test_a_query_nested_past_the_depth_bound_is_refused() -> None:
    node: dict[str, Any] = {"match_all": {}}
    for _ in range(12):
        node = {"bool": {"must": [node]}}
    await _refused(_Recorder(), action="search", index="agent-logs", query=node)


@pytest.mark.asyncio
async def test_a_query_with_too_many_clauses_is_refused() -> None:
    wide = {"bool": {"should": [{"term": {"level": str(i)}} for i in range(80)]}}
    await _refused(_Recorder(), action="search", index="agent-logs", query=wide)


_BYPASS_AGGS: list[dict[str, Any]] = [
    {"a": {"global": {}, "aggs": {"t": {"terms": {"field": "event_type"}}}}},
    {"a": {"top_hits": {"size": 5}}},
    {"a": {"top_metrics": {"metrics": {"field": "message"}, "sort": {"@timestamp": "desc"}}}},
    {"a": {"scripted_metric": {"init_script": "1", "map_script": "1", "combine_script": "1"}}},
    {"a": {"terms": {"field": "event_type", "script": "1"}}},
    {"a": {"terms": {"field": "event_type", "missing": "x"}}},
    {"a": {"terms": {"field": "user_message.keyword"}}},
    {"a": {"terms": {"field": "user_id"}}},
    {"a": {"terms": {"field": "alias_of_user_message"}}},
    {"a": {"terms": {"field": "event_type", "size": 1000}}},
    {"a": {"terms": {"field": "event_type", "size": 0}}},
    {"a": {"terms": {"field": "event_type", "order": {"x.y": "desc"}}}},
    {"a": {"terms": {"field": "event_type", "order": {"_count": "sideways"}}}},
    {"a": {"significant_text": {"field": "message"}}},
    {"a": {"filter": {"term": {"level": "x"}}}},
    {"a": {"terms": {"field": "event_type"}, "avg": {"field": "latency_ms"}}},
    {"a": {"avg": {"field": "latency_ms"}, "aggs": {"b": {"avg": {"field": "latency_ms"}}}}},
    {"a": {"date_histogram": {"field": "@timestamp", "calendar_interval": "1000 years"}}},
    {"a": {"date_histogram": {"field": "@timestamp"}}},
    {"a": {"percentiles": {"field": "latency_ms", "percents": list(range(20))}}},
    {"bad name!": {"avg": {"field": "latency_ms"}}},
    {"a": {}},
    {"a": "avg"},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("aggs", _BYPASS_AGGS, ids=lambda a: json.dumps(a)[:60])
async def test_a_bypass_aggregation_is_refused_before_any_request(aggs: Any) -> None:
    await _refused(_Recorder(), action="search", index="agent-logs", aggs=aggs)


@pytest.mark.asyncio
async def test_aggregations_nested_past_the_bound_are_refused() -> None:
    node: dict[str, Any] = {"avg": {"field": "latency_ms"}}
    for i in range(6):
        node = {"terms": {"field": "event_type"}, "aggs": {f"n{i}": node}}
    await _refused(_Recorder(), action="search", index="agent-logs", aggs={"top": node})


@pytest.mark.asyncio
async def test_more_than_ten_aggregations_are_refused() -> None:
    aggs = {f"a{i}": {"avg": {"field": "latency_ms"}} for i in range(11)}
    await _refused(_Recorder(), action="search", index="agent-logs", aggs=aggs)


@pytest.mark.asyncio
async def test_aggregations_are_refused_on_a_count() -> None:
    await _refused(
        _Recorder(),
        action="count",
        index="agent-logs",
        aggs={"a": {"avg": {"field": "latency_ms"}}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        ["user_message"],
        ["@timestamp", "message_preview"],
        ["*"],
        ["user_*"],
        ["event_type", "arguments"],
        ["_source"],
        "event_type",
        [f"f{i}" for i in range(40)],
        [""],
        [7],
    ],
)
async def test_a_field_outside_the_allowlist_is_refused(fields: Any) -> None:
    await _refused(_Recorder(), action="search", index="agent-logs", fields=fields)


@pytest.mark.asyncio
async def test_a_field_of_another_family_is_refused() -> None:
    """Allowlists are per family: a topology field is not an slm-health field."""
    await _refused(
        _Recorder(),
        action="search",
        index="agent-monitors-slm-health",
        query={"term": {"planner_decision": "decline"}},
    )


@pytest.mark.asyncio
async def test_the_refusal_names_the_allowed_fields() -> None:
    with pytest.raises(ToolExecutionError) as err:
        await _run(_Recorder(), action="search", index="agent-logs", fields=["user_message"])
    assert "event_type" in str(err.value)
    assert "user_message" in str(err.value)


# ── Failure mapping ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_elasticsearch_rejection_becomes_a_short_error() -> None:
    reason = "x" * 900
    rec = _Recorder(
        lambda _r: httpx.Response(
            400, json={"error": {"root_cause": [{"reason": reason}], "type": "parse_exception"}}
        )
    )
    with pytest.raises(ToolExecutionError) as err:
        await _run(rec, action="search", index="agent-logs")
    assert "rejected" in str(err.value)
    assert len(str(err.value)) < 500


@pytest.mark.asyncio
async def test_a_connection_failure_becomes_a_tool_error() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(ToolExecutionError, match="Cannot connect"):
        await _run(_Recorder(boom), action="search", index="agent-logs")


@pytest.mark.asyncio
async def test_a_timeout_becomes_a_tool_error() -> None:
    def slow(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ToolExecutionError, match="timed out"):
        await _run(_Recorder(slow), action="search", index="agent-logs")


# ── Code-review findings (FRE-1564) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_shard_is_reported_not_hidden() -> None:
    """ES answers 200 with a failed month's data missing; the model must be told."""
    reply = {
        "hits": {"total": {"value": 5}, "hits": []},
        "_shards": {
            "total": 3,
            "failed": 1,
            "failures": [{"reason": {"reason": "fielddata is disabled on text fields"}}],
        },
    }
    out = await _run(
        _Recorder(lambda _r: httpx.Response(200, json=reply)), action="search", index="agent-logs"
    )
    assert out["shards_failed"] == 1
    assert "fielddata" in out["shard_failure_reason"]

    count = await _run(
        _Recorder(lambda _r: httpx.Response(200, json={"count": 4, "_shards": {"failed": 2}})),
        action="count",
        index="agent-logs",
    )
    assert count == {"count": 4, "shards_failed": 2}


@pytest.mark.asyncio
async def test_a_clean_reply_has_no_shard_warning() -> None:
    reply = {"hits": {"total": {"value": 1}, "hits": []}, "_shards": {"total": 3, "failed": 0}}
    out = await _run(
        _Recorder(lambda _r: httpx.Response(200, json=reply)), action="search", index="agent-logs"
    )
    assert "shards_failed" not in out


@pytest.mark.asyncio
async def test_an_oversized_reply_drops_hits_and_keeps_the_aggregations() -> None:
    reply = {
        "hits": {
            "total": {"value": 50},
            "hits": [{"_source": {"error": "e" * 1000}} for _ in range(50)],
        },
        "aggregations": {"by_event": {"buckets": [{"key": "a", "doc_count": 3}]}},
        "timed_out": True,
    }
    out = await _run(
        _Recorder(lambda _r: httpx.Response(200, json=reply)),
        action="search",
        index="agent-logs",
        size=50,
    )
    assert out["truncated"] is True
    assert out["aggregations"] == reply["aggregations"]
    assert out["timed_out"] is True
    assert 0 < out["hits_returned"] < 50
    assert out["hits_returned"] == len(out["hits"])
    assert len(json.dumps(out)) <= 20_000


@pytest.mark.asyncio
async def test_aggregations_alone_over_the_cap_fall_back_to_a_partial_reply() -> None:
    buckets = [{"key": f"k{i}", "doc_count": i} for i in range(3000)]
    reply = {
        "hits": {"total": {"value": 9}, "hits": []},
        "aggregations": {"big": {"buckets": buckets}},
    }
    out = await _run(
        _Recorder(lambda _r: httpx.Response(200, json=reply)), action="search", index="agent-logs"
    )
    assert out["truncated"] is True
    assert out["total"] == 9
    assert len(out["partial_json"]) == 20_000


@pytest.mark.asyncio
async def test_a_reply_that_is_not_a_json_object_is_a_tool_error() -> None:
    rec = _Recorder(lambda _r: httpx.Response(200, json=[1, 2]))
    with pytest.raises(ToolExecutionError, match="not a JSON object"):
        await _run(rec, action="search", index="agent-logs")


@pytest.mark.asyncio
async def test_a_dict_valued_bool_clause_with_an_allowed_field_is_accepted() -> None:
    """Pins the shape that `test_a_bypass_query...user_id` refuses only for its field."""
    rec = _Recorder()
    await _run(
        rec,
        action="search",
        index="agent-logs",
        query={"bool": {"filter": {"term": {"level": "ERROR"}}}},
    )
    assert rec.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "aggs",
    [
        {"a": {"date_histogram": {"field": "@timestamp", "calendar_interval": ["day"]}}},
        {"a": {"date_histogram": {"field": "@timestamp", "calendar_interval": {"x": 1}}}},
        {"a": {"date_histogram": {"field": "@timestamp", "fixed_interval": "15ms"}}},
        {"a": {"date_histogram": {"field": "@timestamp", "fixed_interval": ["15m"]}}},
        {
            "a": {
                "date_histogram": {
                    "field": "@timestamp",
                    "calendar_interval": "day",
                    "fixed_interval": "1h",
                }
            }
        },
    ],
)
async def test_a_malformed_interval_is_a_clean_refusal(aggs: dict[str, Any]) -> None:
    await _refused(_Recorder(), action="search", index="agent-logs", aggs=aggs)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
async def test_a_non_finite_number_is_a_clean_refusal(bad: float) -> None:
    await _refused(_Recorder(), action="search", index="agent-logs", size=bad)
    await _refused(
        _Recorder(),
        action="search",
        index="agent-logs",
        query={"range": {"latency_ms": {"gte": bad}}},
    )
    await _refused(
        _Recorder(),
        action="search",
        index="agent-logs",
        aggs={"p": {"percentiles": {"field": "latency_ms", "percents": [bad]}}},
    )
