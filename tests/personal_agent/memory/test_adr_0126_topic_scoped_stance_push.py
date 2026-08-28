"""ADR-0126 D1/D2 (topic-scoped half)/D5 (push half)/D6 — AC-1, AC-5 (push half), AC-6.

Live-Neo4j behavioural proof (marked ``integration``; runs against the isolated test Neo4j
at :7688 — mirrors ``test_adr_0126_claims_pull.py``'s pattern for T3 and
``test_adr_0126_supersession_chain.py``'s pattern for T4).

**Real entity recall through the real write path, real stance retrieval.** Earlier
revisions of this suite monkeypatched ``MemoryService.query_memory`` to return a fixed
entity for every probe. Master's review (2026-07-31) found this tautological: the
entity-selection precondition (below) then checked the admission record for the same
entity the stub had just installed, so it could never fail and could never report the
INCONCLUSIVE state ADR-0126's precondition clause exists to produce.

The stub is gone. Entities are seeded through the real production write path
(``MemoryService.create_conversation`` with ``key_entities=[name]``), which creates a real
``:Turn`` and the ``:Turn-[:DISCUSSES]->:Entity`` edge.

**Which real recall path this suite actually exercises, and why (verified empirically,
not assumed).** Two live probes ruled out the two paths a first-pass design would reach
for first:

1. The legacy (``multipath_recall_enabled=False``, the default) entity-name-match Cypher
   in ``MemoryService.query_memory`` returns ``MemoryQueryResult.conversations`` only —
   reading the method body shows it never populates ``.entities`` at all on this branch,
   regardless of what ``:DISCUSSES`` edges exist. (This is a pre-existing fact about
   ``main``, unrelated to this ticket — ``_multipath_query_memory``'s own docstring names
   it directly: "entity items previously expanded into their most-recent turns instead, so
   ``entities`` was always empty regardless of fusion rank" (FRE-1021).)
2. The proactive path (``suggest_relevant`` / ``build_proactive_suggestions``) requires a
   live embedding call before it reaches the mention-pin mechanism at all; this environment
   has no ``managed_embedding_token`` credential configured, so every probe returns empty at
   the ``zero_embedding`` gate before mention-pinning is ever reached.

What genuinely works, live, in this environment: ``multipath_recall_enabled`` +
``lexical_arm_enabled`` together (set via an autouse fixture below, real settings —
not a monkeypatch of any recall *logic*). This routes ``query_memory`` through
``_multipath_query_memory``, the FRE-1021 fix that actually resolves fused items back to
real ``EntityNode``s. Its dense arm fails open to empty (same missing credential), but its
lexical arm runs ``CALL db.index.fulltext.queryNodes('turn_entity_fulltext', ...)`` for
real over the seeded entity's own name and turn text — a genuine, capable-of-failing
recall, confirmed live (`hit_count=50`, entity resolved into ``MemoryQueryResult.entities``)
before this suite was written this way, not assumed. Both flags are themselves real,
shippable ADR-0104/FRE-723/FRE-724 code paths — currently flag-dark pending their own
separate production rollout gate (FRE-489/670), not test-only fictions.

From there: ``MemoryService.resolve_message_entity_names`` (FRE-1041) resolves the probe
message's literal mention against the live ``turn_entity_fulltext`` index,
``request_gateway/context.py``'s entity-name-match branch recalls it via the real
(multipath-routed) Cypher above, and ``MemoryServiceAdapter.get_current_stances()`` ->
``MemoryService.query_current_stances()`` retrieves the stance, also for real.
``assemble_context()``, the renderer, the inliner, and ``build_wire_messages()`` all run for
real and unstubbed — nothing in this suite is deterministic by construction; every
positive/control case can genuinely fail (and did, repeatedly, during development of this
fixture, before the multipath+lexical settings were identified as the working path).

Real end-to-end recall-*ranking* determinism (including FRE-1021's displacement mechanism)
is still explicitly out of scope — that is what production observation and FRE-1021's own
measurement ticket own. Seeding a real Turn-DISCUSSES-Entity edge makes the entity a real,
findable recall candidate; this suite does not simulate or hand-construct a fused rank —
the lexical arm's live full-text score decides it.

**Precondition semantics.** Every criterion below that depends on "the target entity was
recalled" asserts *recall candidacy* — an entity-kind record exists in the turn-evidence
admission record's ``items`` whose ``drop_reason`` is not one of the seven producer-side
``RECALL_*`` gate reasons (i.e. not a discard from a path that never actually delivered it
to ``_enrich_with_stances``) — not
render *admission* (``.admitted``). An entity with an empty description is still a
legitimate recall candidate even though it will not render (FRE-1010's empty-description
filter), which is what AC-6's empty-affect fixture needs: it must be a real candidate
(proving the stance mechanism actually saw it) without being forced to also render (which
would make "no orphaned target name" un-checkable).

**Token-disjoint names.** Entity names across this suite deliberately share no substring
with each other (e.g. no common prefix). AC-1's negative half recalls one entity while
proving another is absent; a shared token risked the live full-text resolver surfacing
both off one turn's text (an inference codex's review raised but could not confirm or
rule out) — removing the shared substring removes the question entirely rather than
depending on undocumented analyzer behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio

from personal_agent.captains_log.turn_evidence import (
    DropReason,
    MemoryItemKind,
    build_turn_evidence,
)
from personal_agent.memory.models import Stance, TurnNode
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
from personal_agent.memory.service import MemoryService
from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.types import Complexity, IntentResult, TaskType

pytestmark = pytest.mark.integration

_OWNER_UID = UUID("00000000-0000-0000-0000-00000000ad26")
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(days=30)

# Cleanup/scoping marker: stamped by create_conversation onto both the seeded :Turn and
# every :Entity it touches (ON CREATE), so one property covers both node types from one
# real write path instead of a name-prefix scan (which the token-disjoint names above no
# longer share anyway).
_SEED_SESSION_ID = "fre1015-seed-session"


@pytest.fixture(autouse=True)
def _use_working_real_recall_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route entity recall through the one path that actually works live in this
    environment (see module docstring). Real settings, not a stub of recall logic --
    both flags gate genuine ADR-0104/FRE-723/FRE-724 production code, just dark pending
    their own separate rollout.
    """
    from personal_agent.config import settings

    monkeypatch.setattr(settings, "multipath_recall_enabled", True)
    monkeypatch.setattr(settings, "lexical_arm_enabled", True)


async def _seed_discussed_entity(
    service: MemoryService, name: str, *, turn_id: str, user_message: str
) -> None:
    """Seed a real recall precondition via the production write path (FRE-1015 rework).

    Creates a real ``:Turn`` and the ``:Turn-[:DISCUSSES]->:Entity`` edge that
    ``MemoryService.query_memory``'s legacy Cypher requires -- a bare ``MERGE (:Entity)``
    has no such edge and would never be returned by entity-name-match recall regardless
    of what the probe message names. Also gives ``resolve_message_entity_names`` a real,
    full-text-indexed ``:Entity.name`` to resolve the probe message's literal mention
    against.
    """
    await service.create_conversation(
        TurnNode(
            turn_id=turn_id,
            session_id=_SEED_SESSION_ID,
            trace_id=_SEED_SESSION_ID,
            timestamp=datetime.now(timezone.utc),
            user_message=user_message,
            assistant_response="",
            key_entities=[name],
        ),
        user_id=_OWNER_UID,
        visibility="group",
    )


@pytest_asyncio.fixture
async def owner_service():
    """Connected MemoryService with a clean is_owner Person, no FRE1015 seed residue, and
    the live full-text index confirmed present (idempotent -- does not assume the test
    substrate bootstrap already created it).
    """
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")

    await service.ensure_fulltext_index()

    assert service.driver is not None
    async with service.driver.session() as s:
        await s.run(
            "MATCH (n) WHERE n.originating_session_id = $sid DETACH DELETE n",
            sid=_SEED_SESSION_ID,
        )
        result = await s.run("MATCH (p:Person {is_owner: true}) RETURN count(p) AS n")
        record = await result.single()
        if record is None or record["n"] == 0:
            await s.run(
                "CREATE (:Person {user_id: $user_id, is_owner: true, name: 'FRE1015 Test Owner'})",
                user_id=str(_OWNER_UID),
            )

    yield service

    async with service.driver.session() as s:
        await s.run(
            "MATCH (n) WHERE n.originating_session_id = $sid DETACH DELETE n",
            sid=_SEED_SESSION_ID,
        )
    await service.disconnect()


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


_EPISODES_HEADING = "## Relevant Past Conversations"


def _entity_and_stance_sections_of(wire: list[dict[str, Any]]) -> str:
    """``_memory_section_of`` minus the episode ("## Relevant Past Conversations")
    section.

    AC-6's "no entry in any form" concerns the entity/stance rendering path (FRE-1010's
    empty-description filter, D6's empty-affect filter) -- it does not forbid a
    legitimate episode recall of the very Turn ``_seed_discussed_entity`` created, which
    correctly lists the entity by name as conversation metadata ("Entities: ..."). That
    Turn is now real, unstubbed recall content (the seeding write path creates a real
    Turn, not just an Entity), and rendering its own key_entities is a different,
    unrelated mechanism this suite's real recall also legitimately exercises -- not the
    empty/orphaned stance bullet D6 exists to prevent.
    """
    section = _memory_section_of(wire)
    start = section.find(_EPISODES_HEADING)
    if start == -1:
        return section
    next_heading = section.find("\n\n## ", start + len(_EPISODES_HEADING))
    if next_heading == -1:
        return section[:start]
    return section[:start] + section[next_heading:]


# The eight RECALL_* members (FRE-1060, FRE-1114) name a producer-side gate that
# discarded a candidate *before* context assembly -- i.e. before _enrich_with_stances
# ever saw it. An item bearing one of these was never delivered to the stance
# mechanism, so it must not satisfy the entity-selection precondition even though it
# appears in evidence.recall.items (a gap codex's plan review found: RecalledMemoryRecord
# carries these discards alongside genuinely-delivered candidates).
#
# RECALL_EMPTY_DESCRIPTION (FRE-1114) means an entity with no description is no longer
# delivered to _enrich_with_stances at all when recalled via the proactive path -- a
# deliberate behaviour change (previously it reached candidacy and could still be
# stance-enriched even though its own description never rendered). This suite's fixtures
# are unaffected: they route through the legacy/multipath entity-name-match path (see
# module docstring), which never produces a RECALL_* drop_reason in the first place.
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


def _assert_entity_candidacy_or_skip(evidence: Any, target: str) -> None:
    """The entity-selection precondition (ADR-0126, FRE-1021 inheritance clause).

    Checks recall *candidacy* (an entity-kind record exists in the admission record's
    items and was not a producer-side gate discard) -- not render *admission*
    (``.admitted``) -- see the module docstring for why. Skips with an explicit message on
    failure: a precondition miss is INCONCLUSIVE, never a stance defect and never a silent
    pass.
    """
    holds = any(_is_undropped_entity_candidate(item, target) for item in evidence.recall.items)
    if not holds:
        pytest.skip(
            f"precondition failed: {target!r} not a recall candidate this turn — re-fixture"
        )


class TestAC1TopicScopedStanceReachesModelOnlyWhenEntityRecalled:
    @pytest.mark.asyncio
    async def test_positive_half_affect_present_when_entity_recalled(
        self, owner_service: MemoryService
    ) -> None:
        await _seed_discussed_entity(
            owner_service,
            "Kelvorine",
            turn_id="fre1015-seed-ac1-positive",
            user_message="Tell me about Kelvorine please",
        )
        await owner_service.assert_stance(
            Stance(target="Kelvorine", affect="prefers it over the alternative", observed_at=_T0)
        )

        wire, evidence = await _run_turn(owner_service, "Tell me about Kelvorine please")

        _assert_entity_candidacy_or_skip(evidence, "Kelvorine")
        assert "prefers it over the alternative" in _serialized(wire)

    @pytest.mark.asyncio
    async def test_negative_half_affect_absent_when_entity_not_recalled(
        self, owner_service: MemoryService
    ) -> None:
        """The target entity's absence is asserted directly (not inferred from "some
        other entity was recalled instead") -- proving its absence is why the affect is
        missing, not a masked stance-layer defect. The control entity's own candidacy is
        asserted first, so "target absent" is not indistinguishable from "recall found
        nothing at all, including the control" (a gap codex's plan review flagged).

        "Kelvorine" is seeded as a real recall candidate on its *own* turn -- so its
        stance genuinely exists (``assert_stance`` matches on an existing target and
        writes nothing otherwise, verified below by the ``True`` return) -- but that
        turn is never the one probed. The probe turn only discusses "Bragmoss"
        (token-disjoint from "Kelvorine"), so the assertion below is discriminating: it
        can only pass because Kelvorine was not *recalled* on this turn, not because it
        was never written.
        """
        await _seed_discussed_entity(
            owner_service,
            "Kelvorine",
            turn_id="fre1015-seed-ac1-negative-kelvorine",
            user_message="Tell me about Kelvorine please",
        )
        ok = await owner_service.assert_stance(
            Stance(target="Kelvorine", affect="prefers it over the alternative", observed_at=_T0)
        )
        assert ok is True

        await _seed_discussed_entity(
            owner_service,
            "Bragmoss",
            turn_id="fre1015-seed-ac1-negative-bragmoss",
            user_message="Tell me about Bragmoss please",
        )

        wire, evidence = await _run_turn(owner_service, "Tell me about Bragmoss please")

        # Control: the unrelated entity must itself be a genuine recall candidate, or
        # "Kelvorine absent" would be indistinguishable from "recall found nothing".
        if not any(
            _is_undropped_entity_candidate(item, "Bragmoss") for item in evidence.recall.items
        ):
            pytest.skip("precondition failed: control entity 'Bragmoss' not recalled — re-fixture")

        assert not any(
            _is_undropped_entity_candidate(item, "Kelvorine") for item in evidence.recall.items
        )
        assert "prefers it over the alternative" not in _serialized(wire)


class TestAC5PushHalfCurrentPresentSupersededAbsent:
    @pytest.mark.asyncio
    async def test_current_affect_present_superseded_absent(
        self, owner_service: MemoryService
    ) -> None:
        """Mirrors test_adr_0126_supersession_chain.py's Sorbet fixture (vague ->
        specific), proving the push half: only the current affect reaches the wire.
        """
        await _seed_discussed_entity(
            owner_service,
            "Thennoray",
            turn_id="fre1015-seed-ac5",
            user_message="Tell me about Thennoray please",
        )
        ok1 = await owner_service.assert_stance(
            Stance(target="Thennoray", affect="prefers it", observed_at=_T0)
        )
        ok2 = await owner_service.assert_stance(
            Stance(
                target="Thennoray",
                affect="prefers a distinctly softer texture",
                observed_at=_T1,
            )
        )
        assert ok1 is True
        assert ok2 is True

        wire, evidence = await _run_turn(owner_service, "Tell me about Thennoray please")

        _assert_entity_candidacy_or_skip(evidence, "Thennoray")
        serialized = _serialized(wire)
        assert "prefers a distinctly softer texture" in serialized
        assert "prefers it" not in serialized


class TestAC6EmptyItemFilteredPopulatedControlArrives:
    @pytest.mark.asyncio
    async def test_empty_affect_produces_no_entry_in_any_form(
        self, owner_service: MemoryService
    ) -> None:
        """The target entity carries no description either, so it is a legitimate
        recall candidate (precondition holds) without being forced to render via the
        entity section -- making "no entry for it in any form" achievable rather than
        self-contradicting (see module docstring).
        """
        await _seed_discussed_entity(
            owner_service,
            "Wexbriar",
            turn_id="fre1015-seed-ac6-empty",
            user_message="Tell me about Wexbriar please",
        )
        await owner_service.assert_stance(Stance(target="Wexbriar", affect="", observed_at=_T0))

        wire, evidence = await _run_turn(owner_service, "Tell me about Wexbriar please")

        _assert_entity_candidacy_or_skip(evidence, "Wexbriar")
        assert "Wexbriar" not in _entity_and_stance_sections_of(wire)

    @pytest.mark.asyncio
    async def test_whitespace_only_affect_produces_no_entry_in_any_form(
        self, owner_service: MemoryService
    ) -> None:
        await _seed_discussed_entity(
            owner_service,
            "Wexbriar",
            turn_id="fre1015-seed-ac6-whitespace",
            user_message="Tell me about Wexbriar please",
        )
        await owner_service.assert_stance(Stance(target="Wexbriar", affect="   ", observed_at=_T0))

        wire, evidence = await _run_turn(owner_service, "Tell me about Wexbriar please")

        _assert_entity_candidacy_or_skip(evidence, "Wexbriar")
        assert "Wexbriar" not in _entity_and_stance_sections_of(wire)

    @pytest.mark.asyncio
    async def test_populated_control_arrives(self, owner_service: MemoryService) -> None:
        """Separate turn, per the ADR's own check structure ("Then run a turn
        recalling a populated topic-scoped stance") -- proves suppressing the section
        entirely would not pass this half, since it explicitly requires presence.
        """
        await _seed_discussed_entity(
            owner_service,
            "Delphruvia",
            turn_id="fre1015-seed-ac6-populated",
            user_message="Tell me about Delphruvia please",
        )
        await owner_service.assert_stance(
            Stance(
                target="Delphruvia",
                affect="prefers it as a daily staple",
                observed_at=_T0,
            )
        )

        wire, evidence = await _run_turn(owner_service, "Tell me about Delphruvia please")

        _assert_entity_candidacy_or_skip(evidence, "Delphruvia")
        assert "prefers it as a daily staple" in _serialized(wire)
