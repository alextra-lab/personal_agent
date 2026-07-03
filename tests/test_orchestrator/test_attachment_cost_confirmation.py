"""Durable cloud-attachment confirmation persistence + re-injection (FRE-749 / ADR-0101 §8b).

The cost gate pauses on turn 1 when an image's cloud-vision estimate exceeds the
threshold; the user's affirmative reply arrives on turn 2 — a *separate HTTP
request* served by a fresh ``Orchestrator`` + in-memory ``SessionManager``. The
pending attachment therefore cannot live in the in-memory session; it is
persisted to the durable ``sessions.metadata`` JSONB column and reloaded on the
next turn. These tests prove:

* the pure TTL helper and affirmative-detection logic;
* the ``SessionRepository`` key-level JSONB SQL round-trips (mock-DB);
* the topology-accurate re-injection: pending saved in one context is reloaded
  and re-injected in a *separate* context that shares only the session id — no
  shared ``SessionManager`` (fake durable store, hermetic);
* the true cross-connection durability against real Postgres (integration).
"""

from __future__ import annotations

import socket
import time
from dataclasses import asdict
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator import executor as executor_mod
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import (
    AttachmentRef,
    ExecutionContext,
    PendingCloudAttachmentConfirmation,
)


def _pending(created_at: float | None = None, ttl_seconds: int = 600) -> dict[str, Any]:
    """Return a serialized pending-confirmation payload for one image attachment."""
    return asdict(
        PendingCloudAttachmentConfirmation(
            attachments=(
                AttachmentRef(
                    artifact_id="art-1",
                    content_type="image/jpeg",
                    title="test.jpg",
                    r2_key="uploads/test.jpg",
                ),
            ),
            cloud_vision_model_key="claude_sonnet",
            estimate_usd=0.75,
            created_at=created_at if created_at is not None else time.time(),
            ttl_seconds=ttl_seconds,
            original_trace_id="trace-1",
        )
    )


# ---------------------------------------------------------------------------
# Pure TTL helper
# ---------------------------------------------------------------------------


class TestPendingExpiry:
    """`_pending_is_expired` is pure — no DB, exercised directly."""

    def test_not_expired_within_ttl(self) -> None:
        """A payload inside its TTL window is not expired."""
        now = time.time()
        assert (
            executor_mod._pending_is_expired({"created_at": now, "ttl_seconds": 600}, now) is False
        )

    def test_expired_after_ttl(self) -> None:
        """A payload past created_at + ttl_seconds is expired."""
        now = time.time()
        assert (
            executor_mod._pending_is_expired({"created_at": now - 1000, "ttl_seconds": 600}, now)
            is True
        )

    def test_missing_fields_treated_as_expired(self) -> None:
        """A payload missing created_at/ttl_seconds is treated as expired."""
        # A malformed record is dropped rather than replayed.
        assert executor_mod._pending_is_expired({}, time.time()) is True
        assert executor_mod._pending_is_expired({"created_at": 0.0}, time.time()) is True


# ---------------------------------------------------------------------------
# Affirmative-confirmation detection
# ---------------------------------------------------------------------------


class TestAffirmativeConfirmationDetection:
    """`_is_affirmative_confirmation` — strict, avoids incidental-"yes" false positives."""

    def test_affirmative_messages_detected(self) -> None:
        """Common confirmation phrases are detected as affirmative."""
        for msg in [
            "proceed",
            "yes",
            "ok",
            "okay",
            "confirm",
            "cloud",
            "Proceed on cloud",
            "Yes, proceed",
            "proceed on cloud please",
            "PROCEED",
            "  yes  ",
        ]:
            assert executor_mod._is_affirmative_confirmation(msg), f"should be affirmative: {msg!r}"

    def test_non_affirmative_messages_rejected(self) -> None:
        """Ambiguous or unrelated messages are not treated as affirmative."""
        for msg in [
            "keep it local",
            "no cloud",
            "Use the local model",
            "What does the image show?",
            "Yes, I agree with that",
            "Is that a yes or no?",
            "",
            "   ",
        ]:
            assert not executor_mod._is_affirmative_confirmation(msg), (
                f"should NOT be affirmative: {msg!r}"
            )


# ---------------------------------------------------------------------------
# SessionRepository key-level JSONB SQL (mock-DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSessionRepositoryPendingSQL:
    """The repo uses key-scoped JSONB SQL so a sibling metadata writer is not clobbered."""

    def _repo(self, execute_result: Any = None):
        from personal_agent.service.repositories.session_repository import SessionRepository

        db = MagicMock()
        db.execute = AsyncMock(return_value=execute_result)
        db.commit = AsyncMock()
        return SessionRepository(db), db

    async def test_save_uses_jsonb_set_and_returns_rowcount(self) -> None:
        """Save writes via jsonb_set with the payload+sid params and returns rowcount."""
        result = MagicMock()
        result.rowcount = 1
        repo, db = self._repo(result)
        sid = uuid4()

        rows = await repo.save_pending_confirmation(sid, {"k": "v"})

        assert rows == 1
        sql = str(db.execute.await_args.args[0])
        assert "jsonb_set" in sql
        params = db.execute.await_args.args[1]
        assert params["sid"] == str(sid)
        assert '"k": "v"' in params["payload"]  # JSON-serialized payload
        db.commit.assert_awaited_once()

    async def test_save_returns_zero_when_no_row(self) -> None:
        """Save returns 0 when the session row does not exist."""
        result = MagicMock()
        result.rowcount = 0
        repo, _ = self._repo(result)
        assert await repo.save_pending_confirmation(uuid4(), {"k": "v"}) == 0

    async def test_load_decodes_dict_row(self) -> None:
        """Load returns the payload when the driver hands back a dict."""
        result = MagicMock()
        result.first.return_value = ({"cloud_vision_model_key": "claude_sonnet"},)
        repo, _ = self._repo(result)
        loaded = await repo.load_pending_confirmation(uuid4())
        assert loaded == {"cloud_vision_model_key": "claude_sonnet"}

    async def test_load_decodes_json_string_row(self) -> None:
        """Load json-decodes the payload when the driver hands back a string."""
        # Some drivers hand back the JSONB `->` result as a JSON string.
        result = MagicMock()
        result.first.return_value = ('{"cloud_vision_model_key": "claude_sonnet"}',)
        repo, _ = self._repo(result)
        loaded = await repo.load_pending_confirmation(uuid4())
        assert loaded == {"cloud_vision_model_key": "claude_sonnet"}

    async def test_load_returns_none_when_absent(self) -> None:
        """Load returns None for a missing row or a null key."""
        for row in (None, (None,)):
            result = MagicMock()
            result.first.return_value = row
            repo, _ = self._repo(result)
            assert await repo.load_pending_confirmation(uuid4()) is None

    async def test_clear_deletes_only_the_pending_key(self) -> None:
        """Clear removes only the pending_cloud_confirmation key."""
        repo, db = self._repo(MagicMock())
        await repo.clear_pending_confirmation(uuid4())
        sql = str(db.execute.await_args.args[0])
        assert "metadata - 'pending_cloud_confirmation'" in sql
        db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Topology-accurate re-injection (fake durable store, hermetic)
# ---------------------------------------------------------------------------


@pytest.fixture
def durable_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """Replace the executor's durable helpers with a shared in-memory store.

    Simulates ``sessions.metadata`` surviving across requests WITHOUT a shared
    SessionManager — the exact property the in-memory fix lacked. The real DB SQL
    is covered separately by the mock-DB and integration tests.
    """
    store: dict[str, dict[str, Any]] = {}

    async def fake_save(session_id: str, pending: dict[str, Any], *, trace_id: str) -> None:
        store[session_id] = pending

    async def fake_load(session_id: str, *, trace_id: str) -> dict[str, Any] | None:
        pending = store.get(session_id)
        if pending is None:
            return None
        if executor_mod._pending_is_expired(pending, time.time()):
            store.pop(session_id, None)
            return None
        return pending

    async def fake_clear(session_id: str, *, trace_id: str) -> None:
        store.pop(session_id, None)

    monkeypatch.setattr(executor_mod, "_save_pending_cloud_confirmation", fake_save)
    monkeypatch.setattr(executor_mod, "_load_pending_cloud_confirmation", fake_load)
    monkeypatch.setattr(executor_mod, "_clear_pending_cloud_confirmation", fake_clear)
    return store


def _turn2_ctx(session_id: str, message: str) -> ExecutionContext:
    """A fresh turn-2 context — no shared SessionManager, only the session id in common."""
    return ExecutionContext(
        session_id=session_id,
        trace_id="trace-2",
        user_message=message,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        attachments=(),
        attachment_cost_confirmed=False,
    )


@pytest.mark.asyncio
class TestReinjectionTopology:
    """Save in one context, re-inject in a *separate* context sharing only the session id."""

    async def test_affirmative_reply_reinjects_across_contexts(
        self, durable_store: dict[str, dict[str, Any]]
    ) -> None:
        """Pending saved in one context is re-injected in a separate turn-2 context."""
        session_id = "sess-A"
        # Turn 1: the gate persists pending on pause.
        await executor_mod._save_pending_cloud_confirmation(
            session_id, _pending(), trace_id="trace-1"
        )

        # Turn 2: brand-new context, affirmative reply.
        ctx2 = _turn2_ctx(session_id, "Proceed")
        await executor_mod._maybe_reinject_pending_cloud_attachment(ctx2)

        assert len(ctx2.attachments) == 1
        assert ctx2.attachments[0].artifact_id == "art-1"
        # The re-injected turn must be marked confirmed so the gate does NOT re-pause.
        assert ctx2.attachment_cost_confirmed is True
        # Pending is consumed.
        assert session_id not in durable_store

    async def test_non_affirmative_reply_drops_pending(
        self, durable_store: dict[str, dict[str, Any]]
    ) -> None:
        """A non-affirmative reply drops the pending state without re-injecting (AC-2)."""
        session_id = "sess-B"
        await executor_mod._save_pending_cloud_confirmation(
            session_id, _pending(), trace_id="trace-1"
        )

        ctx2 = _turn2_ctx(session_id, "Actually, keep it local")
        await executor_mod._maybe_reinject_pending_cloud_attachment(ctx2)

        assert len(ctx2.attachments) == 0
        assert ctx2.attachment_cost_confirmed is False
        assert session_id not in durable_store  # AC-2: dropped

    async def test_expired_pending_is_not_reinjected(
        self, durable_store: dict[str, dict[str, Any]]
    ) -> None:
        """An expired pending record is cleared and never re-injected."""
        session_id = "sess-C"
        await executor_mod._save_pending_cloud_confirmation(
            session_id, _pending(created_at=time.time() - 1000, ttl_seconds=60), trace_id="t1"
        )

        ctx2 = _turn2_ctx(session_id, "Proceed")
        await executor_mod._maybe_reinject_pending_cloud_attachment(ctx2)

        assert len(ctx2.attachments) == 0
        assert ctx2.attachment_cost_confirmed is False
        assert session_id not in durable_store  # expired record cleared on load

    async def test_no_pending_leaves_context_unchanged(
        self, durable_store: dict[str, dict[str, Any]]
    ) -> None:
        """With no pending state the context is left untouched."""
        ctx2 = _turn2_ctx("sess-D", "Proceed")
        await executor_mod._maybe_reinject_pending_cloud_attachment(ctx2)
        assert len(ctx2.attachments) == 0
        assert ctx2.attachment_cost_confirmed is False


# ---------------------------------------------------------------------------
# True cross-connection durability (real Postgres — integration)
# ---------------------------------------------------------------------------


def _postgres_available() -> bool:
    """True when the isolated test Postgres substrate (:5433) is reachable."""
    try:
        with socket.create_connection(("localhost", 5433), timeout=2):
            return True
    except OSError:
        return False


@pytest.mark.asyncio
@pytest.mark.integration
class TestDurablePersistenceIntegration:
    """The seam that fails on the in-memory fix and passes on the durable one.

    Save via one ``AsyncSessionLocal`` and reload via a *separate* one — two
    connections standing in for the two per-request Orchestrators of turns 1 & 2.
    Requires ``make test-infra-up`` (Postgres on :5433).
    """

    async def _seed_session(self, db: Any, user_id: Any, session_id: Any) -> None:
        from sqlalchemy import text

        await db.execute(
            text("INSERT INTO users (user_id, email) VALUES (:uid, :email) ON CONFLICT DO NOTHING"),
            {"uid": user_id, "email": f"fre749-{user_id}@test.invalid"},
        )
        await db.execute(
            text(
                "INSERT INTO sessions (session_id, user_id, mode, channel, execution_profile)"
                " VALUES (:sid, :uid, 'NORMAL', 'CHAT', 'local')"
            ),
            {"sid": session_id, "uid": user_id},
        )
        await db.commit()

    async def test_pending_survives_across_separate_connections(self) -> None:
        """Pending saved on one connection is reloaded and re-injected on a separate one."""
        if not _postgres_available():
            pytest.skip("Test Postgres (:5433) not reachable — run make test-infra-up")

        from sqlalchemy import text

        from personal_agent.service.database import AsyncSessionLocal

        user_id = uuid4()
        session_id = uuid4()
        async with AsyncSessionLocal() as db:
            await self._seed_session(db, user_id, session_id)

        try:
            # Turn 1: save via the real durable helper (its own connection).
            await executor_mod._save_pending_cloud_confirmation(
                str(session_id), _pending(), trace_id="trace-1"
            )

            # Turn 2: a fresh context re-injects, loading via a SEPARATE connection.
            ctx2 = _turn2_ctx(str(session_id), "Proceed")
            await executor_mod._maybe_reinject_pending_cloud_attachment(ctx2)

            assert len(ctx2.attachments) == 1
            assert ctx2.attachments[0].artifact_id == "art-1"
            assert ctx2.attachment_cost_confirmed is True

            # The key is cleared in Postgres after a successful re-inject.
            async with AsyncSessionLocal() as db:
                row = (
                    await db.execute(
                        text(
                            "SELECT metadata -> 'pending_cloud_confirmation'"
                            " FROM sessions WHERE session_id = :sid"
                        ),
                        {"sid": session_id},
                    )
                ).first()
            assert row is not None and row[0] is None
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    text("DELETE FROM sessions WHERE session_id = :sid"), {"sid": session_id}
                )
                await db.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
                await db.commit()

    async def test_non_affirmative_clears_key_in_postgres(self) -> None:
        """A non-affirmative reply clears the pending key in Postgres."""
        if not _postgres_available():
            pytest.skip("Test Postgres (:5433) not reachable — run make test-infra-up")

        from sqlalchemy import text

        from personal_agent.service.database import AsyncSessionLocal

        user_id = uuid4()
        session_id = uuid4()
        async with AsyncSessionLocal() as db:
            await self._seed_session(db, user_id, session_id)

        try:
            await executor_mod._save_pending_cloud_confirmation(
                str(session_id), _pending(), trace_id="trace-1"
            )
            ctx2 = _turn2_ctx(str(session_id), "keep it local")
            await executor_mod._maybe_reinject_pending_cloud_attachment(ctx2)

            assert len(ctx2.attachments) == 0
            async with AsyncSessionLocal() as db:
                row = (
                    await db.execute(
                        text(
                            "SELECT metadata -> 'pending_cloud_confirmation'"
                            " FROM sessions WHERE session_id = :sid"
                        ),
                        {"sid": session_id},
                    )
                ).first()
            assert row is not None and row[0] is None
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    text("DELETE FROM sessions WHERE session_id = :sid"), {"sid": session_id}
                )
                await db.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
                await db.commit()
