"""A sub-agent's sources reach the primary as markers, and the reply as links (FRE-442).

The reproducing case, session db146e97 / trace 4b72555a (2026-10-21): a worker ran every
search, its report named a page on every price line, and the reply's Source column was
empty. The primary had the URLs but no registered source it was allowed to cite.

These tests run the path without a model: worker result → synthesis context → a reply that
copies the marker → the turn's final text.
"""

from __future__ import annotations

import json
from uuid import uuid4

from personal_agent.governance.models import Mode
from personal_agent.grounding.citations import CITATION_MARKER_PATTERN
from personal_agent.grounding.source_registry import RetrievedToolResult, SourceRegistry
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import _strip_markers_from_turn
from personal_agent.orchestrator.expansion_controller import (
    ExpansionController,
    _validate_plan_json,
)
from personal_agent.orchestrator.sub_agent_types import SubAgentResult
from personal_agent.orchestrator.types import ExecutionContext
from personal_agent.orchestrator.worker_types import Finding, WorkerReport

TURN = "trace-fre442-subagent"
KAYAK = "https://www.kayak.com/flights/PAR-NYC"
EXPEDIA = "https://www.expedia.com/deals/paris"


def _plan_json() -> str:
    return json.dumps(
        {
            "strategy": "HYBRID",
            "tasks": [{"name": "task_0", "goal": "Find fares", "type": "researcher"}],
        }
    )


def _search() -> RetrievedToolResult:
    return RetrievedToolResult(
        tool_name="web_search",
        arguments={"query": "Paris New York business class"},
        content=json.dumps(
            {
                "ok": True,
                "results": [
                    {"title": "Kayak", "url": KAYAK, "snippet": "Business from EUR 2,410."},
                    {"title": "Expedia", "url": EXPEDIA, "snippet": "Business from EUR 1,980."},
                ],
            }
        ),
    )


def _finding(claim: str, url: str) -> Finding:
    return Finding(claim=claim, source_url=url, date_or_period="", why_it_matters="fare")


def _worker(findings: list[Finding], retrieved: tuple[RetrievedToolResult, ...]) -> SubAgentResult:
    report = WorkerReport(working_notes="", findings=findings, gaps=[], tool_gap="")
    return SubAgentResult(
        task_id=uuid4(),
        spec_task="task_0",
        summary="",
        full_output="",
        tools_used=[r.tool_name for r in retrieved],
        token_count=0,
        duration_ms=1.0,
        success=True,
        report=report,
        report_schema="worker_report_v1",
        retrieved=retrieved,
    )


def _context(registry: SourceRegistry, worker: SubAgentResult) -> str:
    plan = _validate_plan_json(_plan_json())
    assert plan is not None
    return ExpansionController()._build_synthesis_context(
        plan=plan, sub_results=[worker], source_registry=registry, trace_id=TURN
    )


def test_two_retrieved_findings_register_and_the_digest_carries_both_markers() -> None:
    """AC-1: two findings from the worker's own results are two sources and two markers."""
    registry = SourceRegistry(turn_id=TURN)
    worker = _worker(
        [_finding("Kayak lists EUR 2,410", KAYAK), _finding("Expedia lists EUR 1,980", EXPEDIA)],
        (_search(),),
    )

    context = _context(registry, worker)

    sources = registry.sources()
    assert len(sources) == 2
    assert {s.referent for s in sources} == {KAYAK, EXPEDIA}
    for source in sources:
        assert f"[{source.identifier}]" in context


def test_an_invented_finding_url_gets_no_marker_in_the_digest() -> None:
    """AC-2 at the digest: the finding still renders, with its URL but with no marker."""
    registry = SourceRegistry(turn_id=TURN)
    invented = "https://www.airfrance.com/fares"
    worker = _worker([_finding("Air France lists EUR 3,000", invented)], (_search(),))

    context = _context(registry, worker)

    assert registry.sources() == ()
    assert invented in context
    assert CITATION_MARKER_PATTERN.search(context) is None


def test_a_tool_free_worker_creates_no_source() -> None:
    """AC-5: a worker that ran no tool has nothing a finding could cite."""
    registry = SourceRegistry(turn_id=TURN)
    worker = _worker([_finding("Kayak lists EUR 2,410", KAYAK)], ())

    context = _context(registry, worker)

    assert registry.sources() == ()
    assert CITATION_MARKER_PATTERN.search(context) is None


def test_a_text_reporting_worker_creates_no_source() -> None:
    """AC-5, the general worker: a text report has no findings, so nothing registers."""
    registry = SourceRegistry(turn_id=TURN)
    worker = SubAgentResult(
        task_id=uuid4(),
        spec_task="task_0",
        summary=f"I think {KAYAK} has fares.",
        full_output="",
        tools_used=[],
        token_count=0,
        duration_ms=1.0,
        success=True,
    )

    _context(registry, worker)

    assert registry.sources() == ()


def test_a_reply_citing_a_worker_marker_reaches_the_reader_as_a_link() -> None:
    """AC-3 end to end: the primary copies a digest marker, and the reader gets a link."""
    registry = SourceRegistry(turn_id=TURN)
    context = _context(registry, _worker([_finding("Kayak lists EUR 2,410", KAYAK)], (_search(),)))
    marker = CITATION_MARKER_PATTERN.search(context)
    assert marker is not None
    reply = f"| Airline | Fare | Source |\n|---|---|---|\n| AF | EUR 2,410 | {marker.group(0)} |"
    ctx = ExecutionContext(
        trace_id=TURN,
        session_id="session-fre442",
        user_message="Business class Paris to New York?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    ctx.final_reply = reply
    ctx.source_registry = registry
    ctx.messages = [{"role": "assistant", "content": reply}]

    _strip_markers_from_turn(ctx)

    assert ctx.final_reply is not None
    assert f"([kayak.com](<{KAYAK}>))" in ctx.final_reply
    assert CITATION_MARKER_PATTERN.search(ctx.final_reply) is None
    assert ctx.messages[0]["content"] == ctx.final_reply
