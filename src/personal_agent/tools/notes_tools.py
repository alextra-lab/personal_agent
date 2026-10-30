"""Notes tools — the first consumer of the R2 artifact substrate (FRE-227).

``notes_write`` parks markdown text in R2 and records an `artifacts` row
(type='note') including a pgvector embedding so future sessions can
recover the same content via ``notes_search``. Together these tools give
the agent durable, NLP-searchable scratch space across sessions — the
original FRE-227 use case ("leaving notes or thought trails").

Architectural anchors
---------------------
* ADR-0069 — R2-backed artifact substrate. Layout / identity / SDK choice.
* ADR-0064 — Cloudflare Access user identity. ``user_id`` is the FK.
* Embedding pipeline reuses ``memory.embeddings.generate_embedding`` so
  these notes share the same vector space as proactive memory recall.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text

from personal_agent.config import settings
from personal_agent.memory.embeddings import generate_embedding
from personal_agent.service.database import AsyncSessionLocal
from personal_agent.storage import (
    ArtifactKeyError,
    build_r2_key,
    get_artifact_store,
)
from personal_agent.telemetry import get_logger
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Failure codes (ADR-0156 D6.1)
# ---------------------------------------------------------------------------

NotesSharedCode = Literal["missing_identity"]
"""Failures raised by helpers that both notes tools call."""

NotesWriteCode = Literal[
    NotesSharedCode,
    "not_configured",
    "invalid_mode",
    "invalid_argument",
    "invalid_slug",
    "prior_not_utf8",
    "too_large",
]
NotesSearchCode = Literal[NotesSharedCode, "invalid_argument"]


class NotesError(ToolError[NotesSharedCode]):
    """A failure in a helper shared by the notes tools."""


class NotesWriteError(ToolError[NotesWriteCode]):
    """A ``notes_write`` failure."""


class NotesSearchError(ToolError[NotesSearchCode]):
    """A ``notes_search`` failure."""


_NOTE_CONTENT_TYPE = "text/markdown; charset=utf-8"
_NOTE_EXT = "md"
_APPEND_SEPARATOR = "\n\n"
_MAX_NOTE_BYTES = 256 * 1024  # 256 KiB ceiling per note (revision-aware).
_MAX_SEARCH_K = 25


def _pgvector_literal(values: list[float]) -> str:
    """Render a list of floats as a pgvector text literal: ``"[v1,v2,...]"``.

    asyncpg does not ship a built-in codec for the pgvector ``vector`` type;
    passing a Python ``list[float]`` directly to a ``CAST($n AS vector)``
    placeholder raises ``DataError: expected str, got list``. The pgvector
    extension accepts a text representation — a JSON-like bracketed list of
    decimal floats — and the ``CAST`` in the SQL converts it to the binary
    column type. ``repr(float)`` preserves full precision (Python's default
    ``str(float)`` is also lossless for round-tripping but ``repr`` is
    explicit), and comma-separated with no whitespace matches pgvector's
    parser exactly.
    """
    return "[" + ",".join(repr(v) for v in values) + "]"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


notes_write_tool = ToolDefinition(
    name="notes_write",
    description=(
        "Save a markdown note under a short kebab-case slug so it survives across sessions; "
        "it is embedded for search with notes_search. "
        "Use it for thought trails, plans and ideas you want to recover in a later session. "
        "Do not use it for documents meant for the user to view (use artifact_draft or "
        "artifact_write). "
        "mode='append' (default) adds to the latest note with the same slug, "
        "mode='overwrite' starts a fresh body; a note is capped at 256 KiB. "
        'Examples: notes_write(slug="project-x-plan", content="## Next steps\\n- ..."), '
        'notes_write(slug="project-x-plan", content="# Plan v2\\n...", mode="overwrite", '
        'tags=["project-x"]).'
    ),
    category="artifact_write",
    parameters=[
        ToolParameter(
            name="slug",
            type="string",
            description=(
                "Short kebab-case handle (alnum start, then alnum/./_/-, "
                "max 64 chars). Acts as the human-readable id for "
                "append-mode rebuilds."
            ),
            required=True,
        ),
        ToolParameter(
            name="content",
            type="string",
            description="Markdown text to store. Up to 256 KiB.",
            required=True,
        ),
        ToolParameter(
            name="title",
            type="string",
            description="Optional human-readable title for inline cards.",
            required=False,
            default=None,
        ),
        ToolParameter(
            name="mode",
            type="string",
            description=(
                "'append' (default) concatenates to the most recent revision "
                "with the same slug; 'overwrite' starts a fresh body."
            ),
            required=False,
            default="append",
        ),
        ToolParameter(
            name="tags",
            type="array",
            description="Optional list of free-form tags for filtering in notes_search.",
            required=False,
            default=None,
            json_schema={"type": "array", "items": {"type": "string"}},
        ),
    ],
    risk_level="medium",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=20,
    error_codes=error_codes(NotesWriteError),
)


notes_search_tool = ToolDefinition(
    name="notes_search",
    description=(
        "Search the notes saved with notes_write by meaning, and return their metadata "
        "(slug, title, summary, tags, similarity, public_url), most similar first. "
        "Use it to recover thought trails, plans or context saved in earlier sessions. "
        "Do not use it for artifacts (use artifact_list) or for what the user said in past "
        "conversations (use recall_personal_history). "
        "It returns at most k results (1-25, default 5) and no note bodies. "
        'Examples: notes_search(query="project x plan"), '
        'notes_search(query="open questions on recall", k=10, tags=["recall"]).'
    ),
    category="memory_read",
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="Natural-language query embedded with Qwen3-Embedding-0.6B.",
            required=True,
        ),
        ToolParameter(
            name="k",
            type="number",
            description="Maximum results to return (1..25). Default 5.",
            required=False,
            default=None,
        ),
        ToolParameter(
            name="tags",
            type="array",
            description="Optional tag filter. Notes must match at least one tag.",
            required=False,
            default=None,
            json_schema={"type": "array", "items": {"type": "string"}},
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=10,
    error_codes=error_codes(NotesSearchError),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_user_id(ctx: Any) -> UUID:
    """Pull ``user_id`` off the orchestrator ExecutionContext.

    Refuses to fall back to the deployment owner — this is intentional:
    every gateway path populates ``ctx.user_id`` (ADR-0064 has been live
    since FRE-213). A missing id is a programming bug.

    Args:
        ctx: The orchestrator context, or ``None``.

    Returns:
        The caller's user id.

    Raises:
        NotesError: When the context carries no user id.
    """
    user_id = getattr(ctx, "user_id", None) if ctx else None
    if user_id is None:
        raise NotesError(
            "missing_identity",
            "notes tools require ctx.user_id (set by the request gateway).",
            retryable=False,
            next_action="No notes call can work without a user identity. Tell the user.",
        )
    return user_id if isinstance(user_id, UUID) else UUID(str(user_id))


def _resolve_session_id(ctx: Any) -> UUID | None:
    sid = getattr(ctx, "session_id", None) if ctx else None
    if sid is None:
        return None
    return sid if isinstance(sid, UUID) else UUID(str(sid))


def _public_url(artifact_id: UUID) -> str | None:
    base = settings.artifacts_public_base_url
    if not base:
        return None
    return f"{base.rstrip('/')}/{artifact_id}"


async def _fetch_latest_note_for_slug(user_id: UUID, slug: str) -> tuple[UUID, str] | None:
    """Return ``(artifact_id, r2_key)`` of the most recent note for ``(user, slug)``."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT id, r2_key
                FROM artifacts
                WHERE user_id = :user_id
                  AND type = 'note'
                  AND slug = :slug
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"user_id": user_id, "slug": slug},
        )
        row = result.first()
    if row is None:
        return None
    return row.id, row.r2_key


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------


async def notes_write_executor(
    slug: str,
    content: str,
    title: str | None = None,
    mode: str = "append",
    tags: list[str] | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Write a note revision and return the new artifact's public URL.

    Args:
        slug: Stable human-readable handle within the owner's scope.
        content: Markdown body to persist.
        title: Optional display title.
        mode: ``"append"`` concatenates to the most recent revision sharing
            the same slug; ``"overwrite"`` starts fresh.
        tags: Optional list of free-form tags.
        ctx: Orchestrator ``ExecutionContext`` with ``user_id`` / ``session_id`` /
            ``trace_id``.

    Returns:
        ``{"artifact_id", "public_url", "slug", "mode_applied", "size_bytes",
        "revision_of"}``.

    Raises:
        NotesWriteError: On an unconfigured substrate, invalid mode, empty slug or content,
            invalid slug, a prior revision that is not UTF-8, or content overflow.
        NotesError: On missing identity.
    """
    store = get_artifact_store()
    if store is None:
        raise NotesWriteError(
            "not_configured",
            "notes substrate is not configured on this deployment.",
            retryable=False,
            next_action="Notes cannot be saved here. Keep the content in your answer instead.",
        )

    if mode not in ("append", "overwrite"):
        raise NotesWriteError(
            "invalid_mode",
            f"mode must be 'append' or 'overwrite', got {mode!r}.",
            retryable=False,
            next_action="Call notes_write again with mode 'append' or 'overwrite'.",
        )

    if not slug or not slug.strip():
        raise NotesWriteError(
            "invalid_argument",
            "slug is required and cannot be empty.",
            retryable=False,
            next_action='Call notes_write again with a kebab-case slug, e.g. slug="project-plan".',
        )
    if not content:
        raise NotesWriteError(
            "invalid_argument",
            "content is required.",
            retryable=False,
            next_action="Call notes_write again with the markdown content to save.",
        )

    user_id = _resolve_user_id(ctx)
    session_id = _resolve_session_id(ctx)
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    # Validate slug & generate the R2 key *before* touching the DB — bad
    # slugs (traversal, slashes, control chars) should bounce without any
    # SELECT, INSERT, embedding, or R2 call.
    artifact_id = uuid4()
    try:
        r2_key = build_r2_key(
            type="note",
            user_id=user_id,
            session_id=session_id,
            artifact_id=artifact_id,
            slug=slug,
            ext=_NOTE_EXT,
        )
    except ArtifactKeyError as exc:
        raise NotesWriteError(
            "invalid_slug",
            str(exc),
            retryable=False,
            next_action=(
                "Call notes_write again with a slug that starts with a letter or digit and "
                "uses only letters, digits, '.', '_' or '-' (max 64 chars)."
            ),
        ) from exc

    # Resolve the prior revision (may be None) only after slug validation.
    revision_of: UUID | None = None
    final_body = content
    if mode == "append":
        prior = await _fetch_latest_note_for_slug(user_id, slug)
        if prior is not None:
            revision_of = prior[0]
            try:
                existing = (await store.get(prior[1], trace_id=trace_id)).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise NotesWriteError(
                    "prior_not_utf8",
                    f"prior note {prior[0]} is not valid utf-8; refusing to append.",
                    retryable=False,
                    next_action="Call notes_write again with mode='overwrite', or a new slug.",
                ) from exc
            final_body = existing + _APPEND_SEPARATOR + content

    final_bytes = final_body.encode("utf-8")
    size_bytes = len(final_bytes)
    if size_bytes > _MAX_NOTE_BYTES:
        raise NotesWriteError(
            "too_large",
            f"note exceeds {_MAX_NOTE_BYTES} bytes (have {size_bytes}); split it.",
            retryable=False,
            next_action=(
                "Split the content across several slugs, or call notes_write with "
                "mode='overwrite' and a shorter body."
            ),
        )

    embedding = await generate_embedding(
        final_body,
        mode="document",
        trace_id=trace_id,
        session_id=str(session_id) if session_id else None,
    )

    log.info(
        "notes_write_uploading",
        trace_id=trace_id,
        user_id=str(user_id),
        slug=slug,
        mode=mode,
        size_bytes=size_bytes,
        revision_of=str(revision_of) if revision_of else None,
    )

    await store.put(
        r2_key=r2_key,
        content=final_bytes,
        content_type=_NOTE_CONTENT_TYPE,
        trace_id=trace_id,
    )

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, user_id, session_id, type, slug, title, summary,
                    content_type, size_bytes, r2_key, tags, embedding,
                    created_by, created_at
                ) VALUES (
                    :id, :user_id, :session_id, 'note', :slug, :title, NULL,
                    :content_type, :size_bytes, :r2_key,
                    CAST(:tags AS text[]), CAST(:embedding AS vector),
                    'agent', NOW()
                )
                """
            ),
            {
                "id": artifact_id,
                "user_id": user_id,
                "session_id": session_id,
                "slug": slug,
                "title": title,
                "content_type": _NOTE_CONTENT_TYPE,
                "size_bytes": size_bytes,
                "r2_key": r2_key,
                "tags": list(tags) if tags else [],
                "embedding": _pgvector_literal(embedding),
            },
        )
        await session.commit()

    log.info(
        "notes_write_committed",
        trace_id=trace_id,
        artifact_id=str(artifact_id),
        slug=slug,
        size_bytes=size_bytes,
    )

    return {
        "artifact_id": str(artifact_id),
        "public_url": _public_url(artifact_id),
        "slug": slug,
        "mode_applied": mode,
        "size_bytes": size_bytes,
        "revision_of": str(revision_of) if revision_of else None,
    }


async def notes_search_executor(
    query: str,
    k: int | None = None,
    tags: list[str] | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Search this user's notes by semantic similarity to ``query``.

    Returns metadata only — the agent dereferences specific notes through
    the public URL when it needs the body. This keeps tool-result token
    cost predictable regardless of note size.

    Args:
        query: Natural-language search string.
        k: Max results (1..25, default 5).
        tags: Optional tag filter — notes must share at least one tag.
        ctx: Orchestrator ``ExecutionContext`` carrying ``user_id``.

    Returns:
        ``{"results": [...]}`` ordered by descending similarity.

    Raises:
        NotesSearchError: On an empty query.
        NotesError: On missing identity.
    """
    if not query or not query.strip():
        raise NotesSearchError(
            "invalid_argument",
            "query is required and cannot be empty.",
            retryable=False,
            next_action="Call notes_search again with a natural-language query.",
        )

    user_id = _resolve_user_id(ctx)
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"
    session_id = _resolve_session_id(ctx)

    effective_k = min(max(int(k) if k is not None else 5, 1), _MAX_SEARCH_K)

    query_emb = await generate_embedding(
        query,
        mode="query",
        trace_id=trace_id,
        session_id=str(session_id) if session_id else None,
    )
    tag_filter = list(tags) if tags else None

    log.info(
        "notes_search_called",
        trace_id=trace_id,
        user_id=str(user_id),
        k=effective_k,
        tag_filter=tag_filter,
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT id, slug, title, summary, created_at, tags,
                       1 - (embedding <=> CAST(:query_emb AS vector)) AS similarity
                FROM artifacts
                WHERE user_id = :user_id
                  AND type = 'note'
                  AND embedding IS NOT NULL
                  AND (
                      CAST(:tag_filter AS text[]) IS NULL
                      OR tags && CAST(:tag_filter AS text[])
                  )
                ORDER BY embedding <=> CAST(:query_emb AS vector)
                LIMIT :k
                """
            ),
            {
                "user_id": user_id,
                "query_emb": _pgvector_literal(query_emb),
                "tag_filter": tag_filter,
                "k": effective_k,
            },
        )
        rows = result.all()

    results = [
        {
            "artifact_id": str(row.id),
            "slug": row.slug,
            "title": row.title,
            "summary": row.summary,
            "tags": list(row.tags) if row.tags else [],
            "similarity": float(row.similarity) if row.similarity is not None else None,
            "public_url": _public_url(row.id),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]

    log.info(
        "notes_search_completed",
        trace_id=trace_id,
        user_id=str(user_id),
        result_count=len(results),
    )

    return {"results": results, "result_count": len(results)}
