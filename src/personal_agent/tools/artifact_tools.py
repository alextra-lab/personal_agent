"""Artifact tools — the second consumer of the R2 artifact substrate (FRE-368).

``artifact_write`` writes human-facing rich content (HTML reports, charts,
comparison tables, generated documents) to R2 and records an ``artifacts``
row (type='artifact') with an optional pgvector embedding for future search.
``artifact_draft`` separates planning from HTML generation: the primary model
provides a structured plan, a sub-agent generates the HTML, then the executor
chains to ``artifact_write_executor`` (ADR-0077).
``artifact_list`` lists the calling user's artifacts from Postgres.
``artifact_read`` fetches a single artifact's metadata (and, for small
textual artifacts, its content inline) so the agent can revise a prior
artifact in a later session.

Architectural anchors
---------------------
* ADR-0069 — R2-backed artifact substrate. Layout / identity / SDK choice.
* ADR-0070 — Output Channel Model. Artifact cards are Tier 3. This module
  is the agent side of the experimental rig (D8 measurement data).
* ADR-0077 — Artifact Draft. Plan/generate split via sub-agent.
* ADR-0064 — Cloudflare Access user identity. ``user_id`` is the FK.
* FRE-227 — substrate implementation (R2ArtifactStore, build_r2_key, schema).
"""

from __future__ import annotations

import base64
import os
import re
import tempfile
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from sqlalchemy import text

from personal_agent.config import settings
from personal_agent.memory.embeddings import generate_embedding
from personal_agent.observability.artifact_envelope.probe import probe_served_envelope
from personal_agent.service.database import AsyncSessionLocal
from personal_agent.storage import (
    ArtifactKeyError,
    build_r2_key,
    get_artifact_store,
)
from personal_agent.telemetry import get_logger
from personal_agent.tools.executor import TerminalToolError, ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_ARTIFACT_TYPE = "artifact"
_MAX_CONTENT_BYTES = 5 * 1024 * 1024  # 5 MB — matches ticket size cap
_MAX_INLINE_READ_BYTES = 256 * 1024  # 256 KB — inline for small textual artifacts

# Permitted content_types. Expanded only via ADR amendment (ADR-0089
# supersedes ADR-0070 D7 — one sealed box for every artifact).
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
        "Persist a pre-rendered artifact (CSV, JSON, markdown, SVG, PNG, or "
        "pre-built HTML) to the R2 substrate. Returns a stable public URL. "
        "For generating NEW HTML documents, prefer artifact_draft instead — "
        "it delegates HTML generation to a fast sub-agent and saves tokens. "
        "Use artifact_write directly only when you already have the final "
        "content (e.g. CSV export, JSON data, image, or pre-existing HTML). "
        "Use notes_write for agent-internal durable notes."
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
        "a prior artifact. For binary or image artifacts no bytes are returned "
        "inline; the result carries a human-display-only URL (openable in a "
        "browser by the user) — the agent cannot fetch that URL."
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
    _commit_path: str = "direct_write",
) -> dict[str, Any]:
    """Write a human-facing artifact to R2 and record the Postgres row.

    This is an **ungated** commit path. Per ADR-0089 D1, security is a property of
    the served-CSP envelope (D2/D3, FRE-509), not of inspecting the bytes — so this
    function never strips or rejects content. Every commit emits one
    ``artifact_gate_decision`` analytics label (FRE-506): ``committed`` for HTML
    (with script/handler/CDN counts), ``not_applicable`` otherwise. ``_commit_path``
    is internal-only (not in the tool schema) — ``artifact_draft`` passes ``draft``
    so the label distinguishes the two commit paths. After the commit, one
    served-envelope probe verifies the CSP/MIME/nosniff posture actually applied
    at the edge and emits ``artifact_envelope_integrity`` (FRE-512, ADR-0089 D5)
    — never load-bearing.

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

    # FRE-506: one content label per commit, computed here for every path so draft
    # and direct writes can never diverge. Visibility only — the served CSP envelope
    # is the boundary (ADR-0089 D1/D5).
    if content_type == _HTML_CONTENT_TYPE:
        decision = "committed"
        violations = _count_sandbox_violations(content)
        script_reaches = _classify_script_reaches(content)
    else:
        decision = "not_applicable"
        violations = (0, 0, 0)
        script_reaches = (0, 0, 0)

    _emit_gate_decision(
        trace_id=trace_id,
        session_id=str(session_id) if session_id is not None else None,
        user_id=user_id,
        artifact_id=artifact_id,
        slug=slug,
        content_type=content_type,
        size_bytes=size_bytes,
        decision=decision,
        commit_path=_commit_path,
        violations=violations,
        script_reaches=script_reaches,
    )

    # FRE-512 (ADR-0089 D5): verify the served envelope with one real GET through
    # the edge. Never load-bearing — the probe swallows its own errors, and this
    # guard ensures even a buggy probe cannot fail the commit.
    public_url = _public_url(artifact_id)
    if settings.artifact_envelope_probe_enabled and public_url is not None:
        try:
            await probe_served_envelope(
                public_url=public_url,
                artifact_id=str(artifact_id),
                slug=slug,
                content_type=content_type,
                trace_id=trace_id,
                session_id=str(session_id) if session_id is not None else None,
                user_id=str(user_id),
            )
        except Exception:
            log.warning(
                "artifact_envelope_probe_error",
                trace_id=trace_id,
                artifact_id=str(artifact_id),
                exc_info=True,
            )

    return {
        "artifact_id": str(artifact_id),
        "public_url": public_url,
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
                  AND (CAST(:prefix AS TEXT) IS NULL OR slug LIKE :prefix || '%')
                  AND (CAST(:since AS TEXT) IS NULL OR created_at > CAST(:since AS TIMESTAMPTZ))
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

    Larger text-like artifacts return metadata + ``public_url``. Binary/image
    artifacts (image/*, application/pdf) return metadata plus a
    ``human_display_url`` (human/browser display only) and a ``note`` — no bytes
    inline and no agent-fetchable URL, per ADR-0101 §7 (AC-8): the public route is
    Cloudflare-Access-protected, so the agent cannot fetch it. Current-turn image
    attachments reach the model as a turn content block, not by URL.

    Args:
        artifact_id: UUID string returned by artifact_write or artifact_list.
        ctx: Orchestrator ``ExecutionContext`` with ``user_id``.

    Returns:
        Metadata dict. ``content`` key present only for small textual artifacts;
        ``public_url`` for text-like artifacts, else ``human_display_url`` + ``note``.

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
                  AND type IN ('artifact', 'upload')
                  AND upload_pending = FALSE
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

    # Text-like content keeps a plain ``public_url``; binary/image content does not
    # (AC-8, below). ``_TEXTUAL_CONTENT_TYPES`` covers the charset-tagged artifact
    # types plus ``application/json``; the ``text/`` prefix additionally covers the
    # bare upload types (``text/plain`` / ``text/markdown`` / ``text/csv`` —
    # uploads_router.ALLOWED_UPLOAD_CONTENT_TYPES) so a text upload is not mislabelled
    # binary. Inline fetch stays gated on the narrower registered-textual set below.
    is_registered_textual = row.content_type in _TEXTUAL_CONTENT_TYPES
    is_text_like = is_registered_textual or row.content_type.startswith("text/")

    output: dict[str, Any] = {
        "artifact_id": str(row.id),
        "slug": row.slug,
        "title": row.title,
        "summary": row.summary,
        "content_type": row.content_type,
        "size_bytes": row.size_bytes,
        "tags": list(row.tags) if row.tags else [],
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "content": None,
    }

    # ADR-0101 §7 (AC-8, artifact_read honesty): the public URL is the
    # Cloudflare-Access-protected route. For a binary/image artifact no bytes are
    # returned inline, so a bare ``public_url`` reads to the model as a fetchable
    # content source — but the agent cannot fetch it (it receives a sign-in page).
    # Present the URL only under an explicitly human-display-only key and state
    # that the bytes reach the model via the turn's content block, never by URL.
    # Text-like artifacts are out of scope and keep ``public_url`` unchanged.
    public_url = _public_url(row.id)
    if is_text_like:
        output["public_url"] = public_url
    else:
        if public_url is not None:
            output["human_display_url"] = public_url
        output["note"] = (
            "Binary or image artifact — the bytes are not URL-fetchable by the agent. "
            "When such an artifact is a current-turn image attachment, its bytes are "
            "delivered directly in this turn's content block. 'human_display_url' is for "
            "human/browser display only and requires interactive sign-in."
        )

    should_fetch_inline = (
        store is not None and is_registered_textual and row.size_bytes <= _MAX_INLINE_READ_BYTES
    )

    if store is not None and is_registered_textual and row.size_bytes <= _MAX_INLINE_READ_BYTES:
        log.info(
            "artifact_read_fetching_inline",
            trace_id=trace_id,
            artifact_id=str(parsed_id),
            size_bytes=row.size_bytes,
        )
        raw = await store.get(row.r2_key, trace_id=trace_id)
        output["content"] = raw.decode("utf-8", errors="replace")

    log.info(
        "artifact_read_completed",
        trace_id=trace_id,
        artifact_id=str(parsed_id),
        inline=should_fetch_inline,
    )

    return output


# ---------------------------------------------------------------------------
# artifact_draft — plan/generate split via sub-agent (ADR-0077)
# ---------------------------------------------------------------------------

# Plan char ceiling. ~16k chars ≈ ~4k input tokens which, with the system prompt
# and title/summary, stays well within the sub-agent's context budget. Plans longer
# than this are truncated-with-warning (FRE-471), never rejected terminally.
_MAX_PLAN_CHARS = 16000
_MIN_HTML_LENGTH = 50

# Appended to a truncated plan so the generator knows the spec is incomplete and
# must not fabricate the omitted sections (FRE-471).
_PLAN_TRUNCATION_NOTICE = (
    "\n\n[NOTICE: This plan was truncated because it exceeded the generation budget. "
    "It is INCOMPLETE — later sections are missing. Build a coherent, complete-looking "
    "HTML document from the sections present above. Do NOT invent or fabricate the "
    "omitted requirements; prioritize finishing what is specified.]"
)


def _truncate_plan(plan: str) -> tuple[str, bool, int]:
    r"""Trim an oversized plan to the char ceiling, boundary-aware (FRE-471).

    Plans within ``_MAX_PLAN_CHARS`` pass through untouched. Longer plans are cut at
    the last line boundary (``\n``) at or before the budget — so a section is not
    severed mid-sentence — and ``_PLAN_TRUNCATION_NOTICE`` is appended. If no line
    boundary exists within the budget, a hard character cut is used as a fallback.
    The returned plan is always ``<= _MAX_PLAN_CHARS`` characters.

    Args:
        plan: The raw plan text supplied by the primary model.

    Returns:
        A ``(effective_plan, was_truncated, original_length)`` tuple where
        ``effective_plan`` is the (possibly trimmed) plan to send to the sub-agent,
        ``was_truncated`` indicates whether trimming occurred, and
        ``original_length`` is the character length of the input ``plan``.
    """
    original_length = len(plan)
    if original_length <= _MAX_PLAN_CHARS:
        return plan, False, original_length

    budget = _MAX_PLAN_CHARS - len(_PLAN_TRUNCATION_NOTICE)
    head = plan[:budget]
    boundary = head.rfind("\n")
    if boundary > 0:
        head = head[:boundary]
    return head + _PLAN_TRUNCATION_NOTICE, True, original_length


def _draft_timeout_s() -> float:
    """Request + wall-clock timeout for ``artifact_draft``'s HTML sub-agent.

    Artifact generation runs on the sub-agent role but is a heavy, primary-class
    job (a full HTML document), so it gets the **reasoning model's** (``primary``)
    configured request timeout rather than the sub_agent fail-fast budget — the
    builder should be allowed to run as long as a reasoning call. Resolved from the
    active model config so it tracks the primary timeout instead of drifting; falls
    back to the global LLM request timeout if ``primary`` can't be resolved.

    Returns:
        Timeout in seconds.
    """
    from personal_agent.config.model_loader import ModelConfigError, resolve_role_definition

    try:
        # "primary" is a ROLE. Since ADR-0121 keyed the catalog by model, a raw
        # models.get("primary") misses and this silently returned the global
        # 120s fallback instead of the reasoning model's 600s — quietly cutting
        # the artifact builder's budget to a fifth.
        primary = resolve_role_definition("primary")
    except ModelConfigError:
        primary = None
    if primary is not None and primary.default_timeout:
        return float(primary.default_timeout)
    return float(settings.llm_timeout_seconds)


def _draft_max_tokens() -> int:
    """Output-token ceiling for the artifact-draft HTML sub-agent (FRE-478).

    Resolved from config at call time so the cap is env-overridable without an
    import-time freeze (mirrors :func:`_draft_timeout_s`). See
    ``settings.artifact_draft_max_tokens`` for the value rationale.

    Returns:
        Maximum output tokens for the generation call.
    """
    return int(settings.artifact_draft_max_tokens)


# Detection-only regexes feeding the per-commit analytics label (ADR-0089 D1/D5).
# NON-LOAD-BEARING: nothing gates, strips, or rejects on these — the served-CSP
# envelope (FRE-509) + opaque-origin sandbox (FRE-510) are the security boundary.
# The event-handler detector is pinned to a real attribute boundary (^ / whitespace /
# quote / slash) so it neither misses a glued `"onclick=` nor false-positives on
# `data-on*`. The CDN regex also matches unquoted href= values.
_SCRIPT_TAG_RE = re.compile(r"<\s*script", re.IGNORECASE)
_EVENT_HANDLER_RE = re.compile(r"""(?:^|[\s"'/])on\w+\s*=""", re.IGNORECASE)
_CDN_LINK_RE = re.compile(
    r'<\s*link\b[^>]*\bhref\s*=\s*["\']?https?://[^"\'>\s]*["\']?[^>]*>', re.IGNORECASE
)
# FRE-526 (ADR-0089 A1): match <script ... src="…"> where src is an absolute
# (https://…) or protocol-relative (//cdn…) URL. Inline <script> blocks (no src)
# and relative-path srcs are not external reaches and are intentionally excluded
# — ADR-0089 A3 specifies the model uses absolute version-pinned /lib/ URLs, so a
# relative /lib/ path is not the expected reach form. Protocol-relative // is
# included because under the served https origin it resolves to a real CDN fetch.
_SCRIPT_SRC_RE = re.compile(
    r"""<\s*script\b[^>]*\bsrc\s*=\s*["']?((?:https?:)?//[^"'>\s]+)""", re.IGNORECASE
)
# Matches <pre class="mermaid">…</pre> and <div class="mermaid">…</div> (FRE-396).
_MERMAID_BLOCK_RE = re.compile(
    r'<(pre|div)\b[^>]*\bclass=["\'][^"\']*\bmermaid\b[^"\']*["\'][^>]*>(.*?)</\1>',
    re.DOTALL | re.IGNORECASE,
)
_MERMAID_RENDER_TIMEOUT_S: float = 30.0

# ---------------------------------------------------------------------------
# FRE-506 — sandbox gate-decision telemetry (non-load-bearing label, ADR-0089 D1/D5)
# ---------------------------------------------------------------------------

_HTML_CONTENT_TYPE = "text/html; charset=utf-8"


def _count_sandbox_violations(html: str) -> tuple[int, int, int]:
    """Count script/handler/CDN constructs for the per-commit analytics label.

    An analytics label only (ADR-0089 D1/D5) — nothing depends on it for safety;
    the served-CSP envelope is the boundary (D2/D3). The name keeps FRE-506's
    "violations" vocabulary for ES field continuity, but under ADR-0089 these
    constructs are permitted content, not violations.

    Args:
        html: The artifact HTML text.

    Returns:
        A ``(script_count, handler_count, cdn_count)`` tuple.
    """
    return (
        len(_SCRIPT_TAG_RE.findall(html)),
        len(_EVENT_HANDLER_RE.findall(html)),
        len(_CDN_LINK_RE.findall(html)),
    )


def _artifacts_lib_netloc() -> str | None:
    """Return the lowercased netloc (host[:port]) of the configured artifacts host.

    A ``<script src>`` reach is *host-allowed* only when it targets this host's
    ``/lib/`` path — the single place the served CSP admits executable JS
    (ADR-0089 A3). Matching on netloc (not scheme) so an absolute ``https`` URL
    and a protocol-relative ``//host/lib/`` reference to our own shelf both
    classify allowed. Without a configured host nothing can be proven allowed.

    Returns:
        The lowercased netloc, or ``None`` when no artifacts host is configured.
    """
    base = settings.artifacts_public_base_url
    if not base:
        return None
    netloc = urlparse(base).netloc
    return netloc.lower() or None


def _is_lib_reach(url: str, lib_netloc: str | None) -> bool:
    """Return whether ``url`` targets the artifacts host's ``/lib/`` shelf.

    Args:
        url: The ``<script src>`` URL (absolute or protocol-relative).
        lib_netloc: The configured artifacts netloc, or ``None``.

    Returns:
        ``True`` when the URL is a satisfied need (host-allowed), else ``False``.
    """
    if lib_netloc is None:
        return False
    parsed = urlparse(url)
    return parsed.netloc.lower() == lib_netloc and parsed.path.startswith("/lib/")


def _classify_script_reaches(html: str) -> tuple[int, int, int]:
    """Count + classify external ``<script src>`` reaches for the analytics label.

    Non-load-bearing (ADR-0089 D1/D5, A1): the served-CSP envelope is the
    boundary; this only makes unmet-capability demand observable. A host-blocked
    reach (any non-``/lib/`` origin) is the real demand signal FRE-498 asked for.

    Args:
        html: The artifact HTML text.

    Returns:
        A ``(external_script_count, host_allowed, host_blocked)`` tuple where
        ``host_allowed`` targets the artifacts ``/lib/`` shelf and
        ``host_blocked`` is any other origin.
    """
    lib_netloc = _artifacts_lib_netloc()
    allowed = 0
    blocked = 0
    for url in _SCRIPT_SRC_RE.findall(html):
        if _is_lib_reach(url, lib_netloc):
            allowed += 1
        else:
            blocked += 1
    return (allowed + blocked, allowed, blocked)


def _emit_gate_decision(
    *,
    trace_id: str,
    session_id: str | None,
    user_id: object | None,
    artifact_id: object | None,
    slug: str,
    content_type: str,
    size_bytes: int,
    decision: str,
    commit_path: str,
    violations: tuple[int, int, int],
    script_reaches: tuple[int, int, int],
) -> None:
    """Emit the per-commit content label (FRE-506 substrate, ADR-0089 D1/D5).

    An observation, never a security verdict: the served-CSP envelope is the boundary
    (ADR-0089 D2/D3, served by the Worker — FRE-509). With the content gate retired
    (FRE-511) the old pass/strip/reject/bypass vocabulary and the ``gate_ran`` field
    are gone; serve-side envelope integrity is FRE-512's alarm surface. The event name
    and remaining fields are kept for ES/dashboard continuity. FRE-526 adds the
    external ``<script src>`` reach counts (A1) — the demand signal FRE-498 asked for.

    Args:
        trace_id: Caller trace id.
        session_id: Caller session id, or None.
        user_id: Owning user id, or None.
        artifact_id: Committed artifact id.
        slug: Artifact slug.
        content_type: Committed MIME type.
        size_bytes: Committed byte length.
        decision: ``committed`` (HTML) or ``not_applicable`` (non-HTML).
        commit_path: ``draft`` or ``direct_write``.
        violations: ``(script_count, handler_count, cdn_count)`` label.
        script_reaches: ``(external_script_count, host_allowed, host_blocked)``
            external ``<script src>`` reach label (FRE-526, ADR-0089 A1).
    """
    script_count, handler_count, cdn_count = violations
    external_script_count, script_reach_allowed, script_reach_blocked = script_reaches
    log.info(
        "artifact_gate_decision",
        trace_id=trace_id,
        session_id=session_id,
        user_id=str(user_id) if user_id is not None else None,
        artifact_id=str(artifact_id) if artifact_id is not None else None,
        slug=slug,
        content_type=content_type,
        size_bytes=size_bytes,
        gate_decision=decision,
        commit_path=commit_path,
        script_count=script_count,
        handler_count=handler_count,
        cdn_count=cdn_count,
        external_script_count=external_script_count,
        script_reach_allowed=script_reach_allowed,
        script_reach_blocked=script_reach_blocked,
    )


#: Neutral placeholder artifacts origin baked into the prompt below (FRE-895) —
#: the real Cloudflare Worker origin never lands in tracked source.
_ARTIFACTS_ORIGIN_PLACEHOLDER = "https://artifacts.example.com"

_HTML_GENERATION_SYSTEM_PROMPT = """\
You are an HTML document generator. You receive a structured plan and produce \
a complete, standalone HTML document.

REQUIREMENTS:
- Output ONLY the HTML document. No explanation, no markdown fences, no preamble.
- Start with <!DOCTYPE html> and end with </html>.
- Define a complete design system in a <style> block in <head>:
  * CSS custom properties for colors: --color-primary, --color-secondary, \
--color-accent, --color-bg, --color-surface, --color-text, --color-muted.
  * Spacing scale: --spacing-1 through --spacing-8 (0.25rem increments).
  * Typography: --font-sans, --font-mono; size classes from text-xs to text-3xl.
  * Utility classes: flex, grid, gap-1 through gap-6, p-1 through p-8, \
m-1 through m-8, text-center, text-left, text-right, font-bold, font-medium, \
rounded, rounded-lg, shadow, shadow-lg, hidden, w-full.
- INTERACTIVITY: JavaScript is available. The document runs in a sandboxed, \
sealed page — inline <script> blocks and event handlers run normally, so use \
them freely for genuine interactivity: simulations, explorable diagrams, \
charts, animations, calculators, tabs, filters. Prefer plain CSS (:hover, \
:target, <details>/<summary>, transitions) when it does the job with less \
code; reach for JavaScript when the experience genuinely needs it.
- CURATED TOOLKIT: a small, vetted /lib/ shelf is hosted on the artifact \
origin — it is the ONLY external origin this page may load. Reference each \
asset by its EXACT absolute, version-pinned URL below (a relative path will \
not load). Reach for one only when the document genuinely needs it; otherwise \
inline your own code.
  * Math (KaTeX) — render TeX/LaTeX:
    <link rel="stylesheet" href="https://artifacts.example.com/lib/katex@0.16.47/katex.min.css">
    <script src="https://artifacts.example.com/lib/katex@0.16.47/katex.min.js"></script>
  * Data viz (Chart.js — global `Chart`) — charts/graphs from inline data:
    <script src="https://artifacts.example.com/lib/chartjs@4.4.7/chart.umd.js"></script>
  * 3-D (three.js — global `THREE`, r171) — scenes/geometry; build meshes in \
code and embed any textures as data: URIs, never fetch them:
    <script src="https://artifacts.example.com/lib/three@0.171.0/three.iife.min.js"></script>
  * Code (highlight.js) — syntax-highlight code blocks:
    <link rel="stylesheet" href="https://artifacts.example.com/lib/highlightjs@11.9.0/github-dark.min.css">
    <script src="https://artifacts.example.com/lib/highlightjs@11.9.0/highlight.min.js"></script>
  * Prose typography (OFL variable fonts) — declare with @font-face, then use \
the family in your design system:
    @font-face { font-family: "Source Serif 4"; src: url("https://artifacts.example.com/lib/fonts/source-serif-4@4.005/source-serif-4.woff2") format("woff2"); }
    @font-face { font-family: "Playfair Display"; src: url("https://artifacts.example.com/lib/fonts/playfair-display@2.103/playfair-display.woff2") format("woff2"); }
    @font-face { font-family: "JetBrains Mono"; src: url("https://artifacts.example.com/lib/fonts/jetbrains-mono@2.304/jetbrains-mono.woff2") format("woff2"); }
    Use Source Serif 4 for body prose, Playfair Display for display headings, \
JetBrains Mono for code.
  * Book/print layout: prefer the NATIVE CSS recipes below (@page, \
column-count, break-*). A paged.js polyfill is also on the shelf but is \
EXPERIMENTAL and may be restricted under the live page policy — do not rely \
on it.
- NATIVE TYPOGRAPHY (no library) — reach for these before any font library for \
refined text:
  * Drop cap: p::first-letter { float: left; font-size: 3.2em; line-height: \
0.8; padding-right: 0.08em; }
  * Justified prose: text-align: justify; hyphens: auto; (set lang on <html>).
  * Balanced headings: text-wrap: balance;
  * Ligatures / old-style figures: font-feature-settings: "liga", "onum", \
"kern";
  * Multi-column flow: column-count, column-gap, column-rule.
  * Print/book pages: @page { margin: ... } with break-inside: avoid; and \
widows/orphans control.
- SEALED-BOX CONSTRAINTS (hard, enforced by the runtime — design within them):
  * No network: fetch/XHR/WebSocket/beacon are blocked. Embed ALL of your own \
data inline; inline images and 3-D textures as data: URIs or inline SVG — \
never fetch them.
  * No storage: localStorage, sessionStorage, IndexedDB, and cookies are \
unavailable. Keep state in JS variables or the DOM.
  * No arbitrary CDN: only the curated /lib/ shelf above loads. Any other \
external script/style/font/image (Tailwind CDN, Alpine.js, jQuery, Google \
Fonts, unpkg, etc.) silently fails — inline it instead. Default to inline CSS \
and native JS wherever no shelf library is warranted.
  * No popups, no form submission to external endpoints.
- PORTABILITY (choose deliberately): for static diagrams that should travel \
with the file — flowcharts, architecture, sequence/class diagrams — use \
<pre class="mermaid">…</pre> markup with Mermaid syntax; the server renders \
these to static inline SVG, so the document stays self-contained and viewable \
anywhere. Example: <pre class="mermaid">graph LR; A[Start] --> B[End];</pre>. \
Use JavaScript instead when the experience is genuinely interactive — such an \
artifact is viewed on its hosted page.
- Use semantic HTML5 elements: header, main, section, article, footer, \
nav, aside, figure, figcaption.
- Responsive: use CSS media queries (@media) for mobile/tablet/desktop.
- Accessibility: heading hierarchy (h1 > h2 > h3), alt text on images, \
ARIA labels where helpful, sufficient color contrast.
- For data tables: <table> with <thead>/<tbody>, striped rows via \
nth-child, sticky header if many rows.
- For metrics/KPIs: card layout with large number and small label beneath.
- For comparison layouts: CSS grid with equal-width columns.
- Maximum document size: aim for under 200KB of HTML text.\
"""


def _html_generation_system_prompt() -> str:
    """The HTML-generation system prompt with the real artifacts origin substituted in.

    ``_HTML_GENERATION_SYSTEM_PROMPT`` bakes in a neutral placeholder host (FRE-895);
    this rebinds it to ``settings.artifacts_public_base_url`` when configured, since
    the LLM must embed the *real* origin in every generated artifact's ``/lib/`` refs.
    """
    if not settings.artifacts_public_base_url:
        return _HTML_GENERATION_SYSTEM_PROMPT
    return _HTML_GENERATION_SYSTEM_PROMPT.replace(
        _ARTIFACTS_ORIGIN_PLACEHOLDER, settings.artifacts_public_base_url
    )


artifact_draft_tool = ToolDefinition(
    name="artifact_draft",
    description=(
        "Plan a rich HTML artifact and delegate HTML generation to a fast "
        "sub-agent. Use this instead of artifact_write when creating HTML "
        "documents — provide a structured plan with content, data, and style "
        "guidance; the sub-agent generates the final HTML. For non-HTML "
        "artifacts (CSV, JSON, markdown, images), use artifact_write directly."
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
            name="title",
            type="string",
            description="Human-readable title for the artifact and inline card.",
            required=True,
        ),
        ToolParameter(
            name="summary",
            type="string",
            description=(
                "One-sentence summary shown in the inline card (ADR-0070 D5). "
                "Keep it under ~120 characters."
            ),
            required=True,
        ),
        ToolParameter(
            name="plan",
            type="string",
            description=(
                "Structured content plan for the HTML artifact. Include: "
                "(1) document structure and sections, "
                "(2) all data and content to render (tables, lists, text, metrics), "
                "(3) style guidance (color palette, emphasis, layout preferences), "
                "(4) any specific patterns to use (cards, grids, callouts). "
                "The sub-agent generates HTML from this plan. Be specific about "
                "content and data. Do not write HTML yourself. Max ~16000 chars; "
                "longer plans are truncated with a notice rather than rejected."
            ),
            required=True,
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
    timeout_seconds=120,
    rate_limit_per_hour=20,
)


def _mermaid_fallback(source: str) -> str:
    """Return a script-free fallback for a Mermaid block that could not be rendered.

    Args:
        source: Raw Mermaid diagram source text.

    Returns:
        A ``<figure>`` containing the source in a ``<pre>`` with an explanatory
        caption — passes ``_validate_html_output`` unchanged.
    """
    escaped = source.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<figure class="mermaid-diagram mermaid-fallback">'
        f"<pre>{escaped}</pre>"
        "<figcaption>Diagram could not be rendered — Mermaid source above.</figcaption>"
        "</figure>"
    )


async def _render_mermaid_one(
    source: str,
    *,
    trace_id: str,
    session_id: str | None,
    mmdc_cmd: str = "mmdc",
) -> str:
    """Render one Mermaid source string to an inline figure with SVG via mmdc.

    Uses two temporary files (*.mmd input, *.svg output) so mmdc can write its
    SVG without stdin/stdout negotiation.  On any failure the block degrades to
    :func:`_mermaid_fallback` — the document still passes ``_validate_html_output``.

    Args:
        source: Raw Mermaid diagram source (content of the ``<pre>`` block).
        trace_id: Caller trace id for structured logging.
        session_id: Caller session id for structured logging.
        mmdc_cmd: Path or name of the mmdc binary (override in tests).

    Returns:
        A ``<figure class="mermaid-diagram">`` wrapping inline SVG, or a
        fallback ``<figure>`` when rendering fails.
    """
    import asyncio  # noqa: PLC0415

    in_fd, in_path = tempfile.mkstemp(suffix=".mmd")
    out_fd, out_path = tempfile.mkstemp(suffix=".svg")
    try:
        os.write(in_fd, source.encode())
        os.close(in_fd)
        os.close(out_fd)

        try:
            proc = await asyncio.create_subprocess_exec(
                mmdc_cmd,
                "-i",
                in_path,
                "-o",
                out_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            log.info(
                "mermaid_render_skipped",
                trace_id=trace_id,
                session_id=session_id,
                reason="mmdc_not_found",
            )
            return _mermaid_fallback(source)

        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_MERMAID_RENDER_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            log.warning(
                "mermaid_render_timeout",
                trace_id=trace_id,
                session_id=session_id,
                timeout_s=_MERMAID_RENDER_TIMEOUT_S,
            )
            return _mermaid_fallback(source)

        if proc.returncode != 0:
            log.warning(
                "mermaid_render_failed",
                trace_id=trace_id,
                session_id=session_id,
                returncode=proc.returncode,
                stderr=stderr.decode(errors="replace")[:200],
            )
            return _mermaid_fallback(source)

        with open(out_path) as fh:
            svg = fh.read()
        # Strip leading XML declaration if mmdc emits one.
        svg = re.sub(r"^\s*<\?xml[^?]*\?>\s*", "", svg, flags=re.IGNORECASE)
        log.info(
            "mermaid_render_ok",
            trace_id=trace_id,
            session_id=session_id,
            svg_bytes=len(svg),
        )
        return f'<figure class="mermaid-diagram">{svg}</figure>'

    finally:
        for path in (in_path, out_path):
            try:
                os.unlink(path)
            except OSError:
                pass


async def _render_mermaid_blocks(
    html: str,
    *,
    trace_id: str,
    session_id: str | None,
) -> str:
    """Replace all mermaid markup blocks with server-rendered inline SVG.

    Extracts every ``<pre class="mermaid">`` block, renders each concurrently
    via :func:`_render_mermaid_one`, then splices results back right-to-left so
    original string positions stay valid.  HTML without mermaid blocks is
    returned unchanged.

    Args:
        html: Full HTML document string from the sub-agent.
        trace_id: Caller trace id for structured logging.
        session_id: Caller session id for structured logging.

    Returns:
        HTML with all Mermaid blocks replaced by inline ``<figure>`` elements
        (SVG on success, fallback ``<pre>`` on failure).
    """
    import asyncio  # noqa: PLC0415

    matches = list(_MERMAID_BLOCK_RE.finditer(html))
    if not matches:
        return html

    rendered = await asyncio.gather(
        *[
            _render_mermaid_one(m.group(2).strip(), trace_id=trace_id, session_id=session_id)
            for m in matches
        ]
    )

    result = html
    for match, replacement in reversed(list(zip(matches, rendered, strict=True))):
        result = result[: match.start()] + replacement + result[match.end() :]
    return result


def _validate_html_output(html: str) -> None:
    """Validate sub-agent HTML for malformation before persisting (ADR-0077 D9).

    A **quality** validator only — it never makes a security decision on the bytes
    (ADR-0089 D1: the served-CSP envelope is the boundary; scripts and handlers are
    permitted content). It catches truncated or trivially broken generations so the
    model can retry instead of committing a husk.

    Raises:
        ToolExecutionError: On recoverable malformation (too small, missing
            DOCTYPE / closing tag) — the model may fix this on retry.
    """
    if len(html) < _MIN_HTML_LENGTH:
        raise ToolExecutionError(
            f"HTML generation produced trivially small output ({len(html)} chars). "
            "Refine the plan or use artifact_write directly."
        )
    if "<!doctype html>" not in html[:200].lower():
        raise ToolExecutionError(
            "Generated HTML is missing <!DOCTYPE html> declaration. "
            "Refine the plan or use artifact_write directly."
        )
    if "</html>" not in html[-200:].lower():
        raise ToolExecutionError(
            "Generated HTML is missing closing </html> tag. "
            "Refine the plan or use artifact_write directly."
        )


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences that instruct models sometimes wrap around output."""
    stripped = text.strip()
    if stripped.startswith("```html"):
        stripped = stripped[7:].strip()
    elif stripped.startswith("```"):
        stripped = stripped[3:].strip()
    if stripped.endswith("```"):
        stripped = stripped[:-3].strip()
    return stripped


async def artifact_draft_executor(
    slug: str,
    title: str,
    summary: str,
    plan: str,
    tags: list[str] | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Plan an HTML artifact and delegate generation to a sub-agent.

    The primary model provides a structured plan; a sub-agent (instruct mode,
    thinking disabled) generates the HTML in a single attempt. The output is
    committed as generated — per ADR-0089 D1 no content is stripped or rejected
    for security (the served-CSP envelope is the boundary); only malformation
    (truncated/empty document) fails, recoverably. Mermaid blocks are server-
    rendered to inline SVG (the portability lane, ADR-0089 D7). The result is
    persisted via ``artifact_write_executor``.

    Args:
        slug: Human-readable kebab-case handle.
        title: Display title for the artifact and inline card.
        summary: One-sentence summary for inline card.
        plan: Structured content plan describing sections, data, style.
        tags: Optional free-form tags.
        ctx: Orchestrator ``TraceContext`` with ``user_id`` / ``session_id`` /
            ``trace_id``.

    Returns:
        Same dict as ``artifact_write_executor``, plus ``generation_method``,
        ``sub_agent_duration_ms``, ``task_id``, ``plan_truncated``, and
        ``plan_original_length``. An oversized plan is truncated-with-warning
        rather than rejected (FRE-471).

    Raises:
        TerminalToolError: On a sub-agent timeout (FRE-402).
        ToolExecutionError: On missing identity, empty plan, sub-agent failure,
            HTML malformation, or any ``artifact_write_executor`` error.
    """
    import asyncio  # noqa: PLC0415
    import time  # noqa: PLC0415

    from personal_agent.llm_client.factory import get_llm_client  # noqa: PLC0415
    from personal_agent.llm_client.types import ModelRole  # noqa: PLC0415
    from personal_agent.telemetry.trace import TraceContext  # noqa: PLC0415

    # --- Input validation ---
    if not plan or not plan.strip():
        raise ToolExecutionError("plan is required and cannot be empty.")

    trace_id: str = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"
    session_id: str | None = getattr(ctx, "session_id", None) if ctx else None
    task_id = f"draft-{uuid4().hex[:12]}"

    # FRE-471: an oversized plan is truncated-with-warning, never rejected — a hard
    # fail at remaining=0 tool budget produced zero artifact (incident trace c216bd40).
    effective_plan, plan_truncated, plan_original_length = _truncate_plan(plan)
    if plan_truncated:
        log.warning(
            "artifact_draft_plan_truncated",
            trace_id=trace_id,
            session_id=session_id,
            slug=slug,
            task_id=task_id,
            original_length=plan_original_length,
            truncated_length=len(effective_plan),
            max_plan_chars=_MAX_PLAN_CHARS,
        )

    log.info(
        "artifact_draft_start",
        trace_id=trace_id,
        session_id=session_id,
        slug=slug,
        plan_length=len(effective_plan),
        task_id=task_id,
    )

    # --- Create child span for sub-agent inference (ADR-0074 joinability) ---
    if isinstance(ctx, TraceContext):
        child_ctx, span_id = ctx.new_span()
    else:
        span_id = str(uuid4())
        child_ctx = TraceContext(
            trace_id=trace_id,
            parent_span_id=span_id,
            session_id=session_id,
        )

    # --- Acquire artifact-builder client (profile-driven: ADR-0044, ADR-0118 T1) ---
    builder_client = get_llm_client(role_name="artifact_builder")

    user_prompt = (
        f"Title: {title}\n"
        f"Summary: {summary}\n\n"
        f"Plan:\n{effective_plan}\n\n"
        "Generate the complete HTML document now."
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _html_generation_system_prompt()},
        {"role": "user", "content": user_prompt},
    ]

    draft_timeout = _draft_timeout_s()
    draft_max_tokens = _draft_max_tokens()

    # --- Sub-agent inference (single attempt — FRE-511 retired the FRE-496 retry) ---
    log.info(
        "artifact_draft_sub_agent_start",
        trace_id=trace_id,
        session_id=session_id,
        span_id=span_id,
        task_id=task_id,
        model_role=ModelRole.ARTIFACT_BUILDER.value,
        max_tokens=draft_max_tokens,
        timeout_s=draft_timeout,
    )

    start_ms = int(time.monotonic() * 1000)
    try:
        response = await asyncio.wait_for(
            builder_client.respond(
                role=ModelRole.ARTIFACT_BUILDER,
                messages=messages,
                max_tokens=draft_max_tokens,
                trace_ctx=child_ctx,
                timeout_s=draft_timeout,
            ),
            timeout=draft_timeout,
        )
    except asyncio.TimeoutError as exc:
        sub_agent_duration_ms = int(time.monotonic() * 1000) - start_ms
        log.warning(
            "artifact_draft_sub_agent_complete",
            trace_id=trace_id,
            session_id=session_id,
            span_id=span_id,
            task_id=task_id,
            success=False,
            duration_ms=sub_agent_duration_ms,
            error="timeout",
        )
        # FRE-402: a sub-agent timeout is non-recoverable — surface immediately
        # instead of spending a full primary-model call to explain the failure.
        raise TerminalToolError(
            f"HTML generation sub-agent timed out after {draft_timeout}s.",
            reason="The artifact generator timed out — the document was too complex to build in time.",
            next_step="Try a simpler artifact, or switch to Cloud for more capacity.",
        ) from exc
    except Exception as exc:
        sub_agent_duration_ms = int(time.monotonic() * 1000) - start_ms
        log.warning(
            "artifact_draft_sub_agent_complete",
            trace_id=trace_id,
            session_id=session_id,
            span_id=span_id,
            task_id=task_id,
            success=False,
            duration_ms=sub_agent_duration_ms,
            error=str(exc),
        )
        raise ToolExecutionError(
            f"HTML generation sub-agent failed: {exc}. Use artifact_write directly as fallback."
        ) from exc

    sub_agent_duration_ms = int(time.monotonic() * 1000) - start_ms

    # --- Extract content from LLMResponse ---
    html_content: str = response.get("content", "") if isinstance(response, dict) else str(response)
    html_content = _strip_code_fences(html_content)

    # --- Output-token accounting + cap-hit detection (FRE-478) ---
    # ``usage`` is dict[str, Any] and may be empty / non-int when the provider
    # omits it, so guard the type before comparing against the cap.
    usage = response.get("usage") or {} if isinstance(response, dict) else {}
    output_tokens = usage.get("completion_tokens")

    log.info(
        "artifact_draft_sub_agent_complete",
        trace_id=trace_id,
        session_id=session_id,
        span_id=span_id,
        task_id=task_id,
        success=True,
        duration_ms=sub_agent_duration_ms,
        html_length=len(html_content),
        output_tokens=output_tokens,
    )

    if isinstance(output_tokens, int) and output_tokens >= draft_max_tokens:
        # The generation hit the configured ceiling — the artifact was likely
        # truncated and spilled into a continuation call (FRE-478). This is the
        # trip-wire for raising the cap or routing to structural sectioning
        # (FRE-476).
        log.warning(
            "artifact_draft_output_cap_hit",
            trace_id=trace_id,
            session_id=session_id,
            span_id=span_id,
            task_id=task_id,
            output_tokens=output_tokens,
            max_tokens=draft_max_tokens,
        )

    # --- Render Mermaid blocks → inline SVG (FRE-396, portability lane — ADR-0089 D7) ---
    html_content = await _render_mermaid_blocks(
        html_content, trace_id=trace_id, session_id=session_id
    )

    # --- Validate HTML output (ADR-0077 D9) — malformation only, never a security
    # decision on the bytes (ADR-0089 D1: the served-CSP envelope is the boundary).
    _validate_html_output(html_content)

    log.info(
        "artifact_draft_html_validated",
        trace_id=trace_id,
        session_id=session_id,
        html_length=len(html_content),
        has_doctype=True,
    )

    # --- Chain to artifact_write_executor (D3: direct call, no governance re-check) ---
    result = await artifact_write_executor(
        slug=slug,
        content_type="text/html; charset=utf-8",
        content=html_content,
        title=title,
        summary=summary,
        tags=tags,
        ctx=ctx,
        _commit_path="draft",
    )

    result["generation_method"] = "draft"
    result["sub_agent_duration_ms"] = sub_agent_duration_ms
    result["task_id"] = task_id
    result["plan_truncated"] = plan_truncated
    result["plan_original_length"] = plan_original_length

    log.info(
        "artifact_draft_completed",
        trace_id=trace_id,
        session_id=session_id,
        artifact_id=result.get("artifact_id"),
        slug=slug,
        size_bytes=result.get("size_bytes"),
        sub_agent_duration_ms=sub_agent_duration_ms,
        task_id=task_id,
    )

    return result
