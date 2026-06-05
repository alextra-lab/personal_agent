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
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from personal_agent.tools import artifact_tools
from personal_agent.tools.executor import ToolExecutionError


def _ctx(user_id: UUID | None = None, session_id: UUID | None = None) -> Any:
    return SimpleNamespace(
        user_id=user_id or uuid4(),
        session_id=session_id,
        trace_id="trace-test",
    )


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
    """Mock LLM client for sub-agent inference in artifact_draft tests.

    ``html_sequence`` (FRE-496) returns a different body per ``respond`` call —
    e.g. a sandbox-violating draft on call 1 and a clean draft on call 2 to
    exercise the bounded CSS-only retry. The last element is reused once the
    sequence is exhausted. ``raises`` (per-call exception) lets a later attempt
    fail so the fall-back-to-prior path can be tested.
    """

    def __init__(
        self,
        html_content: str = _VALID_HTML,
        *,
        html_sequence: list[str] | None = None,
        raises: list[BaseException | None] | None = None,
    ) -> None:
        self.html_content = html_content
        self.html_sequence = html_sequence
        self.raises = raises
        self.respond_calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> dict[str, Any]:
        idx = len(self.respond_calls)
        self.respond_calls.append(kwargs)
        if self.raises is not None and idx < len(self.raises) and self.raises[idx] is not None:
            raise self.raises[idx]
        if self.html_sequence is not None:
            body = self.html_sequence[min(idx, len(self.html_sequence) - 1)]
        else:
            body = self.html_content
        return {
            "role": "assistant",
            "content": body,
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"prompt_tokens": 100, "completion_tokens": 500},
            "response_id": None,
            "raw": {},
        }


def _install_draft_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    html_content: str = _VALID_HTML,
    html_sequence: list[str] | None = None,
    raises: list[BaseException | None] | None = None,
) -> tuple[_FakeStore, _FakeSubAgentClient]:
    """Install fakes for artifact_draft tests: R2/DB/embedding + sub-agent client."""
    store = _FakeStore()
    _install_fakes(monkeypatch, store=store)
    client = _FakeSubAgentClient(
        html_content=html_content, html_sequence=html_sequence, raises=raises
    )
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
    from personal_agent.config.model_loader import load_model_config

    primary = load_model_config().models.get("primary")
    assert primary is not None and primary.default_timeout
    assert artifact_tools._draft_timeout_s() == float(primary.default_timeout)


@pytest.mark.asyncio
async def test_artifact_draft_calls_respond_with_correct_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """respond() receives the configured artifact-draft max_tokens (FRE-478)."""
    from personal_agent.config import settings

    _store, client = _install_draft_fakes(monkeypatch)

    await artifact_tools.artifact_draft_executor(
        slug="tokens-test",
        title="T",
        summary="S",
        plan="A plan.",
        ctx=_ctx(),
    )

    call = client.respond_calls[0]
    assert call["max_tokens"] == artifact_tools._draft_max_tokens()
    assert call["max_tokens"] == settings.artifact_draft_max_tokens


def test_draft_max_tokens_reads_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """_draft_max_tokens() resolves from settings.artifact_draft_max_tokens (FRE-478)."""
    monkeypatch.setattr(artifact_tools.settings, "artifact_draft_max_tokens", 12345)
    assert artifact_tools._draft_max_tokens() == 12345


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
    # the model can retry. Only sandbox violations (script/handlers) are terminal.
    with pytest.raises(ToolExecutionError, match="DOCTYPE") as exc_info:
        await artifact_tools.artifact_draft_executor(
            slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
        )
    assert not isinstance(exc_info.value, TerminalToolError)


# ---------------------------------------------------------------------------
# artifact_draft — FRE-496 sandbox graceful degradation (bounded retry + strip)
# ---------------------------------------------------------------------------

# Body must exceed _MIN_HTML_LENGTH after stripping so validation still passes.
_SCRIPT_HTML = (
    "<!DOCTYPE html><html><head><style>:root{--c:#000}</style></head><body>"
    "<main><h1>Interactive</h1><p>Some real content that survives stripping.</p>"
    "<script>alert(1)</script></main></body></html>"
)
_HANDLER_HTML = (
    "<!DOCTYPE html><html><head><style>:root{--c:#000}</style></head><body>"
    '<main><h1>Interactive</h1><div onclick="evil()">click me for the demo content</div>'
    "</main></body></html>"
)


@pytest.mark.asyncio
async def test_artifact_draft_strips_script_tags_and_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-496: a persistently script-laden draft is stripped + delivered, not hard-failed.

    Both attempts emit <script>, so the bounded retry fires and still fails; the final
    strip-and-deliver path must commit a script-free artifact with a sanitization note.
    """
    from personal_agent.tools.executor import TerminalToolError

    store, client = _install_draft_fakes(monkeypatch, html_content=_SCRIPT_HTML)

    out = await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert not isinstance(out, TerminalToolError)
    assert "artifact_id" in out
    assert out["sanitization_notes"]  # non-empty
    assert out["sub_agent_attempts"] == 2  # retry fired before giving up
    stored = store.put_calls[0]["content"]
    assert b"<script" not in stored.lower()
    assert b"alert(1)" not in stored


@pytest.mark.asyncio
async def test_artifact_draft_strips_event_handlers_and_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-496: inline event handlers are stripped + delivered, not hard-failed."""
    store, _client = _install_draft_fakes(monkeypatch, html_content=_HANDLER_HTML)

    out = await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert "artifact_id" in out
    assert out["sanitization_notes"]
    stored = store.put_calls[0]["content"].lower()
    assert b"onclick=" not in stored


@pytest.mark.asyncio
async def test_artifact_draft_sandbox_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-496: a clean CSS-only retry is delivered as-is — no strip, no banner note."""
    store, client = _install_draft_fakes(monkeypatch, html_sequence=[_SCRIPT_HTML, _VALID_HTML])

    out = await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert len(client.respond_calls) == 2
    assert out["sub_agent_attempts"] == 2
    assert out["sanitization_notes"] == []
    stored = store.put_calls[0]["content"]
    assert b"<script" not in stored.lower()
    assert b"alert(1)" not in stored


@pytest.mark.asyncio
async def test_artifact_draft_clean_first_pass_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-496: a clean first draft is delivered with exactly one sub-agent call."""
    events: list[tuple[str, dict[str, Any]]] = []
    _store, client = _install_draft_fakes(monkeypatch)
    _spy_artifact_log(monkeypatch, events)

    out = await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert len(client.respond_calls) == 1
    assert out["sub_agent_attempts"] == 1
    assert out["sanitization_notes"] == []
    assert not [e for e in events if e[0] == "artifact_draft_sandbox_retry"]


@pytest.mark.asyncio
async def test_artifact_draft_retry_timeout_falls_back_to_prior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-496: a retry that times out falls back to stripping the attempt-1 draft."""
    events: list[tuple[str, dict[str, Any]]] = []
    store, _client = _install_draft_fakes(
        monkeypatch,
        html_sequence=[_SCRIPT_HTML, _VALID_HTML],
        raises=[None, asyncio.TimeoutError()],
    )
    _spy_artifact_log(monkeypatch, events)

    out = await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert "artifact_id" in out
    assert out["sanitization_notes"]  # attempt-1 draft was stripped
    assert b"<script" not in store.put_calls[0]["content"].lower()
    assert [e for e in events if e[0] == "artifact_draft_retry_failed_using_prior"]


@pytest.mark.asyncio
async def test_artifact_draft_sandbox_retry_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-496: the bounded-retry trigger emits artifact_draft_sandbox_retry with notes."""
    events: list[tuple[str, dict[str, Any]]] = []
    _install_draft_fakes(monkeypatch, html_content=_SCRIPT_HTML)
    _spy_artifact_log(monkeypatch, events)

    await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    retries = [e for e in events if e[0] == "artifact_draft_sandbox_retry"]
    assert len(retries) == 1
    assert retries[0][1]["violation_notes"]


@pytest.mark.asyncio
async def test_artifact_draft_sanitization_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-496: the strip-and-deliver fallback logs artifact_draft_sanitized_sandbox_violations."""
    events: list[tuple[str, dict[str, Any]]] = []
    _install_draft_fakes(monkeypatch, html_content=_SCRIPT_HTML)
    _spy_artifact_log(monkeypatch, events)

    await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    sanitized = [e for e in events if e[0] == "artifact_draft_sanitized_sandbox_violations"]
    assert len(sanitized) == 1
    assert sanitized[0][1]["notes"]


@pytest.mark.asyncio
async def test_artifact_draft_strips_cdn_link_and_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-496: an external CDN <link> is stripped and the artifact still ships."""
    cdn_html = (
        "<!DOCTYPE html><html><head><style>:root{--c:#000}</style>"
        '<link rel="stylesheet" href="https://cdn.tailwindcss.com/x.css"></head>'
        "<body><main><h1>Content here that survives stripping fine.</h1></main></body></html>"
    )
    store, _client = _install_draft_fakes(monkeypatch, html_content=cdn_html)

    out = await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert out["sanitization_notes"]
    assert b"cdn.tailwindcss.com" not in store.put_calls[0]["content"]


@pytest.mark.asyncio
async def test_artifact_draft_mermaid_plus_script_both_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FRE-496: a draft mixing a mermaid block and a <script> ships with both handled."""
    mixed = (
        "<!DOCTYPE html><html><head><style>:root{--c:#000}</style></head><body><main>"
        '<pre class="mermaid">graph LR; A--&gt;B;</pre>'
        "<script>alert(1)</script><p>Body content that survives.</p>"
        "</main></body></html>"
    )
    store, _client = _install_draft_fakes(monkeypatch, html_content=mixed)
    # Force mermaid render to fall back (no mmdc binary) — still script-free.
    monkeypatch.setattr(artifact_tools, "_MERMAID_RENDER_TIMEOUT_S", 0.01)

    out = await artifact_tools.artifact_draft_executor(
        slug="x", title="T", summary="S", plan="A plan.", ctx=_ctx()
    )

    assert "artifact_id" in out
    assert b"<script" not in store.put_calls[0]["content"].lower()


def test_sanitize_sandbox_violations_noop_on_clean_html() -> None:
    """FRE-496: clean HTML passes through _sanitize_sandbox_violations unchanged."""
    out, notes = artifact_tools._sanitize_sandbox_violations(_VALID_HTML)
    assert out == _VALID_HTML
    assert notes == []


def test_sanitize_handles_regex_variants() -> None:
    """FRE-496: mixed-case, self-closing, orphan-close, quoted/unquoted handlers, unquoted CDN."""
    raw = (
        "<!DOCTYPE html><html><body>"
        "<SCRIPT>var x=1;</SCRIPT>"
        '<script src="https://cdn.example.com/a.js" />'
        "</script>"  # orphan closer
        "<div onclick='a()'>x</div>"
        "<button onmouseover=b()>y</button>"
        "<link rel=stylesheet href=https://cdn.example.com/a.css>"
        "</body></html>"
    )
    out, notes = artifact_tools._sanitize_sandbox_violations(raw)
    lowered = out.lower()
    assert "<script" not in lowered
    assert "</script" not in lowered
    assert "onclick" not in lowered
    assert "onmouseover" not in lowered
    assert "cdn.example.com/a.css" not in out
    assert len(notes) >= 2


def test_sanitize_strips_attribute_and_whitespace_close_tags() -> None:
    """Close tags with whitespace/attributes are valid HTML end tags and must strip.

    FRE-496 / CodeQL py/bad-tag-filter: HTML treats a close tag carrying trailing
    whitespace or attributes (a space, or a tab/newline, before the closing angle
    bracket) as a valid end tag, so the close pattern must tolerate it — otherwise
    the block (with its JS body) slips the strip.
    """
    raw = (
        "<!DOCTYPE html><html><body>"
        "<script>evil()</script bar>"  # attribute before > on the close tag
        "<script>more()</script\t\n>"  # whitespace before >
        "<p>A CSS-only interactive section, no JavaScript required.</p>"
        "</body></html>"
    )
    out, notes = artifact_tools._sanitize_sandbox_violations(raw)
    lowered = out.lower()
    assert "<script" not in lowered
    assert "</script" not in lowered
    assert "evil()" not in out  # JS body removed with the block, not left as text
    assert "more()" not in out
    assert len(notes) >= 1
    # Defense-in-depth validator must not fire on the sanitized output.
    artifact_tools._validate_html_output(out)


def test_injected_banner_passes_validation() -> None:
    """FRE-496: the injected banner is itself sandbox-clean (no <script>/onX= tokens)."""
    out, notes = artifact_tools._sanitize_sandbox_violations(_SCRIPT_HTML)
    banner_html = artifact_tools._inject_sanitization_banner(out, notes)
    # Must not raise — banner text contains no literal <script or onX= token.
    artifact_tools._validate_html_output(banner_html)
    assert "artifact-sanitization-note" in banner_html


def test_sanitize_banner_bodyless_html() -> None:
    """FRE-496: banner injects into bodyless HTML without breaking doctype validation."""
    bodyless = (
        "<!DOCTYPE html><html><head><style>:root{--c:#000}</style>"
        "<p>Content without a body element but long enough.</p></head></html>"
    )
    out, _notes = artifact_tools._sanitize_sandbox_violations(bodyless)
    banner_html = artifact_tools._inject_sanitization_banner(out, ["Removed embedded scripts."])
    artifact_tools._validate_html_output(banner_html)  # must not raise
    assert "artifact-sanitization-note" in banner_html


def test_validate_html_output_still_terminal_on_script() -> None:
    """FRE-496: the safety net still rejects raw <script> (defense-in-depth intact)."""
    from personal_agent.tools.executor import TerminalToolError

    raw = "<!DOCTYPE html><html><body><script>alert(1)</script>padding padding</body></html>"
    with pytest.raises(TerminalToolError, match="script"):
        artifact_tools._validate_html_output(raw)


def test_html_generation_prompt_redirects_to_css_only() -> None:
    """FRE-496: the generation prompt sanctions CSS-only interactivity + states the consequence."""
    prompt = artifact_tools._HTML_GENERATION_SYSTEM_PROMPT
    assert "CSS only" in prompt or "CSS-only" in prompt
    assert ":target" in prompt
    assert "checkbox-hack" in prompt
    assert "REJECTED" in prompt


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


def test_system_prompt_prohibits_scripts() -> None:
    """The HTML generation system prompt must prohibit script tags (FRE-496 reframe)."""
    prompt = artifact_tools._HTML_GENERATION_SYSTEM_PROMPT
    assert "<script>" in prompt  # the prohibition still names the tag explicitly
    assert "REJECTED" in prompt  # ...and states the consequence (FRE-496)
    assert "event handler" in prompt.lower() or "onclick" in prompt.lower()


def test_system_prompt_instructs_mermaid_markup() -> None:
    """The system prompt must direct the model to use mermaid markup, not <script>."""
    prompt = artifact_tools._HTML_GENERATION_SYSTEM_PROMPT
    assert "mermaid" in prompt.lower()
    assert '<pre class="mermaid">' in prompt or "pre class" in prompt.lower()


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
