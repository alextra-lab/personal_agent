"""Artifact tools — the second consumer of the R2 artifact substrate (FRE-368).

``artifact_write`` writes human-facing rich content (HTML reports, charts,
comparison tables, generated documents) to R2 and records an ``artifacts``
row (type='artifact') with an optional pgvector embedding for future search.
``artifact_list`` lists the calling user's artifacts from Postgres.
``artifact_read`` fetches a single artifact's metadata (and, for small
textual artifacts, its content inline) so the agent can revise a prior
artifact in a later session.

Architectural anchors
---------------------
* ADR-0069 — R2-backed artifact substrate. Layout / identity / SDK choice.
* ADR-0070 — Output Channel Model. Artifact cards are Tier 3. This module
  is the agent side of the experimental rig (D8 measurement data).
* ADR-0064 — Cloudflare Access user identity. ``user_id`` is the FK.
* FRE-227 — substrate implementation (R2ArtifactStore, build_r2_key, schema).
"""

from __future__ import annotations

import base64
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

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_ARTIFACT_TYPE = "artifact"
_MAX_CONTENT_BYTES = 5 * 1024 * 1024  # 5 MB — matches ticket size cap
_MAX_INLINE_READ_BYTES = 256 * 1024  # 256 KB — inline for small textual artifacts

# Permitted content_types. Expanded only via ADR amendment (ADR-0070 D7
# "documents not apps by default").
_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/html; charset=utf-8",
        "text/markdown; charset=utf-8",
        "text/csv; charset=utf-8",
        "application/json",
        "image/png",
        "image/svg+xml",
    }
)

# R2 key extension keyed by content_type.
_EXT_BY_CONTENT_TYPE: dict[str, str] = {
    "text/html; charset=utf-8": "html",
    "text/markdown; charset=utf-8": "md",
    "text/csv; charset=utf-8": "csv",
    "application/json": "json",
    "image/png": "png",
    "image/svg+xml": "svg",
}

# Content types that can be returned inline as decoded UTF-8 text.
_TEXTUAL_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/html; charset=utf-8",
        "text/markdown; charset=utf-8",
        "text/csv; charset=utf-8",
        "application/json",
    }
)

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


artifact_write_tool = ToolDefinition(
    name="artifact_write",
    description=(
        "Persist a human-facing artifact (HTML report, chart, comparison "
        "table, generated document) to the R2 substrate. Returns a stable "
        "public URL the user can open in a browser. Use for content the "
        "user will revisit, share, or bookmark; use notes_write for "
        "agent-internal durable notes. The chat reply should reference the "
        "returned public_url — the PWA renders it as an inline card."
    ),
    category="artifact_write",
    parameters=[
        ToolParameter(
            name="slug",
            type="string",
            description=(
                "Short kebab-case handle (alnum start, then alnum/./_ /-,"
                " max 64 chars). E.g. 'q3-spend-report'."
            ),
            required=True,
        ),
        ToolParameter(
            name="content_type",
            type="string",
            description=(
                "MIME type. Must be one of: "
                "'text/html; charset=utf-8', "
                "'text/markdown; charset=utf-8', "
                "'text/csv; charset=utf-8', "
                "'application/json', "
                "'image/png', "
                "'image/svg+xml'."
            ),
            required=True,
        ),
        ToolParameter(
            name="content",
            type="string",
            description=(
                "Body. UTF-8 text for text/* and application/json. "
                "Base64-encoded bytes for image/png and image/svg+xml."
            ),
            required=True,
        ),
        ToolParameter(
            name="title",
            type="string",
            description="Optional human-readable title shown in the inline card.",
            required=False,
            default=None,
        ),
        ToolParameter(
            name="summary",
            type="string",
            description=(
                "One-sentence summary shown in the inline card (ADR-0070 D5). "
                "Keep it under ~120 characters."
            ),
            required=False,
            default=None,
        ),
        ToolParameter(
            name="tags",
            type="array",
            description="Optional free-form tags for future artifact_list filtering.",
            required=False,
            default=None,
            json_schema={"type": "array", "items": {"type": "string"}},
        ),
    ],
    risk_level="medium",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=30,
)


artifact_list_tool = ToolDefinition(
    name="artifact_list",
    description=(
        "List recent artifacts owned by the current user, newest first. "
        "Returns metadata and public URLs only — call artifact_read to "
        "ingest content for revision."
    ),
    category="memory_read",
    parameters=[
        ToolParameter(
            name="prefix",
            type="string",
            description="Optional slug prefix filter (e.g. 'q3' matches 'q3-report').",
            required=False,
            default=None,
        ),
        ToolParameter(
            name="k",
            type="number",
            description="Maximum results (1..50). Default 10.",
            required=False,
            default=10,
        ),
        ToolParameter(
            name="since",
            type="string",
            description="ISO-8601 timestamp; only artifacts created after this time.",
            required=False,
            default=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=10,
    rate_limit_per_hour=200,
)


artifact_read_tool = ToolDefinition(
    name="artifact_read",
    description=(
        "Fetch an artifact's metadata and (for textual artifacts under "
        "256 KB) its content inline, so the agent can revise or build upon "
        "a prior artifact. For larger or binary artifacts, returns the "
        "public URL only — the user can open it in a browser."
    ),
    category="memory_read",
    parameters=[
        ToolParameter(
            name="artifact_id",
            type="string",
            description="UUID returned by artifact_write or artifact_list.",
            required=True,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=10,
    rate_limit_per_hour=60,
)


# ---------------------------------------------------------------------------
# Helpers (mirrors notes_tools.py — kept local to avoid coupling)
# ---------------------------------------------------------------------------


def _resolve_user_id(ctx: Any) -> UUID:
    """Pull ``user_id`` off the orchestrator ExecutionContext.

    Refuses to fall back — every gateway path sets ctx.user_id (ADR-0064).
    A missing id is a programming bug, not a runtime condition.
    """
    user_id = getattr(ctx, "user_id", None) if ctx else None
    if user_id is None:
        raise ToolExecutionError("artifact tools require ctx.user_id (set by the request gateway).")
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


def _pgvector_literal(values: list[float]) -> str:
    """Render floats as a pgvector text literal ``[v1,v2,...]``.

    asyncpg has no built-in pgvector codec — binding a list raises
    ``DataError: expected str, got list``. The pgvector extension accepts
    the bracketed text form and the CAST in SQL converts it to binary.
    See notes_tools._pgvector_literal for the canonical explanation.
    """
    return "[" + ",".join(repr(v) for v in values) + "]"


def _decode_content(content_type: str, content: str) -> bytes:
    """Decode the string content param to bytes according to content_type.

    Text types: UTF-8 encode.
    Binary types (image/png, image/svg+xml): base64 decode.
    Raises ToolExecutionError on decode failure.
    """
    if not content:
        raise ToolExecutionError("content is required and cannot be empty.")

    if content_type == "image/png":
        try:
            return base64.b64decode(content, validate=True)
        except Exception as exc:
            raise ToolExecutionError("image/png content must be base64-encoded bytes.") from exc

    # All other allowed types are UTF-8 text.
    return content.encode("utf-8")


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------


async def artifact_write_executor(
    slug: str,
    content_type: str,
    content: str,
    title: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Write a human-facing artifact to R2 and record the Postgres row.

    Args:
        slug: Human-readable kebab-case handle (validated by build_r2_key).
        content_type: MIME type from ``_ALLOWED_CONTENT_TYPES``.
        content: UTF-8 text for text/* / JSON; base64 for image/png.
        title: Optional display title for inline cards.
        summary: Optional 1-sentence summary for inline cards (ADR-0070 D5).
        tags: Optional list of free-form tags.
        ctx: Orchestrator ``ExecutionContext`` with ``user_id`` / ``session_id`` /
            ``trace_id``.

    Returns:
        ``{"artifact_id", "public_url", "slug", "content_type", "size_bytes",
        "title", "summary"}``.

    Raises:
        ToolExecutionError: On missing identity, disallowed content_type,
            empty/oversized content, invalid slug, base64 decode failure,
            or substrate-not-configured.
    """
    store = get_artifact_store()
    if store is None:
        raise ToolExecutionError("artifact substrate is not configured on this deployment.")

    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ToolExecutionError(
            f"unsupported content_type {content_type!r}. Allowed: {sorted(_ALLOWED_CONTENT_TYPES)}"
        )

    payload = _decode_content(content_type, content)
    size_bytes = len(payload)

    if size_bytes > _MAX_CONTENT_BYTES:
        raise ToolExecutionError(
            f"artifact exceeds the 5 MB cap ({size_bytes} bytes). "
            "Split it into smaller artifacts or link to external content."
        )

    user_id = _resolve_user_id(ctx)
    session_id = _resolve_session_id(ctx)
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    artifact_id = uuid4()
    ext = _EXT_BY_CONTENT_TYPE[content_type]
    try:
        r2_key = build_r2_key(
            type=_ARTIFACT_TYPE,
            user_id=user_id,
            session_id=session_id,
            artifact_id=artifact_id,
            slug=slug,
            ext=ext,
        )
    except ArtifactKeyError as exc:
        raise ToolExecutionError(str(exc)) from exc

    # Build embedding text from metadata fields — skip if all empty.
    emb_text = "\n".join(filter(None, [title, summary, " ".join(tags or []), slug]))
    if emb_text.strip():
        embedding = await generate_embedding(emb_text, mode="document")
        emb_literal: str | None = _pgvector_literal(embedding)
    else:
        emb_literal = None

    log.info(
        "artifact_write_uploading",
        trace_id=trace_id,
        user_id=str(user_id),
        slug=slug,
        content_type=content_type,
        size_bytes=size_bytes,
    )

    await store.put(
        r2_key=r2_key,
        content=payload,
        content_type=content_type,
        metadata={"artifact_id": str(artifact_id)},
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
                    :id, :user_id, :session_id, 'artifact', :slug, :title, :summary,
                    :content_type, :size_bytes, :r2_key,
                    CAST(:tags AS text[]),
                    CAST(:embedding AS vector),
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
                "summary": summary,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "r2_key": r2_key,
                "tags": list(tags) if tags else [],
                "embedding": emb_literal,
            },
        )
        await session.commit()

    log.info(
        "artifact_write_committed",
        trace_id=trace_id,
        artifact_id=str(artifact_id),
        slug=slug,
        size_bytes=size_bytes,
    )

    return {
        "artifact_id": str(artifact_id),
        "public_url": _public_url(artifact_id),
        "slug": slug,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "title": title,
        "summary": summary,
    }


async def artifact_list_executor(
    prefix: str | None = None,
    k: int = 10,
    since: str | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """List the calling user's artifacts newest-first.

    Args:
        prefix: Optional slug prefix filter (e.g. ``'q3'`` matches ``'q3-report'``).
        k: Maximum results (1..50, clamped). Default 10.
        since: ISO-8601 timestamp string; only artifacts created after this.
        ctx: Orchestrator ``ExecutionContext`` with ``user_id``.

    Returns:
        ``{"results": [...], "result_count": int}`` with per-item metadata.

    Raises:
        ToolExecutionError: On missing user identity.
    """
    user_id = _resolve_user_id(ctx)
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    effective_k = min(max(int(k) if k is not None else 10, 1), 50)

    log.info(
        "artifact_list_called",
        trace_id=trace_id,
        user_id=str(user_id),
        prefix=prefix,
        k=effective_k,
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT id, slug, title, summary, content_type, tags, created_at
                FROM artifacts
                WHERE user_id = :user_id
                  AND type = 'artifact'
                  AND (:prefix IS NULL OR slug LIKE :prefix || '%')
                  AND (:since IS NULL OR created_at > CAST(:since AS TIMESTAMPTZ))
                ORDER BY created_at DESC
                LIMIT :k
                """
            ),
            {
                "user_id": user_id,
                "prefix": prefix,
                "since": since,
                "k": effective_k,
            },
        )
        rows = result.all()

    results = [
        {
            "artifact_id": str(row.id),
            "public_url": _public_url(row.id),
            "slug": row.slug,
            "title": row.title,
            "summary": row.summary,
            "content_type": row.content_type,
            "tags": list(row.tags) if row.tags else [],
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]

    log.info(
        "artifact_list_completed",
        trace_id=trace_id,
        user_id=str(user_id),
        result_count=len(results),
    )

    return {"results": results, "result_count": len(results)}


async def artifact_read_executor(
    artifact_id: str,
    ctx: Any = None,
) -> dict[str, Any]:
    """Fetch an artifact's metadata and optionally its content inline.

    Content is returned inline only when:
    - The artifact is a textual type (text/html, text/markdown, text/csv,
      application/json), AND
    - size_bytes <= 256 KB.

    Larger artifacts and binary types (image/png, image/svg+xml) return
    metadata + public_url only — the agent should direct the user to open
    the URL directly.

    Args:
        artifact_id: UUID string returned by artifact_write or artifact_list.
        ctx: Orchestrator ``ExecutionContext`` with ``user_id``.

    Returns:
        Metadata dict. ``content`` key present only for small textual artifacts.

    Raises:
        ToolExecutionError: On invalid UUID, not found (incl. cross-user),
            or missing identity.
    """
    user_id = _resolve_user_id(ctx)
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    try:
        parsed_id = UUID(artifact_id)
    except ValueError as exc:
        raise ToolExecutionError(f"artifact_id is not a valid UUID: {artifact_id!r}") from exc

    store = get_artifact_store()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT id, slug, title, summary, content_type, size_bytes,
                       r2_key, tags, created_at
                FROM artifacts
                WHERE id = :artifact_id
                  AND user_id = :user_id
                  AND type = 'artifact'
                """
            ),
            {
                "artifact_id": parsed_id,
                "user_id": user_id,
            },
        )
        row = result.first()

    if row is None:
        log.info(
            "artifact_read_not_found",
            trace_id=trace_id,
            artifact_id=str(parsed_id),
            user_id=str(user_id),
        )
        raise ToolExecutionError(f"artifact {parsed_id} not found (or not owned by current user).")

    output: dict[str, Any] = {
        "artifact_id": str(row.id),
        "public_url": _public_url(row.id),
        "slug": row.slug,
        "title": row.title,
        "summary": row.summary,
        "content_type": row.content_type,
        "size_bytes": row.size_bytes,
        "tags": list(row.tags) if row.tags else [],
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "content": None,
    }

    should_fetch_inline = (
        store is not None
        and row.content_type in _TEXTUAL_CONTENT_TYPES
        and row.size_bytes <= _MAX_INLINE_READ_BYTES
    )

    if (
        store is not None
        and row.content_type in _TEXTUAL_CONTENT_TYPES
        and row.size_bytes <= _MAX_INLINE_READ_BYTES
    ):
        log.info(
            "artifact_read_fetching_inline",
            trace_id=trace_id,
            artifact_id=str(parsed_id),
            size_bytes=row.size_bytes,
        )
        raw = await store.get(row.r2_key)
        output["content"] = raw.decode("utf-8", errors="replace")

    log.info(
        "artifact_read_completed",
        trace_id=trace_id,
        artifact_id=str(parsed_id),
        inline=should_fetch_inline,
    )

    return output
