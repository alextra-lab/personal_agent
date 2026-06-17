"""Tests for the per-turn rating endpoint (FRE-407).

All tests mock ES, Postgres, and Redis — no real substrate (FRE-375).
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.gateway.app import create_gateway_router

_TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
_AUTH_HEADERS = {"Cf-Access-Authenticated-User-Email": "tester@example.com"}
_TRACE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_SESSION_ID = str(uuid4())


# ---------------------------------------------------------------------------
# App / DB helpers
# ---------------------------------------------------------------------------


def _build_app(mock_db_session: Any) -> FastAPI:
    """Build a test app with the gateway router and a mocked DB session."""
    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = None
    app.state.es_client = None

    @asynccontextmanager
    async def _factory():
        yield mock_db_session

    app.state.db_session_factory = _factory
    return app


def _make_session_mock(session_id: str = _SESSION_ID) -> Any:
    """Return a mock SessionModel owned by _TEST_USER_ID."""
    s = MagicMock()
    s.session_id = session_id
    s.user_id = _TEST_USER_ID
    s.messages = []
    s.mode = "NORMAL"
    s.channel = "CHAT"
    s.created_at = None
    s.last_active_at = None
    s.execution_profile = "local"
    return s


# ---------------------------------------------------------------------------
# Helper: build an ES hits response for model_call_completed identity lookup
# ---------------------------------------------------------------------------


def _es_identity_hit(
    callsite: str = "orchestrator.primary",
    session_id: str = _SESSION_ID,
) -> dict[str, Any]:
    return {
        "hits": {
            "total": {"value": 1},
            "hits": [
                {
                    "_source": {
                        "prompt_callsite": callsite,
                        "prompt_static_prefix_hash": "hash-static",
                        "prompt_dynamic_hash": "hash-dyn",
                        "prompt_component_ids": ["comp-a"],
                        "session_id": session_id,
                    }
                }
            ],
        }
    }


def _es_empty_hits() -> dict[str, Any]:
    return {"hits": {"total": {"value": 0}, "hits": []}}


# ---------------------------------------------------------------------------
# Happy-path: rating 2 → 200, ES written, bus published
# ---------------------------------------------------------------------------


class TestRatingHappyPath:
    """Submit a valid rating → ES doc stored, bus event published."""

    def test_valid_rating_returns_received(self) -> None:
        """Rating 2 → 200 {"status": "received"}."""
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        app = _build_app(mock_db)

        session_mock = _make_session_mock()

        with (
            patch(
                "personal_agent.gateway.feedback_api._get_user_with_display_name",
                new_callable=AsyncMock,
                return_value=(_TEST_USER_ID, None),
            ),
            patch("personal_agent.gateway.feedback_api.SessionRepository") as MockRepo,
            patch(
                "personal_agent.gateway.feedback_api._lookup_prompt_identity",
                new_callable=AsyncMock,
                return_value={
                    "prompt_callsite": "orchestrator.primary",
                    "prompt_static_prefix_hash": "hash-static",
                    "prompt_dynamic_hash": "hash-dyn",
                    "prompt_component_ids": ["comp-a"],
                },
            ),
            patch("personal_agent.gateway.feedback_api.schedule_es_index"),
            patch(
                "personal_agent.gateway.feedback_api._publish_rating_event",
                new_callable=AsyncMock,
            ),
        ):
            repo_instance = AsyncMock()
            repo_instance.get = AsyncMock(return_value=session_mock)
            MockRepo.return_value = repo_instance

            client = TestClient(app)
            resp = client.post(
                f"/api/v1/turns/{_TRACE_ID}/rating",
                json={"rating": 2, "session_id": _SESSION_ID},
                headers=_AUTH_HEADERS,
            )

        assert resp.status_code == 200
        assert resp.json() == {"status": "received"}

    def test_valid_rating_calls_schedule_es_index(self) -> None:
        """A successful rating schedules an ES index with the correct index name."""
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        app = _build_app(mock_db)
        session_mock = _make_session_mock()

        captured_calls: list[Any] = []

        def _capture(*args: Any, **kwargs: Any) -> None:
            captured_calls.append((args, kwargs))

        with (
            patch(
                "personal_agent.gateway.feedback_api._get_user_with_display_name",
                new_callable=AsyncMock,
                return_value=(_TEST_USER_ID, None),
            ),
            patch("personal_agent.gateway.feedback_api.SessionRepository") as MockRepo,
            patch(
                "personal_agent.gateway.feedback_api._lookup_prompt_identity",
                new_callable=AsyncMock,
                return_value={
                    "prompt_callsite": "orchestrator.primary",
                    "prompt_static_prefix_hash": "h-static",
                    "prompt_dynamic_hash": "h-dyn",
                    "prompt_component_ids": [],
                },
            ),
            patch("personal_agent.gateway.feedback_api.schedule_es_index", side_effect=_capture),
            patch(
                "personal_agent.gateway.feedback_api._publish_rating_event",
                new_callable=AsyncMock,
            ),
        ):
            repo_instance = AsyncMock()
            repo_instance.get = AsyncMock(return_value=session_mock)
            MockRepo.return_value = repo_instance

            client = TestClient(app)
            client.post(
                f"/api/v1/turns/{_TRACE_ID}/rating",
                json={"rating": 2, "session_id": _SESSION_ID},
                headers=_AUTH_HEADERS,
            )

        assert len(captured_calls) == 1
        index_name, doc, *_ = captured_calls[0][0]
        # Monthly partitioning (FRE-559): user-turn-ratings-YYYY.MM, no day component.
        assert re.fullmatch(r"user-turn-ratings-\d{4}\.\d{2}", index_name), index_name
        assert doc["trace_id"] == _TRACE_ID
        assert doc["rating"] == 2
        assert doc["prompt_callsite"] == "orchestrator.primary"


# ---------------------------------------------------------------------------
# Validation: out-of-range ratings → 400
# ---------------------------------------------------------------------------


class TestRatingValidation:
    """Invalid rating values are rejected before any write."""

    @pytest.mark.parametrize("bad_rating", [4, -1, 10, 100])
    def test_invalid_rating_returns_400(self, bad_rating: int) -> None:
        """Rating outside 0–3 → 400."""
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        app = _build_app(mock_db)

        with (
            patch(
                "personal_agent.gateway.feedback_api._get_user_with_display_name",
                new_callable=AsyncMock,
                return_value=(_TEST_USER_ID, None),
            ),
            patch("personal_agent.gateway.feedback_api.schedule_es_index") as mock_es,
        ):
            client = TestClient(app)
            resp = client.post(
                f"/api/v1/turns/{_TRACE_ID}/rating",
                json={"rating": bad_rating, "session_id": _SESSION_ID},
                headers=_AUTH_HEADERS,
            )
            mock_es.assert_not_called()

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Ownership enforcement
# ---------------------------------------------------------------------------


class TestOwnershipEnforcement:
    """Foreign session_id or unowned trace_id → 404, no write."""

    def test_foreign_session_returns_404(self) -> None:
        """session_id not owned by caller → 404, no ES write."""
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        app = _build_app(mock_db)

        with (
            patch(
                "personal_agent.gateway.feedback_api._get_user_with_display_name",
                new_callable=AsyncMock,
                return_value=(_TEST_USER_ID, None),
            ),
            patch("personal_agent.gateway.feedback_api.SessionRepository") as MockRepo,
            patch("personal_agent.gateway.feedback_api.schedule_es_index") as mock_es,
        ):
            # repo.get returns None → session not owned by user
            repo_instance = AsyncMock()
            repo_instance.get = AsyncMock(return_value=None)
            MockRepo.return_value = repo_instance

            client = TestClient(app)
            resp = client.post(
                f"/api/v1/turns/{_TRACE_ID}/rating",
                json={"rating": 1, "session_id": str(uuid4())},
                headers=_AUTH_HEADERS,
            )
            mock_es.assert_not_called()

        assert resp.status_code == 404

    def test_trace_session_mismatch_returns_404(self) -> None:
        """trace_id whose model_call_completed.session_id != supplied session_id → 404."""
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        app = _build_app(mock_db)
        session_mock = _make_session_mock(session_id=_SESSION_ID)
        other_session = str(uuid4())

        with (
            patch(
                "personal_agent.gateway.feedback_api._get_user_with_display_name",
                new_callable=AsyncMock,
                return_value=(_TEST_USER_ID, None),
            ),
            patch("personal_agent.gateway.feedback_api.SessionRepository") as MockRepo,
            # Identity lookup returns a hit where session_id ≠ supplied session_id
            patch(
                "personal_agent.gateway.feedback_api._lookup_prompt_identity",
                new_callable=AsyncMock,
                return_value={
                    "prompt_callsite": "orchestrator.primary",
                    "prompt_static_prefix_hash": None,
                    "prompt_dynamic_hash": None,
                    "prompt_component_ids": [],
                    "_session_id": other_session,  # mismatch signal
                },
            ),
            patch("personal_agent.gateway.feedback_api.schedule_es_index") as mock_es,
            patch(
                "personal_agent.gateway.feedback_api._verify_trace_session_ownership",
                new_callable=AsyncMock,
                return_value=False,  # ownership check fails
            ),
        ):
            repo_instance = AsyncMock()
            repo_instance.get = AsyncMock(return_value=session_mock)
            MockRepo.return_value = repo_instance

            client = TestClient(app)
            resp = client.post(
                f"/api/v1/turns/{_TRACE_ID}/rating",
                json={"rating": 1, "session_id": _SESSION_ID},
                headers=_AUTH_HEADERS,
            )
            mock_es.assert_not_called()

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Refresh-miss: best-effort retry
# ---------------------------------------------------------------------------


class TestRefreshMissRetry:
    """Write-time identity lookup respects retry semantics."""

    def test_null_identity_on_double_miss_still_returns_200(self) -> None:
        """Both ES lookups miss → rating stored with null identity, still 200."""
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        app = _build_app(mock_db)
        session_mock = _make_session_mock()
        captured: list[Any] = []

        def _capture(*args: Any, **kwargs: Any) -> None:
            captured.append((args, kwargs))

        with (
            patch(
                "personal_agent.gateway.feedback_api._get_user_with_display_name",
                new_callable=AsyncMock,
                return_value=(_TEST_USER_ID, None),
            ),
            patch("personal_agent.gateway.feedback_api.SessionRepository") as MockRepo,
            patch(
                "personal_agent.gateway.feedback_api._lookup_prompt_identity",
                new_callable=AsyncMock,
                return_value=None,  # both attempts miss
            ),
            patch("personal_agent.gateway.feedback_api.schedule_es_index", side_effect=_capture),
            patch(
                "personal_agent.gateway.feedback_api._publish_rating_event",
                new_callable=AsyncMock,
            ),
        ):
            repo_instance = AsyncMock()
            repo_instance.get = AsyncMock(return_value=session_mock)
            MockRepo.return_value = repo_instance

            client = TestClient(app)
            resp = client.post(
                f"/api/v1/turns/{_TRACE_ID}/rating",
                json={"rating": 1, "session_id": _SESSION_ID},
                headers=_AUTH_HEADERS,
            )

        assert resp.status_code == 200
        # Rating still stored with null identity
        assert len(captured) == 1
        _, doc, *_ = captured[0][0]
        assert doc["prompt_callsite"] is None
        assert doc["rating"] == 1


# ---------------------------------------------------------------------------
# Re-rate semantics
# ---------------------------------------------------------------------------


class TestReRateSemantics:
    """Re-rating to the same value suppresses the bus event."""

    def test_rerate_same_value_no_bus_event(self) -> None:
        """Re-rate same score → ES overwrite still happens, no bus event emitted."""
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        app = _build_app(mock_db)
        session_mock = _make_session_mock()

        # Simulate ES returning an existing doc with rating=2 (same as new rating)
        existing_doc: dict[str, Any] = {
            "rating": 2,
            "prompt_callsite": "orchestrator.primary",
        }

        with (
            patch(
                "personal_agent.gateway.feedback_api._get_user_with_display_name",
                new_callable=AsyncMock,
                return_value=(_TEST_USER_ID, None),
            ),
            patch("personal_agent.gateway.feedback_api.SessionRepository") as MockRepo,
            patch(
                "personal_agent.gateway.feedback_api._lookup_prompt_identity",
                new_callable=AsyncMock,
                return_value={
                    "prompt_callsite": "orchestrator.primary",
                    "prompt_static_prefix_hash": None,
                    "prompt_dynamic_hash": None,
                    "prompt_component_ids": [],
                },
            ),
            patch(
                "personal_agent.gateway.feedback_api._get_existing_rating",
                new_callable=AsyncMock,
                return_value=existing_doc,
            ),
            patch("personal_agent.gateway.feedback_api.schedule_es_index"),
            patch(
                "personal_agent.gateway.feedback_api._publish_rating_event",
                new_callable=AsyncMock,
            ) as mock_publish,
        ):
            repo_instance = AsyncMock()
            repo_instance.get = AsyncMock(return_value=session_mock)
            MockRepo.return_value = repo_instance

            client = TestClient(app)
            resp = client.post(
                f"/api/v1/turns/{_TRACE_ID}/rating",
                json={"rating": 2, "session_id": _SESSION_ID},
                headers=_AUTH_HEADERS,
            )
            # Bus event suppressed — same rating value
            mock_publish.assert_not_called()

        assert resp.status_code == 200

    def test_rerate_different_value_emits_bus_event(self) -> None:
        """Re-rate with different score → bus event IS emitted."""
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        app = _build_app(mock_db)
        session_mock = _make_session_mock()

        existing_doc: dict[str, Any] = {"rating": 1}

        with (
            patch(
                "personal_agent.gateway.feedback_api._get_user_with_display_name",
                new_callable=AsyncMock,
                return_value=(_TEST_USER_ID, None),
            ),
            patch("personal_agent.gateway.feedback_api.SessionRepository") as MockRepo,
            patch(
                "personal_agent.gateway.feedback_api._lookup_prompt_identity",
                new_callable=AsyncMock,
                return_value={
                    "prompt_callsite": "orchestrator.primary",
                    "prompt_static_prefix_hash": None,
                    "prompt_dynamic_hash": None,
                    "prompt_component_ids": [],
                },
            ),
            patch(
                "personal_agent.gateway.feedback_api._get_existing_rating",
                new_callable=AsyncMock,
                return_value=existing_doc,
            ),
            patch("personal_agent.gateway.feedback_api.schedule_es_index"),
            patch(
                "personal_agent.gateway.feedback_api._publish_rating_event",
                new_callable=AsyncMock,
            ) as mock_publish,
        ):
            repo_instance = AsyncMock()
            repo_instance.get = AsyncMock(return_value=session_mock)
            MockRepo.return_value = repo_instance

            client = TestClient(app)
            resp = client.post(
                f"/api/v1/turns/{_TRACE_ID}/rating",
                json={"rating": 3, "session_id": _SESSION_ID},  # changed: 1 → 3
                headers=_AUTH_HEADERS,
            )
            mock_publish.assert_called_once()

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Fallback identity selection
# ---------------------------------------------------------------------------


class TestFallbackIdentity:
    """Traces with only role.primary or gateway.chat callsites are handled."""

    @pytest.mark.parametrize(
        "callsite",
        ["role.primary", "gateway.chat", "role.sub_agent"],
    )
    def test_fallback_callsite_accepted(self, callsite: str) -> None:
        """Traces with non-primary callsites → rating stored (no 500)."""
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        app = _build_app(mock_db)
        session_mock = _make_session_mock()

        with (
            patch(
                "personal_agent.gateway.feedback_api._get_user_with_display_name",
                new_callable=AsyncMock,
                return_value=(_TEST_USER_ID, None),
            ),
            patch("personal_agent.gateway.feedback_api.SessionRepository") as MockRepo,
            patch(
                "personal_agent.gateway.feedback_api._lookup_prompt_identity",
                new_callable=AsyncMock,
                return_value={
                    "prompt_callsite": callsite,
                    "prompt_static_prefix_hash": None,
                    "prompt_dynamic_hash": None,
                    "prompt_component_ids": [],
                },
            ),
            patch("personal_agent.gateway.feedback_api.schedule_es_index"),
            patch(
                "personal_agent.gateway.feedback_api._publish_rating_event",
                new_callable=AsyncMock,
            ),
        ):
            repo_instance = AsyncMock()
            repo_instance.get = AsyncMock(return_value=session_mock)
            MockRepo.return_value = repo_instance

            client = TestClient(app)
            resp = client.post(
                f"/api/v1/turns/{_TRACE_ID}/rating",
                json={"rating": 2, "session_id": _SESSION_ID},
                headers=_AUTH_HEADERS,
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Regression: ES query shape — event_type discriminator (FRE-407 architect fix)
# ---------------------------------------------------------------------------


class TestEsQueryShape:
    """Pin the ES query body so a wrong field name cannot silently regress.

    Context: the initial implementation used ``event.keyword`` as the
    discriminator for model_call_completed events.  Live-cluster inspection
    confirmed ``event.keyword`` → 0 docs; the correct field is ``event_type``.
    These tests inspect the actual kwargs passed to the mocked ES client and
    assert the filter uses ``event_type``, mirroring the spirit of
    ``tests/observability/test_joinability_walk_unit.py::
    test_es_query_excludes_transport_logger``.
    """

    @pytest.mark.asyncio
    async def test_lookup_prompt_identity_filters_on_event_type(self) -> None:
        """_lookup_prompt_identity must filter on event_type, not event.keyword."""
        from personal_agent.gateway.feedback_api import _lookup_prompt_identity

        mock_es = MagicMock()
        mock_es.search = AsyncMock(return_value=_es_identity_hit())

        await _lookup_prompt_identity(
            trace_id=_TRACE_ID,
            session_id=_SESSION_ID,
            es_client=mock_es,
            ctx_trace_id="test-ctx",
        )

        assert mock_es.search.called, "ES search was not called"
        call_kwargs = mock_es.search.call_args.kwargs
        query_filters: list[dict] = call_kwargs.get("query", {}).get("bool", {}).get("filter", [])
        term_fields = {
            field
            for f in query_filters
            if isinstance(f, dict) and "term" in f
            for field in f["term"]
        }
        assert "event_type" in term_fields, (
            f"ES query does not filter on 'event_type' — found term fields: {term_fields}. "
            "model_call_completed events have no 'event' field; the discriminator must be "
            "'event_type' or every identity lookup silently returns nothing."
        )
        assert "event.keyword" not in term_fields and "event" not in term_fields, (
            f"ES query still references stale 'event'/'event.keyword' field: {term_fields}"
        )

    @pytest.mark.asyncio
    async def test_verify_trace_session_ownership_filters_on_event_type(self) -> None:
        """_verify_trace_session_ownership must filter on event_type, not event.keyword."""
        from personal_agent.gateway.feedback_api import _verify_trace_session_ownership

        mock_es = MagicMock()
        mock_es.search = AsyncMock(
            return_value={
                "hits": {
                    "total": {"value": 1},
                    "hits": [{"_source": {"session_id": _SESSION_ID}}],
                }
            }
        )

        await _verify_trace_session_ownership(
            trace_id=_TRACE_ID,
            session_id=_SESSION_ID,
            es_client=mock_es,
            ctx_trace_id="test-ctx",
        )

        assert mock_es.search.called, "ES search was not called"
        call_kwargs = mock_es.search.call_args.kwargs
        query_filters: list[dict] = call_kwargs.get("query", {}).get("bool", {}).get("filter", [])
        term_fields = {
            field
            for f in query_filters
            if isinstance(f, dict) and "term" in f
            for field in f["term"]
        }
        assert "event_type" in term_fields, (
            f"ES query does not filter on 'event_type' — found term fields: {term_fields}. "
            "Ownership check cannot verify trace→session mapping if it matches 0 docs."
        )
        assert "event.keyword" not in term_fields and "event" not in term_fields, (
            f"ES query still references stale 'event'/'event.keyword' field: {term_fields}"
        )
