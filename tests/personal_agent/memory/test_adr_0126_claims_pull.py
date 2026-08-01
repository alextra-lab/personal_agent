"""ADR-0126 D4 AC-4 — Claims are reachable by pull and unreachable by push.

Live-Neo4j behavioural proof (marked ``integration``; runs against the isolated test
Neo4j at :7688 — ``generate_embedding`` is patched so similarity is deterministic).
Both halves are required (ADR-0126 Verification/Acceptance Criteria, AC-4):

(a) Seed a claim engineered to be maximally recallable for a probe message (high
    embedding similarity, current, ``class=Personal``); assemble context for that
    probe with every recall toggle at its most permissive setting; assert the
    claim's content is absent from the **actual serialized provider request**.

    Reproduced via the same rendering/inlining pipeline the orchestrator runs
    (``_render_memory_section_with_ids`` -> ``_inline_volatile_with_outcome`` ->
    ``build_wire_messages``), not just ``assemble_context()``'s raw ``.messages`` —
    recalled memory rides a separate ``memory_context`` field the *orchestrator*
    renders and inlines, so checking ``.messages`` alone would skip the exact
    surface ADR-0126's fixed observation point exists to cover (codex plan-review
    finding, FRE-1016).

(b) Call the real ``search_memory`` tool executor with a query matching that same
    claim and assert the claim's content is returned, distinguishable from
    entity/turn rows — not the service method directly, since "reachable through
    search_memory" means the tool composes correctly, not merely that the
    underlying query works (codex plan-review finding, FRE-1016).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import pytest_asyncio

from personal_agent.memory.models import Claim
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
from personal_agent.memory.service import MemoryService
from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.types import Complexity, IntentResult, TaskType

pytestmark = pytest.mark.integration

_OWNER_UID = UUID("00000000-0000-0000-0000-00000000ad26")
_PROBE = "What does my FRE1016 lease say about the move-out date?"
_CLAIM_CONTENT = "The FRE1016 test lease ends on the last day of June."


def _fake_embed(text: str) -> list[float]:
    """Deterministic stand-in: the probe and the seeded claim collapse onto one vector."""
    return [1.0, 0.0] if "FRE1016" in text else [0.0, 1.0]


@pytest_asyncio.fixture
async def owner_service():
    """Connected MemoryService with exactly one clean test Person on the test graph."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")

    assert service.driver is not None
    async with service.driver.session() as s:
        await s.run("MATCH (c:Claim) WHERE c.content CONTAINS 'FRE1016' DETACH DELETE c")
        await s.run("MATCH (p:Person {user_id: $user_id}) DETACH DELETE p", user_id=str(_OWNER_UID))
        await s.run(
            "CREATE (:Person {user_id: $user_id, is_owner: false, name: 'FRE1016 Test User'})",
            user_id=str(_OWNER_UID),
        )

    yield service

    async with service.driver.session() as s:
        await s.run("MATCH (c:Claim) WHERE c.content CONTAINS 'FRE1016' DETACH DELETE c")
        await s.run("MATCH (p:Person {user_id: $user_id}) DETACH DELETE p", user_id=str(_OWNER_UID))
    await service.disconnect()


@pytest.mark.asyncio
async def test_ac4a_claim_never_reaches_the_serialized_provider_request(
    owner_service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from personal_agent.config import settings

    # Every recall toggle at its most permissive setting (ADR-0126 AC-4).
    monkeypatch.setattr(settings, "relevance_bounded_recall_enabled", True)
    monkeypatch.setattr(settings, "multipath_recall_enabled", True)
    monkeypatch.setattr(settings, "lexical_arm_enabled", True)
    monkeypatch.setattr(settings, "multiquery_arm_enabled", True)
    monkeypatch.setattr(settings, "structural_arm_enabled", True)
    monkeypatch.setattr(settings, "structural_type_predicate_enabled", True)
    monkeypatch.setattr(settings, "structural_class_predicate_enabled", True)
    monkeypatch.setattr(settings, "proactive_memory_enabled", True)
    monkeypatch.setattr(settings, "recall_similarity_floor", 0.0)

    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(side_effect=lambda t, **_kw: _fake_embed(t)),
    ):
        await owner_service.assert_claim(
            Claim(content=_CLAIM_CONTENT, confidence=0.8, observed_at=datetime.now(timezone.utc)),
            user_id=_OWNER_UID,
        )

        adapter = MemoryServiceAdapter(owner_service)
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )
        result = await assemble_context(
            user_message=_PROBE,
            session_messages=[],
            intent=intent,
            memory_adapter=adapter,
            trace_id="ac4a-trace",
            user_id=_OWNER_UID,
            authenticated=True,
        )

    # Reproduce the orchestrator's real rendering pipeline (not just result.messages) —
    # recalled memory rides result.memory_context until the orchestrator renders and
    # inlines it; this is the actual surface ADR-0126's fixed observation point covers.
    from personal_agent.orchestrator.executor import (
        _inline_volatile_with_outcome,
        _render_memory_section_with_ids,
        build_wire_messages,
    )

    memory_section, _rendered_ids = _render_memory_section_with_ids(result.memory_context or [])
    final_messages, _outcome = _inline_volatile_with_outcome(result.messages, memory_section)
    wire = build_wire_messages(final_messages, "", "ac4a-trace")

    serialized = " ".join(str(m.get("content", "")) for m in wire)
    assert _CLAIM_CONTENT not in serialized


@pytest.mark.asyncio
async def test_ac4b_claim_reachable_via_search_memory_tool(owner_service: MemoryService) -> None:
    from personal_agent.telemetry.trace import TraceContext
    from personal_agent.tools.memory_search import search_memory_executor

    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(side_effect=lambda t, **_kw: _fake_embed(t)),
    ):
        await owner_service.assert_claim(
            Claim(content=_CLAIM_CONTENT, confidence=0.8, observed_at=datetime.now(timezone.utc)),
            user_id=_OWNER_UID,
        )

        fake_app = MagicMock()
        fake_app.memory_service = owner_service
        with patch.dict("sys.modules", {"personal_agent.service.app": fake_app}):
            ctx = TraceContext(trace_id="ac4b-trace", user_id=_OWNER_UID, authenticated=True)
            output = await search_memory_executor(query_text=_PROBE, ctx=ctx)

    assert "claims" in output
    contents = [c["content"] for c in output["claims"]]
    assert _CLAIM_CONTENT in contents
    matched = next(c for c in output["claims"] if c["content"] == _CLAIM_CONTENT)
    # Distinguishable from entity/turn rows: claim-specific key present.
    assert "claim_id" in matched
