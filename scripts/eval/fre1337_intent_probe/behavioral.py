"""Arm 3 (optional) — a live full turn, behavioral signals read back from ES (AC-4).

Drives the isolated eval gateway (``docker-compose.eval.yml``'s ``seshat-gateway-control``
on :9002) the way `fre481_decomposition_ab/harness.py` drives the production gateway
(including its `wait_for_trace`-style ES-settle polling — a turn's events lag its HTTP
response), but targets `elasticsearch-eval` (:9202) instead — never production's ES.

Contamination control (AC-3) is two-part, both required — this is the one place FRE-1338's
incident actually bites. Entity extraction is asynchronous (`brainstem/scheduler.py`'s
consolidation pass, not a synchronous per-turn write) and can land tens of seconds after a
turn's HTTP response returns (FRE-1338's own incident measured 31s). The wipe-before-every-
turn mechanism, and its wait for a turn's `entity_extraction_completed` event before the
*next* turn's wipe runs, now live in `eval_isolation.IsolatedArmRunner` (FRE-1372) — the
same shared, structural mechanism any eval script uses, so this module no longer wipes
`neo4j-eval` directly. `run_contamination_proof` is the second half: it actually checks the
resulting graph afterwards (`substrate.find_cross_session_sources`) rather than asserting
the control worked by construction.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from scripts.eval.fre1337_intent_probe.fixtures import Fixture
from scripts.eval.fre1337_intent_probe.substrate import (
    EVAL_ARMS,
    EVAL_NEO4J_URI,
    fetch_originating_session_ids,
    find_cross_session_sources,
)
from scripts.eval.gateway_freshness import assert_gateway_fresh
from scripts.eval.gateway_freshness import repo_root as _repo_root

if TYPE_CHECKING:
    from scripts.eval.eval_isolation import IsolatedArmRunner

log = structlog.get_logger(__name__)

EVAL_ES_URL = "http://localhost:9202"
EVAL_ES_INDEX = "agent-logs-*"

#: How long a turn's `model_call_completed`/`tool_call_completed` events may lag its HTTP
#: response before ES indexing settles (fre481 precedent's `--trace-timeout-s` default).
SIGNAL_SETTLE_TIMEOUT_S = 120.0

#: How long entity extraction may lag a turn's HTTP response before landing in Neo4j
#: (FRE-1338's incident measured 31s; this is deliberately generous). A timeout here is
#: not fatal — some turns (e.g. a bare greeting) extract nothing at all — but it is always
#: waited out in full before the next fixture's wipe, never skipped.
EXTRACTION_SETTLE_TIMEOUT_S = 90.0

_POLL_INTERVAL_S = 3.0

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
        extraction_settled: Whether ``entity_extraction_completed`` was observed before
            the timeout — ``False`` doesn't invalidate the row (some turns extract
            nothing), but is recorded so a reader can tell "no entities" from
            "extraction still in flight when we moved on".
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
    extraction_settled: bool


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


async def _count_event(client: httpx.AsyncClient, trace_id: str, event_type: str) -> int:
    body = {
        "size": 0,
        "query": {
            "bool": {
                "must": [
                    {"term": {"trace_id": trace_id}},
                    {"term": {"event_type": event_type}},
                ]
            }
        },
    }
    result = await _es_search(client, body)
    return int(result["hits"]["total"]["value"])


async def wait_for_event_settle(
    client: httpx.AsyncClient,
    trace_id: str,
    event_type: str,
    *,
    timeout_s: float,
    require_nonzero: bool = True,
) -> bool:
    """Poll ES until ``event_type`` for ``trace_id`` stops growing (fre481's pattern).

    Args:
        client: Async HTTP client pointed at `elasticsearch-eval`.
        trace_id: Trace id to watch.
        event_type: Event type to count.
        timeout_s: Hard polling timeout.
        require_nonzero: If True, a stable count of 0 does not count as settled — keep
            polling until the timeout (the event just hasn't landed yet). If False, a
            stable 0 is a valid settled state (used when the event may legitimately
            never fire).

    Returns:
        True if the count was seen and stable (or accepted at 0) before the timeout;
        False if the timeout elapsed first.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    seen = -1
    while asyncio.get_event_loop().time() < deadline:
        count = await _count_event(client, trace_id, event_type)
        if count == seen and (count > 0 or not require_nonzero):
            return True
        seen = count
        await asyncio.sleep(_POLL_INTERVAL_S)
    return seen > 0 or not require_nonzero


def _wall_time(hits: list[dict[str, Any]]) -> float:
    stamps = [h["_source"].get("@timestamp") for h in hits if h["_source"].get("@timestamp")]
    if len(stamps) < 2:
        return 0.0
    parsed = sorted(datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps)
    return (parsed[-1] - parsed[0]).total_seconds()


async def _fetch_behavioral_signals(
    client: httpx.AsyncClient, trace_id: str
) -> tuple[int, int, list[int], int, int, float, bool]:
    """Read tool-call counts, token growth, wall clock, and budget exhaustion from ES.

    Caller must have already waited for `model_call_completed`/`tool_call_completed`
    indexing to settle (:func:`wait_for_event_settle`) — reading immediately after the
    turn's HTTP response returns races ES indexing and can silently under-report.
    """
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
    runner: "IsolatedArmRunner",
    http: httpx.AsyncClient,
    es: httpx.AsyncClient,
    fixture: Fixture,
    arm: str = "control",
) -> BehavioralReport:
    """Drive one fixture through the isolated eval gateway and collect its report.

    Isolation — the wipe before this turn, and the wait for both `model_call_completed`
    and `entity_extraction_completed` to settle before returning — is `runner`'s job
    (FRE-1372's `IsolatedArmRunner`). This function only reads the behavioral signals
    back once the turn has landed.
    """
    turn = await runner.run_turn(http, es, fixture.message, arm=arm)
    (
        tool_call_count,
        web_search_count,
        web_search_result_counts,
        fetch_url_count,
        input_token_growth,
        wall_time_s,
        tool_budget_exhausted,
    ) = await _fetch_behavioral_signals(es, turn.trace_id)
    if not turn.extraction_settled:
        log.warning(
            "fre1337_extraction_settle_timeout", fixture=fixture.label, trace_id=turn.trace_id
        )
    report = BehavioralReport(
        fixture_label=fixture.label,
        session_id=turn.session_id,
        trace_id=turn.trace_id,
        tool_call_count=tool_call_count,
        web_search_count=web_search_count,
        web_search_result_counts=web_search_result_counts,
        fetch_url_count=fetch_url_count,
        input_token_growth=input_token_growth,
        wall_time_s=wall_time_s,
        tool_budget_exhausted=tool_budget_exhausted,
        extraction_settled=turn.extraction_settled,
    )
    assert_behavioral_signals_complete(report)
    return report


async def run_behavioral_arm(
    fixtures: list[Fixture], arms: tuple[str, ...] = ("control",), trials: int = 1
) -> list[dict[str, Any]]:
    """Run every fixture through arm 3, isolated via `IsolatedArmRunner` (FRE-1372).

    Covers AC-4 plus AC-3's per-fixture control — see :func:`run_contamination_proof`
    for AC-3's actual "prove it" run. Isolation itself (wiping `neo4j-eval` before every
    turn) no longer happens in this function — it is `IsolatedArmRunner`'s job, the same
    shared mechanism any eval script uses.

    Args:
        fixtures: The fixture set.
        arms: Which eval gateways to drive, by name in ``EVAL_ARMS``. FRE-1350: the
            original run drove only ``control``, whose primitives are disabled, so a
            fixture needing ``bash`` scored zero tool calls for want of the tool. Pass
            both arms to make that difference measurable rather than invisible.
        trials: Repeats per (arm, fixture). Every cell was n=1 before FRE-1350, and the
            ``gpsr_research`` cell was observed flipping between two runs on the same
            day — a single draw cannot separate that variance from signal.

    Returns:
        One JSON-serializable behavioral report per (arm, trial, fixture), each carrying
        its own ``arm`` and ``trial`` so no row's environment is implicit.

    Raises:
        GatewayStaleError: An eval gateway's cached image predates the working tree
            (FRE-1341) — a stale `seshat-gateway:latest` used to serve this silently,
            with `/health` reporting "healthy" throughout. Checked per arm: FRE-1350
            drives two gateways, and asserting only control would leave treatment
            unguarded, which is the same shape as the gap FRE-1341 left on
            `run_contamination_proof`.
    """
    from scripts.eval.eval_isolation import IsolatedArmRunner, create_eval_driver

    driver = create_eval_driver()
    runner = IsolatedArmRunner(driver=driver)
    rows: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient() as http, httpx.AsyncClient() as es:
            for arm in arms:
                # FRE-1341 + FRE-1350: assert freshness PER ARM, not once for control.
                # A stale image serves old code with /health reporting "healthy", and
                # each arm is a separate container that can be stale independently.
                await assert_gateway_fresh(http, EVAL_ARMS[arm], _repo_root())
                for trial in range(trials):
                    for fixture in fixtures:
                        report = await run_one_fixture(runner, http, es, fixture, arm=arm)
                        # FRE-1350 AC-1: the arm and its trial index travel WITH the row.
                        # A tool-call count whose environment is not recorded is what made
                        # the first arm-3 run unattributable.
                        row = asdict(report) | {"arm": arm, "trial": trial}
                        rows.append(row)
                        log.info(
                            "fre1337_behavioral_row",
                            arm=arm,
                            trial=trial,
                            fixture=fixture.label,
                            tool_calls=report.tool_call_count,
                            web_searches=report.web_search_count,
                            extraction_settled=report.extraction_settled,
                        )
    finally:
        await driver.close()
    return rows


@dataclass(frozen=True)
class ContaminationProofResult:
    """AC-3's live "prove it" evidence: the same question run twice in sequence.

    Attributes:
        fixture_label: Which fixture was run twice.
        session_id_a: First run's session id.
        session_id_b: Second run's session id.
        leaked_sources: Records in the post-run graph whose ``originating_session_id``
            is ``session_id_a`` — empty means the control held.
        controlled: ``not leaked_sources`` — the AC-3 pass/fail.
    """

    fixture_label: str
    session_id_a: str
    session_id_b: str
    leaked_sources: list[dict[str, Any]]
    controlled: bool


async def run_contamination_proof(fixture: Fixture) -> ContaminationProofResult:
    """AC-3: run the same fixture twice in sequence; verify B's graph carries nothing traceable to A.

    Sequence: turn A, isolated, then turn B, isolated — `IsolatedArmRunner` wipes and
    waits out both settle events between them (FRE-1372) — then read the graph and check
    nothing in it originates from session A. The read is real
    (:func:`substrate.fetch_originating_session_ids`), not assumed — this is what makes
    it a demonstrated control rather than an asserted one.

    Args:
        fixture: The single fixture to run twice (any fixture; the ticket asks for "the
            same question").

    Returns:
        The proof result — ``controlled`` is the AC-3 pass/fail.
    """
    from scripts.eval.eval_isolation import IsolatedArmRunner, create_eval_driver

    driver = create_eval_driver()
    runner = IsolatedArmRunner(driver=driver)
    try:
        async with httpx.AsyncClient() as http, httpx.AsyncClient() as es:
            turn_a = await runner.run_turn(http, es, fixture.message)
            turn_b = await runner.run_turn(http, es, fixture.message)

            current_sources = await fetch_originating_session_ids(driver, uri=EVAL_NEO4J_URI)
            leaked = find_cross_session_sources(current_sources, turn_a.session_id)
            result = ContaminationProofResult(
                fixture_label=fixture.label,
                session_id_a=turn_a.session_id,
                session_id_b=turn_b.session_id,
                leaked_sources=leaked,
                controlled=not leaked,
            )
            log.info(
                "fre1337_contamination_proof",
                fixture=fixture.label,
                controlled=result.controlled,
                leaked_count=len(leaked),
            )
            return result
    finally:
        await driver.close()
