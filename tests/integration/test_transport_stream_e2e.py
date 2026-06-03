"""Tier-2 transport end-to-end test — real Postgres SessionEventBuffer (FRE-400/FRE-390).

Requires the isolated test substrate:

    make test-infra-up   # Postgres on :5433, ES on :9201, Neo4j on :7688

Invoked by CI's ``backend-integration`` job (and locally via
``PERSONAL_AGENT_INTEGRATION=1 make test-integration``).

This test exercises the full dual-write path (Postgres ``session_events``
table + asyncio.Queue) and the reconnect replay sequence — the exact
gap called out in FRE-390: "no test opens a real WS connection and asserts
on the received event sequence."
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from personal_agent.service.auth import RequestUser
from personal_agent.service.database import AsyncSessionLocal

pytestmark = pytest.mark.integration

_TEST_USER_ID: UUID = uuid4()
_TEST_USER = RequestUser(
    user_id=_TEST_USER_ID,
    email="integration-test@example.com",
    display_name="Integration Test User",
)


# ── Helpers ────────────────────────────────────────────────────────────────────


class _FakeSessionRepository:
    """Always reports a found session (no DB lookup for ownership check)."""

    def __init__(self, db: Any) -> None:
        pass

    async def get(self, session_id: UUID, user_id: UUID | None = None) -> object:
        return object()


@contextmanager
def _ws_connect(
    client: TestClient,
    session_id: str,
    last_seq: int = 0,
) -> Generator[Any, None, None]:
    """Open a WS connection and perform the mandatory CONNECT handshake."""
    with client.websocket_connect(f"/ws/{session_id}") as ws:
        ws.send_json({"type": "CONNECT", "last_seq": last_seq})
        yield ws


def _build_integration_app(mp: pytest.MonkeyPatch) -> FastAPI:
    """Build a test app that uses the REAL SessionEventBuffer (real Postgres).

    Only ``_authenticate_ws`` and ``SessionRepository`` are mocked; everything
    else (``AsyncSessionLocal``, ``SessionEventBuffer``) uses the real test
    substrate on port 5433.
    """
    from personal_agent.transport.agui import ws_endpoint as _wsep
    from personal_agent.transport.agui.ws_endpoint import ws_router

    async def _fake_authenticate(websocket: Any, session_id_str: str) -> RequestUser:
        return _TEST_USER

    mp.setattr(_wsep, "_authenticate_ws", _fake_authenticate)
    mp.setattr(_wsep, "SessionRepository", _FakeSessionRepository)
    from personal_agent.config import settings as _settings

    mp.setattr(_settings, "gateway_auth_enabled", False)

    inject_router = APIRouter()

    @inject_router.post("/__test/text_delta")
    async def _inject_text_delta(session_id: str, text: str) -> dict[str, str]:
        from personal_agent.transport.agui.transport import AGUITransport

        await AGUITransport().send_text_delta(text=text, session_id=session_id)
        return {"ok": "sent"}

    @inject_router.post("/__test/done")
    async def _inject_done(session_id: str) -> dict[str, str]:
        from personal_agent.transport.agui.ws_endpoint import get_event_queue

        await get_event_queue(session_id).put(None)
        return {"ok": "sent"}

    app = FastAPI()
    app.include_router(ws_router)
    app.include_router(inject_router)
    return app


async def _pg_available() -> bool:
    """Check whether the test Postgres (port 5433) is reachable."""
    try:
        async with AsyncSessionLocal() as db:
            from sqlalchemy import text

            await db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestTransportStreamE2E:
    """FRE-390 gap: real WS connection + real Postgres event buffer."""

    @pytest.fixture
    def integration_harness(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[TestClient, None, None]:
        """Build the test app backed by real Postgres and yield a TestClient."""
        app = _build_integration_app(monkeypatch)
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client

    def test_skip_if_postgres_unavailable(self, integration_harness: TestClient) -> None:
        """Marker: this test class requires the test Postgres substrate."""
        import asyncio

        if not asyncio.get_event_loop().run_until_complete(_pg_available()):
            pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

    def test_events_delivered_with_persisted_seq(
        self, integration_harness: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Events are persisted to Postgres and delivered with monotonic seq over WS."""
        import asyncio

        if not asyncio.get_event_loop().run_until_complete(_pg_available()):
            pytest.skip("Test Postgres not reachable")

        client = integration_harness
        session_id = str(uuid4())

        with _ws_connect(client, session_id) as ws:
            for text in ("alpha", "beta", "gamma"):
                client.post("/__test/text_delta", params={"session_id": session_id, "text": text})
            client.post("/__test/done", params={"session_id": session_id})

            msgs: list[dict[str, Any]] = []
            while True:
                msg = ws.receive_json()
                msgs.append(msg)
                if msg["type"] == "DONE":
                    break

        deltas = [m for m in msgs if m["type"] == "TEXT_DELTA"]
        assert len(deltas) == 3

        # Seqs are Postgres-assigned — must be positive integers in order.
        seqs = [m["seq"] for m in deltas]
        assert all(isinstance(s, int) and s > 0 for s in seqs)
        assert seqs == sorted(seqs), "seqs must be monotonically increasing"
        assert len(set(seqs)) == 3, "seqs must be unique"
        assert [m["data"]["text"] for m in deltas] == ["alpha", "beta", "gamma"]

    def test_reconnect_replays_persisted_events(
        self, integration_harness: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disconnect mid-stream and reconnect: missed events replayed from Postgres."""
        import asyncio

        if not asyncio.get_event_loop().run_until_complete(_pg_available()):
            pytest.skip("Test Postgres not reachable")

        client = integration_harness
        session_id = str(uuid4())
        received_seqs: list[int] = []

        # First connection: inject 3 events, receive only the first two.
        with _ws_connect(client, session_id, last_seq=0) as ws:
            for text in ("one", "two", "three"):
                client.post("/__test/text_delta", params={"session_id": session_id, "text": text})

            msg1 = ws.receive_json()
            assert msg1["type"] == "TEXT_DELTA"
            assert msg1["data"]["text"] == "one"
            received_seqs.append(msg1["seq"])

            msg2 = ws.receive_json()
            assert msg2["type"] == "TEXT_DELTA"
            assert msg2["data"]["text"] == "two"
            received_seqs.append(msg2["seq"])

            # Disconnect here — msg3 ("three") was persisted but not delivered.
            last_seq = msg2["seq"]

        # Second connection: reconnect with last_seq = after msg2.
        with _ws_connect(client, session_id, last_seq=last_seq) as ws:
            replayed = ws.receive_json()
            assert replayed["type"] == "TEXT_DELTA"
            assert replayed["data"]["text"] == "three"
            assert replayed["seq"] > last_seq

            client.post("/__test/done", params={"session_id": session_id})
            done = ws.receive_json()
            assert done["type"] == "DONE"
