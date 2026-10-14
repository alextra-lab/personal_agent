"""Tests for the missing-skill request context query (FRE-1322).

A ``missing_skill_requested`` event names a skill the model guessed. The host
that the same turn reached with bash is the stable signal: two different guessed
names that both ``curl`` ``tempo:3200`` are one gap. These tests cover the pure
host extraction and the Elasticsearch join that attaches a host to each request.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.telemetry.queries import (
    MissingSkillRequest,
    TelemetryQueries,
    dominant_host,
    extract_hosts,
)


class TestExtractHosts:
    """``extract_hosts`` reads ``http(s)://host[:port]`` from a command."""

    def test_internal_host_with_port(self) -> None:
        cmd = "curl -s --max-time 5 http://tempo:3200/api/echo"
        assert extract_hosts(cmd) == frozenset({"tempo:3200"})

    def test_public_host_without_port(self) -> None:
        cmd = "curl -s 'https://site.api.espn.com/apis/site/v2/sports'"
        assert extract_hosts(cmd) == frozenset({"site.api.espn.com"})

    def test_several_hosts_in_one_command(self) -> None:
        cmd = "curl http://tempo:3200/a; curl http://grafana:3000/b"
        assert extract_hosts(cmd) == frozenset({"tempo:3200", "grafana:3000"})

    def test_no_url_gives_empty_set(self) -> None:
        assert extract_hosts("jq keys /tmp/trace_last.json") == frozenset()

    def test_empty_command_gives_empty_set(self) -> None:
        assert extract_hosts("") == frozenset()

    def test_userinfo_token_is_never_read_as_the_host(self) -> None:
        cmd = "curl -s https://ghp_AbC123xyz@api.github.com/repos/o/r"
        assert extract_hosts(cmd) == frozenset({"api.github.com"})

    def test_userinfo_with_numeric_password_is_not_a_port(self) -> None:
        assert extract_hosts("curl http://user:12345@db.example/x") == frozenset({"db.example"})

    def test_at_sign_in_the_query_string_is_not_userinfo(self) -> None:
        cmd = "curl 'https://a.example:8080?contact=x@y.example'"
        assert extract_hosts(cmd) == frozenset({"a.example:8080"})

    def test_host_is_lowercased_and_scheme_case_is_ignored(self) -> None:
        assert extract_hosts("curl HTTP://Tempo:3200/x") == frozenset({"tempo:3200"})

    def test_ellipsis_in_prose_is_not_a_host(self) -> None:
        assert extract_hosts("echo see http://... for details") == frozenset()


class TestDominantHost:
    """``dominant_host`` picks the host with the most commands in the trace."""

    def test_most_commands_wins(self) -> None:
        cmds = [
            "curl http://tempo:3200/a",
            "curl http://tempo:3200/b",
            "curl http://grafana:3000/x",
        ]
        assert dominant_host(cmds) == "tempo:3200"

    def test_same_host_twice_in_one_command_counts_once(self) -> None:
        cmds = [
            "curl http://a.example/1 http://a.example/2",
            "curl http://b.example/1",
            "curl http://b.example/2",
        ]
        assert dominant_host(cmds) == "b.example"

    def test_tie_breaks_alphabetically(self) -> None:
        cmds = ["curl http://zeta.example/", "curl http://alpha.example/"]
        assert dominant_host(cmds) == "alpha.example"

    def test_no_host_gives_none(self) -> None:
        assert dominant_host(["echo hi", "jq . /tmp/x.json"]) is None

    def test_no_commands_gives_none(self) -> None:
        assert dominant_host([]) is None


def _event(name: str, session: str | None, trace: str | None) -> dict:
    return {
        "_source": {
            "requested_name": name,
            "session_id": session,
            "trace_id": trace,
            "@timestamp": "2026-08-29T05:00:00+00:00",
        }
    }


def _bash(trace: str, command: str, session: str | None = "s-1") -> dict:
    return {
        "_source": {
            "event_type": "bash_started",
            "trace_id": trace,
            "session_id": session,
            "command": command,
        }
    }


def _resp(hits: list[dict], total: int | None = None) -> dict:
    return {"hits": {"total": {"value": total if total is not None else len(hits)}, "hits": hits}}


def _queries(search_results: list[dict]) -> tuple[TelemetryQueries, AsyncMock]:
    client = MagicMock()
    client.search = AsyncMock(side_effect=search_results)
    return TelemetryQueries(es_client=client, field_validator=MagicMock()), client.search


@pytest.mark.asyncio
class TestGetMissingSkillRequests:
    """The join attaches a dominant host and a session to each request."""

    async def test_attaches_dominant_host_from_the_trace(self) -> None:
        queries, _ = _queries(
            [
                _resp([_event("observability-fetcher", "s-1", "t-1")]),
                _resp(
                    [
                        _bash("t-1", "curl http://tempo:3200/a"),
                        _bash("t-1", "curl http://tempo:3200/b"),
                        _bash("t-1", "curl http://grafana:3000/x"),
                    ]
                ),
            ]
        )
        got = await queries.get_missing_skill_requests(days=7)
        assert got == [MissingSkillRequest("observability-fetcher", "s-1", "t-1", "tempo:3200")]

    async def test_null_session_is_taken_from_a_bash_event_of_the_trace(self) -> None:
        queries, _ = _queries(
            [
                _resp([_event("query-telemetry", None, "t-1")]),
                _resp([_bash("t-1", "curl http://elasticsearch:9200/_cat", session="s-9")]),
            ]
        )
        got = await queries.get_missing_skill_requests(days=7)
        assert got[0].session_id == "s-9"
        assert got[0].host == "elasticsearch:9200"

    async def test_request_without_trace_id_has_no_host_and_is_not_queried(self) -> None:
        queries, search = _queries([_resp([_event("lonely-skill", "s-1", None)])])
        got = await queries.get_missing_skill_requests(days=7)
        assert got == [MissingSkillRequest("lonely-skill", "s-1", None, None)]
        assert search.await_count == 1  # no tool-event query for an empty trace list

    async def test_empty_trace_ids_never_reach_the_terms_filter(self) -> None:
        queries, search = _queries(
            [
                _resp([_event("a-skill", "s-1", "t-1"), _event("b-skill", "s-2", "")]),
                _resp([]),
            ]
        )
        await queries.get_missing_skill_requests(days=7)
        terms = [
            f["terms"]["trace_id"]
            for f in search.await_args_list[1].kwargs["query"]["bool"]["filter"]
            if "terms" in f and "trace_id" in f["terms"]
        ]
        assert terms == [["t-1"]]

    async def test_trace_without_a_url_has_no_host(self) -> None:
        queries, _ = _queries(
            [
                _resp([_event("weather-fetcher", "s-1", "t-1")]),
                _resp([_bash("t-1", "jq keys /tmp/x.json")]),
            ]
        )
        got = await queries.get_missing_skill_requests(days=7)
        assert got[0].host is None

    async def test_only_bash_events_are_fetched(self) -> None:
        """tool_call_started is the highest-volume event and would crowd out bash_started."""
        queries, search = _queries([_resp([_event("a-skill", "s-1", "t-1")]), _resp([])])
        await queries.get_missing_skill_requests(days=7)
        filters = search.await_args_list[1].kwargs["query"]["bool"]["filter"]
        event_types = [f["terms"]["event_type"] for f in filters if "event_type" in f.get("terms", {})]
        assert event_types == [["bash_started"]]

    async def test_trace_ids_are_queried_in_chunks_of_twenty(self) -> None:
        events = [_event(f"skill-{i}", f"s-{i}", f"t-{i}") for i in range(45)]
        queries, search = _queries([_resp(events), _resp([]), _resp([]), _resp([])])
        await queries.get_missing_skill_requests(days=7)
        assert search.await_count == 4  # 1 event query + 3 chunks (20, 20, 5)

    async def test_future_until_is_clamped_to_now(self) -> None:
        queries, search = _queries([_resp([])])
        future = datetime.now(timezone.utc) + timedelta(days=30)
        await queries.get_missing_skill_requests(days=7, until=future)
        rng = search.await_args_list[0].kwargs["query"]["bool"]["filter"][0]["range"]["@timestamp"]
        upper = datetime.fromisoformat(rng["lte"])
        lower = datetime.fromisoformat(rng["gte"])
        assert upper <= datetime.now(timezone.utc)
        assert upper - lower == timedelta(days=7)

    async def test_historical_until_sets_the_window(self) -> None:
        queries, search = _queries([_resp([])])
        until = datetime(2026, 8, 29, 6, 6, 25, tzinfo=timezone.utc)
        await queries.get_missing_skill_requests(days=7, until=until)
        rng = search.await_args_list[0].kwargs["query"]["bool"]["filter"][0]["range"]["@timestamp"]
        assert datetime.fromisoformat(rng["lte"]) == until
        assert datetime.fromisoformat(rng["gte"]) == until - timedelta(days=7)

    async def test_event_query_is_newest_first_and_capped(self) -> None:
        queries, search = _queries([_resp([])])
        await queries.get_missing_skill_requests(days=7)
        kwargs = search.await_args_list[0].kwargs
        assert kwargs["size"] == 2000
        assert kwargs["sort"] == [{"@timestamp": "desc"}]
