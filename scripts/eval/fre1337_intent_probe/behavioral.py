"""Arm 3 (optional) — a live full turn, behavioral signals read back from ES (AC-4).

Drives the isolated eval gateway (``docker-compose.eval.yml``'s ``seshat-gateway-control``
on :9002) exactly the way `fre481_decomposition_ab/harness.py` drives the production
gateway, but targets `elasticsearch-eval` (:9202) instead — never production's ES. Between
every fixture, `substrate.wipe_eval_graph` clears `neo4j-eval` so one fixture's newly
extracted entities can never be picked up by the next fixture's `search_memory` call
(FRE-1338's incident, reproduced as a control here rather than a defect).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import httpx
import structlog
from scripts.eval.fre1337_intent_probe.fixtures import Fixture
from scripts.eval.fre1337_intent_probe.substrate import (
    EVAL_CHAT_BASE_URL,
    EVAL_NEO4J_URI,
    assert_eval_chat_url,
    wipe_eval_graph,
)

log = structlog.get_logger(__name__)

EVAL_ES_URL = "http://localhost:9202"
EVAL_ES_INDEX = "agent-logs-*"

_REQUIRED_FIELDS = (
    "tool_call_count",
    "web_search_count",
    "fetch_url_count",
    "input_token_growth",
    "wall_time_s",
    "tool_budget_exhausted",
)


@dataclass
class BehavioralReport:
    """AC-4's evidence row — one fixture's live behavioral signals.

    Attributes:
        fixture_label: The fixture's label.
        session_id: Session id of the turn.
        trace_id: Trace id of the turn.
        tool_call_count: Total ``tool_call_completed`` events for the trace.
        web_search_count: Of those, how many were ``web_search``.
        web_search_result_counts: Per-call ``result_count`` from ``web_search_completed``.
        fetch_url_count: Of those, how many were ``fetch_url``.
        input_token_growth: Last primary-round ``input_tokens`` minus the first — 0 when
            only one round ran.
        wall_time_s: First→last event timestamp span.
        tool_budget_exhausted: Whether ``tool_budget_warning_injected`` fired.
    """

    fixture_label: str
    session_id: str
    trace_id: str
    tool_call_count: int
    web_search_count: int
    web_search_result_counts: list[int]
    fetch_url_count: int
    input_token_growth: int
    wall_time_s: float
    tool_budget_exhausted: bool


def assert_behavioral_signals_complete(report: BehavioralReport) -> None:
    """AC-4: every required field must be present, not silently missing.

    Args:
        report: A captured behavioral report.

    Raises:
        ValueError: If any required field is ``None`` (a count field found no events
            because the query itself failed, distinct from a genuine zero).
    """
    data = asdict(report)
    missing = [field for field in _REQUIRED_FIELDS if data.get(field) is None]
    if missing:
        raise ValueError(f"fixture {report.fixture_label!r}: behavioral signals missing: {missing}")


async def _es_search(client: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(f"{EVAL_ES_URL}/{EVAL_ES_INDEX}/_search", json=body, timeout=30.0)
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


async def _call_chat(client: httpx.AsyncClient, message: str) -> tuple[str, str]:
    assert_eval_chat_url(EVAL_CHAT_BASE_URL)
    resp = await client.post(
        f"{EVAL_CHAT_BASE_URL}/chat",
        params={"message": message, "channel": "EVAL"},
        timeout=1200.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return str(data["session_id"]), str(data["trace_id"])


def _wall_time(hits: list[dict[str, Any]]) -> float:
    stamps = [h["_source"].get("@timestamp") for h in hits if h["_source"].get("@timestamp")]
    if len(stamps) < 2:
        return 0.0
    parsed = sorted(datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps)
    return (parsed[-1] - parsed[0]).total_seconds()


async def _fetch_behavioral_signals(
    client: httpx.AsyncClient, trace_id: str
) -> tuple[int, int, list[int], int, int, float, bool]:
    """Read tool-call counts, token growth, wall clock, and budget exhaustion from ES."""
    tool_calls = await _es_search(
        client,
        {
            "size": 500,
            "_source": ["tool_name", "@timestamp"],
            "query": {
                "bool": {
                    "must": [
                        {"term": {"trace_id": trace_id}},
                        {"term": {"event_type": "tool_call_completed"}},
                    ]
                }
            },
        },
    )
    tool_hits = tool_calls["hits"]["hits"]
    tool_call_count = len(tool_hits)
    fetch_url_count = sum(1 for h in tool_hits if h["_source"].get("tool_name") == "fetch_url")

    web_search_events = await _es_search(
        client,
        {
            "size": 200,
            "_source": ["result_count"],
            "query": {
                "bool": {
                    "must": [
                        {"term": {"trace_id": trace_id}},
                        {"term": {"event_type": "web_search_completed"}},
                    ]
                }
            },
        },
    )
    web_search_hits = web_search_events["hits"]["hits"]
    web_search_count = len(web_search_hits)
    web_search_result_counts = [int(h["_source"].get("result_count", 0)) for h in web_search_hits]

    model_calls = await _es_search(
        client,
        {
            "size": 200,
            "_source": ["role", "input_tokens", "@timestamp"],
            "query": {
                "bool": {
                    "must": [
                        {"term": {"trace_id": trace_id}},
                        {"term": {"event_type": "model_call_completed"}},
                    ]
                }
            },
            "sort": [{"@timestamp": "asc"}],
        },
    )
    model_hits = model_calls["hits"]["hits"]
    primary_inputs = [
        int(h["_source"].get("input_tokens") or 0)
        for h in model_hits
        if h["_source"].get("role") == "primary"
    ]
    input_token_growth = (primary_inputs[-1] - primary_inputs[0]) if len(primary_inputs) >= 2 else 0
    wall_time_s = _wall_time(model_hits)

    budget_warning = await _es_search(
        client,
        {
            "size": 0,
            "query": {
                "bool": {
                    "must": [
                        {"term": {"trace_id": trace_id}},
                        {"term": {"event_type": "tool_budget_warning_injected"}},
                    ]
                }
            },
        },
    )
    tool_budget_exhausted = int(budget_warning["hits"]["total"]["value"]) > 0

    return (
        tool_call_count,
        web_search_count,
        web_search_result_counts,
        fetch_url_count,
        input_token_growth,
        wall_time_s,
        tool_budget_exhausted,
    )


async def run_one_fixture(
    http: httpx.AsyncClient, es: httpx.AsyncClient, fixture: Fixture
) -> BehavioralReport:
    """Drive one fixture through the eval gateway and collect its behavioral report."""
    session_id, trace_id = await _call_chat(http, fixture.message)
    (
        tool_call_count,
        web_search_count,
        web_search_result_counts,
        fetch_url_count,
        input_token_growth,
        wall_time_s,
        tool_budget_exhausted,
    ) = await _fetch_behavioral_signals(es, trace_id)
    report = BehavioralReport(
        fixture_label=fixture.label,
        session_id=session_id,
        trace_id=trace_id,
        tool_call_count=tool_call_count,
        web_search_count=web_search_count,
        web_search_result_counts=web_search_result_counts,
        fetch_url_count=fetch_url_count,
        input_token_growth=input_token_growth,
        wall_time_s=wall_time_s,
        tool_budget_exhausted=tool_budget_exhausted,
    )
    assert_behavioral_signals_complete(report)
    return report


async def run_behavioral_arm(fixtures: list[Fixture]) -> list[dict[str, Any]]:
    """Run every fixture through arm 3, wiping `neo4j-eval` between each (AC-3).

    Args:
        fixtures: The fixture set.

    Returns:
        One JSON-serializable behavioral report per fixture.
    """
    from neo4j import AsyncGraphDatabase

    # EVAL_NEO4J_URI is the isolated eval substrate constant (substrate.py), never
    # prod — every write on this driver goes through wipe_eval_graph's URI-equality
    # guard before it runs.
    driver = (
        AsyncGraphDatabase.driver(  # fre-375-allow: EVAL_NEO4J_URI only, guarded in substrate.py
            EVAL_NEO4J_URI, auth=("neo4j", _eval_neo4j_password())
        )
    )
    reports: list[BehavioralReport] = []
    try:
        async with httpx.AsyncClient() as http, httpx.AsyncClient() as es:
            for fixture in fixtures:
                await wipe_eval_graph(driver, uri=EVAL_NEO4J_URI)
                report = await run_one_fixture(http, es, fixture)
                reports.append(report)
                log.info(
                    "fre1337_behavioral_row",
                    fixture=fixture.label,
                    tool_calls=report.tool_call_count,
                    web_searches=report.web_search_count,
                )
    finally:
        await driver.close()
    return [asdict(r) for r in reports]


def _eval_neo4j_password() -> str:
    import os

    password = os.environ.get("NEO4J_PASSWORD") or os.environ.get("STUDY_NEO4J_PASSWORD")
    if not password:
        raise RuntimeError(
            "NEO4J_PASSWORD (or STUDY_NEO4J_PASSWORD) must be set to run the behavioral "
            "arm — it authenticates against neo4j-eval, matching docker-compose.eval.yml."
        )
    return password
