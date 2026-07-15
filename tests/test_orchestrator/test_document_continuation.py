"""Durable PDF page-budget continuation persistence + re-injection (ADR-0102 §4 / FRE-685).

FRE-683 (T3) bounds Tier-2 page selection to the per-turn budget and discloses
which pages were dropped. FRE-685 (T7) makes the offered continuation actually
work: the dropped-page offer is persisted to the durable ``sessions.metadata``
JSONB column (mirroring FRE-749's cloud-confirmation pattern) and, on a later
turn — a *separate* request — a matching reply re-resolves the same
already-stored artifact for exactly the requested/dropped pages, no re-upload
needed. These tests prove:

* ``_parse_requested_page_range`` — the pure regex parser;
* the ``SessionRepository`` key-level JSONB SQL round-trips (mock-DB);
* the topology-accurate re-injection: an offer saved in one context is
  reloaded and re-injected in a *separate* context that shares only the
  session id — no shared ``SessionManager`` (fake durable store, hermetic);
* multi-document offers: a request matching one document's dropped pages
  leaves a second document's still-pending offer intact;
* AC-4 (the definition of done): end-to-end via ``resolve_documents`` twice,
  proving a follow-up turn's assembled attachment carries exactly the
  previously-omitted pages.
"""

from __future__ import annotations

import io
import time
from dataclasses import asdict
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pypdfium2 as pdfium
import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator import executor as executor_mod
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.document_resolution import resolve_documents
from personal_agent.orchestrator.types import (
    AttachmentRef,
    DocumentContinuationOffer,
    ExecutionContext,
    PendingDocumentContinuation,
)


def _make_pdf(page_count: int) -> bytes:
    doc = pdfium.PdfDocument.new()
    for _ in range(page_count):
        doc.new_page(200, 200)  # blank pages — no text layer, forces Tier 2
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _pending(
    offers: tuple[DocumentContinuationOffer, ...] | None = None,
    created_at: float | None = None,
    ttl_seconds: int = 600,
) -> dict[str, Any]:
    """Return a serialized pending-continuation payload for one document."""
    return asdict(
        PendingDocumentContinuation(
            offers=offers
            if offers is not None
            else (
                DocumentContinuationOffer(
                    artifact_id="doc-1",
                    content_type="application/pdf",
                    title="report.pdf",
                    r2_key="uploads/report.pdf",
                    processing_target=None,
                    dropped_pages=(
                        24,
                        25,
                        26,
                        27,
                        28,
                        29,
                        30,
                        31,
                        32,
                        33,
                        34,
                        35,
                        36,
                        37,
                        38,
                        39,
                        40,
                    ),
                ),
            ),
            created_at=created_at if created_at is not None else time.time(),
            ttl_seconds=ttl_seconds,
            original_trace_id="trace-1",
        )
    )


# ---------------------------------------------------------------------------
# Pure regex parser
# ---------------------------------------------------------------------------


class TestParseRequestedPageRange:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("pages 24-40", (24, 40)),
            ("page 24 to 40", (24, 40)),
            ("24-40", (24, 40)),
            ("pages 24 through 40", (24, 40)),
            ("Can I see pages 24-40 please?", (24, 40)),
            ("page 5", (5, 5)),
            ("40-24", (24, 40)),  # reversed order normalized ascending
        ],
    )
    def test_parses_range(self, message: str, expected: tuple[int, int]) -> None:
        assert executor_mod._parse_requested_page_range(message) == expected

    @pytest.mark.parametrize(
        "message",
        [
            "what does the document show?",
            "yes",
            "continue",
            "",
            "   ",
        ],
    )
    def test_no_match_returns_none(self, message: str) -> None:
        assert executor_mod._parse_requested_page_range(message) is None


# ---------------------------------------------------------------------------
# SessionRepository key-level JSONB SQL (mock-DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSessionRepositoryPendingDocumentContinuationSQL:
    def _repo(self, execute_result: Any = None):
        from personal_agent.service.repositories.session_repository import SessionRepository

        db = MagicMock()
        db.execute = AsyncMock(return_value=execute_result)
        db.commit = AsyncMock()
        return SessionRepository(db), db

    async def test_save_uses_jsonb_set_under_its_own_key(self) -> None:
        result = MagicMock()
        result.rowcount = 1
        repo, db = self._repo(result)
        sid = uuid4()

        rows = await repo.save_pending_document_continuation(sid, {"offers": []})

        assert rows == 1
        sql = str(db.execute.await_args.args[0])
        assert "jsonb_set" in sql
        assert "pending_document_continuation" in sql
        params = db.execute.await_args.args[1]
        assert params["sid"] == str(sid)
        db.commit.assert_awaited_once()

    async def test_load_decodes_dict_row(self) -> None:
        result = MagicMock()
        result.first.return_value = ({"offers": [{"artifact_id": "doc-1"}]},)
        repo, _ = self._repo(result)
        loaded = await repo.load_pending_document_continuation(uuid4())
        assert loaded == {"offers": [{"artifact_id": "doc-1"}]}

    async def test_load_returns_none_when_absent(self) -> None:
        for row in (None, (None,)):
            result = MagicMock()
            result.first.return_value = row
            repo, _ = self._repo(result)
            assert await repo.load_pending_document_continuation(uuid4()) is None

    async def test_clear_deletes_only_its_own_key(self) -> None:
        repo, db = self._repo(MagicMock())
        await repo.clear_pending_document_continuation(uuid4())
        sql = str(db.execute.await_args.args[0])
        assert "metadata - 'pending_document_continuation'" in sql
        db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Topology-accurate re-injection (fake durable store, hermetic)
# ---------------------------------------------------------------------------


@pytest.fixture
def durable_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """Simulates ``sessions.metadata`` surviving across requests, no shared SessionManager."""
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

    monkeypatch.setattr(executor_mod, "_save_pending_document_continuation", fake_save)
    monkeypatch.setattr(executor_mod, "_load_pending_document_continuation", fake_load)
    monkeypatch.setattr(executor_mod, "_clear_pending_document_continuation", fake_clear)
    return store


def _turn2_ctx(session_id: str, message: str) -> ExecutionContext:
    return ExecutionContext(
        session_id=session_id,
        trace_id="trace-2",
        user_message=message,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        attachments=(),
    )


@pytest.mark.asyncio
class TestReinjectionTopology:
    async def test_range_reply_reinjects_exact_requested_pages(
        self, durable_store: dict[str, dict[str, Any]]
    ) -> None:
        session_id = "sess-A"
        await executor_mod._save_pending_document_continuation(
            session_id, _pending(), trace_id="trace-1"
        )

        ctx2 = _turn2_ctx(session_id, "pages 24-40")
        await executor_mod._maybe_reinject_pending_document_continuation(ctx2)

        assert len(ctx2.attachments) == 1
        assert ctx2.attachments[0].artifact_id == "doc-1"
        assert ctx2.attachments[0].requested_pages == (
            24,
            25,
            26,
            27,
            28,
            29,
            30,
            31,
            32,
            33,
            34,
            35,
            36,
            37,
            38,
            39,
            40,
        )
        # Fully satisfied — pending is consumed.
        assert session_id not in durable_store

    async def test_partial_range_reinjects_only_overlap(
        self, durable_store: dict[str, dict[str, Any]]
    ) -> None:
        session_id = "sess-B"
        await executor_mod._save_pending_document_continuation(
            session_id, _pending(), trace_id="trace-1"
        )

        ctx2 = _turn2_ctx(session_id, "pages 24-26")
        await executor_mod._maybe_reinject_pending_document_continuation(ctx2)

        assert ctx2.attachments[0].requested_pages == (24, 25, 26)

    async def test_broad_affirmative_reinjects_all_dropped_pages(
        self, durable_store: dict[str, dict[str, Any]]
    ) -> None:
        session_id = "sess-C"
        await executor_mod._save_pending_document_continuation(
            session_id, _pending(), trace_id="trace-1"
        )

        ctx2 = _turn2_ctx(session_id, "yes")
        await executor_mod._maybe_reinject_pending_document_continuation(ctx2)

        assert len(ctx2.attachments[0].requested_pages) == 17

    async def test_unrelated_message_leaves_pending_in_place(
        self, durable_store: dict[str, dict[str, Any]]
    ) -> None:
        """An interim unrelated turn must not destroy a legitimate offer."""
        session_id = "sess-D"
        await executor_mod._save_pending_document_continuation(
            session_id, _pending(), trace_id="trace-1"
        )

        ctx2 = _turn2_ctx(session_id, "what's the weather like today?")
        await executor_mod._maybe_reinject_pending_document_continuation(ctx2)

        assert len(ctx2.attachments) == 0
        assert session_id in durable_store  # NOT cleared

    async def test_non_overlapping_range_leaves_pending_in_place(
        self, durable_store: dict[str, dict[str, Any]]
    ) -> None:
        session_id = "sess-E"
        await executor_mod._save_pending_document_continuation(
            session_id, _pending(), trace_id="trace-1"
        )

        ctx2 = _turn2_ctx(session_id, "pages 1-5")  # not in the dropped set (24-40)
        await executor_mod._maybe_reinject_pending_document_continuation(ctx2)

        assert len(ctx2.attachments) == 0
        assert session_id in durable_store

    async def test_expired_pending_is_not_reinjected(
        self, durable_store: dict[str, dict[str, Any]]
    ) -> None:
        session_id = "sess-F"
        await executor_mod._save_pending_document_continuation(
            session_id, _pending(created_at=time.time() - 1000, ttl_seconds=60), trace_id="t1"
        )

        ctx2 = _turn2_ctx(session_id, "pages 24-40")
        await executor_mod._maybe_reinject_pending_document_continuation(ctx2)

        assert len(ctx2.attachments) == 0
        assert session_id not in durable_store  # expired record cleared on load

    async def test_no_pending_leaves_context_unchanged(
        self, durable_store: dict[str, dict[str, Any]]
    ) -> None:
        ctx2 = _turn2_ctx("sess-G", "pages 24-40")
        await executor_mod._maybe_reinject_pending_document_continuation(ctx2)
        assert len(ctx2.attachments) == 0

    async def test_second_document_offer_survives_first_documents_match(
        self, durable_store: dict[str, dict[str, Any]]
    ) -> None:
        """Two over-budget documents in one turn: matching one's range must not

        drop the other's still-pending offer.
        """
        session_id = "sess-H"
        offers = (
            DocumentContinuationOffer(
                artifact_id="doc-a",
                content_type="application/pdf",
                title="a.pdf",
                r2_key="uploads/a.pdf",
                processing_target=None,
                dropped_pages=(24, 25, 26),
            ),
            DocumentContinuationOffer(
                artifact_id="doc-b",
                content_type="application/pdf",
                title="b.pdf",
                r2_key="uploads/b.pdf",
                processing_target=None,
                dropped_pages=(50, 51, 52),
            ),
        )
        await executor_mod._save_pending_document_continuation(
            session_id, _pending(offers=offers), trace_id="trace-1"
        )

        ctx2 = _turn2_ctx(session_id, "pages 24-26")
        await executor_mod._maybe_reinject_pending_document_continuation(ctx2)

        assert len(ctx2.attachments) == 1
        assert ctx2.attachments[0].artifact_id == "doc-a"
        # doc-b's offer must still be pending for a later follow-up.
        assert session_id in durable_store
        remaining_ids = {o["artifact_id"] for o in durable_store[session_id]["offers"]}
        assert remaining_ids == {"doc-b"}


# ---------------------------------------------------------------------------
# AC-4 (the definition of done): end-to-end via resolve_documents twice
# ---------------------------------------------------------------------------


_STORE_PATCH_TARGET = "personal_agent.orchestrator.document_resolution.get_artifact_store"


def _mock_store(bytes_by_key: dict[str, bytes]) -> AsyncMock:
    store = AsyncMock()

    async def _get(r2_key: str, **_kwargs: Any) -> bytes:
        return bytes_by_key[r2_key]

    store.get.side_effect = _get
    return store


@pytest.mark.asyncio
class TestAC4EndToEndContinuation:
    async def test_followup_turn_delivers_exactly_the_dropped_pages(self) -> None:
        """The whole seam: turn 1 over-budget + disclose, turn 2 continuation delivers

        exactly the previously-omitted pages — no re-upload, same artifact.
        """
        pdf_bytes = _make_pdf(10)
        attachment = AttachmentRef(
            artifact_id="doc-1",
            content_type="application/pdf",
            title="report.pdf",
            r2_key="uploads/report.pdf",
        )
        store = _mock_store({attachment.r2_key: pdf_bytes})

        # Turn 1: budget = 6 of 10 pages.
        with (
            patch(_STORE_PATCH_TARGET, return_value=store),
            patch(
                "personal_agent.orchestrator.document_resolution.settings.document_max_pages_per_turn",
                6,
            ),
        ):
            turn1 = await resolve_documents(
                [attachment], resolve_tier2_delivery=lambda: "native_pdf"
            )

        assert len(turn1.continuation_offers) == 1
        offer = turn1.continuation_offers[0]
        assert offer.dropped_pages == (7, 8, 9, 10)
        assert any("7-10" in d for d in turn1.disclosures)

        # Turn 2: a fresh continuation request for exactly the dropped pages,
        # no new attachment upload — same artifact_id/r2_key, requested_pages set.
        continuation_attachment = AttachmentRef(
            artifact_id=offer.artifact_id,
            content_type=offer.content_type,
            title=offer.title,
            r2_key=offer.r2_key,
            processing_target=offer.processing_target,
            requested_pages=offer.dropped_pages,
        )

        with patch(_STORE_PATCH_TARGET, return_value=store):
            turn2 = await resolve_documents(
                [continuation_attachment], resolve_tier2_delivery=lambda: "native_pdf"
            )

        assert turn2.native_pdf_page_count == 4  # exactly pages 7-10, nothing more
        assert turn2.continuation_offers == ()
        sub_doc = pdfium.PdfDocument(
            io.BytesIO(__import__("base64").b64decode(turn2.blocks[0]["source"]["data"]))
        )
        assert len(sub_doc) == 4
