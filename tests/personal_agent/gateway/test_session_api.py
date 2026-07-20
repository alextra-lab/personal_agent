"""Tests for the gateway session API endpoints (FRE-206 + cross-user leak hotfix).

Uses FastAPI's TestClient with mocked SessionRepository. Each test attaches
the ``Cf-Access-Authenticated-User-Email`` header and patches
``_get_user_with_display_name`` so the endpoint's user-scoping helper
returns a stable mock user_id — closes the data leak in
``gateway/session_api.py`` where session data was unscoped.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.gateway.app import create_gateway_router

_TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
_AUTH_HEADERS = {"Cf-Access-Authenticated-User-Email": "tester@example.com"}


@contextmanager
def _patched_user_resolver(user_id: UUID = _TEST_USER_ID) -> Any:
    """Patch the CF Access → user_id resolver to return a stable UUID."""
    with patch(
        "personal_agent.gateway.session_api._get_user_with_display_name",
        new_callable=AsyncMock,
        return_value=(user_id, None),
    ) as mock_resolver:
        yield mock_resolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_model(
    session_id: str | None = None,
    mode: str = "NORMAL",
    channel: str = "CHAT",
    message_count: int = 2,
    messages: list[dict] | None = None,
    execution_profile: str = "local",
) -> Any:
    """Build a minimal mock SessionModel.

    Args:
        session_id: Optional UUID string; generated if not provided.
        mode: Session mode string.
        channel: Session channel string.
        message_count: Number of synthetic messages to generate when
            ``messages`` is not provided.
        messages: Explicit list of message dicts; overrides ``message_count``.
        execution_profile: Server-owned execution profile (ADR-0079).
    """
    sid = session_id or str(uuid4())
    session = MagicMock()
    session.session_id = sid
    session.created_at = datetime(2026, 1, 1, 10, 0, 0)
    session.last_active_at = datetime(2026, 1, 1, 10, 5, 0)
    session.mode = mode
    session.channel = channel
    session.execution_profile = execution_profile
    if messages is not None:
        session.messages = messages
    else:
        session.messages = [
            {"role": "user", "content": f"message {i}"} for i in range(message_count)
        ]
    return session


def _build_app_with_db_factory(mock_session: Any) -> FastAPI:
    """Build test app with a mock DB factory yielding mock_session."""
    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = None
    app.state.es_client = None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _factory():
        yield mock_session

    app.state.db_session_factory = _factory
    return app


# ---------------------------------------------------------------------------
# GET /api/v1/sessions
# ---------------------------------------------------------------------------


def test_list_sessions_returns_list() -> None:
    """GET /sessions returns a list of session summaries."""
    db_session = AsyncMock()
    s1 = _make_session_model()
    s2 = _make_session_model()

    list_recent_mock = AsyncMock(return_value=[s1, s2])
    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.list_recent",
                list_recent_mock,
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/sessions?limit=10", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    assert "session_id" in data[0]
    assert "message_count" in data[0]
    assert "turn_count" in data[0]
    # Hotfix invariant: user_id MUST be threaded to the repository.
    list_recent_mock.assert_awaited_once()
    assert list_recent_mock.await_args.kwargs.get("user_id") == _TEST_USER_ID


def test_list_sessions_401_without_cf_access_header() -> None:
    """GET /sessions returns 401 when the CF Access header is absent."""
    db_session = AsyncMock()
    app = _build_app_with_db_factory(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/sessions")
    assert resp.status_code == 401


def test_list_sessions_503_when_no_factory() -> None:
    """GET /sessions returns 503 when db_session_factory is not attached."""
    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.knowledge_graph = None
    app.state.es_client = None
    app.state.db_session_factory = None

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/sessions")

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /api/v1/sessions/{session_id}
# ---------------------------------------------------------------------------


def test_get_session_found() -> None:
    """GET /sessions/{id} returns 200 with session dict, scoped by user_id."""
    db_session = AsyncMock()
    # FRE-426: get_session now sums api_costs via raw SQL — mock the cost query.
    db_session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=0.0)))
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid)

    get_mock = AsyncMock(return_value=session_model)
    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.get",
                get_mock,
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == sid
    assert data["mode"] == "NORMAL"
    get_mock.assert_awaited_once()
    assert get_mock.await_args.kwargs.get("user_id") == _TEST_USER_ID


def test_get_session_404_when_other_user_owns_it() -> None:
    """GET /sessions/{id} returns 404 (not 403) when another user owns the session.

    repo.get(uuid, user_id=X) returns None on ownership mismatch — endpoint
    must not confirm existence of other users' sessions.
    """
    db_session = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.get",
                new_callable=AsyncMock,
                return_value=None,
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/v1/sessions/{uuid4()}", headers=_AUTH_HEADERS)
    assert resp.status_code == 404


def test_get_session_not_found() -> None:
    """GET /sessions/{id} returns 404 when session does not exist."""
    db_session = AsyncMock()

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.get",
                new_callable=AsyncMock,
                return_value=None,
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/v1/sessions/{uuid4()}", headers=_AUTH_HEADERS)

    assert resp.status_code == 404


def test_get_session_invalid_uuid() -> None:
    """GET /sessions/{id} returns 422 for non-UUID session_id."""
    db_session = AsyncMock()
    app = _build_app_with_db_factory(db_session)

    with _patched_user_resolver():
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/sessions/not-a-valid-uuid", headers=_AUTH_HEADERS)

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/sessions/{session_id}/messages
# ---------------------------------------------------------------------------


def test_get_session_messages_returns_messages() -> None:
    """GET /sessions/{id}/messages returns list of messages, scoped by user_id."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, message_count=3)

    get_mock = AsyncMock(return_value=session_model)
    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.get",
                get_mock,
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}/messages?limit=50", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 3
    assert data[0]["role"] == "user"
    get_mock.assert_awaited_once()
    assert get_mock.await_args.kwargs.get("user_id") == _TEST_USER_ID


def test_get_session_messages_limit_applied() -> None:
    """GET /sessions/{id}/messages respects the limit query param."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, message_count=10)

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.get",
                new_callable=AsyncMock,
                return_value=session_model,
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}/messages?limit=3", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_get_session_messages_401_without_cf_access_header() -> None:
    """GET /sessions/{id}/messages returns 401 when CF Access header missing."""
    db_session = AsyncMock()
    app = _build_app_with_db_factory(db_session)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/api/v1/sessions/{uuid4()}/messages")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Session title derivation
# ---------------------------------------------------------------------------


def test_list_sessions_includes_title() -> None:
    """GET /sessions includes a ``title`` derived from the first user message."""
    db_session = AsyncMock()
    session_model = _make_session_model(
        messages=[{"role": "user", "content": "Hello world this is a test message"}]
    )

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.list_recent",
                new_callable=AsyncMock,
                return_value=[session_model],
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/sessions", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["title"] == "Hello world this is a test message"


def test_list_sessions_truncates_title() -> None:
    """GET /sessions truncates titles longer than 60 chars with an ellipsis."""
    db_session = AsyncMock()
    long_content = "A" * 80  # 80 characters
    session_model = _make_session_model(messages=[{"role": "user", "content": long_content}])

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.list_recent",
                new_callable=AsyncMock,
                return_value=[session_model],
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/sessions", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    # 60 chars + the single '…' character = 61 characters total
    assert len(data[0]["title"]) == 61
    assert data[0]["title"].endswith("…")


# ---------------------------------------------------------------------------
# Title extraction on list-shaped content (ADR-0101 §2, FRE-726)
# ---------------------------------------------------------------------------


def test_extract_title_list_content_extracts_real_text_not_repr() -> None:
    """List-shaped first user message yields its text block, not a Python-repr string."""
    from personal_agent.gateway.session_api import _extract_title

    title = _extract_title(
        [{"role": "user", "content": [{"type": "text", "text": "What's in this diagram?"}]}]
    )
    assert title == "What's in this diagram?"
    assert title is not None
    assert "[{" not in title


def test_extract_title_image_only_first_message_falls_through_to_next_text() -> None:
    """An image-only first user turn is skipped in favor of the next textual user turn."""
    from personal_agent.gateway.session_api import _extract_title

    title = _extract_title(
        [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
            {"role": "user", "content": "actual question here"},
        ]
    )
    assert title == "actual question here"


def test_extract_title_image_only_history_returns_none() -> None:
    """A history with only image-only user turns yields None, not a repr-shaped title."""
    from personal_agent.gateway.session_api import _extract_title

    title = _extract_title(
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]
    )
    assert title is None


# ---------------------------------------------------------------------------
# Turn count (FRE-521)
# ---------------------------------------------------------------------------


def test_list_sessions_turn_count_counts_only_user_messages() -> None:
    """GET /sessions turn_count counts user-role messages only, not assistant/tool/system."""
    db_session = AsyncMock()
    session_model = _make_session_model(
        messages=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "What is Python?"},
            {"role": "tool", "content": "tool output"},
            {"role": "assistant", "content": "Python is a language"},
            {"role": "user", "content": "Thanks"},
        ]
    )

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(
            patch(
                "personal_agent.service.repositories.session_repository.SessionRepository.list_recent",
                new_callable=AsyncMock,
                return_value=[session_model],
            )
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/sessions", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    # 3 user messages, 2 assistant, 1 tool — turn_count must be 3 only
    assert data[0]["turn_count"] == 3
    assert data[0]["message_count"] == 6


# ---------------------------------------------------------------------------
# Execution profile — retired (ADR-0121 T5, FRE-920). GET /sessions/{id} no
# longer surfaces execution_profile and PATCH /sessions/{id} (the profile
# pill mutator) is deleted; only the selection provenance check below remains.
# ---------------------------------------------------------------------------

_SESSION_GET = "personal_agent.service.repositories.session_repository.SessionRepository.get"


def test_get_session_stale_stored_key_provenance_is_default() -> None:
    """A stored key no longer in the catalog hydrates as the default with provenance=default.

    Provenance must not claim 'server-hydrated' when the guardrail dropped the
    stale key — the picker needs to know the selection was reset, not treat the
    default as the user's active choice.
    """
    db_session = AsyncMock()
    db_session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=0.0)))
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, execution_profile="cloud")

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=session_model))
        stack.enter_context(
            patch(_SELECTION_GET, new_callable=AsyncMock, return_value="retired_model_key")
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}", headers=_AUTH_HEADERS)

    body = resp.json()
    assert body["primary_selection"] == "qwen3.6-35b-thinking"  # guardrail fell back to default
    assert body["selection_provenance"] == "default"  # NOT 'server-hydrated'


# ---------------------------------------------------------------------------
# Model selection — GET field + PATCH selection write (ADR-0121 §4/§6 / FRE-917)
# ---------------------------------------------------------------------------

_SELECTION_GET = (
    "personal_agent.service.repositories.session_model_selection_repository."
    "SessionModelSelectionRepository.get"
)
_SELECTION_UPSERT = (
    "personal_agent.service.repositories.session_model_selection_repository."
    "SessionModelSelectionRepository.upsert"
)
_EMIT_SELECTION = "personal_agent.transport.agui.transport.emit_session_selection"


def test_get_session_includes_primary_selection_hydration() -> None:
    """GET /sessions/{id} surfaces the resolved primary selection + provenance."""
    db_session = AsyncMock()
    db_session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=0.0)))
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, execution_profile="cloud")

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=session_model))
        stack.enter_context(
            patch(_SELECTION_GET, new_callable=AsyncMock, return_value="claude_sonnet")
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["primary_selection"] == "claude_sonnet"
    assert body["selection_provenance"] == "server-hydrated"


def test_get_session_selection_defaults_when_no_row() -> None:
    """GET hydration falls to the primary default (provenance=default) with no stored row."""
    db_session = AsyncMock()
    db_session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=0.0)))
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, execution_profile="local")

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=session_model))
        stack.enter_context(patch(_SELECTION_GET, new_callable=AsyncMock, return_value=None))
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}", headers=_AUTH_HEADERS)

    body = resp.json()
    assert body["primary_selection"] == "qwen3.6-35b-thinking"
    assert body["selection_provenance"] == "default"


def test_patch_selection_updates_and_emits() -> None:
    """PATCH .../selection persists an open-role selection (scoped) and emits STATE_DELTA."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid)
    get_mock = AsyncMock(return_value=session_model)

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, get_mock))
        upsert_mock = stack.enter_context(patch(_SELECTION_UPSERT, new_callable=AsyncMock))
        emit_mock = stack.enter_context(patch(_EMIT_SELECTION, new_callable=AsyncMock))
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.patch(
                f"/api/v1/sessions/{sid}/selection",
                json={"role": "primary", "deployment_key": "claude_sonnet"},
                headers=_AUTH_HEADERS,
            )

    assert resp.status_code == 200
    # Ownership invariant: the session read MUST be scoped to the resolved user_id.
    assert get_mock.await_args.kwargs.get("user_id") == _TEST_USER_ID
    upsert_mock.assert_awaited_once()
    assert upsert_mock.await_args.kwargs.get("deployment_key") == "claude_sonnet"
    emit_mock.assert_awaited_once()


def test_patch_selection_pinned_role_422_before_storage() -> None:
    """AC-4b — a selection naming a pinned role is rejected 422 before any storage."""
    db_session = AsyncMock()
    sid = str(uuid4())

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        upsert_mock = stack.enter_context(patch(_SELECTION_UPSERT, new_callable=AsyncMock))
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.patch(
                f"/api/v1/sessions/{sid}/selection",
                json={"role": "entity_extraction", "deployment_key": "claude_sonnet"},
                headers=_AUTH_HEADERS,
            )

    assert resp.status_code == 422
    upsert_mock.assert_not_awaited()  # nothing stored


def test_patch_selection_noncatalog_key_422_before_storage() -> None:
    """AC-4b — a non-catalog key for an open role is rejected 422 before any storage."""
    db_session = AsyncMock()
    sid = str(uuid4())

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        upsert_mock = stack.enter_context(patch(_SELECTION_UPSERT, new_callable=AsyncMock))
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.patch(
                f"/api/v1/sessions/{sid}/selection",
                json={"role": "primary", "deployment_key": "no_such_model_xyz"},
                headers=_AUTH_HEADERS,
            )

    assert resp.status_code == 422
    upsert_mock.assert_not_awaited()


def test_patch_selection_404_when_other_user_owns_it() -> None:
    """AC-6d — a PATCH from a different user's token returns 404 and stores nothing."""
    db_session = AsyncMock()
    sid = str(uuid4())

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=None))
        upsert_mock = stack.enter_context(patch(_SELECTION_UPSERT, new_callable=AsyncMock))
        stack.enter_context(patch(_EMIT_SELECTION, new_callable=AsyncMock))
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.patch(
                f"/api/v1/sessions/{sid}/selection",
                json={"role": "primary", "deployment_key": "claude_sonnet"},
                headers=_AUTH_HEADERS,
            )

    assert resp.status_code == 404
    upsert_mock.assert_not_awaited()  # stored value unchanged


# ---------------------------------------------------------------------------
# GET /api/v1/sessions/{id}/config — picker/observe-view read API (ADR-0121 §3, FRE-918)
# ---------------------------------------------------------------------------

_CHECK_ALL_PROVIDERS = "personal_agent.llm_client.provider_health.check_all_providers"
_SELECTION_GET_ALL = (
    "personal_agent.service.repositories.session_model_selection_repository."
    "SessionModelSelectionRepository.get_all"
)

_ALL_PROVIDERS_UP = {
    "slm_local": True,
    "ovh": True,
    "openai": True,
    "anthropic": True,
    "voyage": True,
}
_LOCAL_DOWN = {**_ALL_PROVIDERS_UP, "slm_local": False}


def test_get_session_config_basic_shape() -> None:
    """GET /sessions/{id}/config returns every declared role and every provider."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, execution_profile="local")

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=session_model))
        stack.enter_context(patch(_SELECTION_GET_ALL, new_callable=AsyncMock, return_value={}))
        stack.enter_context(
            patch(_CHECK_ALL_PROVIDERS, new_callable=AsyncMock, return_value=_ALL_PROVIDERS_UP)
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}/config", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    for role in (
        "primary",
        "artifact_builder",
        "sub_agent",
        "entity_extraction",
        "embedding",
        "reranker",
    ):
        assert role in body["roles"], role
    provider_keys = {p["key"] for p in body["providers"]}
    assert provider_keys == set(_ALL_PROVIDERS_UP)


def test_get_session_config_ac5_candidates_exclude_down_provider_both_directions() -> None:
    """AC-5 — primary's candidate list equals {kind=llm} minus the down provider's deployments."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, execution_profile="local")

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=session_model))
        stack.enter_context(patch(_SELECTION_GET_ALL, new_callable=AsyncMock, return_value={}))
        stack.enter_context(
            patch(_CHECK_ALL_PROVIDERS, new_callable=AsyncMock, return_value=_LOCAL_DOWN)
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}/config", headers=_AUTH_HEADERS)

    candidates = {c["key"] for c in resp.json()["roles"]["primary"]["candidates"]}
    assert candidates == {"claude_sonnet", "claude_haiku", "gpt-5.4-mini"}
    assert "qwen3.6-35b-thinking" not in candidates
    assert "qwen3.6-35b-instruct" not in candidates
    assert "embedding" not in candidates
    assert "reranker" not in candidates


def test_get_session_config_pinned_role_has_no_candidates() -> None:
    """A pinned role never carries a candidates list at all (§6 structural half)."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, execution_profile="local")

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=session_model))
        stack.enter_context(patch(_SELECTION_GET_ALL, new_callable=AsyncMock, return_value={}))
        stack.enter_context(
            patch(_CHECK_ALL_PROVIDERS, new_callable=AsyncMock, return_value=_ALL_PROVIDERS_UP)
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}/config", headers=_AUTH_HEADERS)

    entity_extraction = resp.json()["roles"]["entity_extraction"]
    assert entity_extraction["open"] is False
    assert "candidates" not in entity_extraction


def test_get_session_config_ac9_slice_reports_stored_selection_not_raw_default() -> None:
    """AC-9 slice — a stored selection wins over the raw binding default in the payload."""
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, execution_profile="local")

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=session_model))
        stack.enter_context(
            patch(
                _SELECTION_GET_ALL,
                new_callable=AsyncMock,
                return_value={"primary": "claude_sonnet"},
            )
        )
        stack.enter_context(
            patch(_CHECK_ALL_PROVIDERS, new_callable=AsyncMock, return_value=_ALL_PROVIDERS_UP)
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}/config", headers=_AUTH_HEADERS)

    primary = resp.json()["roles"]["primary"]
    assert primary["resolved"] == "claude_sonnet"  # NOT the raw default qwen3.6-35b-thinking
    assert primary["provenance"] == "server-hydrated"


def test_get_session_config_no_selection_row_falls_back_to_binding_default() -> None:
    """ADR-0121 T5 (FRE-920): a row-less session falls back to the plain binding
    default for every role — the profile bridge FRE-918 relied on is retired,
    since legacy POST /chat now always writes a selection-store row itself
    (closing the gap the bridge used to paper over).
    """
    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, execution_profile="cloud")

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=session_model))
        stack.enter_context(patch(_SELECTION_GET_ALL, new_callable=AsyncMock, return_value={}))
        stack.enter_context(
            patch(_CHECK_ALL_PROVIDERS, new_callable=AsyncMock, return_value=_ALL_PROVIDERS_UP)
        )
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/sessions/{sid}/config", headers=_AUTH_HEADERS)

    roles = resp.json()["roles"]
    assert roles["primary"]["resolved"] == "qwen3.6-35b-thinking"
    assert roles["artifact_builder"]["resolved"] == "claude_sonnet"
    assert roles["sub_agent"]["resolved"] == "claude_sonnet"


def test_get_session_config_selection_store_failure_logs_trace_id() -> None:
    """A selection-store read failure logs trace_id (ADR-0074) and degrades gracefully.

    Regression test for a finding from the code-review workflow: extracting the
    hydration logic into a shared helper dropped trace_id from this warning,
    breaking incident correlation across every role on this endpoint.
    """
    import structlog.testing

    db_session = AsyncMock()
    sid = str(uuid4())
    session_model = _make_session_model(session_id=sid, execution_profile="local")

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=session_model))
        stack.enter_context(
            patch(_SELECTION_GET_ALL, new_callable=AsyncMock, side_effect=RuntimeError("boom"))
        )
        stack.enter_context(
            patch(_CHECK_ALL_PROVIDERS, new_callable=AsyncMock, return_value=_ALL_PROVIDERS_UP)
        )
        app = _build_app_with_db_factory(db_session)
        with structlog.testing.capture_logs() as captured:
            with TestClient(app, raise_server_exceptions=True) as client:
                resp = client.get(f"/api/v1/sessions/{sid}/config", headers=_AUTH_HEADERS)

    assert resp.status_code == 200  # degrades to binding defaults, never 500s
    failure_log = next(e for e in captured if e.get("event") == "selection_store_hydration_failed")
    assert failure_log.get("trace_id")


def test_get_session_config_404_when_other_user_owns_it() -> None:
    """GET /sessions/{id}/config returns 404 (not 403) when another user owns the session."""
    db_session = AsyncMock()
    sid = str(uuid4())

    with ExitStack() as stack:
        stack.enter_context(_patched_user_resolver())
        stack.enter_context(patch(_SESSION_GET, new_callable=AsyncMock, return_value=None))
        app = _build_app_with_db_factory(db_session)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/v1/sessions/{sid}/config", headers=_AUTH_HEADERS)

    assert resp.status_code == 404


def test_get_session_config_invalid_uuid_422() -> None:
    """GET /sessions/{id}/config returns 422 for a non-UUID session_id."""
    db_session = AsyncMock()
    app = _build_app_with_db_factory(db_session)

    with _patched_user_resolver():
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/sessions/not-a-uuid/config", headers=_AUTH_HEADERS)

    assert resp.status_code == 422


def test_get_session_config_401_without_cf_access_header() -> None:
    """GET /sessions/{id}/config returns 401 when the CF Access header is absent."""
    db_session = AsyncMock()
    sid = str(uuid4())
    app = _build_app_with_db_factory(db_session)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/api/v1/sessions/{sid}/config")

    assert resp.status_code == 401
