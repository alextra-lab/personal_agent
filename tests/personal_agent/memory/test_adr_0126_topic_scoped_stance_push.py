"""ADR-0126 D1/D2 (topic-scoped half)/D5 (push half)/D6 — AC-1, AC-5 (push half), AC-6.

Live-Neo4j behavioural proof (marked ``integration``; runs against the isolated test Neo4j
at :7688 — mirrors ``test_adr_0126_claims_pull.py``'s pattern for T3 and
``test_adr_0126_supersession_chain.py``'s pattern for T4).

**Deterministic entity recall, real stance retrieval.** A patched embedding makes an
entity *strong* in one fused-recall arm; it does not guarantee it survives fusion against
competing turn candidates — that is exactly the FRE-1021 mechanism this ADR names as a
known, accepted risk (D2), not something a test should depend on for determinism. Instead,
``MemoryService.query_memory`` (the method ``MemoryServiceAdapter.recall()`` calls) is
monkeypatched to return a directly constructed ``MemoryQueryResult`` for each probe. This
makes entity *recall* deterministic while keeping stance *retrieval* fully real against
live Neo4j via the real ``MemoryServiceAdapter.get_current_stances()`` ->
``MemoryService.query_current_stances()`` -> real Cypher. ``assemble_context()``, the
renderer, the inliner, and ``build_wire_messages()`` all run for real and unstubbed —
only the entity-ranking step (genuinely orthogonal to what T1 does) is deterministic by
construction instead of by hoping fusion cooperates.

Real end-to-end recall-ranking determinism (including FRE-1021's displacement mechanism)
is explicitly out of scope for this suite — that is what production observation and
FRE-1021's own measurement ticket own, not a unit proving stance enrichment is wired
correctly.

**Precondition semantics.** Every criterion below that depends on "the target entity was
recalled" asserts *recall candidacy* — an entity-kind record exists in the turn-evidence
admission record's ``items`` — not render *admission* (``.admitted``). An entity with an
empty description is still a legitimate recall candidate even though it will not render
(FRE-1010's empty-description filter), which is what AC-6's empty-affect fixture needs:
it must be a real candidate (proving the stance mechanism actually saw it) without being
forced to also render (which would make "no orphaned target name" un-checkable). See the
implementation plan (docs/superpowers/plans/2026-07-28-fre-1015-topic-scoped-stance-push.md)
for the full reasoning — this was a codex plan-review finding (2 BLOCKERs) fixed before
implementation, not discovered after.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import pytest_asyncio

from personal_agent.captains_log.turn_evidence import MemoryItemKind, build_turn_evidence
from personal_agent.memory.models import EntityNode, MemoryQueryResult, Stance
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
from personal_agent.memory.service import MemoryService
from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.types import Complexity, IntentResult, TaskType

pytestmark = pytest.mark.integration

_OWNER_UID = UUID("00000000-0000-0000-0000-00000000ad26")
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(days=30)


def _entity_node(name: str, description: str | None) -> EntityNode:
    """In-memory EntityNode for the stubbed query_memory return value only -- does not
    create a graph node. Use _create_entity alongside this for any test that also calls
    assert_stance, which requires the real :Entity node to already exist.
    """
    now = datetime.now(timezone.utc)
    return EntityNode(
        entity_id=name,
        name=name,
        entity_type="Concept",
        description=description,
        first_seen=now,
        last_seen=now,
    )


async def _create_entity(service: MemoryService, name: str) -> None:
    """Create the real :Entity node assert_stance requires (it MATCHes, never MERGEs,
    its target -- ADR-0126's Stance write path assumes the entity already exists).
    """
    assert service.driver is not None
    async with service.driver.session() as s:
        await s.run("MERGE (:Entity {name: $name})", name=name)


@pytest_asyncio.fixture
async def owner_service():
    """Connected MemoryService with a clean is_owner Person and no FRE1015_* residue."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")

    assert service.driver is not None
    async with service.driver.session() as s:
        await s.run("MATCH (e:Entity) WHERE e.name STARTS WITH 'FRE1015_' DETACH DELETE e")
        result = await s.run("MATCH (p:Person {is_owner: true}) RETURN count(p) AS n")
        record = await result.single()
        if record is None or record["n"] == 0:
            await s.run(
                "CREATE (:Person {user_id: $user_id, is_owner: true, name: 'FRE1015 Test Owner'})",
                user_id=str(_OWNER_UID),
            )

    yield service

    async with service.driver.session() as s:
        await s.run("MATCH (e:Entity) WHERE e.name STARTS WITH 'FRE1015_' DETACH DELETE e")
    await service.disconnect()


def _stub_entity_recall(
    monkeypatch: pytest.MonkeyPatch, service: MemoryService, *entities: EntityNode
) -> None:
    """Make entity recall deterministic: query_memory always returns exactly these entities.

    This is the seam ``MemoryServiceAdapter.recall()`` calls -- stubbing here means the
    real adapter and the real ``context.py`` entity-name-match path both run unstubbed;
    only the underlying Neo4j fused-ranking step is replaced.
    """
    monkeypatch.setattr(
        service,
        "query_memory",
        AsyncMock(
            return_value=MemoryQueryResult(
                conversations=[], entities=list(entities), relevance_scores={}
            )
        ),
    )


async def _run_turn(
    service: MemoryService, user_message: str, *, authenticated: bool = True
) -> tuple[list[dict[str, Any]], Any]:
    """Run assemble_context() + the real render/inline/wire pipeline for one turn.

    Returns (wire_messages, turn_evidence) -- wire_messages is what AC-1/AC-5/AC-6's
    "reaches the model" assertions check; turn_evidence.recall.items is what the
    entity-selection precondition checks.
    """
    from personal_agent.orchestrator.executor import (
        _inline_volatile_with_outcome,
        _render_memory_section_with_ids,
        build_wire_messages,
    )

    adapter = MemoryServiceAdapter(service)
    intent = IntentResult(
        task_type=TaskType.CONVERSATIONAL, complexity=Complexity.SIMPLE, confidence=0.9, signals=[]
    )
    result = await assemble_context(
        user_message=user_message,
        session_messages=[],
        intent=intent,
        memory_adapter=adapter,
        trace_id="fre1015-trace",
        user_id=_OWNER_UID,
        authenticated=authenticated,
    )

    memory_section, rendered_ids = _render_memory_section_with_ids(result.memory_context or [])
    final_messages, inline_outcome = _inline_volatile_with_outcome(result.messages, memory_section)
    wire = build_wire_messages(final_messages, "", "fre1015-trace")

    # result.recall_candidates is exactly what assemble_context() built internally via
    # build_recall_candidates -- reused here rather than recomputed, so this is the same
    # candidate set the real turn would carry.
    evidence = build_turn_evidence(
        candidates=result.recall_candidates,
        memory_context_present=result.memory_context is not None,
        rendered_identities=rendered_ids,
        inline_outcome=inline_outcome,
        session_facts_injected=result.session_facts_injected,
        wire_messages=wire,
        system_prompt="",
        user_message=user_message,
        skill_bodies=(),
        call_index=0,
        primary_call_count=1,
    )
    return wire, evidence


def _serialized(wire: list[dict[str, Any]]) -> str:
    return " ".join(str(m.get("content", "")) for m in wire)


def _memory_section_of(wire: list[dict[str, Any]]) -> str:
    """The <turn_context>...</turn_context> fenced block only -- excludes the user's own
    typed message, which follows the closing fence (_inline_volatile_with_outcome,
    executor.py:1318) and would otherwise make an "absent in any form" assertion trivially
    fail whenever the probe message itself mentions the entity by name.
    """
    serialized = _serialized(wire)
    start = serialized.find("<turn_context>")
    end = serialized.find("</turn_context>")
    if start == -1 or end == -1:
        return ""
    return serialized[start : end + len("</turn_context>")]


def _assert_entity_candidacy_or_skip(evidence: Any, target: str) -> None:
    """The entity-selection precondition (ADR-0126, FRE-1021 inheritance clause).

    Checks recall *candidacy* (an entity-kind record exists in the admission record's
    items), not render *admission* -- see the module docstring for why. Skips with an
    explicit message on failure: a precondition miss is INCONCLUSIVE, never a stance
    defect and never a silent pass.
    """
    holds = any(
        item.kind is MemoryItemKind.ENTITY and item.identity == target
        for item in evidence.recall.items
    )
    if not holds:
        pytest.skip(
            f"precondition failed: {target!r} not a recall candidate this turn — re-fixture"
        )


class TestAC1TopicScopedStanceReachesModelOnlyWhenEntityRecalled:
    @pytest.mark.asyncio
    async def test_positive_half_affect_present_when_entity_recalled(
        self, owner_service: MemoryService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _create_entity(owner_service, "FRE1015_Python")
        await owner_service.assert_stance(
            Stance(target="FRE1015_Python", affect="prefers over Java", observed_at=_T0)
        )
        _stub_entity_recall(
            monkeypatch,
            owner_service,
            _entity_node("FRE1015_Python", "a general-purpose programming language"),
        )

        wire, evidence = await _run_turn(owner_service, "Tell me about FRE1015_Python please")

        _assert_entity_candidacy_or_skip(evidence, "FRE1015_Python")
        assert "prefers over Java" in _serialized(wire)

    @pytest.mark.asyncio
    async def test_negative_half_affect_absent_when_entity_not_recalled(
        self, owner_service: MemoryService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The target entity's absence is asserted directly (not inferred from "some
        other entity was recalled instead") -- proving its absence is why the affect is
        missing, not a masked stance-layer defect.
        """
        await _create_entity(owner_service, "FRE1015_Python")
        await owner_service.assert_stance(
            Stance(target="FRE1015_Python", affect="prefers over Java", observed_at=_T0)
        )
        _stub_entity_recall(
            monkeypatch, owner_service, _entity_node("FRE1015_Unrelated", "an unrelated topic")
        )

        wire, evidence = await _run_turn(owner_service, "Tell me about FRE1015_Unrelated please")

        assert not any(
            item.kind is MemoryItemKind.ENTITY and item.identity == "FRE1015_Python"
            for item in evidence.recall.items
        )
        assert "prefers over Java" not in _serialized(wire)


class TestAC5PushHalfCurrentPresentSupersededAbsent:
    @pytest.mark.asyncio
    async def test_current_affect_present_superseded_absent(
        self, owner_service: MemoryService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors test_adr_0126_supersession_chain.py's Sorbet fixture (vague ->
        specific), proving the push half: only the current affect reaches the wire.
        """
        await _create_entity(owner_service, "FRE1015_Sorbet")
        ok1 = await owner_service.assert_stance(
            Stance(target="FRE1015_Sorbet", affect="prefers it", observed_at=_T0)
        )
        ok2 = await owner_service.assert_stance(
            Stance(
                target="FRE1015_Sorbet",
                affect="prefers a sorbet-leaning texture",
                observed_at=_T1,
            )
        )
        assert ok1 is True
        assert ok2 is True
        _stub_entity_recall(
            monkeypatch, owner_service, _entity_node("FRE1015_Sorbet", "a frozen dessert")
        )

        wire, evidence = await _run_turn(owner_service, "Tell me about FRE1015_Sorbet please")

        _assert_entity_candidacy_or_skip(evidence, "FRE1015_Sorbet")
        serialized = _serialized(wire)
        assert "prefers a sorbet-leaning texture" in serialized
        assert "prefers it" not in serialized


class TestAC6EmptyItemFilteredPopulatedControlArrives:
    @pytest.mark.asyncio
    async def test_empty_affect_produces_no_entry_in_any_form(
        self, owner_service: MemoryService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The target entity carries no description either, so it is a legitimate
        recall candidate (precondition holds) without being forced to render via the
        entity section -- making "no entry for it in any form" achievable rather than
        self-contradicting (see module docstring).
        """
        await _create_entity(owner_service, "FRE1015_BarrageRepublicain")
        await owner_service.assert_stance(
            Stance(target="FRE1015_BarrageRepublicain", affect="", observed_at=_T0)
        )
        _stub_entity_recall(
            monkeypatch, owner_service, _entity_node("FRE1015_BarrageRepublicain", None)
        )

        wire, evidence = await _run_turn(
            owner_service, "Tell me about FRE1015_BarrageRepublicain please"
        )

        _assert_entity_candidacy_or_skip(evidence, "FRE1015_BarrageRepublicain")
        assert "FRE1015_BarrageRepublicain" not in _memory_section_of(wire)

    @pytest.mark.asyncio
    async def test_whitespace_only_affect_produces_no_entry_in_any_form(
        self, owner_service: MemoryService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _create_entity(owner_service, "FRE1015_BarrageRepublicain")
        await owner_service.assert_stance(
            Stance(target="FRE1015_BarrageRepublicain", affect="   ", observed_at=_T0)
        )
        _stub_entity_recall(
            monkeypatch, owner_service, _entity_node("FRE1015_BarrageRepublicain", None)
        )

        wire, evidence = await _run_turn(
            owner_service, "Tell me about FRE1015_BarrageRepublicain please"
        )

        _assert_entity_candidacy_or_skip(evidence, "FRE1015_BarrageRepublicain")
        assert "FRE1015_BarrageRepublicain" not in _memory_section_of(wire)

    @pytest.mark.asyncio
    async def test_populated_control_arrives(
        self, owner_service: MemoryService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Separate turn, per the ADR's own check structure ("Then run a turn
        recalling a populated topic-scoped stance") -- proves suppressing the section
        entirely would not pass this half, since it explicitly requires presence.
        """
        await _create_entity(owner_service, "FRE1015_Comte")
        await owner_service.assert_stance(
            Stance(
                target="FRE1015_Comte",
                affect="prefers it as a cheese to keep eating",
                observed_at=_T0,
            )
        )
        _stub_entity_recall(
            monkeypatch, owner_service, _entity_node("FRE1015_Comte", "a French cheese")
        )

        wire, evidence = await _run_turn(owner_service, "Tell me about FRE1015_Comte please")

        _assert_entity_candidacy_or_skip(evidence, "FRE1015_Comte")
        assert "prefers it as a cheese to keep eating" in _serialized(wire)
