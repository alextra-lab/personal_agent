"""FRE-1622 (ADR-0157 D2, D8): the level on both chat entry paths (AC-3, AC-6).

For ``POST /chat/stream`` and ``POST /chat``:

* a new session stores the sent level, or ``standard`` when none is sent;
* an existing session ignores a sent level;
* a change during a turn leaves that turn's spec unchanged;
* the launch event carries the level.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import structlog.testing

from personal_agent.config.seshat_effort import get_current_effort
from personal_agent.service.app import (
    _process_chat_stream_background,
    _resolve_session_effort,
    chat,
    chat_stream_endpoint,
)
from personal_agent.service.auth import RequestUser
from personal_agent.service.idempotency import DeduplicationResult

_USER = RequestUser(user_id=uuid4(), email="test@example.com")


def _level() -> str | None:
    spec = get_current_effort()
    return spec.level if spec is not None else None


class _OneColumnDb:
    """A session whose ``execute`` returns one stored value (or ``None`` for no row)."""

    def __init__(self, stored: str | None) -> None:
        self.stored = stored

    async def execute(self, _stmt: Any) -> Any:
        stored = self.stored
        return SimpleNamespace(scalar_one_or_none=lambda: stored)


def _session_local(db: Any) -> Any:
    @asynccontextmanager
    async def _cm() -> Any:
        yield db

    return lambda: _cm()


# ── POST /chat/stream ─────────────────────────────────────────────────────────


class TestStreamResolver:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stored", "sent", "expected"),
        [
            (None, "fast", ("fast", "adopted")),
            (None, None, ("standard", "default")),
            ("deep", "fast", ("deep", "stored")),
            ("deep", None, ("deep", "stored")),
        ],
    )
    async def test_new_stores_the_sent_level_and_existing_ignores_it(
        self, stored: str | None, sent: str | None, expected: tuple[str, str]
    ) -> None:
        with patch(
            "personal_agent.service.app.AsyncSessionLocal", _session_local(_OneColumnDb(stored))
        ):
            assert await _resolve_session_effort(str(uuid4()), sent, _USER.user_id) == expected  # type: ignore[arg-type]


class TestStreamEndpoint:
    @pytest.mark.asyncio
    async def test_the_launch_hands_the_resolved_level_to_the_turn_and_logs_it(self) -> None:
        launched: dict[str, Any] = {}

        def _capture_task(coro: Any) -> MagicMock:
            launched.update(coro.cr_frame.f_locals)
            coro.close()
            return MagicMock()

        dedup = MagicMock()
        dedup.check_and_record.return_value = DeduplicationResult(is_duplicate=False)
        with (
            patch("personal_agent.service.app.get_deduplicator", return_value=dedup),
            patch(
                "personal_agent.service.app._resolve_session_selection",
                new=AsyncMock(return_value=("qwen3.8-flash-next", "default")),
            ),
            patch(
                "personal_agent.service.app.AsyncSessionLocal", _session_local(_OneColumnDb(None))
            ),
            patch.object(asyncio, "create_task", side_effect=_capture_task),
            structlog.testing.capture_logs() as logs,
        ):
            body = await chat_stream_endpoint(
                message="hello",
                session_id=str(uuid4()),
                primary_selection=None,
                seshat_effort="fast",
                client_msg_id=None,
                attachments=None,
                request_user=_USER,
            )

        assert launched["seshat_effort"] == "fast"
        assert (body["seshat_effort"], body["effort_provenance"]) == ("fast", "adopted")
        (event,) = [e for e in logs if e.get("event") == "chat_stream.launched"]
        assert event["seshat_effort"] == "fast"


@pytest.fixture
def _no_turn_registry_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "personal_agent.transport.agui.transport.open_turn", AsyncMock(return_value=None)
    )


@pytest.mark.usefixtures("_no_turn_registry_db")
class TestStreamTurn:
    async def _run(self, session: Any, *, seshat_effort: str | None) -> dict[str, Any]:
        """Drive the background turn with the substrate stubbed; record what the turn saw."""
        seen: dict[str, Any] = {}
        db = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        repo = MagicMock()
        repo.get = AsyncMock(return_value=session)
        repo.append_message = AsyncMock(return_value=None)

        async def _pipeline(*_a: object, **_k: object) -> MagicMock:
            seen["at_gateway"] = _level()
            # A PATCH lands mid-turn: the stored level changes under the running turn.
            if session is not None:
                session.seshat_effort = "deep"
            await asyncio.sleep(0)
            return MagicMock()

        async def _handle(*_a: object, **_k: object) -> dict[str, str]:
            seen["at_orchestrator"] = _level()
            return {"reply": "hi", "trace_id": "t"}

        orchestrator = MagicMock()
        orchestrator.session_manager = MagicMock(get_session=MagicMock(return_value=None))
        orchestrator.handle_user_request = AsyncMock(side_effect=_handle)
        with (
            patch("personal_agent.service.app.AsyncSessionLocal", _session_local(db)),
            patch("personal_agent.service.app.SessionRepository", return_value=repo),
            patch("personal_agent.service.app.run_gateway_pipeline", side_effect=_pipeline),
            patch("personal_agent.orchestrator.Orchestrator", return_value=orchestrator),
            patch(
                "personal_agent.service.app._append_assistant_message_background",
                new_callable=AsyncMock,
            ),
            patch(
                "personal_agent.service.app._validate_attachments",
                new=AsyncMock(return_value=[]),
            ),
            patch("personal_agent.transport.agui.transport.emit_done", new_callable=AsyncMock),
            patch("personal_agent.transport.agui.transport._push_event", new_callable=AsyncMock),
            patch(
                "personal_agent.transport.agui.transport.emit_session_effort",
                new_callable=AsyncMock,
            ) as emit,
        ):
            await _process_chat_stream_background(
                session_id=str(uuid4()),
                message="hello",
                user_id=_USER.user_id,
                trace_id="t",
                seshat_effort=seshat_effort,  # type: ignore[arg-type]
            )
        seen["added"] = [c.args[0] for c in db.add.call_args_list]
        seen["emitted"] = emit.await_args_list
        return seen

    @pytest.mark.asyncio
    async def test_a_new_session_row_stores_the_level(self) -> None:
        seen = await self._run(None, seshat_effort="fast")
        (row,) = seen["added"]
        assert row.seshat_effort == "fast"
        assert seen["emitted"][0].kwargs["seshat_effort"] == "fast"
        assert seen["at_gateway"] == seen["at_orchestrator"] == "fast"

    @pytest.mark.asyncio
    async def test_a_change_during_the_turn_leaves_its_spec_unchanged(self) -> None:
        session = SimpleNamespace(
            session_id=uuid4(), messages=[], execution_profile="local", seshat_effort="fast"
        )
        seen = await self._run(session, seshat_effort="fast")
        assert session.seshat_effort == "deep"  # the PATCH landed
        assert seen["at_gateway"] == seen["at_orchestrator"] == "fast"


# ── POST /chat ────────────────────────────────────────────────────────────────


class TestChat:
    async def _run(
        self, session: Any, *, session_id: str | None, seshat_effort: str | None
    ) -> tuple[dict[str, Any], MagicMock, list[Any]]:
        seen: dict[str, Any] = {}
        repo = MagicMock()
        repo.get = AsyncMock(return_value=session)
        repo.create = AsyncMock(return_value=session)
        repo.append_message = AsyncMock(return_value=None)

        async def _pipeline(*_a: object, **_k: object) -> MagicMock:
            seen["at_gateway"] = _level()
            session.seshat_effort = "fast" if session.seshat_effort != "fast" else "deep"
            await asyncio.sleep(0)
            return MagicMock()

        async def _handle(*_a: object, **_k: object) -> dict[str, str]:
            seen["at_orchestrator"] = _level()
            return {"reply": "hi", "trace_id": "t"}

        orchestrator = MagicMock()
        orchestrator.session_manager = MagicMock(get_session=MagicMock(return_value=None))
        orchestrator.handle_user_request = AsyncMock(side_effect=_handle)
        with (
            patch("personal_agent.service.app.SessionRepository", return_value=repo),
            patch("personal_agent.service.app.run_gateway_pipeline", side_effect=_pipeline),
            patch("personal_agent.orchestrator.Orchestrator", return_value=orchestrator),
            patch(
                "personal_agent.service.app._resolve_session_selection",
                new=AsyncMock(return_value=("qwen3.8-flash-next", "stored")),
            ),
            patch(
                "personal_agent.service.repositories.session_model_selection_repository."
                "SessionModelSelectionRepository.upsert",
                new_callable=AsyncMock,
            ),
            structlog.testing.capture_logs() as logs,
        ):
            body = await chat(
                message="hello",
                session_id=session_id,
                model=None,
                channel="CHAT",
                seshat_effort=seshat_effort,  # type: ignore[arg-type]
                request_user=_USER,
                db=AsyncMock(),
            )
        seen["body"] = body
        return seen, repo, logs

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("sent", "stored"), [("fast", "fast"), (None, "standard")])
    async def test_a_new_session_stores_the_sent_level_or_standard(
        self, sent: str | None, stored: str
    ) -> None:
        session = SimpleNamespace(
            session_id=uuid4(), messages=[], execution_profile="local", seshat_effort=stored
        )
        seen, repo, logs = await self._run(session, session_id=None, seshat_effort=sent)
        assert repo.create.await_args.kwargs["seshat_effort"] == stored
        assert seen["at_gateway"] == stored
        (event,) = [e for e in logs if e.get("event") == "chat.launched"]
        assert event["seshat_effort"] == stored
        assert event["effort_provenance"] == ("adopted" if sent else "default")
        assert event["trace_id"]

    @pytest.mark.asyncio
    async def test_an_existing_session_ignores_a_sent_level(self) -> None:
        session = SimpleNamespace(
            session_id=uuid4(), messages=[], execution_profile="local", seshat_effort="deep"
        )
        seen, repo, logs = await self._run(
            session, session_id=str(session.session_id), seshat_effort="fast"
        )
        repo.create.assert_not_awaited()
        assert seen["at_gateway"] == "deep"
        assert seen["body"]["seshat_effort"] == "deep"
        (event,) = [e for e in logs if e.get("event") == "chat.launched"]
        assert (event["seshat_effort"], event["effort_provenance"]) == ("deep", "stored")

    @pytest.mark.asyncio
    async def test_a_change_during_the_turn_leaves_its_spec_unchanged(self) -> None:
        session = SimpleNamespace(
            session_id=uuid4(), messages=[], execution_profile="local", seshat_effort="deep"
        )
        seen, _, _ = await self._run(
            session, session_id=str(session.session_id), seshat_effort=None
        )
        assert session.seshat_effort == "fast"  # the PATCH landed
        assert seen["at_gateway"] == seen["at_orchestrator"] == "deep"
