"""Unit tests for the FRE-1348 provenance backfill orchestration (ADR-0098 Amendment A · A5).

These exercise the migration ALGORITHM against an in-memory fake graph — no Neo4j, no
Elasticsearch — so they run in ``make test`` as the CI-gating AC proof. The real Cypher
(:class:`_Neo4jGraph`) is exercised by
``test_migrate_fre1348_provenance_backfill_integration.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import pytest
from scripts.migrate_fre1348_provenance_backfill import (
    ClaimCandidate,
    EntityCandidate,
    ReconstructOutcome,
    _reconstruct,
    run_backfill,
)

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.memory.provenance import SourceRecord
from personal_agent.tools import get_default_registry

_RUN_ID = "fre1348-test"
_NOW = "2026-08-31T00:00:00+00:00"
_TS = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
_REGISTRY = get_default_registry()


def _capture(trace_id: str, *, tool_results: list[dict[str, object]] | None = None) -> TaskCapture:
    return TaskCapture(
        trace_id=trace_id,
        session_id="sess-1348",
        timestamp=_TS,
        user_message="research SafeCart",
        assistant_response="done",
        outcome="completed",
        user_id="00000000-0000-0000-0000-000000000000",
        tool_results=tool_results or [],
    )


def _fetch_url_result(url: str, output: str) -> dict[str, object]:
    return {
        "tool_name": "fetch_url",
        "success": True,
        "arguments": {"url": url},
        "output": output,
    }


def _web_search_result(query: str) -> dict[str, object]:
    return {
        "tool_name": "web_search",
        "success": True,
        "arguments": {"query": query},
        "output": "some search results",
    }


# ---------------------------------------------------------------------------
# _reconstruct — pure reconstruction logic
# ---------------------------------------------------------------------------


def test_reconstruct_missing_capture_when_none_given() -> None:
    outcome = _reconstruct(None, _REGISTRY, attribution="SafeCart")

    assert outcome.missing_capture is True
    assert outcome.matched_sources == []


def test_reconstruct_matches_and_returns_source_records_not_bare_ids() -> None:
    capture = _capture(
        "trace-1",
        tool_results=[
            _fetch_url_result("https://safecart.com", "SafeCart is a checkout platform.")
        ],
    )

    outcome = _reconstruct(capture, _REGISTRY, attribution="SafeCart")

    assert outcome.missing_capture is False
    assert len(outcome.matched_sources) == 1
    source = outcome.matched_sources[0]
    assert isinstance(source, SourceRecord)
    assert source.referent == "https://safecart.com"


def test_reconstruct_no_match_when_content_does_not_mention_it() -> None:
    capture = _capture(
        "trace-2",
        tool_results=[_fetch_url_result("https://example.com", "Nothing relevant here.")],
    )

    outcome = _reconstruct(capture, _REGISTRY, attribution="SafeCart")

    assert outcome.missing_capture is False
    assert outcome.matched_sources == []


def test_reconstruct_web_search_contributes_no_referent() -> None:
    """A2 scope boundary: only referent-declaring tools (fetch_url) become :Source candidates."""
    capture = _capture("trace-3", tool_results=[_web_search_result("SafeCart reviews")])

    outcome = _reconstruct(capture, _REGISTRY, attribution="SafeCart")

    assert outcome.missing_capture is False
    assert outcome.matched_sources == []


# ---------------------------------------------------------------------------
# FakeGraph + FakeCaptureIndex for run_backfill orchestration tests
# ---------------------------------------------------------------------------


class FakeGraph:
    """In-memory graph seam. Nodes are dicts carrying name/claim_id/trace_id/state."""

    def __init__(
        self,
        entities: list[dict[str, object]] | None = None,
        claims: list[dict[str, object]] | None = None,
        relationships_none_count: int = 0,
    ) -> None:
        self.entities = entities or []
        self.claims = claims or []
        self._relationships_none_count = relationships_none_count
        self.entity_writes: list[tuple[str, str, list[SourceRecord]]] = []
        self.claim_writes: list[tuple[str, str, list[SourceRecord]]] = []
        self.mark_relationships_none_calls = 0

    @staticmethod
    def _is_candidate(node: dict[str, object]) -> bool:
        state = node.get("provenance_state")
        return state is None or state not in ("provenanced", "none")

    async def fetch_entity_candidates(
        self, cursor: str | None, limit: int
    ) -> list[EntityCandidate]:
        candidates = sorted(
            (n for n in self.entities if self._is_candidate(n)), key=lambda n: n["eid"]
        )
        if cursor is not None:
            candidates = [n for n in candidates if n["eid"] > cursor]
        return [
            EntityCandidate(
                element_id=n["eid"], name=n["name"], originating_trace_id=n.get("trace_id")
            )
            for n in candidates[:limit]
        ]

    async def write_entity_provenanced(self, name: str, sources: Sequence[SourceRecord]) -> None:
        self.entity_writes.append(("provenanced", name, list(sources)))
        for n in self.entities:
            if n["name"] == name:
                n["provenance_state"] = "provenanced"

    async def write_entity_none(self, name: str) -> None:
        self.entity_writes.append(("none", name, []))
        for n in self.entities:
            if n["name"] == name and n.get("provenance_state") != "provenanced":
                n["provenance_state"] = "none"

    async def fetch_claim_candidates(self, cursor: str | None, limit: int) -> list[ClaimCandidate]:
        candidates = sorted(
            (n for n in self.claims if self._is_candidate(n)), key=lambda n: n["eid"]
        )
        if cursor is not None:
            candidates = [n for n in candidates if n["eid"] > cursor]
        return [
            ClaimCandidate(
                element_id=n["eid"],
                claim_id=n["claim_id"],
                content=n["content"],
                trace_id=n.get("trace_id"),
            )
            for n in candidates[:limit]
        ]

    async def write_claim_provenanced(self, claim_id: str, sources: Sequence[SourceRecord]) -> None:
        self.claim_writes.append(("provenanced", claim_id, list(sources)))
        for n in self.claims:
            if n["claim_id"] == claim_id:
                n["provenance_state"] = "provenanced"

    async def write_claim_none(self, claim_id: str) -> None:
        self.claim_writes.append(("none", claim_id, []))
        for n in self.claims:
            if n["claim_id"] == claim_id and n.get("provenance_state") != "provenanced":
                n["provenance_state"] = "none"

    async def mark_relationships_none(self) -> int:
        self.mark_relationships_none_calls += 1
        return self._relationships_none_count


async def _run(graph: FakeGraph, *, dry_run: bool = False):
    return await run_backfill(
        graph,
        capture_index={},
        tool_registry=_REGISTRY,
        es_client=None,
        run_id=_RUN_ID,
        now=_NOW,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# run_backfill orchestration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconstructed_and_missing_capture_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeGraph(
        entities=[
            {"eid": "e1", "name": "SafeCart", "trace_id": "trace-a"},
            {"eid": "e2", "name": "GhostCorp", "trace_id": "trace-missing"},
        ]
    )

    async def fake_reader(trace_ids, *, disk_index, es_client):
        if "trace-a" in trace_ids:
            return {
                "trace-a": _capture(
                    "trace-a",
                    tool_results=[_fetch_url_result("https://safecart.com", "SafeCart checkout.")],
                )
            }
        return {}

    monkeypatch.setattr(
        "scripts.migrate_fre1348_provenance_backfill.read_captures_by_trace_ids", fake_reader
    )

    report = await _run(graph)

    assert report.entities_total == 2
    assert report.entities_reconstructed == 1
    assert report.entities_none_missing_capture == 1
    assert report.entities_none_no_match == 0
    assert report.entities_errors == 0
    assert graph.entities[0]["provenance_state"] == "provenanced"
    assert graph.entities[1]["provenance_state"] == "none"


@pytest.mark.asyncio
async def test_dry_run_calls_no_write_method(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeGraph(entities=[{"eid": "e1", "name": "SafeCart", "trace_id": "trace-a"}])

    async def fake_reader(trace_ids, *, disk_index, es_client):
        return {
            "trace-a": _capture(
                "trace-a",
                tool_results=[_fetch_url_result("https://safecart.com", "SafeCart checkout.")],
            )
        }

    monkeypatch.setattr(
        "scripts.migrate_fre1348_provenance_backfill.read_captures_by_trace_ids", fake_reader
    )

    report = await _run(graph, dry_run=True)

    assert report.entities_reconstructed == 1
    assert graph.entity_writes == []
    assert graph.mark_relationships_none_calls == 0
    assert report.relationships_marked_none == 0


@pytest.mark.asyncio
async def test_reconstruct_error_counted_and_does_not_abort_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = FakeGraph(
        entities=[
            {"eid": "e1", "name": "Bad", "trace_id": "trace-a"},
            {"eid": "e2", "name": "Good", "trace_id": "trace-b"},
        ]
    )

    async def fake_reader(trace_ids, *, disk_index, es_client):
        return {
            "trace-a": _capture("trace-a"),
            "trace-b": _capture(
                "trace-b", tool_results=[_fetch_url_result("https://good.example", "Good is real.")]
            ),
        }

    def fake_reconstruct(capture, tool_registry, *, attribution, _real=_reconstruct):
        if attribution == "Bad":
            raise RuntimeError("boom")
        return _real(capture, tool_registry, attribution=attribution)

    monkeypatch.setattr(
        "scripts.migrate_fre1348_provenance_backfill.read_captures_by_trace_ids", fake_reader
    )
    monkeypatch.setattr(
        "scripts.migrate_fre1348_provenance_backfill._reconstruct", fake_reconstruct
    )

    report = await _run(graph)

    assert report.entities_total == 2
    assert report.entities_errors == 1
    assert report.entities_reconstructed == 1
    assert report.success is False
    assert (
        report.entities_reconstructed
        + report.entities_none_no_match
        + report.entities_none_missing_capture
        + report.entities_errors
        == report.entities_total
    )


@pytest.mark.asyncio
async def test_claims_bucketed_independently_of_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeGraph(
        entities=[{"eid": "e1", "name": "SafeCart", "trace_id": "trace-a"}],
        claims=[
            {
                "eid": "c1",
                "claim_id": "claim-1",
                "content": "The lease ends in June.",
                "trace_id": "trace-c",
            }
        ],
    )

    async def fake_reader(trace_ids, *, disk_index, es_client):
        out = {}
        if "trace-a" in trace_ids:
            out["trace-a"] = _capture(
                "trace-a",
                tool_results=[_fetch_url_result("https://safecart.com", "SafeCart checkout.")],
            )
        if "trace-c" in trace_ids:
            out["trace-c"] = _capture(
                "trace-c",
                tool_results=[
                    _fetch_url_result("https://lease.example", "The lease ends in June.")
                ],
            )
        return out

    monkeypatch.setattr(
        "scripts.migrate_fre1348_provenance_backfill.read_captures_by_trace_ids", fake_reader
    )

    report = await _run(graph)

    assert report.entities_reconstructed == 1
    assert report.claims_reconstructed == 1
    assert graph.claims[0]["provenance_state"] == "provenanced"
    assert (
        report.claims_reconstructed
        + report.claims_none_no_match
        + report.claims_none_missing_capture
        + report.claims_errors
        == report.claims_total
    )


@pytest.mark.asyncio
async def test_mark_relationships_none_called_once_bulk(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeGraph(relationships_none_count=42)

    async def fake_reader(trace_ids, *, disk_index, es_client):
        return {}

    monkeypatch.setattr(
        "scripts.migrate_fre1348_provenance_backfill.read_captures_by_trace_ids", fake_reader
    )

    report = await _run(graph)

    assert graph.mark_relationships_none_calls == 1
    assert report.relationships_marked_none == 42
