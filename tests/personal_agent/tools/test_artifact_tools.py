"""Unit tests for artifact_write / artifact_list / artifact_read / artifact_draft tools.

These tests mock the three side-effect surfaces (identical pattern to
test_notes_tools.py):

* ``get_artifact_store`` — replaced with a fake that records put/get calls
  in-memory.
* ``generate_embedding`` — replaced with a deterministic vector so the
  INSERT SQL receives a predictable embedding literal.
* ``AsyncSessionLocal`` — replaced with a stub that captures SQL calls and
  returns canned rows.

artifact_draft tests additionally mock ``get_llm_client`` (ADR-0077).

End-to-end DB-backed round-trip lives in the integration suite.
"""

from __future__ import annotations

import asyncio  # noqa: F401 — used by _HangingClient in draft timeout test
import base64
from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from personal_agent.config import settings
from personal_agent.observability.artifact_envelope.spec import load_lib_manifest
from personal_agent.orchestrator.constraint_options import (
    ConstraintDecision,
    reset_artifact_builder_resolution,
    set_artifact_builder_resolution,
)
from personal_agent.tools import artifact_tools
from personal_agent.tools.executor import ToolExecutionError


def _ctx(user_id: UUID | None = None, session_id: UUID | None = None) -> Any:
    return SimpleNamespace(
        user_id=user_id or uuid4(),
        session_id=session_id,
        trace_id="trace-test",
    )


@pytest.fixture(autouse=True)
def _reset_builder_carrier() -> Iterator[None]:
    """Token-reset the ADR-0122 T5 turn-state ContextVar around every test (isolation).

    Uses a token (not a bare ``set(None)``) so a value set by one test can never leak
    into the next; the default (``None``) is the honest state for a direct
    ``artifact_draft`` unit call — no ``step_init`` ran — which the build boundary treats
    as a missed prediction (AC-11).
    """
    token = set_artifact_builder_resolution(None)
    try:
        yield
    finally:
        reset_artifact_builder_resolution(token)


# ---------------------------------------------------------------------------
# Fakes (mirrors _FakeStore / _FakeSession in test_notes_tools.py)
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
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self._queue: list[Any] = []

    def enqueue(self, result: Any) -> None:
        self._queue.append(result)

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = statement.text if hasattr(statement, "text") else str(statement)
        self.calls.append((sql, dict(params or {})))
        if self._queue:
            return self._queue.pop(0)
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
    monkeypatch.setattr(artifact_tools, "get_artifact_store", lambda: store)
    monkeypatch.setattr(
        artifact_tools,
        "generate_embedding",
        AsyncMock(return_value=embedding if embedding is not None else [0.1] * 1024),
    )
    monkeypatch.setattr(artifact_tools, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        artifact_tools.settings,
        "artifacts_public_base_url",
        "https://artifacts.test",
        raising=False,
    )
    # FRE-512: stub the served-envelope probe so unit tests never issue HTTP.
    monkeypatch.setattr(artifact_tools, "probe_served_envelope", AsyncMock(), raising=False)
    return session


# ---------------------------------------------------------------------------
# artifact_write — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_write_html_returns_expected_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: text/html artifact returns all expected output keys."""
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)

    out = await artifact_tools.artifact_write_executor(
        slug="q3-report",
        content_type="text/html; charset=utf-8",
        content="<h1>Hello</h1>",
        title="Q3 Report",
        summary="A summary",
        ctx=_ctx(),
    )

    assert "artifact_id" in out
    assert out["public_url"].startswith("https://artifacts.test/")
    assert out["slug"] == "q3-report"
    assert out["content_type"] == "text/html; charset=utf-8"
    assert out["size_bytes"] > 0
    assert out["title"] == "Q3 Report"
    assert out["summary"] == "A summary"


@pytest.mark.asyncio
async def test_artifact_write_html_r2_key_ends_in_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The R2 key extension is derived from the content_type."""
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)

    await artifact_tools.artifact_write_executor(
        slug="chart",
        content_type="text/html; charset=utf-8",
        content="<p>chart</p>",
        ctx=_ctx(),
    )

    assert len(store.put_calls) == 1
    r2_key: str = store.put_calls[0]["r2_key"]
    assert r2_key.endswith(".html")
    assert r2_key.startswith("artifact/")


@pytest.mark.asyncio
async def test_artifact_write_image_png_is_base64_decoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """image/png content arrives as base64; executor decodes to raw bytes."""
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)

    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    b64 = base64.b64encode(raw).decode("ascii")

    await artifact_tools.artifact_write_executor(
        slug="graph",
        content_type="image/png",
        content=b64,
        ctx=_ctx(),
    )

    assert len(store.put_calls) == 1
    assert store.put_calls[0]["content"] == raw
    assert store.put_calls[0]["r2_key"].endswith(".png")


@pytest.mark.asyncio
async def test_artifact_write_postgres_insert_carries_artifact_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INSERT SQL must write type='artifact' and created_by='agent'."""
    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)

    await artifact_tools.artifact_write_executor(
        slug="tbl",
        content_type="text/html; charset=utf-8",
        content="<table></table>",
        ctx=_ctx(),
    )

    insert_sql, insert_params = next(c for c in session.calls if "INSERT INTO artifacts" in c[0])
    assert (
        "'artifact'" in insert_sql
        or insert_params.get("type_") == "artifact"
        or "artifact" in str(insert_params)
    )
    assert "agent" in insert_sql or "agent" in str(insert_params)


@pytest.mark.asyncio
async def test_artifact_write_embedding_is_pgvector_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedding in the INSERT params must be the bracketed text literal."""
    store = _FakeStore()
    vec = [0.25] * 1024
    session = _install_fakes(monkeypatch, store=store, embedding=vec)

    await artifact_tools.artifact_write_executor(
        slug="embed-test",
        content_type="application/json",
        content='{"x":1}',
        title="T",
        summary="S",
        ctx=_ctx(),
    )

    insert_sql, insert_params = next(c for c in session.calls if "INSERT INTO artifacts" in c[0])
    emb_value = insert_params.get("embedding")
    assert isinstance(emb_value, str)
    assert emb_value.startswith("[") and emb_value.endswith("]")
    parsed = [float(v) for v in emb_value[1:-1].split(",")]
    assert parsed == vec


@pytest.mark.asyncio
async def test_artifact_write_no_title_summary_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """title/summary/tags are all optional — write must succeed without them."""
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)

    out = await artifact_tools.artifact_write_executor(
        slug="minimal",
        content_type="text/markdown; charset=utf-8",
        content="# Heading",
        ctx=_ctx(),
    )

    assert out["title"] is None
    assert out["summary"] is None
    assert len(store.put_calls) == 1


# ---------------------------------------------------------------------------
# artifact_write — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_write_requires_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ctx.user_id is a programming bug — refuse with ToolExecutionError."""
    _install_fakes(monkeypatch, store=_FakeStore())

    with pytest.raises(ToolExecutionError, match="user_id"):
        await artifact_tools.artifact_write_executor(
            slug="x",
            content_type="text/html; charset=utf-8",
            content="<p>x</p>",
            ctx=SimpleNamespace(trace_id="t"),
        )


@pytest.mark.asyncio
async def test_artifact_write_substrate_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_tools, "get_artifact_store", lambda: None)

    with pytest.raises(ToolExecutionError, match="substrate"):
        await artifact_tools.artifact_write_executor(
            slug="x",
            content_type="text/html; charset=utf-8",
            content="<p>y</p>",
            ctx=_ctx(),
        )


@pytest.mark.asyncio
async def test_artifact_write_rejects_unknown_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)

    with pytest.raises(ToolExecutionError, match="content_type"):
        await artifact_tools.artifact_write_executor(
            slug="bad",
            content_type="application/pdf",  # not in allowlist
            content="bytes",
            ctx=_ctx(),
        )

    assert store.put_calls == []


@pytest.mark.asyncio
async def test_artifact_write_rejects_empty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)

    with pytest.raises(ToolExecutionError, match="content"):
        await artifact_tools.artifact_write_executor(
            slug="empty",
            content_type="text/html; charset=utf-8",
            content="",
            ctx=_ctx(),
        )

    assert store.put_calls == []


@pytest.mark.asyncio
async def test_artifact_write_rejects_oversized_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content exceeding 5 MB (5 * 1024 * 1024 bytes) is refused."""
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)

    too_big = "x" * (5 * 1024 * 1024 + 1)
    with pytest.raises(ToolExecutionError, match="5 MB"):
        await artifact_tools.artifact_write_executor(
            slug="big",
            content_type="text/html; charset=utf-8",
            content=too_big,
            ctx=_ctx(),
        )

    assert store.put_calls == []


@pytest.mark.asyncio
async def test_artifact_write_invalid_slug_rejected_before_r2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path-traversal slug is caught by build_r2_key before any R2/DB call."""
    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)

    with pytest.raises(ToolExecutionError):
        await artifact_tools.artifact_write_executor(
            slug="../etc/passwd",
            content_type="text/html; charset=utf-8",
            content="<p>x</p>",
            ctx=_ctx(),
        )

    assert store.put_calls == []
    assert session.calls == []


@pytest.mark.asyncio
async def test_artifact_write_rejects_slug_with_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)

    with pytest.raises(ToolExecutionError):
        await artifact_tools.artifact_write_executor(
            slug="path/traversal",
            content_type="text/html; charset=utf-8",
            content="<p>x</p>",
            ctx=_ctx(),
        )

    assert store.put_calls == []
    assert session.calls == []


@pytest.mark.asyncio
async def test_artifact_write_rejects_invalid_base64_for_png(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-base64 content for image/png is rejected before R2."""
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)

    with pytest.raises(ToolExecutionError, match="base64"):
        await artifact_tools.artifact_write_executor(
            slug="bad-png",
            content_type="image/png",
            content="not-valid-base64!!!",
            ctx=_ctx(),
        )

    assert store.put_calls == []


# ---------------------------------------------------------------------------
# artifact_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_list_returns_ordered_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)

    rid1, rid2 = uuid4(), uuid4()
    canned = [
        SimpleNamespace(
            id=rid1,
            slug="b",
            title="B",
            summary="sum-b",
            content_type="text/html; charset=utf-8",
            tags=["x"],
            created_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            id=rid2,
            slug="a",
            title="A",
            summary=None,
            content_type="application/json",
            tags=[],
            created_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        ),
    ]
    session.enqueue(SimpleNamespace(all=lambda: canned))

    out = await artifact_tools.artifact_list_executor(ctx=_ctx())

    assert out["result_count"] == 2
    rows = out["results"]
    assert rows[0]["artifact_id"] == str(rid1)
    assert rows[0]["slug"] == "b"
    assert rows[0]["public_url"].endswith(str(rid1))
    assert rows[1]["artifact_id"] == str(rid2)


@pytest.mark.asyncio
async def test_artifact_list_requires_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fakes(monkeypatch, store=_FakeStore())

    with pytest.raises(ToolExecutionError, match="user_id"):
        await artifact_tools.artifact_list_executor(ctx=SimpleNamespace(trace_id="t"))


@pytest.mark.asyncio
async def test_artifact_list_passes_prefix_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_fakes(monkeypatch, store=_FakeStore())
    session.enqueue(SimpleNamespace(all=lambda: []))

    await artifact_tools.artifact_list_executor(prefix="q3", ctx=_ctx())

    sql_call = next(c for c in session.calls if "artifacts" in c[0].lower())
    assert sql_call[1].get("prefix") == "q3"


@pytest.mark.asyncio
async def test_artifact_list_passes_k_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_fakes(monkeypatch, store=_FakeStore())
    session.enqueue(SimpleNamespace(all=lambda: []))

    await artifact_tools.artifact_list_executor(k=7, ctx=_ctx())

    sql_call = next(c for c in session.calls if "artifacts" in c[0].lower())
    assert sql_call[1].get("k") == 7


@pytest.mark.asyncio
async def test_artifact_list_passes_since_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_fakes(monkeypatch, store=_FakeStore())
    session.enqueue(SimpleNamespace(all=lambda: []))

    ts = "2026-05-01T00:00:00Z"
    await artifact_tools.artifact_list_executor(since=ts, ctx=_ctx())

    sql_call = next(c for c in session.calls if "artifacts" in c[0].lower())
    assert sql_call[1].get("since") == ts


@pytest.mark.asyncio
async def test_artifact_list_always_filters_artifact_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQL must always carry type='artifact' to exclude notes/uploads/captures."""
    session = _install_fakes(monkeypatch, store=_FakeStore())
    session.enqueue(SimpleNamespace(all=lambda: []))

    await artifact_tools.artifact_list_executor(ctx=_ctx())

    sql_call = next(c for c in session.calls if "artifacts" in c[0].lower())
    sql_text = sql_call[0]
    # The WHERE must constrain type to 'artifact'
    assert "artifact" in sql_text


@pytest.mark.asyncio
async def test_artifact_list_empty_returns_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_fakes(monkeypatch, store=_FakeStore())
    session.enqueue(SimpleNamespace(all=lambda: []))

    out = await artifact_tools.artifact_list_executor(ctx=_ctx())

    assert out["results"] == []
    assert out["result_count"] == 0


# ---------------------------------------------------------------------------
# artifact_read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_read_small_html_returns_content_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Small textual artifacts (<= 256 KB) are returned inline as 'content'."""
    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)

    art_id = uuid4()
    r2_key = f"artifact/uid/GLOBAL/{art_id}.html"
    store.stash(r2_key, b"<h1>Hello</h1>")

    session.enqueue(
        SimpleNamespace(
            first=lambda: SimpleNamespace(
                id=art_id,
                user_id=uuid4(),
                slug="report",
                title="Report",
                summary="s",
                content_type="text/html; charset=utf-8",
                size_bytes=14,
                r2_key=r2_key,
                tags=["x"],
                created_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
            )
        )
    )

    out = await artifact_tools.artifact_read_executor(artifact_id=str(art_id), ctx=_ctx())

    assert out["content"] == "<h1>Hello</h1>"
    assert store.get_calls == [r2_key]
    assert out["public_url"].endswith(str(art_id))


@pytest.mark.asyncio
async def test_artifact_read_large_artifact_omits_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifacts exceeding 256 KB return URL only — no R2 get call."""
    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)

    art_id = uuid4()
    r2_key = f"artifact/uid/GLOBAL/{art_id}.html"

    session.enqueue(
        SimpleNamespace(
            first=lambda: SimpleNamespace(
                id=art_id,
                user_id=uuid4(),
                slug="huge",
                title="Huge",
                summary=None,
                content_type="text/html; charset=utf-8",
                size_bytes=256 * 1024 + 1,  # just over the threshold
                r2_key=r2_key,
                tags=[],
                created_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
            )
        )
    )

    out = await artifact_tools.artifact_read_executor(artifact_id=str(art_id), ctx=_ctx())

    assert "content" not in out or out.get("content") is None
    assert store.get_calls == []
    assert out["public_url"].endswith(str(art_id))


@pytest.mark.asyncio
async def test_artifact_read_binary_content_type_omits_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """image/png is binary — content is never returned inline regardless of size."""
    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)

    art_id = uuid4()
    r2_key = f"artifact/uid/GLOBAL/{art_id}.png"
    store.stash(r2_key, b"\x89PNG")

    session.enqueue(
        SimpleNamespace(
            first=lambda: SimpleNamespace(
                id=art_id,
                user_id=uuid4(),
                slug="img",
                title=None,
                summary=None,
                content_type="image/png",
                size_bytes=4,
                r2_key=r2_key,
                tags=[],
                created_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
            )
        )
    )

    out = await artifact_tools.artifact_read_executor(artifact_id=str(art_id), ctx=_ctx())

    # R2 must not be fetched for binary types
    assert store.get_calls == []
    assert out.get("content") is None


@pytest.mark.asyncio
async def test_artifact_read_binary_no_agent_fetchable_url_ac8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-8 (ADR-0101 §7): a binary/image read exposes no agent-fetchable URL field.

    Any URL must be absent from the agent-readable content or carried only under an
    explicitly human-display-only key, and a note must state the bytes are delivered
    via the turn's content block, not by URL.
    """
    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)

    art_id = uuid4()
    session.enqueue(
        SimpleNamespace(
            first=lambda: SimpleNamespace(
                id=art_id,
                user_id=uuid4(),
                slug="img",
                title=None,
                summary=None,
                content_type="image/png",
                size_bytes=4,
                r2_key=f"artifact/uid/GLOBAL/{art_id}.png",
                tags=[],
                created_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
            )
        )
    )

    out = await artifact_tools.artifact_read_executor(artifact_id=str(art_id), ctx=_ctx())

    # No bare public_url — that field reads as an agent-fetchable content source.
    assert "public_url" not in out
    # The URL, wherever it appears, is only under the explicitly human-display key.
    public = f"https://artifacts.test/{art_id}"
    url_keys = [k for k, v in out.items() if isinstance(v, str) and public in v]
    assert url_keys == ["human_display_url"]
    # The note must state the bytes are not URL-fetchable and come via the turn.
    assert "content block" in out["note"]
    assert "not url-fetchable" in out["note"].lower()


@pytest.mark.asyncio
async def test_artifact_read_bare_text_upload_keeps_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare text upload (text/plain, no charset) is text-like — keeps public_url.

    Guards the AC-8 predicate against mislabelling upload text types
    (uploads_router.ALLOWED_UPLOAD_CONTENT_TYPES) as binary/image.
    """
    store = _FakeStore()
    session = _install_fakes(monkeypatch, store=store)

    art_id = uuid4()
    session.enqueue(
        SimpleNamespace(
            first=lambda: SimpleNamespace(
                id=art_id,
                user_id=uuid4(),
                slug="notes",
                title="Notes",
                summary=None,
                content_type="text/plain",  # bare upload type — not in _TEXTUAL_CONTENT_TYPES
                size_bytes=512 * 1024,  # over inline cap → no inline content either way
                r2_key=f"upload/uid/GLOBAL/{art_id}.txt",
                tags=[],
                created_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
            )
        )
    )

    out = await artifact_tools.artifact_read_executor(artifact_id=str(art_id), ctx=_ctx())

    # Text-like: plain public_url, not the binary/image note treatment.
    assert out["public_url"].endswith(str(art_id))
    assert "human_display_url" not in out
    assert "note" not in out


@pytest.mark.asyncio
async def test_artifact_read_cross_user_raises_toolexec_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row not found for (artifact_id, user_id) → ToolExecutionError, not 404."""
    session = _install_fakes(monkeypatch, store=_FakeStore())
    # Canned result: no row found (existence-hiding per ADR-0064 D3)
    session.enqueue(SimpleNamespace(first=lambda: None))

    with pytest.raises(ToolExecutionError, match="not found"):
        await artifact_tools.artifact_read_executor(artifact_id=str(uuid4()), ctx=_ctx())


@pytest.mark.asyncio
async def test_artifact_read_invalid_uuid_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fakes(monkeypatch, store=_FakeStore())

    with pytest.raises(ToolExecutionError):
        await artifact_tools.artifact_read_executor(artifact_id="not-a-uuid", ctx=_ctx())


@pytest.mark.asyncio
async def test_artifact_read_requires_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fakes(monkeypatch, store=_FakeStore())

    with pytest.raises(ToolExecutionError, match="user_id"):
        await artifact_tools.artifact_read_executor(
            artifact_id=str(uuid4()), ctx=SimpleNamespace(trace_id="t")
        )


# ---------------------------------------------------------------------------
# artifact_draft — fakes and helpers (ADR-0077)
# ---------------------------------------------------------------------------

_VALID_HTML = (
    "<!DOCTYPE html><html><head><style>:root{--color-primary:#000}</style>"
    "</head><body><main><h1>Test</h1></main></body></html>"
)


class _FakeSubAgentClient:
    """Mock LLM client for sub-agent inference in artifact_draft tests."""

    def __init__(self, html_content: str = _VALID_HTML, completion_tokens: int = 500) -> None:
        self.html_content = html_content
        self.completion_tokens = completion_tokens
        self.respond_calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> dict[str, Any]:
        self.respond_calls.append(kwargs)
        return {
            "role": "assistant",
            "content": self.html_content,
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"prompt_tokens": 100, "completion_tokens": self.completion_tokens},
            "response_id": None,
            "raw": {},
        }


def _install_draft_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    html_content: str = _VALID_HTML,
    completion_tokens: int = 500,
) -> tuple[_FakeStore, _FakeSubAgentClient]:
    """Install fakes for artifact_draft tests: R2/DB/embedding + sub-agent client.

    Since ADR-0122 T5 the builder decision is raised at TURN START, not at the build
    boundary. A direct ``artifact_draft_executor`` call never runs ``step_init``, so the
    resolution carrier is ``None`` (guaranteed by the autouse ``_reset_builder_carrier``
    fixture) — a missed prediction (§3b/AC-11): the build keeps the pre-existing
    role-name path and additionally emits ``artifact_build_intent_missed`` (an event
    legacy assertions ignore). Tests wanting the selected-key path install a decision
    via :func:`_install_builder_decision`.
    """
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)
    client = _FakeSubAgentClient(html_content=html_content, completion_tokens=completion_tokens)
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client",
        lambda role_name="primary": client,
    )
    return store, client


class _SpyLogger:
    """Records (event, kwargs) for every structlog level call (FRE-478 cap-hit test)."""

    def __init__(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        self._events = events

    def _record(self, event: str, **kwargs: Any) -> None:
        self._events.append((event, kwargs))

    info = _record
    warning = _record
    error = _record
    debug = _record

    def bind(self, **_kwargs: Any) -> "_SpyLogger":
        return self


def _spy_artifact_log(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    """Replace the artifact_tools module logger with a recording spy."""
    monkeypatch.setattr(artifact_tools, "log", _SpyLogger(events))


# ---------------------------------------------------------------------------
# artifact_draft — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_draft_returns_expected_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Result includes artifact_write keys plus draft-specific metadata."""
    _install_draft_fakes(monkeypatch)

    out = await artifact_tools.artifact_draft_executor(
        slug="report",
        title="Test Report",
        summary="A test",
        plan="Section 1: Introduction. Section 2: Data.",
        ctx=_ctx(),
    )

    assert "artifact_id" in out
    assert out["public_url"].startswith("https://artifacts.test/")
    assert out["slug"] == "report"
    assert out["content_type"] == "text/html; charset=utf-8"
    assert out["size_bytes"] > 0
    assert out["title"] == "Test Report"
    assert out["summary"] == "A test"
    assert out["generation_method"] == "draft"
    assert isinstance(out["sub_agent_duration_ms"], int)
    assert out["task_id"].startswith("draft-")


@pytest.mark.asyncio
async def test_artifact_draft_uses_artifact_builder_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1 slice (ADR-0118 T1, FRE-879): telemetry role is ARTIFACT_BUILDER, not SUB_AGENT."""
    from personal_agent.llm_client.types import ModelRole

    _store, client = _install_draft_fakes(monkeypatch)

    await artifact_tools.artifact_draft_executor(
        slug="role-check", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert len(client.respond_calls) == 1
    assert client.respond_calls[0]["role"] == ModelRole.ARTIFACT_BUILDER


@pytest.mark.asyncio
async def test_artifact_draft_start_log_reports_artifact_builder_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1 slice: the artifact_draft_sub_agent_start log's model_role field switches too."""
    events: list[tuple[str, dict[str, Any]]] = []
    _install_draft_fakes(monkeypatch)
    _spy_artifact_log(monkeypatch, events)

    await artifact_tools.artifact_draft_executor(
        slug="log-check", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    start_events = [kwargs for event, kwargs in events if event == "artifact_draft_sub_agent_start"]
    assert len(start_events) == 1
    assert start_events[0]["model_role"] == "artifact_builder"


# ---------------------------------------------------------------------------
# artifact_draft — ADR-0122 §2/§4 build-time decision wiring (FRE-882)
# ---------------------------------------------------------------------------


def _install_builder_decision(
    *,
    resolution: str,
    action_id: str = "unused",
) -> ConstraintDecision:
    """Publish a turn-start builder resolution on the ADR-0122 T5 carrier.

    Since T5 the build boundary READS the turn-scoped resolution rather than raising a
    pause, so tests install the resolution the way ``step_init`` would.
    """
    decision = ConstraintDecision(action_id, resolution)
    set_artifact_builder_resolution(decision)
    return decision


class _KeyFactorySpy:
    """Records ``get_llm_client_for_key`` calls and returns a fixed fake client."""

    def __init__(self, client: _FakeSubAgentClient) -> None:
        self._client = client
        self.calls: list[tuple[str, str]] = []

    def __call__(self, model_key: str, budget_role: str = "skill_routing") -> _FakeSubAgentClient:
        self.calls.append((model_key, budget_role))
        return self._client


@pytest.mark.asyncio
async def test_artifact_draft_does_not_raise_pause_at_build_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T5: the build boundary no longer raises the pause — it reads the turn-scoped carrier.

    The ask now fires at turn start (step_init); asking here raised the card ~117 s late
    (the AC-7 failure). A card pick installed on the carrier is honoured without any
    build-boundary pause.
    """
    _store, client = _install_draft_fakes(monkeypatch)
    _install_builder_decision(resolution="user_choice", action_id="claude_sonnet")
    monkeypatch.setattr(
        "personal_agent.orchestrator.constraint_options.resolve_artifact_builder_key",
        lambda selected_key, config, **_kw: selected_key,
    )
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client_for_key",
        lambda model_key, budget_role="skill_routing": client,
    )
    pause = AsyncMock(return_value=ConstraintDecision("unused", "connection_lost"))
    monkeypatch.setattr("personal_agent.orchestrator.executor._maybe_pause_for_constraint", pause)

    await artifact_tools.artifact_draft_executor(
        slug="x", title="My Report", summary="S", plan="A plan.", ctx=_ctx()
    )

    pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_artifact_draft_missing_resolution_emits_intent_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-11: a build reached with no turn-scoped resolution degrades to the default AND logs.

    The classifier never predicted this build (a false negative). The artifact renders on
    the role's configured default (pre-card behaviour) and an ``artifact_build_intent_missed``
    event is emitted identifying the turn, so the regex vocabulary can be tuned — never a
    silent fallback. (The request text is not re-logged here — it already rides the
    ``task_started`` event under the same ``trace_id``.)
    """
    events: list[tuple[str, dict[str, Any]]] = []
    _store, client = _install_draft_fakes(monkeypatch)
    _spy_artifact_log(monkeypatch, events)
    # No resolution on the carrier — the state step_init leaves when the
    # artifact_build_intent signal was absent (guaranteed by the autouse fixture).
    set_artifact_builder_resolution(None)
    key_factory = MagicMock()
    monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client_for_key", key_factory)

    out = await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert "artifact_id" in out  # build completes, never errors
    key_factory.assert_not_called()  # never the by-key path
    assert len(client.respond_calls) == 1  # role-name default path ran
    missed = [kw for ev, kw in events if ev == "artifact_build_intent_missed"]
    assert len(missed) == 1
    assert missed[0]["slug"] == "x"  # identifies the turn's build; no raw request text


@pytest.mark.asyncio
async def test_artifact_draft_two_builds_share_one_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-10(c): one turn-start selection covers every build in the turn (no re-ask).

    Two ``artifact_draft`` calls in the same turn both run on the single selected key.
    """
    _store, client = _install_draft_fakes(monkeypatch)
    _install_builder_decision(resolution="user_choice", action_id="claude_sonnet")
    monkeypatch.setattr(
        "personal_agent.orchestrator.constraint_options.resolve_artifact_builder_key",
        lambda selected_key, config, **_kw: selected_key,
    )
    key_spy = _KeyFactorySpy(client)
    monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client_for_key", key_spy)

    await artifact_tools.artifact_draft_executor(
        slug="a", title="A", summary="S", plan="Plan A.", ctx=_ctx()
    )
    await artifact_tools.artifact_draft_executor(
        slug="b", title="B", summary="S", plan="Plan B.", ctx=_ctx()
    )

    assert key_spy.calls == [
        ("claude_sonnet", "artifact_builder"),
        ("claude_sonnet", "artifact_builder"),
    ]


@pytest.mark.asyncio
async def test_resolution_survives_step_init_to_gather_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1/AC-10 (codex plan-review): the pick survives step_init → asyncio.gather → artifact_draft.

    The real tool dispatch runs each call inside ``asyncio.gather`` (executor.py:4377).
    This drives the actual carrier path — resolve at turn start, then dispatch the build in
    a gather child task — and asserts the selected model still runs, proving the resolution
    survives the task boundary and intervening work rather than only an immediate
    post-``step_init`` read.
    """
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator import executor as executor_mod
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.session import SessionManager
    from personal_agent.orchestrator.types import ExecutionContext
    from personal_agent.request_gateway.types import (
        AssembledContext,
        Complexity,
        DecompositionResult,
        DecompositionStrategy,
        GatewayOutput,
        GovernanceContext,
        IntentResult,
        TaskType,
    )
    from personal_agent.telemetry.trace import TraceContext

    _store, client = _install_draft_fakes(monkeypatch)
    monkeypatch.setattr(
        "personal_agent.orchestrator.constraint_options.resolve_artifact_builder_key",
        lambda selected_key, config, **_kw: selected_key,
    )
    key_spy = _KeyFactorySpy(client)
    monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client_for_key", key_spy)
    monkeypatch.setattr(
        executor_mod,
        "_maybe_pause_for_constraint",
        AsyncMock(return_value=ConstraintDecision("claude_sonnet", "user_choice")),
    )

    sm = SessionManager()
    session_id = sm.create_session(Mode.NORMAL, Channel.CHAT)
    trace = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace.trace_id,
        user_message="build me a dashboard",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        eval_mode=True,
    )
    ctx.gateway_output = GatewayOutput(
        intent=IntentResult(
            task_type=TaskType.TOOL_USE,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=["tool_intent_pattern", "artifact_build_intent"],
        ),
        governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
        decomposition=DecompositionResult(strategy=DecompositionStrategy.SINGLE, reason="t"),
        context=AssembledContext(messages=[], memory_context=None, tool_definitions=None),
        session_id=session_id,
        trace_id=trace.trace_id,
    )

    # Turn start resolves the builder (sets the carrier in this task's context).
    await executor_mod.step_init(ctx, sm, trace)

    # Later: the build runs inside asyncio.gather, exactly as step_tool_execution dispatches.
    (out,) = await asyncio.gather(
        artifact_tools.artifact_draft_executor(
            slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
        )
    )

    assert "artifact_id" in out
    assert key_spy.calls == [("claude_sonnet", "artifact_builder")]


@pytest.mark.asyncio
async def test_ac13_stored_preference_end_to_end_no_pause_correct_sizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-13, full loop: a real stored preference at turn start → no pause → correct sizing.

    Drives the actual ``_maybe_pause_for_constraint`` preference branch (only
    ``_load_constraint_preference`` is faked, not the pause helper itself), asserts
    no ``ConstraintPauseEvent`` is ever pushed, and — via the same step_init →
    asyncio.gather → artifact_draft path as
    ``test_resolution_survives_step_init_to_gather_dispatch`` — that the generation
    call's requested ``max_tokens`` equals the preferred deployment's (claude_haiku,
    4096) derived budget, not the global constant (32768). This is the exact
    invisible failure AC-13 exists to catch: an implementation could pass AC-12 on
    the card path while still sizing the silent preference path off the constant.
    """
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator import executor as executor_mod
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.session import SessionManager
    from personal_agent.orchestrator.types import ExecutionContext
    from personal_agent.request_gateway.types import (
        AssembledContext,
        Complexity,
        DecompositionResult,
        DecompositionStrategy,
        GatewayOutput,
        GovernanceContext,
        IntentResult,
        TaskType,
    )
    from personal_agent.telemetry.trace import TraceContext

    _store, client = _install_draft_fakes(monkeypatch, completion_tokens=4096)
    monkeypatch.setattr(
        "personal_agent.orchestrator.constraint_options.resolve_artifact_builder_key",
        lambda selected_key, config, **_kw: selected_key,
    )
    key_spy = _KeyFactorySpy(client)
    monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client_for_key", key_spy)
    monkeypatch.setattr(
        executor_mod, "_load_constraint_preference", AsyncMock(return_value="claude_haiku")
    )
    push_pause = AsyncMock()
    monkeypatch.setattr(
        "personal_agent.transport.agui.transport.register_and_push_constraint", push_pause
    )

    sm = SessionManager()
    session_id = sm.create_session(Mode.NORMAL, Channel.CHAT)
    trace = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace.trace_id,
        user_message="build me a dashboard",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        eval_mode=True,
    )
    ctx.gateway_output = GatewayOutput(
        intent=IntentResult(
            task_type=TaskType.TOOL_USE,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=["tool_intent_pattern", "artifact_build_intent"],
        ),
        governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
        decomposition=DecompositionResult(strategy=DecompositionStrategy.SINGLE, reason="t"),
        context=AssembledContext(messages=[], memory_context=None, tool_definitions=None),
        session_id=session_id,
        trace_id=trace.trace_id,
    )

    await executor_mod.step_init(ctx, sm, trace)

    assert ctx.artifact_builder_resolution == ConstraintDecision(
        "claude_haiku", "preference_applied"
    )
    push_pause.assert_not_awaited()  # no card — the preference resolved silently
    assert ctx.artifact_builder_planning_note is not None
    assert "claude_haiku" in ctx.artifact_builder_planning_note
    assert "4096" in ctx.artifact_builder_planning_note

    (out,) = await asyncio.gather(
        artifact_tools.artifact_draft_executor(
            slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
        )
    )

    assert "artifact_id" in out
    assert key_spy.calls == [("claude_haiku", "artifact_builder")]
    assert client.respond_calls[0]["max_tokens"] == 4096  # claude_haiku's declared cap
    assert client.respond_calls[0]["max_tokens"] != settings.artifact_draft_max_tokens


@pytest.mark.asyncio
async def test_artifact_draft_resolution_does_not_leak_after_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-10(c): a resolved pick does not leak past its turn — a later build is a miss.

    Mirrors the ``execute_task`` finally reset: once the carrier is cleared, a subsequent
    build reads ``None`` and degrades to the default (missed prediction), never the prior
    turn's pick.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    _store, client = _install_draft_fakes(monkeypatch)
    _spy_artifact_log(monkeypatch, events)
    token = set_artifact_builder_resolution(ConstraintDecision("claude_sonnet", "user_choice"))
    key_factory = MagicMock()
    monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client_for_key", key_factory)

    # Simulate the turn ending: clear the carrier back to the pre-turn default.
    reset_artifact_builder_resolution(token)

    await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    key_factory.assert_not_called()  # the prior turn's pick did not leak
    assert len(client.respond_calls) == 1  # default path
    assert any(ev == "artifact_build_intent_missed" for ev, _ in events)


@pytest.mark.asyncio
async def test_artifact_draft_user_choice_uses_key_path_with_budget_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1/AC-2: an interactive card pick switches to the by-key call.

    Explicitly billing the artifact_builder lane (the new mis-billing risk).
    """
    _store, client = _install_draft_fakes(monkeypatch)
    _install_builder_decision(resolution="user_choice", action_id="claude_sonnet")
    monkeypatch.setattr(
        "personal_agent.orchestrator.constraint_options.resolve_artifact_builder_key",
        lambda selected_key, config, **_kw: selected_key,
    )
    role_name_factory = MagicMock()
    monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client", role_name_factory)
    key_spy = _KeyFactorySpy(client)
    monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client_for_key", key_spy)

    await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert key_spy.calls == [("claude_sonnet", "artifact_builder")]
    role_name_factory.assert_not_called()


@pytest.mark.asyncio
async def test_artifact_draft_preference_applied_uses_key_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A standing preference (pre-resolved silently, no card) also runs on its own key."""
    _store, client = _install_draft_fakes(monkeypatch)
    _install_builder_decision(resolution="preference_applied", action_id="qwen3.6-35b-thinking")
    monkeypatch.setattr(
        "personal_agent.orchestrator.constraint_options.resolve_artifact_builder_key",
        lambda selected_key, config, **_kw: selected_key,
    )
    key_spy = _KeyFactorySpy(client)
    monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client_for_key", key_spy)

    await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert key_spy.calls == [("qwen3.6-35b-thinking", "artifact_builder")]


@pytest.mark.parametrize("resolution", ["timeout_default", "connection_lost", "user_cancel"])
@pytest.mark.asyncio
async def test_artifact_draft_no_decision_keeps_role_name_path(
    monkeypatch: pytest.MonkeyPatch, resolution: str
) -> None:
    """AC-2 regression guard: a no-answer resolution never touches the by-key call.

    It keeps today's already-correct role-name path and default binding (FRE-879).
    """
    _store, client = _install_draft_fakes(monkeypatch)
    _install_builder_decision(resolution=resolution, action_id="qwen3.6-35b-instruct")
    key_factory = MagicMock()
    monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client_for_key", key_factory)
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client",
        lambda role_name="primary": client,
    )

    await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    key_factory.assert_not_called()
    assert len(client.respond_calls) == 1


@pytest.mark.asyncio
async def test_artifact_draft_invalid_key_falls_back_and_logs_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-4: a card-selected key absent from the catalog never reaches the factory.

    It substitutes the configured default and logs the substitution.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    _store, client = _install_draft_fakes(monkeypatch)
    _spy_artifact_log(monkeypatch, events)
    _install_builder_decision(resolution="user_choice", action_id="not-a-real-model")
    key_spy = _KeyFactorySpy(client)
    monkeypatch.setattr("personal_agent.llm_client.factory.get_llm_client_for_key", key_spy)

    out = await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert "artifact_id" in out  # the build still completes (never errors, never no-model)
    assert len(key_spy.calls) == 1
    substituted_key, budget_role = key_spy.calls[0]
    assert substituted_key != "not-a-real-model"  # the invalid key never reached the factory
    assert budget_role == "artifact_builder"

    substitution_events = [kw for ev, kw in events if ev == "artifact_builder_key_substituted"]
    assert len(substitution_events) == 1
    assert substitution_events[0]["requested_key"] == "not-a-real-model"
    assert substitution_events[0]["substituted_key"] == substituted_key


@pytest.mark.asyncio
async def test_artifact_draft_no_substitution_log_when_key_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No substitution log fires when the resolved key equals what was requested."""
    events: list[tuple[str, dict[str, Any]]] = []
    _store, client = _install_draft_fakes(monkeypatch)
    _spy_artifact_log(monkeypatch, events)
    _install_builder_decision(resolution="user_choice", action_id="claude_sonnet")
    monkeypatch.setattr(
        "personal_agent.orchestrator.constraint_options.resolve_artifact_builder_key",
        lambda selected_key, config, **_kw: selected_key,
    )
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client_for_key",
        _KeyFactorySpy(client),
    )

    await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    substitution_events = [kw for ev, kw in events if ev == "artifact_builder_key_substituted"]
    assert substitution_events == []


@pytest.mark.asyncio
async def test_artifact_draft_chains_to_artifact_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 store receives the sub-agent HTML and Postgres INSERT fires."""
    store, _client = _install_draft_fakes(monkeypatch)

    await artifact_tools.artifact_draft_executor(
        slug="chained",
        title="Chained",
        summary="s",
        plan="Build a table.",
        ctx=_ctx(),
    )

    assert len(store.put_calls) == 1
    assert store.put_calls[0]["content_type"] == "text/html; charset=utf-8"
    assert b"<!DOCTYPE html>" in store.put_calls[0]["content"]


@pytest.mark.asyncio
async def test_artifact_draft_content_type_is_always_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """artifact_draft always produces text/html; charset=utf-8."""
    store, _client = _install_draft_fakes(monkeypatch)

    out = await artifact_tools.artifact_draft_executor(
        slug="always-html",
        title="T",
        summary="S",
        plan="Make a chart.",
        ctx=_ctx(),
    )

    assert out["content_type"] == "text/html; charset=utf-8"
    assert store.put_calls[0]["r2_key"].endswith(".html")


@pytest.mark.asyncio
async def test_artifact_draft_strips_markdown_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Markdown code fences wrapping the HTML are stripped before write."""
    fenced_html = f"```html\n{_VALID_HTML}\n```"
    store, _client = _install_draft_fakes(monkeypatch, html_content=fenced_html)

    await artifact_tools.artifact_draft_executor(
        slug="fenced",
        title="T",
        summary="S",
        plan="Build a page.",
        ctx=_ctx(),
    )

    written = store.put_calls[0]["content"].decode("utf-8")
    assert not written.startswith("```")
    assert written.startswith("<!DOCTYPE html>")


@pytest.mark.asyncio
async def test_artifact_draft_passes_tags_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tags propagate to artifact_write_executor."""
    _install_draft_fakes(monkeypatch)

    out = await artifact_tools.artifact_draft_executor(
        slug="tagged",
        title="T",
        summary="S",
        plan="Build it.",
        tags=["report", "q3"],
        ctx=_ctx(),
    )

    assert out["slug"] == "tagged"


# ---------------------------------------------------------------------------
# artifact_draft — observability (D8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_draft_passes_trace_ctx_to_respond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """respond() receives a TraceContext child span with matching trace_id and session_id."""
    from personal_agent.telemetry.trace import TraceContext

    _store, client = _install_draft_fakes(monkeypatch)

    user_id = uuid4()
    session_id = str(uuid4())
    ctx = TraceContext(
        trace_id="trace-abc",
        session_id=session_id,
        user_id=user_id,
    )

    await artifact_tools.artifact_draft_executor(
        slug="trace-test",
        title="T",
        summary="S",
        plan="Plan content here.",
        ctx=ctx,
    )

    assert len(client.respond_calls) == 1
    call = client.respond_calls[0]
    child_ctx = call["trace_ctx"]
    assert child_ctx.trace_id == "trace-abc"
    assert child_ctx.session_id == session_id
    assert child_ctx.parent_span_id is not None  # child span was created


@pytest.mark.asyncio
async def test_artifact_draft_passes_timeout_to_respond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """respond() receives timeout_s matching the draft timeout constant."""
    _store, client = _install_draft_fakes(monkeypatch)

    await artifact_tools.artifact_draft_executor(
        slug="timeout-test",
        title="T",
        summary="S",
        plan="A plan.",
        ctx=_ctx(),
    )

    call = client.respond_calls[0]
    assert call["timeout_s"] == artifact_tools._draft_timeout_s()


@pytest.mark.asyncio
async def test_draft_timeout_matches_primary_reasoning_model() -> None:
    """artifact_draft's sub-agent timeout tracks the reasoning model (primary) budget."""
    from personal_agent.config.model_loader import resolve_role_definition

    # "primary" is a ROLE; since ADR-0121 the catalog is keyed by model, so it
    # must be resolved through its binding rather than looked up as a key.
    primary = resolve_role_definition("primary")
    assert primary is not None and primary.default_timeout
    assert artifact_tools._draft_timeout_s() == float(primary.default_timeout)


@pytest.mark.asyncio
async def test_artifact_draft_calls_respond_with_correct_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """respond() receives the effective (deployment-floored) max_tokens (ADR-0122 §5).

    No turn-scoped resolution is installed, so the build renders on the configured
    default (``claude_sonnet``, declared ``max_tokens`` 32768) — which happens to
    equal the operator ceiling, matching the pre-T6 assertion.
    """
    _store, client = _install_draft_fakes(monkeypatch)

    await artifact_tools.artifact_draft_executor(
        slug="tokens-test",
        title="T",
        summary="S",
        plan="A plan.",
        ctx=_ctx(),
    )

    call = client.respond_calls[0]
    assert call["max_tokens"] == settings.artifact_draft_max_tokens
    assert call["max_tokens"] == 32768


def test_draft_max_tokens_derives_effective_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """_draft_max_tokens() derives min(deployment_max_tokens, ceiling) (ADR-0122 §5, AC-12)."""
    monkeypatch.setattr(artifact_tools.settings, "artifact_draft_max_tokens", 12345)
    # Model below ceiling — the model wins.
    assert artifact_tools._draft_max_tokens(4096) == 4096
    # Model above ceiling — the ceiling wins.
    assert artifact_tools._draft_max_tokens(32768) == 12345
    # No declared cap (provider default) — the ceiling alone applies.
    assert artifact_tools._draft_max_tokens(None) == 12345


@pytest.mark.asyncio
async def test_artifact_draft_max_tokens_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overriding the setting flows through to the respond() call (FRE-478)."""
    _store, client = _install_draft_fakes(monkeypatch)
    monkeypatch.setattr(artifact_tools.settings, "artifact_draft_max_tokens", 24576)

    await artifact_tools.artifact_draft_executor(
        slug="configurable-tokens",
        title="T",
        summary="S",
        plan="A plan.",
        ctx=_ctx(),
    )

    assert client.respond_calls[0]["max_tokens"] == 24576


# ---------------------------------------------------------------------------
# artifact_draft — ADR-0122 §5/T6: the output budget follows the selected model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_draft_ac12a_model_below_ceiling_model_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-12(a): claude_haiku (declared max_tokens 4096) under the default 32768 ceiling.

    Requests 4096, and the truncation-warning threshold is likewise 4096.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    _store, client = _install_draft_fakes(monkeypatch, completion_tokens=4096)
    _spy_artifact_log(monkeypatch, events)
    _install_builder_decision(resolution="user_choice", action_id="claude_haiku")
    monkeypatch.setattr(
        "personal_agent.orchestrator.constraint_options.resolve_artifact_builder_key",
        lambda selected_key, config, **_kw: selected_key,
    )
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client_for_key", _KeyFactorySpy(client)
    )

    await artifact_tools.artifact_draft_executor(
        slug="haiku-under-ceiling", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert client.respond_calls[0]["max_tokens"] == 4096
    cap_hits = [kw for ev, kw in events if ev == "artifact_draft_output_cap_hit"]
    assert len(cap_hits) == 1
    assert cap_hits[0]["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_artifact_draft_ac12b_model_above_ceiling_ceiling_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-12(b): claude_sonnet (declared max_tokens 32768) under a 2048 ceiling.

    Requests 2048 — the ceiling wins.
    """
    monkeypatch.setattr(artifact_tools.settings, "artifact_draft_max_tokens", 2048)
    events: list[tuple[str, dict[str, Any]]] = []
    _store, client = _install_draft_fakes(monkeypatch, completion_tokens=2048)
    _spy_artifact_log(monkeypatch, events)
    _install_builder_decision(resolution="user_choice", action_id="claude_sonnet")
    monkeypatch.setattr(
        "personal_agent.orchestrator.constraint_options.resolve_artifact_builder_key",
        lambda selected_key, config, **_kw: selected_key,
    )
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client_for_key", _KeyFactorySpy(client)
    )

    await artifact_tools.artifact_draft_executor(
        slug="sonnet-over-ceiling", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert client.respond_calls[0]["max_tokens"] == 2048
    cap_hits = [kw for ev, kw in events if ev == "artifact_draft_output_cap_hit"]
    assert len(cap_hits) == 1
    assert cap_hits[0]["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_artifact_draft_ac13_preference_path_sizes_to_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-13: the silent preference path also sizes to the deployment, not the constant.

    An implementation that pre-resolves the preference only to suppress the card,
    without threading the deployment into the budget, would still request the global
    constant here — this is the invisible failure AC-13 exists to catch.
    """
    _store, client = _install_draft_fakes(monkeypatch, completion_tokens=4096)
    _install_builder_decision(resolution="preference_applied", action_id="claude_haiku")
    monkeypatch.setattr(
        "personal_agent.orchestrator.constraint_options.resolve_artifact_builder_key",
        lambda selected_key, config, **_kw: selected_key,
    )
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client_for_key", _KeyFactorySpy(client)
    )

    await artifact_tools.artifact_draft_executor(
        slug="preference-sizing", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert client.respond_calls[0]["max_tokens"] == 4096  # claude_haiku's declared cap
    assert client.respond_calls[0]["max_tokens"] != settings.artifact_draft_max_tokens


@pytest.mark.asyncio
async def test_artifact_draft_no_decision_sizes_to_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-answer resolution also sizes to the configured default's declared cap.

    The role-name path ignores the carried (unconfirmed) action_id and always renders
    on the configured default (``claude_sonnet``, 32768) — floored here by a 2048
    ceiling — never the pre-T6 flat constant regardless of deployment.
    """
    monkeypatch.setattr(artifact_tools.settings, "artifact_draft_max_tokens", 2048)
    _store, client = _install_draft_fakes(monkeypatch, completion_tokens=2048)
    _install_builder_decision(resolution="timeout_default", action_id="claude_haiku")

    await artifact_tools.artifact_draft_executor(
        slug="timeout-sizing", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert client.respond_calls[0]["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_artifact_draft_logs_output_cap_hit_when_cap_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cap-hit warning fires when output_tokens reaches the configured cap (FRE-478)."""
    monkeypatch.setattr(artifact_tools.settings, "artifact_draft_max_tokens", 500)
    _store, client = _install_draft_fakes(monkeypatch)
    # _FakeSubAgentClient reports completion_tokens=500 → equals the cap.

    events: list[tuple[str, dict[str, Any]]] = []
    _spy_artifact_log(monkeypatch, events)

    await artifact_tools.artifact_draft_executor(
        slug="cap-hit",
        title="T",
        summary="S",
        plan="A plan.",
        ctx=_ctx(),
    )

    cap_hits = [e for e in events if e[0] == "artifact_draft_output_cap_hit"]
    assert len(cap_hits) == 1
    assert cap_hits[0][1]["output_tokens"] == 500
    assert cap_hits[0][1]["max_tokens"] == 500


@pytest.mark.asyncio
async def test_artifact_draft_no_cap_hit_log_under_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cap-hit warning when output_tokens is below the cap (FRE-478)."""
    monkeypatch.setattr(artifact_tools.settings, "artifact_draft_max_tokens", 32768)
    _store, client = _install_draft_fakes(monkeypatch)
    # _FakeSubAgentClient reports completion_tokens=500 ≪ 32768.

    events: list[tuple[str, dict[str, Any]]] = []
    _spy_artifact_log(monkeypatch, events)

    await artifact_tools.artifact_draft_executor(
        slug="under-cap",
        title="T",
        summary="S",
        plan="A plan.",
        ctx=_ctx(),
    )

    assert not [e for e in events if e[0] == "artifact_draft_output_cap_hit"]


# ---------------------------------------------------------------------------
# artifact_draft — input validation (D9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_draft_empty_plan_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_draft_fakes(monkeypatch)

    with pytest.raises(ToolExecutionError, match="plan"):
        await artifact_tools.artifact_draft_executor(
            slug="x", title="T", summary="S", plan="", ctx=_ctx()
        )


@pytest.mark.asyncio
async def test_artifact_draft_oversized_plan_truncates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversized plan is truncated-with-warning, not rejected (FRE-471)."""
    _store, client = _install_draft_fakes(monkeypatch)

    plan = "section\n" * 4000  # ~32k chars with line boundaries
    out = await artifact_tools.artifact_draft_executor(
        slug="x",
        title="T",
        summary="S",
        plan=plan,
        ctx=_ctx(),
    )

    # Still produced an artifact rather than raising terminally.
    assert "artifact_id" in out
    assert out["plan_truncated"] is True
    assert out["plan_original_length"] == len(plan)

    # The sub-agent prompt carried the (truncated) plan plus the truncation notice.
    prompt = client.respond_calls[0]["messages"][1]["content"]
    assert artifact_tools._PLAN_TRUNCATION_NOTICE in prompt

    # The effective plan stayed within the cap and ended on a line boundary
    # (boundary-aware truncation — no mid-word "section" sever).
    effective_plan, was_truncated, original_length = artifact_tools._truncate_plan(plan)
    assert was_truncated is True
    assert original_length == len(plan)
    assert len(effective_plan) <= artifact_tools._MAX_PLAN_CHARS
    body = effective_plan[: -len(artifact_tools._PLAN_TRUNCATION_NOTICE)]
    assert body.endswith("section\n") or body.endswith("section")


@pytest.mark.asyncio
async def test_artifact_draft_plan_within_cap_not_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal plan passes through untouched with plan_truncated=False (FRE-471)."""
    _store, client = _install_draft_fakes(monkeypatch)

    plan = "Section 1: Intro. Section 2: Data table."
    out = await artifact_tools.artifact_draft_executor(
        slug="x",
        title="T",
        summary="S",
        plan=plan,
        ctx=_ctx(),
    )

    assert out["plan_truncated"] is False
    assert out["plan_original_length"] == len(plan)
    prompt = client.respond_calls[0]["messages"][1]["content"]
    assert artifact_tools._PLAN_TRUNCATION_NOTICE not in prompt


@pytest.mark.asyncio
async def test_artifact_draft_requires_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing user_id propagates from artifact_write_executor."""
    _install_draft_fakes(monkeypatch)

    with pytest.raises(ToolExecutionError, match="user_id"):
        await artifact_tools.artifact_draft_executor(
            slug="x",
            title="T",
            summary="S",
            plan="A plan.",
            ctx=SimpleNamespace(trace_id="t"),
        )


# ---------------------------------------------------------------------------
# artifact_draft — output validation (D9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_draft_rejects_missing_doctype(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from personal_agent.tools.executor import TerminalToolError

    _install_draft_fakes(
        monkeypatch,
        html_content="<html><head><title>Test</title></head><body><h1>No doctype here</h1></body></html>",
    )

    # Malformation (truncated/incomplete output) is recoverable — NOT terminal — so
    # the model can retry (quality validator, ADR-0089 D1).
    with pytest.raises(ToolExecutionError, match="DOCTYPE") as exc_info:
        await artifact_tools.artifact_draft_executor(
            slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
        )
    assert not isinstance(exc_info.value, TerminalToolError)


# ---------------------------------------------------------------------------
# artifact_draft — scripts ship intact (ADR-0089 D1, FRE-511)
# ---------------------------------------------------------------------------

_SCRIPT_HTML = (
    "<!DOCTYPE html><html><head><style>:root{--c:#000}</style></head><body>"
    "<main><h1>Interactive</h1><p>Some real content in the document body.</p>"
    "<script>alert(1)</script></main></body></html>"
)
_HANDLER_HTML = (
    "<!DOCTYPE html><html><head><style>:root{--c:#000}</style></head><body>"
    '<main><h1>Interactive</h1><div onclick="go()">click me for the demo content</div>'
    "</main></body></html>"
)


@pytest.mark.asyncio
async def test_artifact_draft_script_artifact_committed_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0089 D1: a <script> draft commits byte-intact — no strip, no banner, no fail.

    The served-CSP envelope (FRE-509) + opaque-origin sandbox (FRE-510) are the
    security boundary; the commit path makes no security decision on the bytes.
    """
    store, _client = _install_draft_fakes(monkeypatch, html_content=_SCRIPT_HTML)

    out = await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert "artifact_id" in out
    assert "sanitization_notes" not in out  # FRE-496 machinery retired
    stored = store.put_calls[0]["content"]
    assert stored == _SCRIPT_HTML.encode("utf-8")  # byte-identical
    assert b"artifact-sanitization-note" not in stored


@pytest.mark.asyncio
async def test_artifact_draft_event_handler_committed_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0089 D1: inline event handlers survive the commit unmodified."""
    store, _client = _install_draft_fakes(monkeypatch, html_content=_HANDLER_HTML)

    out = await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert "artifact_id" in out
    assert store.put_calls[0]["content"] == _HANDLER_HTML.encode("utf-8")


@pytest.mark.asyncio
async def test_artifact_write_direct_script_html_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct artifact_write of script-laden HTML commits intact (pins the ungated path)."""
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)

    out = await artifact_tools.artifact_write_executor(
        slug="direct-script",
        content_type="text/html; charset=utf-8",
        content=_SCRIPT_HTML,
        ctx=_ctx(),
    )

    assert "artifact_id" in out
    assert store.put_calls[0]["content"] == _SCRIPT_HTML.encode("utf-8")


@pytest.mark.asyncio
async def test_artifact_draft_single_attempt_on_script_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script-bearing draft triggers no retry — exactly one sub-agent call (FRE-511)."""
    _store, client = _install_draft_fakes(monkeypatch, html_content=_SCRIPT_HTML)

    await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert len(client.respond_calls) == 1


@pytest.mark.asyncio
async def test_artifact_draft_mermaid_plus_script_both_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draft mixing a mermaid block and a <script> ships with both lanes intact.

    The mermaid block is server-rendered to inline SVG (portability lane) while the
    script survives unmodified (interactivity lane) — ADR-0089 D7.
    """
    mixed = (
        "<!DOCTYPE html><html><head><style>:root{--c:#000}</style></head><body><main>"
        '<pre class="mermaid">graph LR; A--&gt;B;</pre>'
        "<script>alert(1)</script><p>Body content of the document.</p>"
        "</main></body></html>"
    )
    fake_svg = "<svg xmlns='http://www.w3.org/2000/svg'><text>diagram</text></svg>"

    async def _fake_render_one(source: str, *, trace_id: str, session_id: object) -> str:
        return f'<figure class="mermaid-diagram">{fake_svg}</figure>'

    store, _client = _install_draft_fakes(monkeypatch, html_content=mixed)
    monkeypatch.setattr(artifact_tools, "_render_mermaid_one", _fake_render_one)

    out = await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert "artifact_id" in out
    written = store.put_calls[0]["content"].decode("utf-8")
    assert "<svg" in written  # mermaid rendered
    assert '<pre class="mermaid">' not in written
    assert "<script>alert(1)</script>" in written  # script intact


# ---------------------------------------------------------------------------
# _validate_html_output — quality validator only (ADR-0089 D1)
# ---------------------------------------------------------------------------


def test_validate_html_output_accepts_scripts() -> None:
    """ADR-0089 D1: scripts and handlers are not a validation concern — must not raise."""
    artifact_tools._validate_html_output(_SCRIPT_HTML)
    artifact_tools._validate_html_output(_HANDLER_HTML)


def test_validate_html_output_rejects_truncated() -> None:
    """Malformation check: a document missing </html> still rejects (quality, recoverable)."""
    truncated = "<!DOCTYPE html><html><body><p>a document body that was cut off mid-stream"
    with pytest.raises(ToolExecutionError, match="</html>"):
        artifact_tools._validate_html_output(truncated)


def test_validate_html_output_rejects_tiny() -> None:
    """Malformation check: trivially small output still rejects (quality, recoverable)."""
    with pytest.raises(ToolExecutionError, match="small"):
        artifact_tools._validate_html_output("<p>x</p>")


def test_event_handler_detector_ignores_data_on_attributes() -> None:
    """A legit data-on* attribute must not inflate the analytics handler count."""
    html = (
        '<!DOCTYPE html><html><body><div data-online="yes" data-on-load="x">'
        "plenty of body content here</div></body></html>"
    )
    assert not artifact_tools._EVENT_HANDLER_RE.search(html)
    _scripts, handlers, _cdn = artifact_tools._count_sandbox_violations(html)
    assert handlers == 0


@pytest.mark.asyncio
async def test_artifact_draft_subagent_empty_html_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_draft_fakes(monkeypatch, html_content="")

    with pytest.raises(ToolExecutionError, match="trivially small"):
        await artifact_tools.artifact_draft_executor(
            slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
        )


@pytest.mark.asyncio
async def test_artifact_draft_subagent_timeout_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sub-agent timeout surfaces as a TerminalToolError with user-facing guidance (FRE-402)."""
    from personal_agent.tools.executor import TerminalToolError

    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)

    class _HangingClient:
        async def respond(self, **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(999)
            return {"content": ""}  # never reached

    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client",
        lambda role_name="primary": _HangingClient(),
    )
    # Temporarily reduce timeout for test speed
    monkeypatch.setattr(artifact_tools, "_draft_timeout_s", lambda: 0.1)

    with pytest.raises(TerminalToolError, match="timed out") as exc_info:
        await artifact_tools.artifact_draft_executor(
            slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
        )
    assert exc_info.value.reason
    assert exc_info.value.next_step


@pytest.mark.asyncio
async def test_artifact_draft_subagent_exception_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arbitrary sub-agent exception surfaces as ToolExecutionError with fallback guidance."""
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)

    class _FailingClient:
        async def respond(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("GPU OOM")

    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client",
        lambda role_name="primary": _FailingClient(),
    )

    with pytest.raises(ToolExecutionError, match="artifact_write directly"):
        await artifact_tools.artifact_draft_executor(
            slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
        )


# ---------------------------------------------------------------------------
# artifact_draft — static assertions
# ---------------------------------------------------------------------------


def test_system_prompt_allows_scripts() -> None:
    """FRE-511: the prompt no longer forbids <script>; it documents the sealed box.

    Regression on the reframed wording (ADR-0089 D7): no prohibition/rejection
    language, JS affirmatively available, sealed-box constraints named, and the
    portability steering present.
    """
    prompt = artifact_tools._html_generation_system_prompt()
    # No prohibition language left.
    assert "REJECTED" not in prompt
    assert "JavaScript-free" not in prompt
    assert "cannot run" not in prompt
    # JS affirmatively available + sealed-box constraints documented.
    assert "JavaScript is available" in prompt
    assert "No network" in prompt
    assert "No storage" in prompt
    # Portability steering: mermaid/SVG travels with the file; JS is view-on-origin.
    assert "PORTABILITY" in prompt
    assert "travel" in prompt


def test_system_prompt_instructs_mermaid_markup() -> None:
    """The system prompt must direct the model to use mermaid markup for static diagrams."""
    prompt = artifact_tools._html_generation_system_prompt()
    assert "mermaid" in prompt.lower()
    assert '<pre class="mermaid">' in prompt or "pre class" in prompt.lower()


def test_system_prompt_advertises_curated_lib_toolkit() -> None:
    """FRE-528 (ADR-0089 A4): the prompt advertises the curated /lib/ shelf.

    Manifest-driven drift guard: every non-eval-gated asset must appear as its
    full absolute, version-pinned URL (``origin + /lib/ + path``). A relative
    ``/lib/`` path is not counted as a demand-met reach by the meter
    (``_SCRIPT_SRC_RE``), so the prompt must steer absolute URLs. Native
    typography recipes (no library) must be present, and the eval-gated
    paged.js must NOT appear as a first-class snippet.
    """
    prompt = artifact_tools._html_generation_system_prompt()
    origin, assets = load_lib_manifest()

    for asset in assets:
        url = f"{origin}/lib/{asset.path}"
        if asset.eval_gated:
            assert url not in prompt, f"eval-gated asset must not be first-class: {url}"
        else:
            assert url in prompt, f"missing curated /lib/ snippet: {url}"

    # Native typography recipes (no library).
    assert "::first-letter" in prompt
    assert "hyphens: auto" in prompt
    assert "text-wrap: balance" in prompt
    assert "font-feature-settings" in prompt
    assert "column-count" in prompt
    assert "@page" in prompt

    # paged.js is named but flagged experimental/gated, never first-class.
    assert "experimental" in prompt.lower()

    # Arbitrary CDNs are still steered against — only the curated shelf is admitted.
    assert "curated" in prompt.lower()


def test_system_prompt_placeholder_untouched_when_setting_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-895: with no real artifacts origin configured, the placeholder ships as-is."""
    monkeypatch.setattr(settings, "artifacts_public_base_url", None)
    prompt = artifact_tools._html_generation_system_prompt()
    assert "https://artifacts.example.com/lib/katex" in prompt


def test_system_prompt_real_origin_substituted_when_setting_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-895: settings.artifacts_public_base_url rebinds the placeholder host."""
    monkeypatch.setattr(settings, "artifacts_public_base_url", "https://artifacts.real-host.test")
    prompt = artifact_tools._html_generation_system_prompt()
    assert "https://artifacts.real-host.test/lib/katex" in prompt
    assert "artifacts.example.com" not in prompt


def test_artifact_design_doc_matches_manifest() -> None:
    """FRE-529 (ADR-0089 A4): docs/skills/artifact-design.md is the source-of-truth.

    The skill doc is the maintainable superset the generation prompt is distilled
    from. This manifest-driven drift guard keeps doc ↔ manifest ↔ prompt in
    lockstep: every non-eval-gated asset must appear as its full absolute,
    version-pinned URL (``origin + /lib/ + path``); the eval-gated paged.js must
    be present but flagged experimental, never first-class; the native-typography
    recipes and the D4 "never bake secrets" rule must be documented.
    """
    from personal_agent.observability.artifact_envelope.spec import (
        DEFAULT_LIB_MANIFEST_PATH,
    )

    repo_root = DEFAULT_LIB_MANIFEST_PATH.resolve().parents[1]
    doc_path = repo_root / "docs" / "skills" / "artifact-design.md"
    doc = doc_path.read_text(encoding="utf-8")

    origin, assets = load_lib_manifest()

    # Self-describing frontmatter (sibling to mermaid-diagrams.md).
    assert "name: artifact-design" in doc

    for asset in assets:
        url = f"{origin}/lib/{asset.path}"
        if asset.eval_gated:
            assert url not in doc, f"eval-gated asset must not be first-class: {url}"
        else:
            assert url in doc, f"missing curated /lib/ recipe URL: {url}"

    # paged.js: present (it is in the curated brief) but flagged experimental.
    assert "paged.js" in doc.lower()
    assert "experimental" in doc.lower()

    # Native typography recipes (no library).
    assert "::first-letter" in doc
    assert "hyphens: auto" in doc
    assert "text-wrap: balance" in doc
    assert "font-feature-settings" in doc
    assert "column-count" in doc
    assert "@page" in doc

    # The standing D4 rule.
    assert "never bake secrets" in doc.lower()


# ---------------------------------------------------------------------------
# Mermaid render helpers — unit tests (FRE-396)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_mermaid_blocks_replaces_with_svg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mermaid blocks are replaced with inline SVG when _render_mermaid_one succeeds."""
    fake_svg = "<svg xmlns='http://www.w3.org/2000/svg'><text>diagram</text></svg>"

    async def _fake_render_one(source: str, *, trace_id: str, session_id: object) -> str:
        return f'<figure class="mermaid-diagram">{fake_svg}</figure>'

    monkeypatch.setattr(artifact_tools, "_render_mermaid_one", _fake_render_one)

    html = '<!DOCTYPE html><html><body><pre class="mermaid">graph TD; A-->B</pre></body></html>'
    result = await artifact_tools._render_mermaid_blocks(html, trace_id="t", session_id=None)

    assert "<svg" in result
    assert '<pre class="mermaid">' not in result
    assert "<script" not in result


@pytest.mark.asyncio
async def test_render_mermaid_blocks_no_blocks_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTML without mermaid blocks is returned unchanged."""
    called = []

    async def _fake_render_one(source: str, *, trace_id: str, session_id: object) -> str:
        called.append(source)
        return source

    monkeypatch.setattr(artifact_tools, "_render_mermaid_one", _fake_render_one)

    html = "<!DOCTYPE html><html><body><p>no diagrams here</p></body></html>"
    result = await artifact_tools._render_mermaid_blocks(html, trace_id="t", session_id=None)

    assert result == html
    assert called == []


@pytest.mark.asyncio
async def test_render_mermaid_one_mmdc_not_found_falls_back() -> None:
    """When mmdc is not installed, _render_mermaid_one returns a <pre> fallback."""
    source = "graph TD; A-->B"
    result = await artifact_tools._render_mermaid_one(
        source, trace_id="t", session_id=None, mmdc_cmd="__nonexistent_mmdc_binary__"
    )

    assert "graph TD" in result
    assert "<script" not in result
    assert "<pre>" in result or "<pre " in result


@pytest.mark.asyncio
async def test_artifact_draft_mermaid_rendered_to_svg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """artifact_draft stores SVG when sub-agent returns mermaid markup (FRE-396 AC1)."""
    html_with_mermaid = (
        "<!DOCTYPE html><html><head><style>:root{--color-primary:#000}</style></head>"
        '<body><pre class="mermaid">graph TD; A-->B</pre></body></html>'
    )
    fake_svg = "<svg xmlns='http://www.w3.org/2000/svg'><text>diagram</text></svg>"

    async def _fake_render_one(source: str, *, trace_id: str, session_id: object) -> str:
        return f'<figure class="mermaid-diagram">{fake_svg}</figure>'

    store, _client = _install_draft_fakes(monkeypatch, html_content=html_with_mermaid)
    monkeypatch.setattr(artifact_tools, "_render_mermaid_one", _fake_render_one)

    await artifact_tools.artifact_draft_executor(
        slug="fsm-diagram",
        title="FSM Diagram",
        summary="Finite State Machine visualization",
        plan="Draw the FSM states and transitions.",
        ctx=_ctx(),
    )

    written = store.put_calls[0]["content"].decode("utf-8")
    assert "<svg" in written
    assert '<pre class="mermaid">' not in written
    assert "<script" not in written


def test_artifact_draft_tool_category_is_artifact_write() -> None:
    """Governance category matches artifact_write for consistent policy."""
    assert artifact_tools.artifact_draft_tool.category == "artifact_write"


# ---------------------------------------------------------------------------
# FRE-506 — per-commit content label (non-load-bearing analytics, ADR-0089 D1/D5)
# ---------------------------------------------------------------------------

_GATE_EVENT = "artifact_gate_decision"

# HTML carrying a <script> block, an inline handler, and a CDN link — exercises
# all three analytics counters on one commit.
_COUNTED_HTML = (
    "<!DOCTYPE html><html><head><style>:root{--c:#000}</style>"
    '<link rel="stylesheet" href="https://cdn.example.com/x.css"></head><body><main>'
    '<h1>Interactive</h1><div onclick="go()">click</div><script>alert(1)</script>'
    "</main></body></html>"
)


def _gate_events(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [kw for ev, kw in events if ev == _GATE_EVENT]


@pytest.mark.asyncio
async def test_gate_decision_committed_on_direct_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct artifact_write of HTML emits gate_decision=committed with all three counts.

    FRE-511: the label is analytics only (ADR-0089 D1) — no gate exists, so the old
    pass/strip/reject/bypass vocabulary and the gate_ran field are retired. The
    served-CSP envelope is the boundary; FRE-512 owns serve-side envelope integrity.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    _install_fakes(monkeypatch, store=_FakeStore())
    _spy_artifact_log(monkeypatch, events)

    await artifact_tools.artifact_write_executor(
        slug="direct",
        content_type="text/html; charset=utf-8",
        content=_COUNTED_HTML,
        ctx=_ctx(),
    )

    gates = _gate_events(events)
    assert len(gates) == 1
    g = gates[0]
    assert g["gate_decision"] == "committed"
    assert g["commit_path"] == "direct_write"
    assert "gate_ran" not in g  # field retired with the gate (FRE-511)
    assert g["script_count"] == 1
    assert g["handler_count"] == 1
    assert g["cdn_count"] == 1
    assert g["artifact_id"]  # committed → has an id


@pytest.mark.asyncio
async def test_gate_decision_committed_on_draft_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draft commit emits exactly one committed event labelled commit_path=draft."""
    events: list[tuple[str, dict[str, Any]]] = []
    _install_draft_fakes(monkeypatch, html_content=_SCRIPT_HTML)
    _spy_artifact_log(monkeypatch, events)

    await artifact_tools.artifact_draft_executor(
        slug="draft", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    gates = _gate_events(events)
    assert len(gates) == 1  # single emit despite the draft→write chain
    g = gates[0]
    assert g["gate_decision"] == "committed"
    assert g["commit_path"] == "draft"
    assert g["script_count"] == 1
    assert g["handler_count"] == 0
    assert g["cdn_count"] == 0


@pytest.mark.asyncio
async def test_gate_decision_not_applicable_for_non_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-HTML direct write logs gate_decision=not_applicable with zero counts."""
    events: list[tuple[str, dict[str, Any]]] = []
    _install_fakes(monkeypatch, store=_FakeStore())
    _spy_artifact_log(monkeypatch, events)

    await artifact_tools.artifact_write_executor(
        slug="data",
        content_type="application/json",
        content='{"a": 1}',
        ctx=_ctx(),
    )

    gates = _gate_events(events)
    assert len(gates) == 1
    g = gates[0]
    assert g["gate_decision"] == "not_applicable"
    assert g["script_count"] == 0
    assert g["handler_count"] == 0
    assert g["cdn_count"] == 0


# ---------------------------------------------------------------------------
# FRE-526 — external <script src> reach meter (non-load-bearing, ADR-0089 A1)
# ---------------------------------------------------------------------------


def _html_with(body: str) -> str:
    """Wrap a body fragment in a minimal valid HTML document."""
    return (
        "<!DOCTYPE html><html><head><style>:root{--c:#000}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def test_classify_script_reaches_blocks_external_cdn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absolute CDN <script src> is an external, host-blocked reach."""
    monkeypatch.setattr(
        artifact_tools.settings,
        "artifacts_public_base_url",
        "https://artifacts.test",
        raising=False,
    )
    html = _html_with('<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>')
    assert artifact_tools._classify_script_reaches(html) == (1, 0, 1)


def test_classify_script_reaches_allows_lib_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A <script src> on the artifacts host's /lib/ shelf is host-allowed."""
    monkeypatch.setattr(
        artifact_tools.settings,
        "artifacts_public_base_url",
        "https://artifacts.test",
        raising=False,
    )
    html = _html_with('<script src="https://artifacts.test/lib/katex@0.16.js"></script>')
    assert artifact_tools._classify_script_reaches(html) == (1, 1, 0)


def test_classify_script_reaches_mixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One /lib/ reach + one CDN reach → (2, 1, 1)."""
    monkeypatch.setattr(
        artifact_tools.settings,
        "artifacts_public_base_url",
        "https://artifacts.test",
        raising=False,
    )
    html = _html_with(
        '<script src="https://artifacts.test/lib/chart@4.js"></script>'
        '<script src="https://cdn.jsdelivr.net/npm/three"></script>'
    )
    assert artifact_tools._classify_script_reaches(html) == (2, 1, 1)


def test_classify_script_reaches_protocol_relative_cdn_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A protocol-relative //cdn src resolves to a real CDN fetch — counted blocked."""
    monkeypatch.setattr(
        artifact_tools.settings,
        "artifacts_public_base_url",
        "https://artifacts.test",
        raising=False,
    )
    html = _html_with('<script src="//cdn.jsdelivr.net/x.js"></script>')
    assert artifact_tools._classify_script_reaches(html) == (1, 0, 1)


def test_classify_script_reaches_ignores_inline_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inline <script> block (no src) is not an external reach."""
    monkeypatch.setattr(
        artifact_tools.settings,
        "artifacts_public_base_url",
        "https://artifacts.test",
        raising=False,
    )
    html = _html_with("<script>alert(1)</script>")
    assert artifact_tools._classify_script_reaches(html) == (0, 0, 0)


def test_classify_script_reaches_ignores_empty_src(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty src="" is not an external reach."""
    monkeypatch.setattr(
        artifact_tools.settings,
        "artifacts_public_base_url",
        "https://artifacts.test",
        raising=False,
    )
    html = _html_with('<script src=""></script>')
    assert artifact_tools._classify_script_reaches(html) == (0, 0, 0)


def test_classify_script_reaches_no_script_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document with no script tags yields all zeros."""
    monkeypatch.setattr(
        artifact_tools.settings,
        "artifacts_public_base_url",
        "https://artifacts.test",
        raising=False,
    )
    assert artifact_tools._classify_script_reaches(_html_with("<p>just prose</p>")) == (0, 0, 0)


def test_classify_script_reaches_two_tags_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two <script src> tags on a single line are both counted (findall iterates all)."""
    monkeypatch.setattr(
        artifact_tools.settings,
        "artifacts_public_base_url",
        "https://artifacts.test",
        raising=False,
    )
    one_line = (
        '<script src="https://artifacts.test/lib/a.js"></script>'
        '<script src="https://evil.example/b.js"></script>'
    )
    assert artifact_tools._classify_script_reaches(_html_with(one_line)) == (2, 1, 1)


def test_classify_script_reaches_no_base_url_all_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no configured artifacts host, nothing can be proven allowed."""
    monkeypatch.setattr(artifact_tools.settings, "artifacts_public_base_url", None, raising=False)
    html = _html_with('<script src="https://artifacts.test/lib/x.js"></script>')
    assert artifact_tools._classify_script_reaches(html) == (1, 0, 1)


@pytest.mark.asyncio
async def test_gate_decision_emits_script_reach_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct HTML write with a CDN <script src> emits the three reach fields."""
    events: list[tuple[str, dict[str, Any]]] = []
    _install_fakes(monkeypatch, store=_FakeStore())  # sets base_url=https://artifacts.test
    _spy_artifact_log(monkeypatch, events)

    await artifact_tools.artifact_write_executor(
        slug="reach",
        content_type="text/html; charset=utf-8",
        content=_html_with('<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>'),
        ctx=_ctx(),
    )

    gates = _gate_events(events)
    assert len(gates) == 1
    g = gates[0]
    assert g["external_script_count"] == 1
    assert g["script_reach_blocked"] == 1
    assert g["script_reach_allowed"] == 0


@pytest.mark.asyncio
async def test_gate_decision_allowed_lib_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A /lib/ <script src> on the configured host counts as host-allowed."""
    events: list[tuple[str, dict[str, Any]]] = []
    _install_fakes(monkeypatch, store=_FakeStore())  # base_url=https://artifacts.test
    _spy_artifact_log(monkeypatch, events)

    await artifact_tools.artifact_write_executor(
        slug="lib-reach",
        content_type="text/html; charset=utf-8",
        content=_html_with('<script src="https://artifacts.test/lib/katex@0.16.js"></script>'),
        ctx=_ctx(),
    )

    g = _gate_events(events)[0]
    assert g["external_script_count"] == 1
    assert g["script_reach_allowed"] == 1
    assert g["script_reach_blocked"] == 0


@pytest.mark.asyncio
async def test_gate_decision_script_reach_fields_zero_for_non_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-HTML write reports zero for all three reach fields."""
    events: list[tuple[str, dict[str, Any]]] = []
    _install_fakes(monkeypatch, store=_FakeStore())
    _spy_artifact_log(monkeypatch, events)

    await artifact_tools.artifact_write_executor(
        slug="data",
        content_type="application/json",
        content='{"a": 1}',
        ctx=_ctx(),
    )

    g = _gate_events(events)[0]
    assert g["external_script_count"] == 0
    assert g["script_reach_allowed"] == 0
    assert g["script_reach_blocked"] == 0


# ---------------------------------------------------------------------------
# FRE-512 — served-envelope probe hook (ADR-0089 D5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_write_triggers_envelope_probe_with_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every direct-write commit probes the served URL with full ADR-0074 identity."""
    _install_fakes(monkeypatch, store=_FakeStore())
    ctx = _ctx(session_id=uuid4())

    out = await artifact_tools.artifact_write_executor(
        slug="probe-me",
        content_type="text/html; charset=utf-8",
        content="<h1>x</h1>",
        ctx=ctx,
    )

    probe = artifact_tools.probe_served_envelope
    probe.assert_awaited_once()
    kwargs = probe.await_args.kwargs
    assert kwargs["public_url"] == out["public_url"]
    assert kwargs["artifact_id"] == out["artifact_id"]
    assert kwargs["slug"] == "probe-me"
    assert kwargs["content_type"] == "text/html; charset=utf-8"
    assert kwargs["trace_id"] == "trace-test"
    assert kwargs["session_id"] == str(ctx.session_id)
    assert kwargs["user_id"] == str(ctx.user_id)


@pytest.mark.asyncio
async def test_artifact_draft_triggers_envelope_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The draft path commits through artifact_write_executor — one probe fires."""
    _install_draft_fakes(monkeypatch)

    await artifact_tools.artifact_draft_executor(
        slug="draft-probe",
        title="t",
        summary="s",
        plan="Build a table.",
        ctx=_ctx(),
    )

    artifact_tools.probe_served_envelope.assert_awaited_once()


@pytest.mark.asyncio
async def test_envelope_probe_exception_does_not_fail_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe is never load-bearing: even a buggy probe cannot fail the commit."""
    _install_fakes(monkeypatch, store=_FakeStore())
    monkeypatch.setattr(
        artifact_tools,
        "probe_served_envelope",
        AsyncMock(side_effect=RuntimeError("probe bug")),
        raising=False,
    )

    out = await artifact_tools.artifact_write_executor(
        slug="resilient",
        content_type="text/html; charset=utf-8",
        content="<h1>x</h1>",
        ctx=_ctx(),
    )

    assert "artifact_id" in out  # commit succeeded despite the probe raising


@pytest.mark.asyncio
async def test_envelope_probe_skipped_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fakes(monkeypatch, store=_FakeStore())
    monkeypatch.setattr(
        artifact_tools.settings, "artifact_envelope_probe_enabled", False, raising=False
    )

    await artifact_tools.artifact_write_executor(
        slug="no-probe",
        content_type="text/html; charset=utf-8",
        content="<h1>x</h1>",
        ctx=_ctx(),
    )

    artifact_tools.probe_served_envelope.assert_not_awaited()


@pytest.mark.asyncio
async def test_envelope_probe_skipped_without_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No public base URL (local dev) → nothing to probe."""
    _install_fakes(monkeypatch, store=_FakeStore())
    monkeypatch.setattr(artifact_tools.settings, "artifacts_public_base_url", None, raising=False)

    await artifact_tools.artifact_write_executor(
        slug="local-only",
        content_type="text/html; charset=utf-8",
        content="<h1>x</h1>",
        ctx=_ctx(),
    )

    artifact_tools.probe_served_envelope.assert_not_awaited()
