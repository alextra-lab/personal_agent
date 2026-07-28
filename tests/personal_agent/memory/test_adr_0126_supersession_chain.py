"""ADR-0126 D5 AC-5 (chain-on-pull half) — the supersession chain is reachable on demand.

Live-Neo4j behavioural proof (marked ``integration``; runs against the isolated test Neo4j at
:7688 — mirrors ``test_adr_0126_claims_pull.py``'s pattern for T3). Proves this ticket's half
of AC-5 only: "request the chain through the pull path and assert it returns both entries plus
the supersession link." The push half (current present, superseded absent on a sorbet-topic
push turn) belongs to T1, which has not shipped in this codebase, and is proven there.

Uses the ADR's own named fixture: the owner's stance toward Sorbet, where the vague
``"prefers"`` was superseded by the specific ``"prefers a sorbet-leaning texture"``.
``assert_stance`` computes no embedding, so no embedder patch is needed here (unlike the Claim
suite in ``test_adr_0126_claims_pull.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
import pytest_asyncio

from personal_agent.memory.models import Stance
from personal_agent.memory.service import MemoryService

pytestmark = pytest.mark.integration

_OWNER_UID = UUID("00000000-0000-0000-0000-00000000ad26")
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(days=30)


@pytest_asyncio.fixture
async def owner_service():
    """Connected MemoryService with a clean is_owner Person and a Sorbet Entity."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")

    assert service.driver is not None
    async with service.driver.session() as s:
        await s.run(
            "MATCH (:Person {is_owner: true})-[r:HAS_STANCE]->(:Entity {name: 'FRE1018_Sorbet'})"
            " DELETE r"
        )
        await s.run("MATCH (e:Entity {name: 'FRE1018_Sorbet'}) DETACH DELETE e")
        await s.run("MATCH (p:Person {is_owner: true}) RETURN p LIMIT 1")
        result = await s.run("MATCH (p:Person {is_owner: true}) RETURN count(p) AS n")
        record = await result.single()
        if record is None or record["n"] == 0:
            await s.run(
                "CREATE (:Person {user_id: $user_id, is_owner: true, name: 'FRE1018 Test Owner'})",
                user_id=str(_OWNER_UID),
            )
        await s.run("CREATE (:Entity {name: 'FRE1018_Sorbet', class: 'World'})")

    yield service

    async with service.driver.session() as s:
        await s.run(
            "MATCH (:Person {is_owner: true})-[r:HAS_STANCE]->(:Entity {name: 'FRE1018_Sorbet'})"
            " DELETE r"
        )
        await s.run("MATCH (e:Entity {name: 'FRE1018_Sorbet'}) DETACH DELETE e")
    await service.disconnect()


@pytest.mark.asyncio
async def test_ac5_sorbet_stance_chain_reachable_via_search_memory_tool(
    owner_service: MemoryService,
) -> None:
    from personal_agent.telemetry.trace import TraceContext
    from personal_agent.tools.memory_search import search_memory_executor

    ok1 = await owner_service.assert_stance(
        Stance(target="FRE1018_Sorbet", affect="prefers it", mastery=None, observed_at=_T0)
    )
    ok2 = await owner_service.assert_stance(
        Stance(
            target="FRE1018_Sorbet",
            affect="prefers a sorbet-leaning texture",
            mastery=None,
            observed_at=_T1,
        )
    )
    assert ok1 is True
    assert ok2 is True

    fake_app = MagicMock()
    fake_app.memory_service = owner_service
    with patch.dict("sys.modules", {"personal_agent.service.app": fake_app}):
        ctx = TraceContext(trace_id="ac5-trace", user_id=_OWNER_UID, authenticated=True)
        output = await search_memory_executor(
            query_text="FRE1018_Sorbet",
            entity_names=["FRE1018_Sorbet"],
            include_history=True,
            ctx=ctx,
        )

    assert "stance_history" in output
    chain = output["stance_history"]["FRE1018_Sorbet"]
    assert len(chain) == 2

    affects = {entry["affect"] for entry in chain}
    assert affects == {"prefers it", "prefers a sorbet-leaning texture"}

    current = [entry for entry in chain if entry["is_current"]]
    assert len(current) == 1
    assert current[0]["affect"] == "prefers a sorbet-leaning texture"


@pytest.mark.asyncio
async def test_ac5_history_not_exposed_when_flag_is_off(owner_service: MemoryService) -> None:
    """D5's 'on demand' requirement: history must not leak into ordinary search_memory calls."""
    from personal_agent.telemetry.trace import TraceContext
    from personal_agent.tools.memory_search import search_memory_executor

    await owner_service.assert_stance(
        Stance(target="FRE1018_Sorbet", affect="prefers it", mastery=None, observed_at=_T0)
    )
    await owner_service.assert_stance(
        Stance(
            target="FRE1018_Sorbet",
            affect="prefers a sorbet-leaning texture",
            mastery=None,
            observed_at=_T1,
        )
    )

    fake_app = MagicMock()
    fake_app.memory_service = owner_service
    with patch.dict("sys.modules", {"personal_agent.service.app": fake_app}):
        ctx = TraceContext(trace_id="ac5-off-trace", user_id=_OWNER_UID, authenticated=True)
        output = await search_memory_executor(
            query_text="FRE1018_Sorbet", entity_names=["FRE1018_Sorbet"], ctx=ctx
        )

    assert "stance_history" not in output
    assert "claims_history" not in output


@pytest.mark.asyncio
async def test_ac5_query_text_only_reaches_sorbet_history(owner_service: MemoryService) -> None:
    """Codex finding #3 regression, live: query_text alone (no entity_names) reaches history."""
    from personal_agent.telemetry.trace import TraceContext
    from personal_agent.tools.memory_search import search_memory_executor

    await owner_service.assert_stance(
        Stance(target="FRE1018_Sorbet", affect="prefers it", mastery=None, observed_at=_T0)
    )
    await owner_service.assert_stance(
        Stance(
            target="FRE1018_Sorbet",
            affect="prefers a sorbet-leaning texture",
            mastery=None,
            observed_at=_T1,
        )
    )

    fake_app = MagicMock()
    fake_app.memory_service = owner_service
    with patch.dict("sys.modules", {"personal_agent.service.app": fake_app}):
        ctx = TraceContext(trace_id="ac5-qtext-trace", user_id=_OWNER_UID, authenticated=True)
        output = await search_memory_executor(
            query_text="FRE1018_Sorbet", include_history=True, ctx=ctx
        )

    assert "FRE1018_Sorbet" in output["stance_history"]
    assert len(output["stance_history"]["FRE1018_Sorbet"]) == 2
