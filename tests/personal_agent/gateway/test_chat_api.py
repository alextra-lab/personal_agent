"""Tests for the gateway chat API endpoint (FRE-235 + cross-user leak hotfix).

Uses FastAPI's TestClient with mocked SessionRepository and Anthropic client.
Background streaming tasks are patched out for unit tests — only the
synchronous contract of the endpoint is verified here. After the cross-user
hotfix the endpoint requires authentication; each test attaches the CF
Access header and overrides the FastAPI dependency to return a stable
mock user identity.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.gateway.chat_api import router as chat_router
from personal_agent.service.auth import RequestUser, get_request_user

_TEST_USER = RequestUser(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    email="tester@example.com",
    display_name=None,
)
_AUTH_HEADERS = {"Cf-Access-Authenticated-User-Email": "tester@example.com"}


def _override_user(app: FastAPI) -> None:
    """Override the get_request_user FastAPI dependency for unit tests."""
    app.dependency_overrides[get_request_user] = lambda: _TEST_USER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_model(session_id: str | None = None) -> Any:
    """Build a minimal mock SessionModel for a cloud chat session."""
    sid = session_id or str(uuid4())
    session = MagicMock()
    session.session_id = sid
    session.created_at = datetime(2026, 1, 1, 10, 0, 0)
    session.last_active_at = datetime(2026, 1, 1, 10, 5, 0)
    session.mode = "NORMAL"
    session.channel = "CHAT"
    session.messages = []
    return session


def _build_app() -> FastAPI:
    """Build a minimal test FastAPI app with the chat router mounted."""
    app = FastAPI()
    app.include_router(chat_router)
    _override_user(app)
    return app


# ---------------------------------------------------------------------------
# POST /chat — response shape
# ---------------------------------------------------------------------------


def test_chat_starts_streaming() -> None:
    """POST /chat returns session_id, trace_id, and status=streaming immediately."""
    sid = str(uuid4())
    mock_session = _make_session_model(session_id=sid)

    with (
        patch(
            "personal_agent.gateway.chat_api.get_settings",
            return_value=MagicMock(anthropic_api_key="sk-test"),
        ),
        patch(
            "personal_agent.gateway.chat_api.AsyncSessionLocal",
        ) as mock_session_local,
        patch(
            "personal_agent.service.repositories.session_repository.SessionRepository.get",
            new_callable=AsyncMock,
            return_value=mock_session,
        ),
        patch("asyncio.create_task") as mock_create_task,
    ):
        # Make AsyncSessionLocal a context manager that yields an AsyncMock db session
        mock_db = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_local.return_value = mock_ctx

        app = _build_app()
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                "/chat",
                data={"message": "Hello", "session_id": sid},
                headers=_AUTH_HEADERS,
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == sid
    assert data["status"] == "streaming"
    # FRE-1231: trace_id now comes from read_or_mint_trace_id() (ADR-0129 D1), which
    # renders 32 lowercase hex chars (the OTel shape) rather than a dashed UUID —
    # matching the id every log record of this request carries once the standalone
    # gateway has an active root span. No repository consumer parses this field
    # (grep-verified against seshat-pwa and the CLI); only this test asserted the
    # old dashed shape.
    assert "trace_id" in data
    assert len(data["trace_id"]) == 32
    assert data["trace_id"].count("-") == 0
    assert all(c in "0123456789abcdef" for c in data["trace_id"])
    mock_create_task.assert_called_once()


def test_chat_normalizes_prior_list_content_to_text() -> None:
    """List-shaped prior content (ADR-0101 §2) is extracted to text, not stringified (FRE-709).

    ``str(list)`` would send Anthropic a Python-repr string (e.g. ``"[{'type': ...}]"``)
    instead of the actual text — corrupting history the first time a real image block
    lands in persisted session messages.
    """
    sid = str(uuid4())
    mock_session = _make_session_model(session_id=sid)
    mock_session.messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "prior answer"},
                {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
            ],
        }
    ]

    with (
        patch(
            "personal_agent.gateway.chat_api.get_settings",
            return_value=MagicMock(anthropic_api_key="sk-test"),
        ),
        patch(
            "personal_agent.gateway.chat_api.AsyncSessionLocal",
        ) as mock_session_local,
        patch(
            "personal_agent.service.repositories.session_repository.SessionRepository.get",
            new_callable=AsyncMock,
            return_value=mock_session,
        ),
        patch("asyncio.create_task") as mock_create_task,
        patch("personal_agent.gateway.chat_api._stream_to_queue") as mock_stream_to_queue,
    ):
        mock_db = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_local.return_value = mock_ctx

        app = _build_app()
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                "/chat",
                data={"message": "follow-up", "session_id": sid},
                headers=_AUTH_HEADERS,
            )

    assert resp.status_code == 200
    mock_create_task.assert_called_once()
    mock_stream_to_queue.assert_called_once()
    anthropic_messages = mock_stream_to_queue.call_args.kwargs["anthropic_messages"]
    assert anthropic_messages[0]["content"] == "prior answer"


def test_chat_invalid_uuid() -> None:
    """POST /chat with a non-UUID session_id returns 422."""
    with (
        patch(
            "personal_agent.gateway.chat_api.get_settings",
            return_value=MagicMock(anthropic_api_key="sk-test"),
        ),
    ):
        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/chat",
                data={"message": "Hello", "session_id": "not-a-uuid"},
                headers=_AUTH_HEADERS,
            )

    assert resp.status_code == 422


def test_chat_missing_api_key() -> None:
    """POST /chat returns 503 when no Anthropic API key is configured."""
    sid = str(uuid4())

    with (
        patch(
            "personal_agent.gateway.chat_api.get_settings",
            return_value=MagicMock(anthropic_api_key=None),
        ),
    ):
        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/chat",
                data={"message": "Hello", "session_id": sid},
                headers=_AUTH_HEADERS,
            )

    assert resp.status_code == 503
    assert "Anthropic API key" in resp.json()["detail"]


def test_chat_401_without_cf_access_header() -> None:
    """POST /chat returns 401 when the CF Access header is absent.

    Patches ``gateway_auth_enabled=True`` and clears ``agent_owner_email`` so
    the dev fallback in :func:`get_request_user` cannot fire and confuse the
    test.
    """
    sid = str(uuid4())
    mock_settings = MagicMock(
        gateway_auth_enabled=True,
        agent_owner_email=None,
        anthropic_api_key="sk-test",
    )
    with (
        patch("personal_agent.service.auth.settings", mock_settings),
        patch("personal_agent.gateway.chat_api.get_settings", return_value=mock_settings),
    ):
        app = FastAPI()
        app.include_router(chat_router)  # NO _override_user — real dep fires
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/chat",
                data={"message": "Hello", "session_id": sid},
            )
    assert resp.status_code == 401


def test_chat_404_when_session_owned_by_other_user() -> None:
    """POST /chat returns 404 when session exists but belongs to another user.

    repo.get(uuid, user_id=A) → None (ownership mismatch), repo.get(uuid)
    → other_session (exists under user B). Endpoint must return 404 to
    avoid confirming existence of other users' sessions, never INSERT.
    """
    sid = str(uuid4())
    other_session = _make_session_model(session_id=sid)

    repo_get = AsyncMock(side_effect=[None, other_session])

    with (
        patch(
            "personal_agent.gateway.chat_api.get_settings",
            return_value=MagicMock(anthropic_api_key="sk-test"),
        ),
        patch("personal_agent.gateway.chat_api.AsyncSessionLocal") as mock_session_local,
        patch(
            "personal_agent.service.repositories.session_repository.SessionRepository.get",
            repo_get,
        ),
    ):
        mock_db = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_local.return_value = mock_ctx

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/chat",
                data={"message": "Hello", "session_id": sid},
                headers=_AUTH_HEADERS,
            )

    assert resp.status_code == 404
    # Both scoped and unscoped lookups must have fired.
    assert repo_get.await_count == 2
    # First call scoped to the calling user, second unscoped (to detect
    # cross-user attempt).
    first_call_kwargs = repo_get.await_args_list[0].kwargs
    assert first_call_kwargs.get("user_id") == _TEST_USER.user_id


# ---------------------------------------------------------------------------
# Gateway telemetry dark-path closure (ADR-0078 D4 / FRE-405)
# ---------------------------------------------------------------------------


def test_gateway_emits_model_call_completed_with_identity() -> None:
    """The gateway success path emits a canonical model_call_completed stamped
    with callsite='gateway.chat' (was previously untelemetered).
    """
    from personal_agent.gateway.chat_api import _emit_gateway_model_call_completed

    usage = MagicMock(input_tokens=120, output_tokens=45)
    final_message = MagicMock(usage=usage)

    with patch("personal_agent.llm_client.telemetry.emit_model_call_completed") as mock_emit:
        _emit_gateway_model_call_completed(
            trace_id="trace-xyz",
            session_id="11111111-1111-1111-1111-111111111111",
            span_id="0123456789abcdef",
            final_message=final_message,
        )

    mock_emit.assert_called_once()
    kwargs = mock_emit.call_args.kwargs
    identity = kwargs["prompt_identity"]
    assert identity.callsite == "gateway.chat"
    assert identity.component_ids == ("gateway_persona",)
    assert kwargs["input_tokens"] == 120
    assert kwargs["output_tokens"] == 45
    assert kwargs["trace_ctx"].trace_id == "trace-xyz"
    assert kwargs["trace_ctx"].session_id == "11111111-1111-1111-1111-111111111111"


def test_gateway_emit_tolerates_missing_usage() -> None:
    """No usage on the final message → emit still fires with None token counts."""
    from personal_agent.gateway.chat_api import _emit_gateway_model_call_completed

    with patch("personal_agent.llm_client.telemetry.emit_model_call_completed") as mock_emit:
        _emit_gateway_model_call_completed(
            trace_id="t",
            session_id="s",
            span_id="fedcba9876543210",
            final_message=None,
        )

    mock_emit.assert_called_once()
    kwargs = mock_emit.call_args.kwargs
    assert kwargs["input_tokens"] is None
    assert kwargs["output_tokens"] is None
    assert kwargs["prompt_identity"].callsite == "gateway.chat"


# ---------------------------------------------------------------------------
# request.completed field quality (FRE-1033 second defect)
# ---------------------------------------------------------------------------
#
# This publish site is confirmed unreachable in production today (the
# "seshat-gateway" container actually runs personal_agent.service.app:app, not
# gateway.app:gateway_app — the only place chat_router is mounted).


class _FakeAnthropicStream:
    """Minimal async context manager mirroring anthropic's MessageStreamManager."""

    def __init__(self, text_chunks: list[str], final_message: Any) -> None:
        self._text_chunks = text_chunks
        self._final_message = final_message

    async def __aenter__(self) -> "_FakeAnthropicStream":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    @property
    async def text_stream(self):  # type: ignore[no-untyped-def]
        for chunk in self._text_chunks:
            yield chunk

    async def get_final_message(self) -> Any:
        return self._final_message


def test_stream_to_queue_publishes_user_id_and_no_timer_fields() -> None:
    """Redis branch publishes identity fields; carries no RequestTimer payload.

    ADR-0129 D3 / FRE-1067 retired RequestTimer and the
    trace_summary/trace_breakdown fields it fed — span timing now lives in
    the OTel span tree (the model-call span this function opens), not this
    event.
    """
    import asyncio

    import redis.asyncio as aioredis

    from personal_agent.events.bus import NoOpBus, set_global_event_bus
    from personal_agent.events.redis_backend import RedisStreamBus
    from personal_agent.gateway.chat_api import _stream_to_queue

    user_id = uuid4()
    session_uuid = uuid4()
    final_message = MagicMock(usage=MagicMock(input_tokens=10, output_tokens=5))

    mock_redis = AsyncMock(spec=aioredis.Redis)
    mock_redis.xadd = AsyncMock(return_value="1-0")
    bus = RedisStreamBus(mock_redis)
    set_global_event_bus(bus)
    try:
        with (
            patch("personal_agent.gateway.chat_api.anthropic.AsyncAnthropic") as mock_anthropic_cls,
            patch("personal_agent.transport.agui.transport._push_event", new_callable=AsyncMock),
            patch("personal_agent.transport.agui.transport.emit_done", new_callable=AsyncMock),
            patch("personal_agent.gateway.chat_api._emit_gateway_model_call_completed"),
            patch(
                "personal_agent.gateway.chat_api._record_gateway_cost_safe",
                new_callable=AsyncMock,
            ),
        ):
            mock_client = MagicMock()
            mock_client.messages.stream = MagicMock(
                return_value=_FakeAnthropicStream(["Hello", " world"], final_message)
            )
            mock_anthropic_cls.return_value = mock_client

            asyncio.run(
                _stream_to_queue(
                    trace_id="trace-gw-1",
                    session_uuid=session_uuid,
                    anthropic_messages=[{"role": "user", "content": "hi"}],
                    api_key="fake-key",
                    user_id=user_id,
                )
            )
    finally:
        set_global_event_bus(NoOpBus())

    mock_redis.xadd.assert_called_once()
    import orjson

    fields = mock_redis.xadd.call_args[0][1]
    payload = orjson.loads(fields["data"])
    assert payload["user_id"] == str(user_id)
    assert payload["source_component"] == "gateway.chat_api"
    assert payload["assistant_response"] == "Hello world"
    assert "trace_summary" not in payload
    assert "trace_breakdown" not in payload
