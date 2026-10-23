"""The source registry as the executor populates it (ADR-0138, FRE-1280).

The unit tests in ``tests/personal_agent/grounding/`` prove the registry's own rules. These
prove the executor actually feeds it — a registry with perfect rules and no wiring records
nothing, and AC-1 is a claim about what a *turn* holds, not about what a constructor does.
"""

from __future__ import annotations

import json
from typing import Any

from personal_agent.captains_log.turn_evidence import (
    AssembledContextRecord,
    EvidenceState,
    RecallAdmissionRecord,
    TurnEvidence,
)
from personal_agent.grounding.source_registry import SourceKind, SourceRegistry
from personal_agent.orchestrator.executor import (
    _log_source_registry_snapshot,
    _register_admitted_memory_sources,
    _register_tool_source,
    _with_citation_marker,
    _with_entry_sources,
)
from personal_agent.orchestrator.types import ExecutionContext

TRACE_ID = "trace-wiring-1280"


def _context(**overrides: Any) -> ExecutionContext:
    """An execution context with a live registry, as ``execute_task`` builds one."""
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel

    ctx = ExecutionContext(
        session_id="session-1280",
        trace_id=TRACE_ID,
        user_message="Which tinned tuna should I buy in France?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        **overrides,
    )
    ctx.source_registry = SourceRegistry(turn_id=ctx.trace_id)
    ctx.source_registry.register_user_message(ctx.user_message)
    return ctx


def _turn_evidence(admitted: list[str]) -> TurnEvidence:
    """A minimal evidence record naming which memory identities were admitted."""
    return TurnEvidence(
        recall=RecallAdmissionRecord(
            state=EvidenceState.PRESENT, candidate_count=len(admitted), admitted_count=len(admitted)
        ),
        assembled_context=AssembledContextRecord(
            state=EvidenceState.PRESENT,
            message_count=1,
            system_prompt_chars=0,
            memory_identities=admitted,
        ),
    )


def test_all_four_kinds_registered_in_one_turn() -> None:
    """AC-1 — a turn exercising every D2 source kind enumerates all of them.

    Every retrieved item is asserted individually, not one representative per kind: an
    implementation registering the first item of each kind would satisfy a kind-set check
    while silently losing half the sources a citation could resolve to.
    """
    ctx = _context()
    ctx.memory_context = [
        {"type": "entity", "name": "Ortiz", "description": "A Spanish cannery."},
        {"type": "entity", "name": "Nardin", "description": "A Basque cannery."},
        {"type": "entity", "name": "Dropped", "description": "Never admitted."},
    ]
    ctx.turn_evidence = _turn_evidence(["Ortiz", "Nardin"])

    _register_admitted_memory_sources(ctx)
    _register_tool_source(
        ctx,
        tool_name="web_search",
        arguments={"query": "best tinned tuna france"},
        content=json.dumps({"results": [{"content": "Ortiz is sold in Biarritz."}]}),
        success=True,
    )
    _register_tool_source(
        ctx,
        tool_name="get_library_docs",
        arguments={"library": "httpx", "topic": "timeouts"},
        content="AsyncClient accepts a timeout argument.",
        success=True,
    )

    assert ctx.source_registry is not None
    sources = ctx.source_registry.sources()

    assert {source.kind for source in sources} == {
        SourceKind.USER,
        SourceKind.MEMORY,
        SourceKind.TOOL,
        SourceKind.DOCUMENTATION,
    }
    # user + two admitted memory items + web_search + get_library_docs
    assert len(sources) == 5
    assert len({source.identifier for source in sources}) == 5


def test_dropped_memory_item_is_not_a_source() -> None:
    """Admission is read from the ADR-0125 record, not re-decided here.

    An item recall offered but the turn dropped never reached the model, so the turn
    cannot cite it — and a registry that disagreed with the evidence record about that
    would put two contradicting accounts of one turn in the corpus.
    """
    ctx = _context()
    ctx.memory_context = [
        {"type": "entity", "name": "Ortiz", "description": "A Spanish cannery."},
        {"type": "entity", "name": "Dropped", "description": "Never admitted."},
    ]
    ctx.turn_evidence = _turn_evidence(["Ortiz"])

    _register_admitted_memory_sources(ctx)

    assert ctx.source_registry is not None
    labels = {source.label for source in ctx.source_registry.sources()}
    assert "Ortiz" in labels
    assert "Dropped" not in labels


def test_laundered_tool_result_is_not_registered_through_the_executor() -> None:
    """AC-2 at the wiring level — the arguments must reach the registry, not just content.

    If the executor passed only ``content``, the registry could not tell a fetched page
    from the model's own words echoed by a shell, and this would register.
    """
    ctx = _context()

    _register_tool_source(
        ctx,
        tool_name="bash",
        arguments={"command": "printf 'Paris has 9 million residents'"},
        content="Paris has 9 million residents",
        success=True,
    )

    assert ctx.source_registry is not None
    assert [source.kind for source in ctx.source_registry.sources()] == [SourceKind.USER]


def test_failed_tool_call_is_not_registered() -> None:
    """A failed call retrieved nothing, so there is nothing for a citation to reach."""
    ctx = _context()

    _register_tool_source(
        ctx,
        tool_name="web_search",
        arguments={"query": "tinned tuna"},
        content="",
        success=False,
    )

    assert ctx.source_registry is not None
    assert [source.kind for source in ctx.source_registry.sources()] == [SourceKind.USER]


def test_registration_helpers_no_op_without_a_registry() -> None:
    """Sub-agent paths never enter ``execute_task`` and carry no registry.

    The helpers must degrade to nothing rather than raise: a missing registry costs a
    citation, an exception would cost the turn.
    """
    ctx = _context()
    ctx.source_registry = None
    ctx.memory_context = [{"type": "entity", "name": "Ortiz", "description": "A cannery."}]
    ctx.turn_evidence = _turn_evidence(["Ortiz"])

    _register_admitted_memory_sources(ctx)
    _register_tool_source(
        ctx, tool_name="web_search", arguments={"query": "x"}, content="{}", success=True
    )
    _log_source_registry_snapshot(ctx)


def test_snapshot_failure_does_not_escape_into_the_turn() -> None:
    """The seeded negative for the guard, because it runs from a turn-scoped `finally`.

    An exception escaping here propagates past ``return ctx`` in ``execute_task`` and is
    caught by ``execute_task_safe``, which would report a turn that actually succeeded as
    failed. A clean tree proves nothing about a guard; this makes the registry raise.
    """

    class _ExplodingRegistry:
        def sources(self) -> tuple[object, ...]:
            raise RuntimeError("registry unavailable")

    ctx = _context()
    ctx.source_registry = _ExplodingRegistry()  # type: ignore[assignment]

    _log_source_registry_snapshot(ctx)  # must not raise


def test_register_tool_source_returns_the_minted_identifier() -> None:
    """FRE-1296: the caller needs the identifier to splice it into the model's content.

    FRE-1280 registered the source but discarded the return value entirely — the only
    consumer was a debug log on the inadmissible path. Nothing could ever render it.
    """
    ctx = _context()

    identifier = _register_tool_source(
        ctx,
        tool_name="web_search",
        arguments={"query": "best tinned tuna france"},
        content=json.dumps({"results": [{"content": "Ortiz is sold in Biarritz."}]}),
        success=True,
    )

    assert identifier is not None
    assert ctx.source_registry is not None
    assert ctx.source_registry.resolve(identifier) is not None


def test_register_tool_source_returns_none_when_inadmissible() -> None:
    ctx = _context()

    identifier = _register_tool_source(
        ctx,
        tool_name="bash",
        arguments={"command": "printf 'Paris has 9 million residents'"},
        content="Paris has 9 million residents",
        success=True,
    )

    assert identifier is None


def test_register_tool_source_returns_none_without_a_registry() -> None:
    ctx = _context()
    ctx.source_registry = None

    identifier = _register_tool_source(
        ctx, tool_name="web_search", arguments={"query": "x"}, content="{}", success=True
    )

    assert identifier is None


def test_snapshot_logs_identifiers_without_content(caplog: Any) -> None:
    """The observable surface for AC-1, and it must not carry retrieved text.

    Content can hold anything the web or the graph returned, including PII; the snapshot
    is an index, not a copy.
    """
    ctx = _context()
    _register_tool_source(
        ctx,
        tool_name="fetch_url",
        arguments={"url": "https://example.com/tuna"},
        content="Ortiz packs bonito del norte in olive oil.",
        success=True,
    )

    with caplog.at_level("INFO"):
        _log_source_registry_snapshot(ctx)

    snapshot = next(
        record for record in caplog.records if "source_registry_snapshot" in record.getMessage()
    )
    rendered = snapshot.getMessage()
    assert "fetch_url" in rendered
    assert "Ortiz packs bonito del norte" not in rendered


# ── FRE-1662: a primary web_search gives each entry its own marker ───────────────────────

_SEARCH_ARGS = {"query": "best tinned tuna france"}
_ORTIZ = "https://www.conservasortiz.com/en/"
_BIARRITZ = "https://www.biarritz.example/epicerie"


def _search_content() -> str:
    """A ``web_search`` result as ``render_content`` hands it to the executor."""
    return json.dumps(
        {
            "ok": True,
            "answers": [{"engine": "wikidata", "answer": "Ortiz was founded in 1891."}],
            "results": [
                {"title": "Ortiz", "url": _ORTIZ, "snippet": "Bonito del norte in olive oil."},
                {"title": "Epicerie", "url": _BIARRITZ, "snippet": "Ortiz is sold in Biarritz."},
                {"title": "No link", "url": "", "snippet": "Tuna prices vary."},
            ],
            "result_count": 3,
            "query": _SEARCH_ARGS["query"],
        }
    )


def _cite_search(ctx: ExecutionContext, content: str, *, success: bool = True) -> str:
    """Run the phase-3 citation steps the executor runs on one tool result."""
    identifier = _register_tool_source(
        ctx, tool_name="web_search", arguments=_SEARCH_ARGS, content=content, success=success
    )
    if identifier is None:
        return content
    return _with_entry_sources(
        ctx,
        tool_name="web_search",
        arguments=_SEARCH_ARGS,
        content=content,
        call_identifier=identifier,
    )


def test_the_model_reads_one_marker_per_entry_in_valid_json() -> None:
    """FRE-1662 AC-3: each url-bearing entry carries a marker that resolves to its url."""
    ctx = _context()
    assert ctx.source_registry is not None

    content = _cite_search(ctx, _search_content())

    parsed = json.loads(content)
    results = parsed["results"]
    for entry in results[:2]:
        marker = entry["_citation"]
        source = ctx.source_registry.resolve(marker.strip("[]"))
        assert source is not None
        assert source.referent == entry["url"]
    assert "_citation" not in results[2]
    # The call-level marker sits only beside the answers, so a result fact has one marker
    # to copy: its entry's. The answers stay citable.
    assert "_citation" not in parsed
    call_level = ctx.source_registry.resolve(parsed["answers"][0]["_citation"].strip("[]"))
    assert call_level is not None and call_level.referent is None


def test_an_entry_source_holds_no_marker_text() -> None:
    """Registering before marking keeps a marker out of any registered content."""
    ctx = _context()
    assert ctx.source_registry is not None

    _cite_search(ctx, _search_content())

    assert all("_citation" not in s.content for s in ctx.source_registry.sources())


def test_a_failed_search_registers_no_entries() -> None:
    ctx = _context()
    assert ctx.source_registry is not None

    content = _cite_search(ctx, _search_content(), success=False)

    assert content == _search_content()
    assert all(s.kind is SourceKind.USER for s in ctx.source_registry.sources())


def test_a_non_search_tool_gets_no_entry_markers() -> None:
    """A ``url`` field in fetched JSON is a link the page carries, not a retrieval."""
    ctx = _context()
    content = json.dumps({"ok": True, "items": [{"url": _ORTIZ, "text": "Ortiz"}]})
    marked = _with_entry_sources(
        ctx,
        tool_name="fetch_url",
        arguments={"url": _BIARRITZ},
        content=content,
        call_identifier="S2@0123456789abcdef",
    )

    assert marked == _with_citation_marker(content, "S2@0123456789abcdef")
    assert "_citation" not in json.loads(marked)["items"][0]


def test_an_entry_registration_error_keeps_the_entries_already_marked(monkeypatch: Any) -> None:
    """A failure on the second entry must not hide the first entry's marker.

    The first entry's source is already admitted. If its marker were dropped, the turn would
    count a source the model never saw (ADR-0139 D1).
    """
    ctx = _context()
    registry = ctx.source_registry
    assert registry is not None
    real = registry.register_result_entry
    calls = 0

    def _second_fails(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("registry failure")
        return real(**kwargs)

    monkeypatch.setattr(registry, "register_result_entry", _second_fails)

    content = _with_entry_sources(
        ctx,
        tool_name="web_search",
        arguments=_SEARCH_ARGS,
        content=_search_content(),
        call_identifier="S2@0123456789abcdef",
    )

    results = json.loads(content)["results"]
    first = registry.resolve(results[0]["_citation"].strip("[]"))
    assert first is not None and first.referent == _ORTIZ
    assert "_citation" not in results[1]


def test_an_entry_registration_error_on_the_first_entry_keeps_the_call_marker(
    monkeypatch: Any,
) -> None:
    ctx = _context()
    assert ctx.source_registry is not None

    def _boom(**_: object) -> None:
        raise RuntimeError("registry failure")

    monkeypatch.setattr(ctx.source_registry, "register_result_entry", _boom)
    content = _search_content()

    assert _with_entry_sources(
        ctx,
        tool_name="web_search",
        arguments=_SEARCH_ARGS,
        content=content,
        call_identifier="S2@0123456789abcdef",
    ) == _with_citation_marker(content, "S2@0123456789abcdef")


def test_snapshot_shows_each_entry_url(caplog: Any) -> None:
    """FRE-1662 AC-5's live surface: the snapshot names each entry's url."""
    ctx = _context()
    _cite_search(ctx, _search_content())

    with caplog.at_level("INFO"):
        _log_source_registry_snapshot(ctx)

    snapshot = next(
        record for record in caplog.records if "source_registry_snapshot" in record.getMessage()
    )
    rendered = snapshot.getMessage()
    assert _ORTIZ in rendered
    assert _BIARRITZ in rendered
    assert "Bonito del norte" not in rendered


def test_a_url_only_in_the_query_gets_no_marker_in_the_model_content() -> None:
    """FRE-1662 AC-2 through the executor: the echoed query is not an entry."""
    ctx = _context()
    assert ctx.source_registry is not None
    planted = "https://invented.example/tuna-ranking"
    content = json.dumps(
        {
            "ok": True,
            "results": [{"title": "Ortiz", "url": _ORTIZ, "snippet": "Bonito del norte."}],
            "query": planted,
        }
    )
    arguments = {"query": planted}
    identifier = _register_tool_source(
        ctx, tool_name="web_search", arguments=arguments, content=content, success=True
    )
    assert identifier is not None

    marked = _with_entry_sources(
        ctx,
        tool_name="web_search",
        arguments=arguments,
        content=content,
        call_identifier=identifier,
    )

    assert json.loads(marked)["query"] == planted
    referents = [s.referent for s in ctx.source_registry.sources() if s.referent]
    assert referents == [_ORTIZ]


def test_an_answer_that_is_also_an_entry_keeps_its_own_marker() -> None:
    """The call-level marker never overwrites an entry's admitted marker."""
    ctx = _context()
    assert ctx.source_registry is not None
    content = json.dumps(
        {
            "ok": True,
            "answers": [{"engine": "wikidata", "answer": "Ortiz, 1891.", "url": _ORTIZ}],
            "results": [{"title": "Epicerie", "url": _BIARRITZ, "snippet": "Sold here."}],
        }
    )

    marked = json.loads(
        _with_entry_sources(
            ctx,
            tool_name="web_search",
            arguments=_SEARCH_ARGS,
            content=content,
            call_identifier="S2@0123456789abcdef",
        )
    )

    answer_source = ctx.source_registry.resolve(marked["answers"][0]["_citation"].strip("[]"))
    assert answer_source is not None and answer_source.referent == _ORTIZ
