"""Unit tests for the notes_write / notes_search tools (FRE-227).

These tests mock the three side-effect surfaces:

* ``get_artifact_store`` — replaced with a fake that records put/get calls
  in-memory.
* ``generate_embedding`` — replaced with a deterministic vector so
  similarity ordering is reproducible.
* ``AsyncSessionLocal`` — replaced with a stub that captures the SQL it is
  asked to execute and returns canned rows.

End-to-end DB-backed tests live alongside the resolve-endpoint suite where
the round-trip semantics are best validated.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from personal_agent.tools import notes_tools
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.notes_tools import _pgvector_literal


def test_pgvector_literal_basic_shape() -> None:
    """Bracketed, comma-separated, no whitespace — matches pgvector grammar."""
    out = _pgvector_literal([0.1, -0.2, 0.0])
    assert out.startswith("[") and out.endswith("]")
    assert out.count(",") == 2
    assert " " not in out
    # Round-trip lossless.
    assert [float(x) for x in out[1:-1].split(",")] == [0.1, -0.2, 0.0]


def test_pgvector_literal_empty_list() -> None:
    """Empty list renders as ``[]`` (pgvector parses but vector(N) will reject — that's correct)."""
    assert _pgvector_literal([]) == "[]"


def test_pgvector_literal_handles_full_1024_vector() -> None:
    """Hot path: realistic 1024-dim vector serializes without scientific-notation surprises."""
    vec = [float(i) / 1024.0 for i in range(1024)]
    out = _pgvector_literal(vec)
    assert out.count(",") == 1023
    parsed = [float(x) for x in out[1:-1].split(",")]
    assert parsed == vec


def _ctx(user_id: UUID | None = None, session_id: UUID | None = None) -> Any:
    return SimpleNamespace(
        user_id=user_id or uuid4(),
        session_id=session_id,
        trace_id="trace-test",
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self._payloads: dict[str, bytes] = {}

    def stash(self, key: str, payload: bytes) -> None:
        self._payloads[key] = payload

    async def put(
        self,
        *,
        r2_key: str,
        content: bytes,
        content_type: str,
        metadata: Any = None,
        trace_id: str | None = None,
    ) -> None:
        self.put_calls.append(
            {
                "r2_key": r2_key,
                "content": content,
                "content_type": content_type,
                "metadata": metadata,
                "trace_id": trace_id,
            }
        )
        self._payloads[r2_key] = content

    async def get(self, r2_key: str, *, trace_id: str | None = None) -> bytes:
        self.get_calls.append(r2_key)
        return self._payloads.get(r2_key, b"")


class _FakeSession:
    """Captures execute() calls + replays canned results, for both INSERT and SELECT."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        # next_result is consulted in FIFO order.
        self._queue: list[Any] = []

    def enqueue(self, result: Any) -> None:
        self._queue.append(result)

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = statement.text if hasattr(statement, "text") else str(statement)
        self.calls.append((sql, dict(params or {})))
        if self._queue:
            return self._queue.pop(0)
        # INSERT path — no result needed by the executor.
        return MagicMock()

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: _FakeStore,
    embedding: list[float] | None = None,
) -> _FakeSession:
    """Patch the three side-effect doors and return the fake session."""
    session = _FakeSession()

    monkeypatch.setattr(notes_tools, "get_artifact_store", lambda: store)
    monkeypatch.setattr(
        notes_tools,
        "generate_embedding",
        AsyncMock(return_value=embedding if embedding is not None else [0.0] * 1024),
    )
    monkeypatch.setattr(notes_tools, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        notes_tools.settings, "artifacts_public_base_url", "https://artifacts.test", raising=False
    )
    return session


# ---------------------------------------------------------------------------
# notes_write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notes_write_requires_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ctx without user_id is a programming bug — refuse cleanly."""
    _install_fakes(monkeypatch, store=_FakeStore())

    with pytest.raises(ToolExecutionError, match="user_id"):
        await notes_tools.notes_write_executor(
            slug="a", content="b", ctx=SimpleNamespace(trace_id="t")
        )


@pytest.mark.asyncio
async def test_notes_write_rejects_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, store=_FakeStore())

    with pytest.raises(ToolExecutionError, match="mode"):
        await notes_tools.notes_write_executor(slug="a", content="b", mode="replace", ctx=_ctx())


@pytest.mark.asyncio
async def test_notes_write_invalid_slug_rejected_pre_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """``build_r2_key`` rejects the slug before R2 / DB are touched."""
    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)

    with pytest.raises(ToolExecutionError):
        await notes_tools.notes_write_executor(slug="../etc/passwd", content="x", ctx=_ctx())

    assert store.put_calls == []
    assert session.calls == []  # SELECT for prior revision happens only after slug passes


@pytest.mark.asyncio
async def test_notes_write_substrate_not_configured_is_toolexec_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notes_tools, "get_artifact_store", lambda: None)

    with pytest.raises(ToolExecutionError, match="substrate"):
        await notes_tools.notes_write_executor(slug="a", content="b", ctx=_ctx())


@pytest.mark.asyncio
async def test_notes_write_overwrite_skips_prior_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """``overwrite`` never reads R2 — only PUT + INSERT."""
    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)

    user_id = uuid4()
    out = await notes_tools.notes_write_executor(
        slug="my-note",
        content="fresh body",
        mode="overwrite",
        ctx=_ctx(user_id=user_id),
    )

    assert store.get_calls == []  # no prior fetch
    assert len(store.put_calls) == 1
    body = store.put_calls[0]["content"]
    assert body == b"fresh body"
    assert out["mode_applied"] == "overwrite"
    assert out["revision_of"] is None
    assert out["public_url"].startswith("https://artifacts.test/")


@pytest.mark.asyncio
async def test_notes_write_append_concatenates_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``append`` reads the prior revision's R2 body and concatenates."""
    store = _FakeStore()
    prior_artifact_id = uuid4()
    prior_key = "note/aaa/GLOBAL/old.md"
    store.stash(prior_key, b"earlier text")

    session = _install_fakes(monkeypatch, store=store)
    session.enqueue(
        SimpleNamespace(first=lambda: SimpleNamespace(id=prior_artifact_id, r2_key=prior_key))
    )

    out = await notes_tools.notes_write_executor(
        slug="continuing",
        content="new chunk",
        mode="append",
        ctx=_ctx(),
    )

    assert store.get_calls == [prior_key]
    written = store.put_calls[0]["content"]
    assert written == b"earlier text\n\nnew chunk"
    assert out["mode_applied"] == "append"
    assert out["revision_of"] == str(prior_artifact_id)


@pytest.mark.asyncio
async def test_notes_write_append_with_no_prior_is_fresh_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)
    session.enqueue(SimpleNamespace(first=lambda: None))

    out = await notes_tools.notes_write_executor(
        slug="firsttime", content="hello", mode="append", ctx=_ctx()
    )

    assert store.get_calls == []
    assert store.put_calls[0]["content"] == b"hello"
    assert out["revision_of"] is None


@pytest.mark.asyncio
async def test_notes_write_embedding_passed_with_correct_dim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedding emitted by the helper is what the INSERT carries."""
    store = _FakeStore()
    vec = [0.1] * 1024
    session = _install_fakes(monkeypatch, store=store, embedding=vec)

    await notes_tools.notes_write_executor(
        slug="dim-test", content="body", mode="overwrite", ctx=_ctx()
    )

    # The INSERT is the only call with an embedding param. asyncpg cannot
    # serialise a list to pgvector — the executor must bind the literal
    # bracketed text form so `CAST($n AS vector)` succeeds.
    insert_call = next(c for c in session.calls if "INSERT INTO artifacts" in c[0])
    bound = insert_call[1]["embedding"]
    assert isinstance(bound, str)
    assert bound.startswith("[") and bound.endswith("]")
    assert bound.count(",") == len(vec) - 1
    # Round-trip the literal back to floats and assert equality.
    parsed = [float(x) for x in bound[1:-1].split(",")]
    assert parsed == vec


@pytest.mark.asyncio
async def test_notes_write_rejects_oversized_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)

    too_big = "x" * (256 * 1024 + 1)
    with pytest.raises(ToolExecutionError, match="exceeds"):
        await notes_tools.notes_write_executor(
            slug="big", content=too_big, mode="overwrite", ctx=_ctx()
        )
    assert store.put_calls == []


# ---------------------------------------------------------------------------
# notes_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notes_search_requires_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, store=_FakeStore())

    with pytest.raises(ToolExecutionError, match="user_id"):
        await notes_tools.notes_search_executor(query="hello", ctx=SimpleNamespace(trace_id="t"))


@pytest.mark.asyncio
async def test_notes_search_rejects_empty_query(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, store=_FakeStore())

    with pytest.raises(ToolExecutionError, match="query"):
        await notes_tools.notes_search_executor(query="   ", ctx=_ctx())


@pytest.mark.asyncio
async def test_notes_search_returns_metadata_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Result shape matches the documented response."""
    from datetime import datetime, timezone

    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)
    rid = uuid4()
    canned = [
        SimpleNamespace(
            id=rid,
            slug="my-note",
            title="My Note",
            summary=None,
            tags=["x"],
            created_at=datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
            similarity=0.92,
        )
    ]
    session.enqueue(SimpleNamespace(all=lambda: canned))

    out = await notes_tools.notes_search_executor(query="anything", ctx=_ctx())

    assert out["result_count"] == 1
    row = out["results"][0]
    assert row["artifact_id"] == str(rid)
    assert row["slug"] == "my-note"
    assert row["tags"] == ["x"]
    assert row["similarity"] == pytest.approx(0.92)
    assert row["public_url"].endswith(str(rid))


@pytest.mark.asyncio
async def test_notes_search_k_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """K below 1 clamps to 1; k above 25 clamps to 25."""
    session = _install_fakes(monkeypatch, store=_FakeStore())
    session.enqueue(SimpleNamespace(all=lambda: []))
    session.enqueue(SimpleNamespace(all=lambda: []))

    await notes_tools.notes_search_executor(query="q", k=0, ctx=_ctx())
    await notes_tools.notes_search_executor(query="q", k=999, ctx=_ctx())

    sql_calls = [c for c in session.calls if "FROM artifacts" in c[0]]
    assert sql_calls[0][1]["k"] == 1
    assert sql_calls[1][1]["k"] == 25


@pytest.mark.asyncio
async def test_notes_search_passes_tag_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _install_fakes(monkeypatch, store=_FakeStore())
    session.enqueue(SimpleNamespace(all=lambda: []))

    await notes_tools.notes_search_executor(query="anything", tags=["proj-x"], ctx=_ctx())

    sql_call = next(c for c in session.calls if "FROM artifacts" in c[0])
    assert sql_call[1]["tag_filter"] == ["proj-x"]


@pytest.mark.asyncio
async def test_notes_search_no_tags_means_null_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _install_fakes(monkeypatch, store=_FakeStore())
    session.enqueue(SimpleNamespace(all=lambda: []))

    await notes_tools.notes_search_executor(query="anything", ctx=_ctx())

    sql_call = next(c for c in session.calls if "FROM artifacts" in c[0])
    assert sql_call[1]["tag_filter"] is None
