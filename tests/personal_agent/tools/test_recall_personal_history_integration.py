"""Live Neo4j integration tests for recall_personal_history (FRE-1119).

Requires the isolated test Neo4j (`make test-infra-up`, port 7688 — see
tests/CLAUDE.md). Marked ``integration`` so it stays out of the unit-only
``make test`` run. Fixture pattern mirrors
tests/personal_agent/memory/test_participated_in_edge.py.

This is the real proof for FRE-1119's identity + topic-ranking fix — the
mocked unit tests in test_recall_personal_history.py are a cheap regression
tripwire only, not evidence the query is actually correct against Neo4j.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from personal_agent.memory.models import TurnNode
from personal_agent.memory.service import MemoryService
from personal_agent.tools.personal_history import recall_personal_history_executor

pytestmark = pytest.mark.integration


def _ctx(user_id: UUID):
    return SimpleNamespace(trace_id="trace-1", user_id=user_id)


@pytest_asyncio.fixture
async def memory_service():
    """Create and connect to MemoryService against live (test) Neo4j."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    connected = await service.connect()
    if not connected:
        pytest.skip("Neo4j not available (make test-infra-up)")

    yield service

    await service.disconnect()


@pytest_asyncio.fixture
async def clean_test_data(memory_service: MemoryService):
    """Yield; each test uses unique IDs so no teardown is required."""
    yield


@pytest_asyncio.fixture(autouse=True)
def _patch_memory_service(memory_service: MemoryService, monkeypatch):
    """Point the tool at the live test MemoryService instead of the global app one."""
    monkeypatch.setattr(
        "personal_agent.tools.personal_history._get_memory_service", lambda: memory_service
    )


async def _provision_person(memory_service: MemoryService, user_id: UUID) -> None:
    await memory_service.get_or_provision_user_person(
        user_id=user_id,
        email=f"test-{user_id}@example.com",
        display_name="Test User",
    )


async def _merge_edge(memory_service: MemoryService, user_id: UUID, turn_id: str) -> None:
    """Write a PARTICIPATED_IN edge directly, bypassing create_conversation."""
    async with memory_service.driver.session() as session:
        await session.run(
            """
            MATCH (p:Person {user_id: $uid})
            MATCH (t:Turn {turn_id: $tid})
            MERGE (p)-[r:PARTICIPATED_IN]->(t)
            ON CREATE SET r.created_at = datetime()
            """,
            uid=str(user_id),
            tid=turn_id,
        )


async def _discuss_entities(
    memory_service: MemoryService, turn_id: str, entity_names: list[str]
) -> None:
    async with memory_service.driver.session() as session:
        for name in entity_names:
            await session.run(
                """
                MATCH (t:Turn {turn_id: $tid})
                MERGE (e:Entity {name: $name})
                MERGE (t)-[:DISCUSSES]->(e)
                """,
                tid=turn_id,
                name=name,
            )


def _turn(user_message: str, assistant_response: str = "", **kwargs) -> TurnNode:
    return TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=kwargs.pop("timestamp", datetime.now(timezone.utc)),
        user_message=user_message,
        assistant_response=assistant_response,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Identity matrix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_property_only_turn_is_found(memory_service: MemoryService, clean_test_data) -> None:
    """Property set, no :Person provisioned so no edge — still found (FRE-1119 cause #1)."""
    uid = uuid4()
    turn = _turn("property only, no person node")
    ok = await memory_service.create_conversation(turn, user_id=uid, visibility="group")
    assert ok is True

    out = await recall_personal_history_executor(days_ago=1, ctx=_ctx(uid))
    assert turn.turn_id in {t["turn_id"] for t in out["turns"]}


@pytest.mark.asyncio
async def test_edge_only_turn_is_found(memory_service: MemoryService, clean_test_data) -> None:
    """No property (create_conversation called without user_id), edge present — still found."""
    uid = uuid4()
    await _provision_person(memory_service, uid)
    turn = _turn("edge only, no property")
    ok = await memory_service.create_conversation(turn, visibility="group")
    assert ok is True
    await _merge_edge(memory_service, uid, turn.turn_id)

    out = await recall_personal_history_executor(days_ago=1, ctx=_ctx(uid))
    assert turn.turn_id in {t["turn_id"] for t in out["turns"]}


@pytest.mark.asyncio
async def test_both_agree_found_exactly_once(
    memory_service: MemoryService, clean_test_data
) -> None:
    """Property and edge agree — the UNION must not duplicate the turn."""
    uid = uuid4()
    await _provision_person(memory_service, uid)
    turn = _turn("both agree")
    await memory_service.create_conversation(turn, user_id=uid, visibility="group")

    out = await recall_personal_history_executor(days_ago=1, ctx=_ctx(uid))
    matches = [t for t in out["turns"] if t["turn_id"] == turn.turn_id]
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_conflicting_identity_property_is_authoritative(
    memory_service: MemoryService, clean_test_data
) -> None:
    """Tenant isolation: property=A + a stray edge from B must not let B see A's turn."""
    uid_a = uuid4()
    uid_b = uuid4()
    await _provision_person(memory_service, uid_b)
    turn = _turn("owned by A, stray edge to B")
    await memory_service.create_conversation(turn, user_id=uid_a, visibility="group")
    await _merge_edge(memory_service, uid_b, turn.turn_id)

    out_b = await recall_personal_history_executor(days_ago=1, ctx=_ctx(uid_b))
    assert turn.turn_id not in {t["turn_id"] for t in out_b["turns"]}, (
        "property-owner A's turn leaked to edge-holder B — authoritative-property semantics failed"
    )

    out_a = await recall_personal_history_executor(days_ago=1, ctx=_ctx(uid_a))
    assert turn.turn_id in {t["turn_id"] for t in out_a["turns"]}


@pytest.mark.asyncio
async def test_neither_signal_is_not_found(memory_service: MemoryService, clean_test_data) -> None:
    """No property, no edge — correctly unreachable (documents the residual historical gap)."""
    uid = uuid4()
    turn = _turn("neither property nor edge")
    ok = await memory_service.create_conversation(turn, visibility="group")
    assert ok is True

    out = await recall_personal_history_executor(days_ago=1, ctx=_ctx(uid))
    assert turn.turn_id not in {t["turn_id"] for t in out["turns"]}


@pytest.mark.asyncio
async def test_wrong_user_gets_nothing(memory_service: MemoryService, clean_test_data) -> None:
    """A user unrelated to any seeded turn gets nothing back."""
    uid_owner = uuid4()
    uid_other = uuid4()
    turn = _turn("belongs only to the owner")
    await memory_service.create_conversation(turn, user_id=uid_owner, visibility="group")

    out = await recall_personal_history_executor(days_ago=1, ctx=_ctx(uid_other))
    assert turn.turn_id not in {t["turn_id"] for t in out["turns"]}


# ---------------------------------------------------------------------------
# Topic ranking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topic_match_via_assistant_response_is_found_and_visible(
    memory_service: MemoryService, clean_test_data
) -> None:
    """Topic present only in assistant_response — found, ranked, and the evidence is visible."""
    uid = uuid4()
    turn = _turn("tell me about that thing", assistant_response="We discussed Athens extensively")
    await memory_service.create_conversation(turn, user_id=uid, visibility="group")

    out = await recall_personal_history_executor(days_ago=1, topic="Athens", ctx=_ctx(uid))
    matches = [t for t in out["turns"] if t["turn_id"] == turn.turn_id]
    assert len(matches) == 1
    assert matches[0]["topic_matched"] is True
    assert "Athens" in matches[0]["assistant_response"]


@pytest.mark.asyncio
async def test_topic_match_ranks_above_recency_amid_noise(
    memory_service: MemoryService, clean_test_data
) -> None:
    """An older topically-matching turn outranks newer unrelated in-window turns."""
    uid = uuid4()
    now = datetime.now(timezone.utc)
    matching = _turn("planning the Athens trip", timestamp=now - timedelta(hours=5))
    await memory_service.create_conversation(matching, user_id=uid, visibility="group")

    noise_turns = []
    for i in range(12):
        noise = _turn(f"unrelated chatter {i}", timestamp=now - timedelta(minutes=i + 1))
        await memory_service.create_conversation(noise, user_id=uid, visibility="group")
        noise_turns.append(noise)

    out = await recall_personal_history_executor(
        days_ago=1, topic="Athens", limit=10, ctx=_ctx(uid)
    )
    turn_ids = [t["turn_id"] for t in out["turns"]]
    assert matching.turn_id in turn_ids
    assert turn_ids[0] == matching.turn_id, "topic match should rank first despite being older"


@pytest.mark.asyncio
async def test_topic_miss_returns_recent_turns_not_empty(
    memory_service: MemoryService, clean_test_data
) -> None:
    """A topic that matches nothing degrades to recency, never to []."""
    uid = uuid4()
    turn = _turn("completely unrelated content")
    await memory_service.create_conversation(turn, user_id=uid, visibility="group")

    out = await recall_personal_history_executor(
        days_ago=1, topic="zzz-no-such-topic-zzz", ctx=_ctx(uid)
    )
    assert out["total"] > 0
    assert all(t["topic_matched"] is False for t in out["turns"])


@pytest.mark.asyncio
async def test_ensure_turn_user_id_index_succeeds(memory_service: MemoryService) -> None:
    """The new index (mirrors test_ensure_session_id_index_succeeds's pattern) is live."""
    assert await memory_service.ensure_turn_user_id_index() is True

    async with memory_service.driver.session() as session:
        result = await session.run(
            "SHOW INDEXES YIELD name WHERE name = 'turn_user_id_index' RETURN name"
        )
        record = await result.single()
    assert record is not None


@pytest.mark.asyncio
async def test_harmony_shaped_semantic_gap_is_not_closed(
    memory_service: MemoryService, clean_test_data
) -> None:
    """Documents the deferred scope (owner decision, FRE-1119 plan).

    A topic with zero lexical overlap anywhere in the graph is NOT found by this
    fix — the turn is still returned (ranking-not-filtering works), but
    topic_matched stays False. This is a known limitation, not a regression;
    closing it needs semantic matching, tracked in a separate ticket.
    """
    uid = uuid4()
    turn = _turn(
        "let's go over voice leading and counterpoint again",
        assistant_response="Sure — we covered parallel octaves too.",
    )
    await memory_service.create_conversation(turn, user_id=uid, visibility="group")
    await _discuss_entities(
        memory_service, turn.turn_id, ["Voice leading", "Counterpoint", "Fusion Interval"]
    )

    out = await recall_personal_history_executor(days_ago=1, topic="harmony", ctx=_ctx(uid))
    matches = [t for t in out["turns"] if t["turn_id"] == turn.turn_id]
    assert len(matches) == 1, "identity reachability must still work even on the semantic gap case"
    assert matches[0]["topic_matched"] is False
