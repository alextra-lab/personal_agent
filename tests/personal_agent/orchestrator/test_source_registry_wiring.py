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
        tool_name="mcp_fetch_content",
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
    assert "mcp_fetch_content" in rendered
    assert "Ortiz packs bonito del norte" not in rendered
