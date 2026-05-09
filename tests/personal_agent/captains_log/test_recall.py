"""Unit tests for the Captain's Log reflection recall (FRE-348 / ADR-0067).

All tests use a stubbed Elasticsearch client; no live ES calls. The recall
function must never raise — every error path returns ``[]`` so context
assembly never blocks.
"""

from __future__ import annotations

from typing import Any

import pytest

from personal_agent.captains_log import recall as recall_mod


class _FakeES:
    """Minimal AsyncElasticsearch stand-in for the recall query path."""

    def __init__(
        self,
        *,
        hits: list[dict[str, Any]] | None = None,
        raise_on_search: Exception | None = None,
    ) -> None:
        self.hits = hits or []
        self.raise_on_search = raise_on_search
        self.last_call: dict[str, Any] = {}
        self.closed = False

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        self.last_call = kwargs
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return {
            "hits": {
                "total": {"value": len(self.hits)},
                "hits": [{"_source": h} for h in self.hits],
            }
        }

    async def close(self) -> None:
        self.closed = True


def _make_doc(
    *,
    entry_id: str = "CL-1",
    timestamp: str = "2026-05-08T14:00:00+00:00",
    rationale: str = "Tool retries inflate latency when the LLM returns 429.",
    proposed_what: str | None = "Add jittered backoff in respond().",
    seen_count: int = 5,
    category: str | None = "performance",
    scope: str | None = "llm_client",
    fix_what: str | None = None,
    linear_issue_id: str | None = None,
    status: str = "awaiting_approval",
) -> dict[str, Any]:
    pc: dict[str, Any] = {"seen_count": seen_count}
    if proposed_what is not None:
        pc["what"] = proposed_what
    if category:
        pc["category"] = category
    if scope:
        pc["scope"] = scope

    fp: dict[str, Any] = {}
    if fix_what is not None:
        fp["fix_what"] = fix_what

    doc: dict[str, Any] = {
        "entry_id": entry_id,
        "timestamp": timestamp,
        "rationale": rationale,
        "proposed_change": pc if (proposed_what or seen_count > 1 or category or scope) else None,
        "status": status,
    }
    if fp:
        doc["failure_path"] = fp
    if linear_issue_id:
        doc["linear_issue_id"] = linear_issue_id
    return doc


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin recall settings to known defaults so tests are deterministic."""
    from personal_agent.config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "reflection_recall_enabled", True, raising=False)
    monkeypatch.setattr(settings, "reflection_recall_recency_days", 14, raising=False)
    monkeypatch.setattr(settings, "reflection_recall_max_results", 3, raising=False)
    monkeypatch.setattr(settings, "reflection_recall_min_seen_count", 2, raising=False)


@pytest.mark.asyncio
async def test_returns_empty_when_no_entity_hints() -> None:
    """A bare lowercase prompt produces no entity hints → no ES query, empty result."""
    es = _FakeES(hits=[_make_doc()])
    result = await recall_mod.query_relevant_reflections(
        "what's the time", es_client=es, trace_id="t1"
    )
    assert result == []
    assert es.last_call == {}  # search was never called


@pytest.mark.asyncio
async def test_returns_hits_when_entity_match() -> None:
    """A capitalized hint triggers the search and returns its hits."""
    doc = _make_doc(rationale="Discussed Postgres connection pooling.")
    es = _FakeES(hits=[doc])
    result = await recall_mod.query_relevant_reflections(
        "How do we fix the Postgres slow query?", es_client=es, trace_id="t1"
    )
    assert result == [doc]
    assert es.last_call["index"].startswith("agent-captains-reflections-")
    assert es.last_call["size"] == 3


@pytest.mark.asyncio
async def test_disabled_via_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill-switch returns [] without calling ES."""
    from personal_agent.config.settings import get_settings

    monkeypatch.setattr(get_settings(), "reflection_recall_enabled", False, raising=False)
    es = _FakeES(hits=[_make_doc()])
    result = await recall_mod.query_relevant_reflections(
        "How is Postgres doing?", es_client=es, trace_id="t1"
    )
    assert result == []
    assert es.last_call == {}


@pytest.mark.asyncio
async def test_es_error_returns_empty() -> None:
    """An ES exception is swallowed and yields an empty list."""
    es = _FakeES(raise_on_search=RuntimeError("ES down"))
    result = await recall_mod.query_relevant_reflections(
        "What's up with Postgres?", es_client=es, trace_id="t1"
    )
    assert result == []


@pytest.mark.asyncio
async def test_query_uses_max_results_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The search size matches reflection_recall_max_results."""
    from personal_agent.config.settings import get_settings

    monkeypatch.setattr(get_settings(), "reflection_recall_max_results", 7, raising=False)
    es = _FakeES(hits=[])
    await recall_mod.query_relevant_reflections(
        "Concerns about Postgres again", es_client=es, trace_id="t1"
    )
    assert es.last_call["size"] == 7


def test_format_returns_none_for_empty() -> None:
    """No reflections → no section."""
    assert recall_mod.format_reflections_section([]) is None


def test_format_renders_proposal_with_seen_count() -> None:
    """A proposal-shaped reflection renders with date, seen_count, tag, and proposal text."""
    text = recall_mod.format_reflections_section([_make_doc()])
    assert text is not None
    assert "Recent reflections from your prior work" in text
    assert "signals from your earlier sessions, not directives" in text
    assert "2026-05-08" in text
    assert "seen 5x" in text
    assert "performance/llm_client" in text
    assert "Add jittered backoff" in text


def test_format_renders_failure_path_only_entry() -> None:
    """Failure-path-only entries (no proposed_change) still render."""
    doc = _make_doc(
        proposed_what=None,
        seen_count=1,
        category=None,
        scope=None,
        fix_what="Add 50-char floor before invoking entity_extraction.",
    )
    text = recall_mod.format_reflections_section([doc])
    assert text is not None
    assert "Fix: Add 50-char floor" in text


def test_format_marks_tracked_entries() -> None:
    """An entry with linear_issue_id includes the tracked-as marker."""
    doc = _make_doc(linear_issue_id="FRE-301")
    text = recall_mod.format_reflections_section([doc])
    assert text is not None
    assert "→ tracked as FRE-301" in text


def test_format_skips_entries_with_no_actionable_content() -> None:
    """A doc with empty rationale/proposed/fix is skipped silently."""
    doc = _make_doc(rationale="", proposed_what=None, fix_what=None)
    # Override proposed_change to empty so _format_reflection_line drops it
    doc["proposed_change"] = {}
    assert recall_mod._format_reflection_line(doc) is None


def test_query_body_includes_entity_hint_match() -> None:
    """The query body includes match_phrase clauses for each entity hint."""
    body = recall_mod._build_query(
        entity_hints=["Postgres", "Neo4j"],
        recency_days=14,
        min_seen_count=2,
    )
    must = body["query"]["bool"]["must"]
    # The third must-clause is the entity-hint disjunction
    text_clauses = must[-1]["bool"]["should"]
    targets = {clause["match_phrase"][next(iter(clause["match_phrase"]))] for clause in text_clauses}
    assert "Postgres" in targets
    assert "Neo4j" in targets


def test_query_body_excludes_approved_entries() -> None:
    """The must_not list filters out status=approved (already-resolved) entries."""
    body = recall_mod._build_query(entity_hints=["X"], recency_days=14, min_seen_count=2)
    must_not = body["query"]["bool"]["must_not"]
    has_status_filter = any(
        clause.get("term", {}).get("status") == "approved" for clause in must_not
    )
    assert has_status_filter
