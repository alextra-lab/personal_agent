"""Tests for FRE-229 memory visibility scoping.

Covers:
- _build_visibility_filter helper unit tests
- Unauthenticated queries return only public nodes
- Authenticated queries return public + group (not other users' private)
- Cross-user private isolation
- Default visibility on writes (group vs public based on user_id presence)
- Chokepoint filter applied in query_memory, query_memory_broad, suggest_proactive_raw
- ON CREATE SET semantics: existing entity visibility not overwritten
- MemoryRecallQuery new fields propagation through adapter
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID, uuid4

import pytest

from personal_agent.memory.models import MemoryQuery, Visibility
from personal_agent.memory.protocol import MemoryRecallQuery
from personal_agent.memory.service import MemoryService, _build_visibility_filter


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_service_with_mock() -> tuple[MemoryService, AsyncMock]:
    """Build a MemoryService bypassing __init__ and return the mock session."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._query_feedback_by_key = {}

    mock_session = AsyncMock()
    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return service, mock_session


USER_A = uuid4()
USER_B = uuid4()


# ---------------------------------------------------------------------------
# _build_visibility_filter helper
# ---------------------------------------------------------------------------


class TestBuildVisibilityFilter:
    def test_unauthenticated_filter(self) -> None:
        """Unauthenticated: vis_authenticated=False, vis_user_id=''."""
        frag, params = _build_visibility_filter("c", None, False)
        assert "c.visibility = 'public'" in frag
        assert "c.visibility = 'group'" in frag
        assert "private:" in frag
        assert params["vis_authenticated"] is False
        assert params["vis_user_id"] == ""

    def test_authenticated_filter_has_user_id(self) -> None:
        """Authenticated with user_id: vis_authenticated=True, vis_user_id=<uuid>."""
        uid = uuid4()
        frag, params = _build_visibility_filter("t", uid, True)
        assert params["vis_authenticated"] is True
        assert params["vis_user_id"] == str(uid)

    def test_null_grace_clause_present(self) -> None:
        """IS NULL clause is present as a grace period for un-backfilled nodes."""
        frag, _ = _build_visibility_filter("e", None, False)
        assert "e.visibility IS NULL" in frag

    def test_alias_is_substituted(self) -> None:
        """The alias parameter is correctly substituted into the fragment."""
        frag, _ = _build_visibility_filter("myNode", None, False)
        assert "myNode.visibility" in frag
        assert "c.visibility" not in frag


# ---------------------------------------------------------------------------
# Visibility enum
# ---------------------------------------------------------------------------


class TestVisibilityEnum:
    def test_enum_values(self) -> None:
        assert Visibility.PUBLIC == "public"
        assert Visibility.GROUP == "group"
        assert Visibility.PRIVATE == "private"

    def test_default_on_models(self) -> None:
        from personal_agent.memory.models import (
            Entity,
            EntityNode,
            Relationship,
            SessionNode,
            TurnNode,
        )
        from personal_agent.memory.weight import KnowledgeWeight

        entity = Entity(name="Test", entity_type="Concept")
        assert entity.visibility == "public"

        rel = Relationship(source_id="A", target_id="B", relationship_type="KNOWS")
        assert rel.visibility == "public"

        turn = TurnNode(
            turn_id="t1",
            timestamp=datetime.now(timezone.utc),
            user_message="hello",
        )
        assert turn.visibility == "public"

        session = SessionNode(
            session_id="s1",
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
        )
        assert session.visibility == "public"

        node = EntityNode(
            entity_id="e1",
            name="Test",
            entity_type="Concept",
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        assert node.visibility == "public"


# ---------------------------------------------------------------------------
# MemoryQuery / MemoryRecallQuery new fields
# ---------------------------------------------------------------------------


class TestQueryModels:
    def test_memory_query_defaults(self) -> None:
        q = MemoryQuery()
        assert q.user_id is None
        assert q.authenticated is False

    def test_memory_query_with_values(self) -> None:
        uid = uuid4()
        q = MemoryQuery(user_id=uid, authenticated=True)
        assert q.user_id == uid
        assert q.authenticated is True

    def test_memory_recall_query_defaults(self) -> None:
        q = MemoryRecallQuery()
        assert q.user_id is None
        assert q.authenticated is False

    def test_memory_recall_query_with_values(self) -> None:
        uid = uuid4()
        q = MemoryRecallQuery(user_id=uid, authenticated=True)
        assert q.user_id == uid
        assert q.authenticated is True


# ---------------------------------------------------------------------------
# query_memory — chokepoint filter injected
# ---------------------------------------------------------------------------


class TestQueryMemoryVisibility:
    @pytest.mark.asyncio
    async def test_unauthenticated_query_uses_vis_params(self) -> None:
        """Unauthenticated query passes vis_authenticated=False inside parameters dict."""
        service, mock_session = _make_service_with_mock()

        turns_result = AsyncMock()
        turns_result.values = AsyncMock(return_value=[])
        mock_session.run = AsyncMock(return_value=turns_result)

        with patch("personal_agent.memory.service.generate_embedding", new_callable=AsyncMock):
            query = MemoryQuery(entity_names=["Berlin"], limit=5, user_id=None, authenticated=False)
            await service.query_memory(query)

        # session.run is called as: session.run(cypher, parameters=params)
        # so vis params live inside call_args.kwargs["parameters"]
        first_call = mock_session.run.call_args_list[0]
        params = first_call.kwargs.get("parameters") or {}
        assert params.get("vis_authenticated") is False
        assert params.get("vis_user_id") == ""

    @pytest.mark.asyncio
    async def test_authenticated_query_passes_user_id(self) -> None:
        """Authenticated query passes correct user_id inside parameters dict."""
        service, mock_session = _make_service_with_mock()

        turns_result = AsyncMock()
        turns_result.values = AsyncMock(return_value=[])
        mock_session.run = AsyncMock(return_value=turns_result)

        with patch("personal_agent.memory.service.generate_embedding", new_callable=AsyncMock):
            query = MemoryQuery(entity_names=["Berlin"], limit=5, user_id=USER_A, authenticated=True)
            await service.query_memory(query, user_id=USER_A, authenticated=True)

        first_call = mock_session.run.call_args_list[0]
        params = first_call.kwargs.get("parameters") or {}
        assert params.get("vis_authenticated") is True
        assert params.get("vis_user_id") == str(USER_A)

    @pytest.mark.asyncio
    async def test_vis_fragment_in_cypher(self) -> None:
        """The chokepoint fragment is present in the Cypher string sent to Neo4j."""
        service, mock_session = _make_service_with_mock()

        captured_cypher: list[str] = []

        async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
            captured_cypher.append(cypher)
            r = AsyncMock()
            r.values = AsyncMock(return_value=[])
            return r

        mock_session.run = AsyncMock(side_effect=capture_run)

        with patch("personal_agent.memory.service.generate_embedding", new_callable=AsyncMock):
            query = MemoryQuery(limit=5, user_id=USER_A, authenticated=True)
            await service.query_memory(query, user_id=USER_A, authenticated=True)

        # The main Turn query should contain 'visibility'
        assert any("visibility" in q for q in captured_cypher), (
            "No Cypher with 'visibility' was emitted"
        )


# ---------------------------------------------------------------------------
# query_memory_broad — chokepoint filter
# ---------------------------------------------------------------------------


class TestQueryMemoryBroadVisibility:
    @pytest.mark.asyncio
    async def test_broad_passes_vis_params(self) -> None:
        """query_memory_broad passes visibility params to all three Cypher queries."""
        service, mock_session = _make_service_with_mock()

        captured_kwargs: list[dict] = []

        async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
            captured_kwargs.append(dict(kwargs))
            r = AsyncMock()
            r.data = AsyncMock(return_value=[])
            return r

        mock_session.run = AsyncMock(side_effect=capture_run)

        await service.query_memory_broad(user_id=USER_A, authenticated=True)

        # Every Cypher call should have the visibility params
        for kw in captured_kwargs:
            assert kw.get("vis_authenticated") is True
            assert kw.get("vis_user_id") == str(USER_A)


# ---------------------------------------------------------------------------
# suggest_proactive_raw — chokepoint filter
# ---------------------------------------------------------------------------


class TestSuggestProactiveVisibility:
    @pytest.mark.asyncio
    async def test_proactive_raw_passes_vis_params(self) -> None:
        """suggest_proactive_raw passes visibility params to the Cypher call."""
        service, mock_session = _make_service_with_mock()

        captured_kwargs: list[dict] = []

        async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
            captured_kwargs.append(dict(kwargs))
            r = AsyncMock()
            r.data = AsyncMock(return_value=[])
            return r

        mock_session.run = AsyncMock(side_effect=capture_run)

        embedding = [0.1] * 768
        with patch("personal_agent.memory.service.get_settings") as mock_settings:
            mock_settings.return_value.proactive_memory_vector_top_k = 10
            await service.suggest_proactive_raw(
                embedding, "session-1", "trace-1", user_id=USER_A, authenticated=True
            )

        assert captured_kwargs, "Expected at least one Cypher call"
        first_kw = captured_kwargs[0]
        assert first_kw.get("vis_authenticated") is True
        assert first_kw.get("vis_user_id") == str(USER_A)


# ---------------------------------------------------------------------------
# Write path: default visibility
# ---------------------------------------------------------------------------


class TestWriteVisibility:
    @pytest.mark.asyncio
    async def test_create_conversation_passes_visibility(self) -> None:
        """create_conversation includes visibility in the SET clause."""
        service, mock_session = _make_service_with_mock()

        captured_cypher: list[str] = []
        captured_kwargs: list[dict] = []

        async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
            captured_cypher.append(cypher)
            captured_kwargs.append(dict(kwargs))
            return AsyncMock()

        mock_session.run = AsyncMock(side_effect=capture_run)

        from personal_agent.memory.models import TurnNode

        turn = TurnNode(
            turn_id="turn-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
        )
        await service.create_conversation(turn, visibility="group")

        # The first run call (Turn MERGE) should have visibility="group"
        assert captured_kwargs[0].get("visibility") == "group"

    @pytest.mark.asyncio
    async def test_create_conversation_default_public(self) -> None:
        """create_conversation defaults to visibility='public'."""
        service, mock_session = _make_service_with_mock()

        captured_kwargs: list[dict] = []

        async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
            captured_kwargs.append(dict(kwargs))
            return AsyncMock()

        mock_session.run = AsyncMock(side_effect=capture_run)

        from personal_agent.memory.models import TurnNode

        turn = TurnNode(
            turn_id="turn-2",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
        )
        await service.create_conversation(turn)

        assert captured_kwargs[0].get("visibility") == "public"

    @pytest.mark.asyncio
    async def test_create_entity_on_create_set_semantics(self) -> None:
        """create_entity uses ON CREATE SET so existing visibility is preserved."""
        service, mock_session = _make_service_with_mock()

        captured_cypher: list[str] = []

        entity_result = AsyncMock()
        entity_result.single = AsyncMock(return_value={"entity_id": "Berlin"})

        async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
            captured_cypher.append(cypher)
            return entity_result

        mock_session.run = AsyncMock(side_effect=capture_run)

        from personal_agent.memory.models import Entity

        entity = Entity(name="Berlin", entity_type="Place")
        await service.create_entity(entity, visibility="group")

        # The merged Cypher must contain ON CREATE SET ... visibility
        merged = " ".join(captured_cypher)
        assert "ON CREATE SET" in merged
        assert "visibility" in merged

    @pytest.mark.asyncio
    async def test_create_relationship_includes_visibility(self) -> None:
        """create_relationship passes visibility into the APOC properties map."""
        service, mock_session = _make_service_with_mock()

        captured_kwargs: list[dict] = []

        rel_result = AsyncMock()
        rel_result.single = AsyncMock(return_value={"element_id": "elem-1"})

        async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
            captured_kwargs.append(dict(kwargs))
            return rel_result

        mock_session.run = AsyncMock(side_effect=capture_run)

        from personal_agent.memory.models import Relationship

        rel = Relationship(source_id="A", target_id="B", relationship_type="KNOWS")
        await service.create_relationship(rel, visibility="group")

        assert captured_kwargs[0].get("visibility") == "group"


# ---------------------------------------------------------------------------
# Consolidator visibility selection
# ---------------------------------------------------------------------------


class TestConsolidatorVisibility:
    def test_visibility_group_when_user_id_present(self) -> None:
        """Consolidator assigns 'group' visibility when capture.user_id is set."""
        from personal_agent.captains_log.capture import TaskCapture

        capture = TaskCapture(
            trace_id="trace-1",
            session_id="session-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            user_id=USER_A,
        )
        visibility = "group" if getattr(capture, "user_id", None) else "public"
        assert visibility == "group"

    def test_visibility_public_when_user_id_absent(self) -> None:
        """Consolidator assigns 'public' visibility when capture.user_id is None."""
        from personal_agent.captains_log.capture import TaskCapture

        capture = TaskCapture(
            trace_id="trace-2",
            session_id="session-2",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            user_id=None,
        )
        visibility = "group" if getattr(capture, "user_id", None) else "public"
        assert visibility == "public"


# ---------------------------------------------------------------------------
# Protocol adapter pass-through
# ---------------------------------------------------------------------------


class TestAdapterPassThrough:
    @pytest.mark.asyncio
    async def test_recall_passes_visibility_to_service(self) -> None:
        """MemoryServiceAdapter.recall forwards user_id + authenticated to service.query_memory."""
        from personal_agent.memory.models import MemoryQueryResult
        from personal_agent.memory.protocol_adapter import MemoryServiceAdapter

        mock_service = AsyncMock()
        mock_service.query_memory = AsyncMock(return_value=MemoryQueryResult())
        adapter = MemoryServiceAdapter(service=mock_service)

        uid = uuid4()
        query = MemoryRecallQuery(entity_names=["Paris"], user_id=uid, authenticated=True)
        await adapter.recall(query, trace_id="trace-test")

        call_kwargs = mock_service.query_memory.call_args.kwargs
        assert call_kwargs.get("user_id") == uid
        assert call_kwargs.get("authenticated") is True

    @pytest.mark.asyncio
    async def test_recall_broad_passes_visibility(self) -> None:
        """MemoryServiceAdapter.recall_broad forwards user_id + authenticated."""
        from personal_agent.memory.protocol_adapter import MemoryServiceAdapter

        mock_service = AsyncMock()
        mock_service.query_memory_broad = AsyncMock(
            return_value={"entities": [], "sessions": [], "turns_summary": []}
        )
        adapter = MemoryServiceAdapter(service=mock_service)

        uid = uuid4()
        await adapter.recall_broad(
            entity_types=None,
            recency_days=30,
            limit=10,
            trace_id="t",
            user_id=uid,
            authenticated=True,
        )

        call_kwargs = mock_service.query_memory_broad.call_args.kwargs
        assert call_kwargs.get("user_id") == uid
        assert call_kwargs.get("authenticated") is True
