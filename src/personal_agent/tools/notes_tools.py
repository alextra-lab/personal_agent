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

from typing import Any
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
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)


_NOTE_CONTENT_TYPE = "text/markdown; charset=utf-8"
_NOTE_EXT = "md"
_APPEND_SEPARATOR = "\n\n"
_MAX_NOTE_BYTES = 256 * 1024  # 256 KiB ceiling per note (revision-aware).
_MAX_SEARCH_K = 25


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


notes_write_tool = ToolDefinition(
    name="notes_write",
    description=(
        "Save a durable note that survives across sessions. Use for thought "
        "trails, durable notes, ideas you want to recover later, and "
        "knowledge that should outlive the current conversation. Notes "
        "are indexed by an embedding and retrievable via notes_search. "
        "Use mode='append' to grow an existing slug; mode='overwrite' to "
        "replace it. Slugs are short kebab-case handles (e.g. "
        "'project-x-plan')."
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
    rate_limit_per_hour=60,
)


notes_search_tool = ToolDefinition(
    name="notes_search",
    description=(
        "Search the agent's durable notes by meaning. Returns metadata "
        "(slug, title, similarity, public_url) ordered by semantic "
        "similarity to the query. Use this to surface prior thought "
        "trails, plans, or context the agent saved in earlier sessions."
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
    rate_limit_per_hour=200,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_user_id(ctx: Any) -> UUID:
    """Pull ``user_id`` off the orchestrator ExecutionContext.

    Refuses to fall back to the deployment owner — this is intentional:
    every gateway path populates ``ctx.user_id`` (ADR-0064 has been live
    since FRE-213). A missing id is a programming bug.
    """
    user_id = getattr(ctx, "user_id", None) if ctx else None
    if user_id is None:
        raise ToolExecutionError("notes tools require ctx.user_id (set by the request gateway).")
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
        ToolExecutionError: On missing identity, invalid slug/mode, content
            overflow, or substrate-not-configured.
    """
    store = get_artifact_store()
    if store is None:
        raise ToolExecutionError("notes substrate is not configured on this deployment.")

    if mode not in ("append", "overwrite"):
        raise ToolExecutionError(f"mode must be 'append' or 'overwrite', got {mode!r}.")

    if not slug or not slug.strip():
        raise ToolExecutionError("slug is required and cannot be empty.")
    if not content:
        raise ToolExecutionError("content is required.")

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
        raise ToolExecutionError(str(exc)) from exc

    # Resolve the prior revision (may be None) only after slug validation.
    revision_of: UUID | None = None
    final_body = content
    if mode == "append":
        prior = await _fetch_latest_note_for_slug(user_id, slug)
        if prior is not None:
            revision_of = prior[0]
            try:
                existing = (await store.get(prior[1])).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolExecutionError(
                    f"prior note {prior[0]} is not valid utf-8; refusing to append."
                ) from exc
            final_body = existing + _APPEND_SEPARATOR + content

    final_bytes = final_body.encode("utf-8")
    size_bytes = len(final_bytes)
    if size_bytes > _MAX_NOTE_BYTES:
        raise ToolExecutionError(
            f"note exceeds {_MAX_NOTE_BYTES} bytes (have {size_bytes}); split it."
        )

    embedding = await generate_embedding(final_body, mode="document")

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
                "embedding": embedding,
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
        ToolExecutionError: On missing identity or invalid query.
    """
    if not query or not query.strip():
        raise ToolExecutionError("query is required and cannot be empty.")

    user_id = _resolve_user_id(ctx)
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    effective_k = min(max(int(k) if k is not None else 5, 1), _MAX_SEARCH_K)

    query_emb = await generate_embedding(query, mode="query")
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
                      :tag_filter IS NULL
                      OR tags && CAST(:tag_filter AS text[])
                  )
                ORDER BY embedding <=> CAST(:query_emb AS vector)
                LIMIT :k
                """
            ),
            {
                "user_id": user_id,
                "query_emb": query_emb,
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
