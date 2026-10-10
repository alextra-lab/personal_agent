"""Tests for query_telemetry's Tempo read and its field list (FRE-1567).

AC-1: the tool description lists every allowed field and span attribute, and nothing else.
AC-4: the ``latency`` action reads Tempo's search API only, with a required window, a bounded
trace limit and allowlisted attributes. Every refusal also asserts that no request left the
process (``calls == 0``). No test talks to Tempo or Elasticsearch: all use
``httpx.MockTransport`` (tests/CLAUDE.md, FRE-375).

The twin refusal tests at the end keep the FRE-1564 refusal rules proven after FRE-1567 took
``latency_ms`` and ``duration_ms`` out of the allowlist (see the plan, section 2.1).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from personal_agent.orchestrator.expansion_controller import _build_planner_system_prompt
from personal_agent.orchestrator.worker_types import WORKER_TYPES, WorkerType
from personal_agent.telemetry import TraceContext
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.telemetry_query import (
    ALLOWED_FIELDS,
    TEMPO_ATTRIBUTES,
    TEMPO_GROUP_KEYS,
    TEMPO_SPANS,
    query_telemetry_executor,
    query_telemetry_tool,
)

_CTX = TraceContext.new_trace()

# Span attributes that carry text, a URL or a conversation id (live tag list, 2026-10-10).
# None may be selectable, groupable or returned.
_SENSITIVE_ATTRIBUTES = (
    "statusMessage",
    "url.full",
    "http.target",
    "slm.session_id",
    "slm.client.trace_id",
    "db.elasticsearch.path_parts.id",
)
_CONTENT_FIELDS = ("user_message", "message_preview", "query_text", "arguments", "results")
_RETIRED = ("duration_ms", "latency_ms", "response_time_ms")


def _span(
    span_id: str,
    duration_ms: float,
    role: str = "primary",
    model: str = "m/a",
    status: str = "unset",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    attributes: list[dict[str, Any]] = [
        {"key": "gen_ai.operation.name", "value": {"stringValue": role}},
        {"key": "gen_ai.request.model", "value": {"stringValue": model}},
        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "100"}},
        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "10"}},
        {"key": "status", "value": {"stringValue": status}},
    ]
    for key, value in (extra or {}).items():
        typed = {"intValue": str(value)} if isinstance(value, int) else {"stringValue": value}
        attributes.append({"key": key, "value": typed})
    return {
        "spanID": span_id,
        "name": f"model_call {model}",
        "durationNanos": str(int(duration_ms * 1_000_000)),
        "attributes": attributes,
    }


def _reply(*span_sets: list[dict[str, Any]], jobs: tuple[int, int] = (5, 5)) -> dict[str, Any]:
    """A Tempo search reply with one trace per span set, ``matched`` equal to its spans."""
    traces = [
        {
            "traceID": f"t{i}",
            "rootServiceName": "seshat-vps",
            "rootTraceName": "POST /chat",
            "spanSets": [{"spans": spans, "matched": len(spans)}],
        }
        for i, spans in enumerate(span_sets)
    ]
    return {
        "traces": traces,
        "metrics": {"completedJobs": jobs[0], "totalJobs": jobs[1]},
    }


class _Recorder:
    """Stand-in transport: records requests and returns a canned reply."""

    def __init__(self, reply: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._reply = reply or (lambda _r: httpx.Response(200, json=_reply()))

    @property
    def calls(self) -> int:
        return len(self.requests)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._reply(request)

    def params(self, index: int = 0) -> dict[str, str]:
        query = parse_qs(urlsplit(str(self.requests[index].url)).query)
        return {key: values[0] for key, values in query.items()}


def _canned(data: Mapping[str, Any]) -> _Recorder:
    return _Recorder(lambda _r: httpx.Response(200, json=data))


async def _run(rec: _Recorder, **kwargs: Any) -> dict[str, Any]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    with patch(
        "personal_agent.tools.telemetry_query.create_guarded_http_client", return_value=client
    ):
        return await query_telemetry_executor(ctx=_CTX, **kwargs)


async def _refused(rec: _Recorder, match: str = "refused", **kwargs: Any) -> None:
    with pytest.raises(ToolExecutionError, match=match):
        await _run(rec, **kwargs)
    assert rec.calls == 0, "a refused call must send no request"


def _listed(prefix: str) -> set[str]:
    """Return the comma-separated items of the description line that starts with ``prefix``."""
    lines = [
        line for line in query_telemetry_tool.description.splitlines() if line.startswith(prefix)
    ]
    assert len(lines) == 1, f"expected one description line starting {prefix!r}"
    return {item.strip() for item in lines[0].removeprefix(prefix).rstrip(".").split(",")}


# ── AC-1: the fields are known before the first query ─────────────────────────


@pytest.mark.parametrize("family", list(ALLOWED_FIELDS))
def test_the_description_lists_exactly_each_familys_fields(family: str) -> None:
    assert _listed(f"- {family} fields: ") == set(ALLOWED_FIELDS[family])


def test_the_description_lists_exactly_the_span_attributes() -> None:
    assert _listed("- latency span attributes: ") == set(TEMPO_ATTRIBUTES)


def test_the_description_lists_exactly_the_span_kinds_and_group_keys() -> None:
    assert _listed("- latency spans: ") == set(TEMPO_SPANS)
    assert _listed("- latency group_by: ") == set(TEMPO_GROUP_KEYS)


def test_no_sensitive_name_is_offered_in_the_description() -> None:
    description = query_telemetry_tool.description
    for name in (*_SENSITIVE_ATTRIBUTES, *_CONTENT_FIELDS):
        assert name not in description, name


def test_the_description_says_where_model_call_timing_lives() -> None:
    description = query_telemetry_tool.description
    assert "Tempo" in description
    assert "latency_total_ms" in description and "FRE-1568" in description
    for name in _RETIRED:
        assert name in description  # named only in the "not in agent-logs" note
        for fields in ALLOWED_FIELDS.values():
            assert name not in fields


def test_the_span_attribute_allowlist_holds_no_text_attribute() -> None:
    for name in _SENSITIVE_ATTRIBUTES:
        assert name not in TEMPO_ATTRIBUTES
    assert set(TEMPO_GROUP_KEYS.values()) <= TEMPO_ATTRIBUTES


def test_the_planner_prompt_does_not_depend_on_the_tool_description() -> None:
    """Scope item 4: the planner request is byte-identical, so ADR-0154 D7 need not re-run."""
    tools = [*WORKER_TYPES[WorkerType.GENERAL].tools, "web_search", "fetch_url"]
    before = _build_planner_system_prompt(tools)
    with patch.object(query_telemetry_tool, "description", "changed"):
        after = _build_planner_system_prompt(tools)
    assert before == after
    assert "span attributes" not in before


# ── AC-4: what works ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_latency_sends_one_bounded_get_to_the_search_api() -> None:
    rec = _Recorder()
    await _run(rec, action="latency", since="24h")

    assert rec.calls == 1
    request = rec.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/api/search"
    params = rec.params()
    assert set(params) == {"q", "start", "end", "limit", "spss"}
    assert int(params["end"]) - int(params["start"]) == 24 * 3600
    assert params["limit"] == "200"
    assert params["spss"] == "100"
    assert params["q"] == (
        '{ span:name =~ "model_call .*" } | select(span.gen_ai.operation.name, '
        "span.gen_ai.request.model, span.gen_ai.usage.input_tokens, "
        "span.gen_ai.usage.output_tokens, span:status)"
    )


@pytest.mark.asyncio
async def test_filters_and_the_slm_span_kind_build_the_expected_traceql() -> None:
    rec = _Recorder()
    await _run(
        rec,
        action="latency",
        since="7d",
        span="slm_chat",
        role="chat",
        model="unsloth/qwen3.8-flash-next",
        limit=500,
    )
    params = rec.params()
    assert params["q"] == (
        '{ resource.service.name = "slm-server" && span:name =~ "chat .*" '
        '&& span.gen_ai.operation.name = "chat" '
        '&& span.gen_ai.request.model = "unsloth/qwen3.8-flash-next" } '
        "| select(span.gen_ai.operation.name, span.gen_ai.request.model, "
        "span.gen_ai.usage.input_tokens, span.gen_ai.usage.output_tokens, span:status, "
        "span.slm.prefill_ms, span.slm.decode_ms)"
    )
    assert params["limit"] == "500"
    assert int(params["end"]) - int(params["start"]) == 7 * 24 * 3600


@pytest.mark.asyncio
async def test_the_result_has_percentiles_counts_errors_and_tokens_per_group() -> None:
    primary = [_span(f"p{i}", ms) for i, ms in enumerate([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])]
    sub = [
        _span("s1", 5, role="sub_agent", model="m/b"),
        _span("s2", 15, role="sub_agent", model="m/b", status="error"),
    ]
    out = await _run(_canned(_reply(primary, sub)), action="latency", since="24h")

    assert out["source"] == "tempo"
    assert out["complete"] is True
    assert "sample" not in out
    assert out["percentiles_over"] == "spans_used"
    assert out["traces_returned"] == 2
    assert out["spans_used"] == 12
    groups = {(g["role"], g["model"]): g for g in out["groups"]}
    assert groups[("primary", "m/a")] == {
        "role": "primary",
        "model": "m/a",
        "count": 10,
        "errors": 0,
        "p50_ms": 50.0,
        "p90_ms": 90.0,
        "max_ms": 100.0,
        "input_tokens": 1000,
        "output_tokens": 100,
    }
    assert groups[("sub_agent", "m/b")]["errors"] == 1
    assert groups[("sub_agent", "m/b")]["p50_ms"] == 5.0
    assert out["groups"][0]["role"] == "primary"  # largest group first


@pytest.mark.asyncio
async def test_group_by_role_alone_merges_the_models() -> None:
    spans = [_span("a", 10, model="m/a"), _span("b", 30, model="m/b")]
    out = await _run(_canned(_reply(spans)), action="latency", since="1h", group_by=["role"])
    assert out["groups"] == [
        {
            "role": "primary",
            "count": 2,
            "errors": 0,
            "p50_ms": 10.0,
            "p90_ms": 30.0,
            "max_ms": 30.0,
            "input_tokens": 200,
            "output_tokens": 20,
        }
    ]


@pytest.mark.asyncio
async def test_slm_spans_add_prefill_and_decode_percentiles() -> None:
    spans = [
        _span("a", 100, role="chat", extra={"slm.prefill_ms": 40, "slm.decode_ms": 60}),
        _span("b", 200, role="chat", extra={"slm.prefill_ms": 80, "slm.decode_ms": 120}),
    ]
    out = await _run(_canned(_reply(spans)), action="latency", since="1h", span="slm_chat")
    (group,) = out["groups"]
    assert group["prefill_p50_ms"] == 40.0
    assert group["prefill_p90_ms"] == 80.0
    assert group["decode_p50_ms"] == 60.0
    assert group["decode_p90_ms"] == 120.0


# ── AC-4: refusals, nothing leaves the process ────────────────────────────────


@pytest.mark.asyncio
async def test_a_missing_window_is_refused() -> None:
    await _refused(_Recorder(), action="latency")


@pytest.mark.asyncio
@pytest.mark.parametrize("since", ["8d", "169h", "10081m", "30d", "0m", "1y", "now-1h", ""])
async def test_a_window_outside_one_minute_to_seven_days_is_refused(since: str) -> None:
    await _refused(_Recorder(), action="latency", since=since)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [501, 10_000, 0, -1, "200", 2.5, float("nan"), True])
async def test_an_oversized_or_malformed_limit_is_refused(limit: Any) -> None:
    await _refused(_Recorder(), action="latency", since="24h", limit=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "group_by",
    [
        # Seeded negatives: attributes that carry text, a URL or a conversation id.
        ["statusMessage"],
        ["url.full"],
        ["http.target"],
        ["slm.session_id"],
        ["role", "slm.client.trace_id"],
        # Raw attribute names are not group keys either: only the listed keys are.
        ["gen_ai.operation.name"],
        ["span:name"],
        ["role", "role"],
        ["role", "model", "role"],
        "role",
        [7],
    ],
)
async def test_a_group_key_outside_the_allowlist_is_refused(group_by: Any) -> None:
    await _refused(_Recorder(), action="latency", since="24h", group_by=group_by)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "span", ["../api/traces/x", "traces", "http", "tool_call", "/api/v2/traces", "MODEL_CALL", 3]
)
async def test_a_span_kind_outside_the_list_is_refused(span: Any) -> None:
    await _refused(_Recorder(), action="latency", since="24h", span=span)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        'primary" || true || "',
        "primary\\",
        "a } | select(statusMessage) | { true",
        "x" * 101,
        "",
        "with space",
        ["primary"],
        5,
    ],
)
@pytest.mark.parametrize("key", ["role", "model"])
async def test_a_filter_value_that_could_change_the_traceql_is_refused(
    key: str, value: Any
) -> None:
    await _refused(_Recorder(), action="latency", since="24h", **{key: value})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"index": "agent-logs"},
        {"query": {"match_all": {}}},
        {"aggs": {"a": {"avg": {"field": "input_tokens"}}}},
        {"fields": ["event_type"]},
        {"size": 5},
    ],
)
async def test_latency_refuses_elasticsearch_arguments(extra: dict[str, Any]) -> None:
    await _refused(_Recorder(), action="latency", since="24h", **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [{"span": "model_call"}, {"group_by": ["role"]}, {"role": "primary"}, {"limit": 10}],
)
async def test_search_refuses_tempo_arguments(extra: dict[str, Any]) -> None:
    await _refused(_Recorder(), action="search", index="agent-logs", **extra)


@pytest.mark.asyncio
async def test_search_and_count_still_need_an_index() -> None:
    await _refused(_Recorder(), action="search")
    await _refused(_Recorder(), action="count")


@pytest.mark.asyncio
async def test_every_accepted_latency_call_reaches_only_the_search_endpoint() -> None:
    rec = _Recorder()
    for kwargs in (
        {"since": "1m"},
        {"since": "7d", "span": "slm_chat"},
        {"since": "2h", "group_by": ["model"], "role": "sub_agent"},
        {"since": "90m", "limit": 1, "model": "anthropic/claude-sonnet-5"},
    ):
        await _run(rec, action="latency", **kwargs)
    assert rec.calls == 4
    assert {(r.method, r.url.path) for r in rec.requests} == {("GET", "/api/search")}


# ── The output contract: nothing outside the allowlist comes back ─────────────


@pytest.mark.asyncio
async def test_no_sensitive_value_in_the_reply_reaches_the_output() -> None:
    secret = "SECRET-user-text"
    span = _span(
        "a",
        10,
        extra={
            "url.full": f"http://x/?q={secret}",
            "http.target": f"/chat/{secret}",
            "slm.session_id": secret,
            "statusMessage": secret,
        },
    )
    span["name"] = f"model_call {secret}"
    span["statusMessage"] = secret
    span["events"] = [{"name": secret}]
    data = _reply([span])
    data["traces"][0]["rootTraceName"] = secret
    data["traces"][0]["rootServiceName"] = secret
    data["traces"][0]["statusMessage"] = secret

    out = await _run(_canned(data), action="latency", since="1h")
    assert out["spans_used"] == 1
    assert secret not in json.dumps(out)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label",
    ['the user said "hello there"', "x" * 101, "multi\nline", "{ select(statusMessage) }"],
)
async def test_an_unexpected_group_value_is_replaced(label: str) -> None:
    out = await _run(_canned(_reply([_span("a", 10, role=label)])), action="latency", since="1h")
    assert out["groups"][0]["role"] == "<unrecognised>"
    assert label not in json.dumps(out)


@pytest.mark.asyncio
async def test_a_missing_group_value_is_reported_as_none() -> None:
    span = _span("a", 10)
    span["attributes"] = [a for a in span["attributes"] if a["key"] != "gen_ai.operation.name"]
    out = await _run(_canned(_reply([span])), action="latency", since="1h")
    assert out["groups"][0]["role"] == "<none>"


# ── Completeness: a capped result never reads as complete ─────────────────────


@pytest.mark.asyncio
async def test_reaching_the_trace_limit_marks_the_result_incomplete() -> None:
    out = await _run(
        _canned(_reply([_span("a", 10)], [_span("b", 20)])), action="latency", since="1h", limit=2
    )
    assert out["complete"] is False
    assert out["sample"] is True
    assert any("trace limit" in r for r in out["incomplete_reasons"])


@pytest.mark.asyncio
async def test_a_span_set_with_more_matches_than_spans_marks_it_incomplete() -> None:
    data = _reply([_span("a", 10)])
    data["traces"][0]["spanSets"][0]["matched"] = 250
    out = await _run(_canned(data), action="latency", since="1h")
    assert out["complete"] is False
    assert any("span set" in r for r in out["incomplete_reasons"])


@pytest.mark.asyncio
@pytest.mark.parametrize("matched", [None, "x", -1])
async def test_a_missing_or_malformed_match_count_marks_it_incomplete(matched: Any) -> None:
    data = _reply([_span("a", 10)])
    if matched is None:
        del data["traces"][0]["spanSets"][0]["matched"]
    else:
        data["traces"][0]["spanSets"][0]["matched"] = matched
    out = await _run(_canned(data), action="latency", since="1h")
    assert out["complete"] is False


@pytest.mark.asyncio
async def test_unfinished_search_jobs_mark_it_incomplete() -> None:
    out = await _run(_canned(_reply([_span("a", 10)], jobs=(3, 64))), action="latency", since="1h")
    assert out["complete"] is False
    assert any("blocks" in r for r in out["incomplete_reasons"])


@pytest.mark.asyncio
async def test_a_span_without_a_valid_duration_marks_it_incomplete() -> None:
    span = _span("a", 10)
    span["durationNanos"] = "not-a-number"
    out = await _run(_canned(_reply([span, _span("b", 20)])), action="latency", since="1h")
    assert out["spans_used"] == 1
    assert out["complete"] is False


@pytest.mark.asyncio
async def test_more_than_fifty_groups_are_cut_and_marked_incomplete() -> None:
    spans = [_span(f"s{i}", 10, model=f"m/{i}") for i in range(60)]
    out = await _run(_canned(_reply(spans)), action="latency", since="1h")
    assert len(out["groups"]) == 50
    assert out["complete"] is False
    assert any("groups" in r for r in out["incomplete_reasons"])
    assert len(json.dumps(out)) <= 20_000


@pytest.mark.asyncio
async def test_the_same_span_in_two_span_sets_is_counted_once() -> None:
    span = _span("a", 10)
    data = _reply([span])
    data["traces"][0]["spanSets"].append({"spans": [span], "matched": 1})
    out = await _run(_canned(data), action="latency", since="1h")
    assert out["spans_used"] == 1


@pytest.mark.asyncio
async def test_a_reply_with_the_single_span_set_shape_is_read() -> None:
    data = _reply([_span("a", 10)])
    trace = data["traces"][0]
    trace["spanSet"] = trace.pop("spanSets")[0]
    out = await _run(_canned(data), action="latency", since="1h")
    assert out["spans_used"] == 1
    assert out["complete"] is True


@pytest.mark.asyncio
async def test_an_empty_window_is_complete_with_no_groups() -> None:
    out = await _run(_canned({"traces": [], "metrics": {}}), action="latency", since="5m")
    assert out["groups"] == []
    assert out["spans_used"] == 0
    assert out["complete"] is True


# ── Failure mapping and size bound ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_oversized_reply_is_a_tool_error() -> None:
    big = b"x" * (16 * 1024 * 1024 + 1)
    rec = _Recorder(lambda _r: httpx.Response(200, content=big))
    with pytest.raises(ToolExecutionError, match="too large"):
        await _run(rec, action="latency", since="24h")


@pytest.mark.asyncio
async def test_a_tempo_rejection_becomes_a_short_error() -> None:
    rec = _Recorder(lambda _r: httpx.Response(400, text="bad query " + "x" * 900))
    with pytest.raises(ToolExecutionError, match="rejected") as err:
        await _run(rec, action="latency", since="24h")
    assert len(str(err.value)) < 500


@pytest.mark.asyncio
async def test_a_connection_failure_becomes_a_tool_error() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(ToolExecutionError, match="Cannot connect to Tempo"):
        await _run(_Recorder(boom), action="latency", since="24h")


@pytest.mark.asyncio
async def test_a_timeout_becomes_a_tool_error() -> None:
    def slow(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ToolExecutionError, match="timed out"):
        await _run(_Recorder(slow), action="latency", since="24h")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'{"traces": "x"}'])
async def test_a_reply_of_the_wrong_shape_is_a_tool_error(body: bytes) -> None:
    rec = _Recorder(lambda _r: httpx.Response(200, content=body))
    with pytest.raises(ToolExecutionError, match="Tempo"):
        await _run(rec, action="latency", since="24h")


# ── Elasticsearch: the retired fields, and the FRE-1564 rules kept proven ─────


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _RETIRED)
async def test_a_retired_timing_field_is_refused(field: str) -> None:
    await _refused(_Recorder(), action="search", index="agent-logs", fields=[field])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            {
                "aggs": {
                    "a": {
                        "avg": {"field": "input_tokens"},
                        "aggs": {"b": {"avg": {"field": "input_tokens"}}},
                    }
                }
            },
            "cannot hold sub-aggregations",
        ),
        (
            {
                "aggs": {
                    "a": {"percentiles": {"field": "input_tokens", "percents": list(range(20))}}
                }
            },
            "percents is a list",
        ),
        (
            {"aggs": {f"a{i}": {"avg": {"field": "input_tokens"}} for i in range(11)}},
            "at most 10 aggregations",
        ),
        (
            {"query": {"range": {"input_tokens": {"gte": float("nan")}}}},
            "a value must be",
        ),
        (
            {"aggs": {"p": {"percentiles": {"field": "input_tokens", "percents": [float("inf")]}}}},
            "percents is a list",
        ),
    ],
)
async def test_each_fre1564_rule_still_refuses_for_its_own_reason(
    kwargs: dict[str, Any], reason: str
) -> None:
    """Twins of FRE-1564 refusal tests whose field (``latency_ms``) left the allowlist."""
    await _refused(_Recorder(), match=reason, action="search", index="agent-logs", **kwargs)
