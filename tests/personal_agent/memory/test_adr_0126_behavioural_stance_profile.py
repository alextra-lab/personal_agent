"""ADR-0126 D2 (behavioural half)/D3/AC-7 — AC-2, AC-3, AC-7 (T2, FRE-1017).

Live-Neo4j behavioural proof (marked ``integration``; runs against the isolated test Neo4j
at :7688 — mirrors ``test_adr_0126_topic_scoped_stance_push.py``'s pattern for T1 and
``test_adr_0126_supersession_chain.py``'s named-entity fixture pattern).

**Two kinds of fixture in this suite, and why both exist.** A first draft of this suite used
only synthetic, monkeypatched curated-target names. Codex's plan review (2026-07-31) caught
that this makes AC-2 vacuous with respect to the *actual shipped* curated set: a suite that
only ever monkeypatches ``CURATED_BEHAVIOURAL_STANCE_TARGETS`` never proves the real constant
works — it could be empty, misspelled, or point at nonexistent entities and stay green. So
this suite carries both:

- A monkeypatched, synthetic-name suite (``TestAC2AC3StandingBehaviouralAlwaysPresent``) that
  proves the *mechanism* — the injector fires regardless of recall, the topic-scoped surface
  stays independent — without depending on what the real curated set happens to contain.
- A real, unmonkeypatched-constant test (``TestAC2RealProductionCuratedSet``) that proves the
  *actual shipped* ``CURATED_BEHAVIOURAL_STANCE_TARGETS`` reaches the wire end to end.

**Bare-entity seeding, not ``_seed_discussed_entity``.** ``assert_stance``'s Cypher is
``MATCH (c:Entity {name: $target})`` — it creates nothing itself, and returns ``False``
(logged, skipped) when the target does not already exist. A first draft of this suite tried
to seed via ``assert_stance`` alone against synthetic names with no backing node, which
silently wrote nothing. ``_seed_bare_entity`` below creates the ``:Entity`` directly via raw
Cypher (mirrors ``test_adr_0126_supersession_chain.py:56``'s exact precedent) — no
``:Turn``/``:DISCUSSES`` edge, so the target exists for ``assert_stance`` but is never a
recall candidate.

**AC-7's byte measurement is differential, against the JSON-serialized wire.** ADR-0126 fixes
the observation point as "the actual serialized provider request", and AC-7's own wording asks
for "the byte length the behavioural layer *contributes*" — a marginal measurement, not an
absolute one. ``_wire_json_bytes`` below measures ``len(json.dumps(wire,
ensure_ascii=False).encode("utf-8"))`` — the JSON encoding of the exact message list
``build_wire_messages`` hands to the HTTP client layer — and the AC-7 test takes it
differentially (curated set present vs. an ablated empty-tuple baseline on the identical
turn), which is immune to exactly how the section renders/escapes and reflects real JSON
overhead a raw joined-string measurement would miss.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio

import personal_agent.request_gateway.context as context_module
from personal_agent.captains_log.turn_evidence import (
    DropReason,
    MemoryItemKind,
    build_turn_evidence,
)
from personal_agent.memory.models import Stance
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
from personal_agent.memory.service import MemoryService
from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.types import Complexity, IntentResult, TaskType

pytestmark = pytest.mark.integration

_OWNER_UID = UUID("00000000-0000-0000-0000-00000000ad26")
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Every synthetic name this suite ever seeds, cleaned up before and after (mirrors
# test_adr_0126_supersession_chain.py's explicit named-entity cleanup pattern, not a
# session-id scan -- these targets carry no :Turn to scope by).
_SYNTHETIC_NAMES = (
    "FRE1017_StandingOne",
    "FRE1017_StandingTwo",
    "FRE1017_StandingThree",
    "FRE1017_TopicScoped",
    "FRE1017_ByteBoundOne",
    "FRE1017_ByteBoundTwo",
)


@pytest_asyncio.fixture
async def owner_service():
    """Connected MemoryService with a clean is_owner Person and no residue from this
    suite's synthetic names *or* the real, unmonkeypatched curated-target names (the
    latter cleanup only ever touches the isolated :7688 test substrate, never
    cloud-sim-neo4j / production).
    """
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")

    assert service.driver is not None
    names_to_clean = list(_SYNTHETIC_NAMES) + list(
        context_module.CURATED_BEHAVIOURAL_STANCE_TARGETS
    )

    async def _cleanup() -> None:
        assert service.driver is not None
        async with service.driver.session() as s:
            await s.run(
                "MATCH (:Person {is_owner: true})-[r:HAS_STANCE]->(e:Entity)"
                " WHERE e.name IN $names DELETE r",
                names=names_to_clean,
            )
            await s.run(
                "MATCH (e:Entity) WHERE e.name IN $names DETACH DELETE e",
                names=names_to_clean,
            )

    await _cleanup()
    async with service.driver.session() as s:
        result = await s.run("MATCH (p:Person {is_owner: true}) RETURN count(p) AS n")
        record = await result.single()
        if record is None or record["n"] == 0:
            await s.run(
                "CREATE (:Person {user_id: $user_id, is_owner: true, name: 'FRE1017 Test Owner'})",
                user_id=str(_OWNER_UID),
            )

    yield service

    await _cleanup()
    await service.disconnect()


async def _seed_bare_entity(service: MemoryService, name: str) -> None:
    """A bare :Entity, no Turn/DISCUSSES edge -- exists for assert_stance's
    ``MATCH (c:Entity {name: $target})`` precondition (which CREATEs nothing itself
    and returns False when the target is absent) without ever being a recall
    candidate. Mirrors test_adr_0126_supersession_chain.py:56's exact precedent.
    """
    assert service.driver is not None
    async with service.driver.session() as s:
        await s.run("MERGE (:Entity {name: $name, class: 'World'})", name=name)


async def _run_turn(
    service: MemoryService, user_message: str, *, authenticated: bool = True
) -> tuple[list[dict[str, Any]], Any]:
    """Run assemble_context() + the real render/inline/wire pipeline for one turn.

    Mirrors test_adr_0126_topic_scoped_stance_push.py's ``_run_turn`` exactly.
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
        trace_id="fre1017-trace",
        user_id=_OWNER_UID,
        authenticated=authenticated,
    )

    memory_section, rendered_ids = _render_memory_section_with_ids(result.memory_context or [])
    final_messages, inline_outcome = _inline_volatile_with_outcome(result.messages, memory_section)
    wire = build_wire_messages(final_messages, "", "fre1017-trace")

    evidence = build_turn_evidence(
        candidates=result.recall_candidates,
        memory_context_present=result.memory_context is not None,
        rendered_identities=rendered_ids,
        inline_outcome=inline_outcome,
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


def _wire_json_bytes(wire: list[dict[str, Any]]) -> int:
    """A defensible proxy for 'the serialized provider request' (ADR-0126's fixed
    observation point): the JSON encoding of the exact message list
    build_wire_messages hands to the HTTP client layer -- unlike a raw joined-string
    measurement, this reflects JSON escaping overhead. Used differentially (see
    module docstring).
    """
    return len(json.dumps(wire, ensure_ascii=False).encode("utf-8"))


# The eight RECALL_* members (FRE-1060, FRE-1114) name a producer-side gate that
# discarded a candidate before context assembly. Mirrors
# test_adr_0126_topic_scoped_stance_push.py's precondition helper exactly.
_PRODUCER_DISCARD_REASONS = frozenset(
    {
        DropReason.RECALL_EMPTY_DESCRIPTION,
        DropReason.RECALL_SCORE_THRESHOLD,
        DropReason.RECALL_CANDIDATE_CAP,
        DropReason.RECALL_ITEM_CAP,
        DropReason.RECALL_SCORE_FLOOR,
        DropReason.RECALL_SCORE_GAP,
        DropReason.RECALL_ITEM_OVERSIZED,
        DropReason.RECALL_TOKEN_BUDGET,
    }
)


def _is_undropped_entity_candidate(item: Any, target: str) -> bool:
    return (
        item.kind is MemoryItemKind.ENTITY
        and item.identity == target
        and item.drop_reason not in _PRODUCER_DISCARD_REASONS
    )


_PROBE_MESSAGE = "What is a good approach to sorting a small array quickly?"


class TestAC2AC3StandingBehaviouralAlwaysPresent:
    """The mechanism, proven with synthetic, monkeypatched curated targets -- proves
    the injector fires regardless of recall and the topic-scoped surface stays
    independent, without depending on what the real curated set contains.
    """

    @pytest.mark.asyncio
    async def test_curated_affects_present_topic_scoped_absent_on_probe_turn(
        self, owner_service: MemoryService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        names = ("FRE1017_StandingOne", "FRE1017_StandingTwo")
        monkeypatch.setattr(context_module, "CURATED_BEHAVIOURAL_STANCE_TARGETS", names)

        for name in names:
            await _seed_bare_entity(owner_service, name)
        ok1 = await owner_service.assert_stance(
            Stance(
                target="FRE1017_StandingOne",
                affect="prefers explicit request first",
                observed_at=_T0,
            )
        )
        ok2 = await owner_service.assert_stance(
            Stance(
                target="FRE1017_StandingTwo",
                affect="prefers plain text by default",
                observed_at=_T0,
            )
        )
        assert ok1 is True
        assert ok2 is True

        # AC-3's topic-scoped counterpart: seeded and stance-bearing, but never recalled.
        await _seed_bare_entity(owner_service, "FRE1017_TopicScoped")
        ok3 = await owner_service.assert_stance(
            Stance(
                target="FRE1017_TopicScoped",
                affect="prefers it as a cheese to keep eating",
                observed_at=_T0,
            )
        )
        assert ok3 is True

        wire, evidence = await _run_turn(owner_service, _PROBE_MESSAGE)

        # Precondition: recall set contains none of the three targets as an entity
        # candidate. A miss here is INCONCLUSIVE, never a stance defect (mirrors
        # test_adr_0126_topic_scoped_stance_push.py's _assert_entity_candidacy_or_skip
        # idiom, established after a master-gate bounce on the T1 PR found a hard
        # assert here reports a broken feature instead of the re-fixture signal).
        for target in (*names, "FRE1017_TopicScoped"):
            if any(_is_undropped_entity_candidate(item, target) for item in evidence.recall.items):
                pytest.skip(
                    f"precondition failed: {target!r} unexpectedly a recall candidate — re-fixture"
                )

        serialized = _serialized(wire)
        assert "prefers explicit request first" in serialized  # AC-2
        assert "prefers plain text by default" in serialized  # AC-2
        assert "prefers it as a cheese to keep eating" not in serialized  # AC-3


class TestAC2RealProductionCuratedSet:
    """Closes the vacuity gap codex's plan review flagged: proves the ACTUAL shipped
    CURATED_BEHAVIOURAL_STANCE_TARGETS (unmonkeypatched) has real, reachable affects --
    not just that its length is <= 12.
    """

    @pytest.mark.asyncio
    async def test_real_curated_targets_reach_the_wire(self, owner_service: MemoryService) -> None:
        curated = context_module.CURATED_BEHAVIOURAL_STANCE_TARGETS
        assert 0 < len(curated) <= 12  # AC-7 cardinality, against the real shipped set

        affects: dict[str, str] = {}
        for i, target in enumerate(curated):
            await _seed_bare_entity(owner_service, target)
            affect = f"fre1017 real-set probe affect {i}"
            ok = await owner_service.assert_stance(
                Stance(target=target, affect=affect, observed_at=_T0)
            )
            assert ok is True
            affects[target] = affect

        wire, evidence = await _run_turn(owner_service, "Completely unrelated probe about weather")

        # Precondition: none of the real curated targets is itself a recall candidate on
        # this turn -- proves the affects below reach the wire because of always-present
        # injection, not because entity recall independently surfaced them (AC-2's own
        # failure mode, and the exact conjunction the criterion requires). A miss here is
        # INCONCLUSIVE, not a stance defect.
        for target in curated:
            if any(_is_undropped_entity_candidate(item, target) for item in evidence.recall.items):
                pytest.skip(
                    f"precondition failed: {target!r} unexpectedly a recall candidate — re-fixture"
                )

        serialized = _serialized(wire)
        for target, affect in affects.items():
            assert affect in serialized, f"curated target {target!r} affect missing from wire"


class TestAC7ByteBoundAndResponsiveness:
    """AC-7's byte-contribution bound and its responsiveness to the curated set
    changing, measured differentially against the JSON-serialized wire.
    """

    @pytest.mark.asyncio
    async def test_byte_contribution_bounded_and_responsive(
        self, owner_service: MemoryService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _seed_bare_entity(owner_service, "FRE1017_ByteBoundOne")
        ok1 = await owner_service.assert_stance(
            Stance(
                target="FRE1017_ByteBoundOne",
                affect="prefers explicit request first",
                observed_at=_T0,
            )
        )
        await _seed_bare_entity(owner_service, "FRE1017_ByteBoundTwo")
        ok2 = await owner_service.assert_stance(
            Stance(
                target="FRE1017_ByteBoundTwo",
                affect="prefers plain text by default",
                observed_at=_T0,
            )
        )
        assert ok1 is True
        assert ok2 is True

        monkeypatch.setattr(context_module, "CURATED_BEHAVIOURAL_STANCE_TARGETS", ())
        wire_baseline, _ = await _run_turn(owner_service, _PROBE_MESSAGE)
        baseline_bytes = _wire_json_bytes(wire_baseline)

        monkeypatch.setattr(
            context_module, "CURATED_BEHAVIOURAL_STANCE_TARGETS", ("FRE1017_ByteBoundOne",)
        )
        wire_one, _ = await _run_turn(owner_service, _PROBE_MESSAGE)
        contribution_one = _wire_json_bytes(wire_one) - baseline_bytes
        assert 0 < contribution_one <= 1500

        monkeypatch.setattr(
            context_module,
            "CURATED_BEHAVIOURAL_STANCE_TARGETS",
            ("FRE1017_ByteBoundOne", "FRE1017_ByteBoundTwo"),
        )
        wire_two, _ = await _run_turn(owner_service, _PROBE_MESSAGE)
        contribution_two = _wire_json_bytes(wire_two) - baseline_bytes
        assert contribution_two > contribution_one
