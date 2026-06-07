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
import re  # noqa: F401 — used by _SCRIPT_TAG_RE / _EVENT_HANDLER_RE at module level
import tempfile
from collections.abc import Sequence
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
from personal_agent.tools.executor import TerminalToolError, ToolExecutionError
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
    _gate_decision: str | None = None,
    _gate_violations: tuple[int, int, int] | None = None,
    _commit_path: str = "direct_write",
) -> dict[str, Any]:
    """Write a human-facing artifact to R2 and record the Postgres row.

    This is intentionally an **ungated** commit path. Per ADR-0089 D1, security is a
    property of the served-CSP envelope (D2/D3, FRE-509), not of inspecting the bytes —
    so this function does not strip or reject HTML. The ``_gate_*`` parameters are
    internal-only (not in the tool schema): ``artifact_draft`` passes the decision it
    computed so the single per-commit ``artifact_gate_decision`` event (FRE-506) reflects
    the gated path; a direct caller leaves them unset and the commit is labelled
    ``bypassed`` (HTML) or ``not_applicable`` (other types) for visibility.

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

    # FRE-506: one sandbox gate-decision label per commit. The gated draft path passes
    # its decision via _gate_decision; a direct HTML write ran no gate (bypassed); a
    # non-HTML write has no sandbox gate (not_applicable). Visibility only — the served
    # CSP envelope is the boundary (ADR-0089 D1/D5).
    if _gate_decision is not None:
        decision = _gate_decision
        violations = _gate_violations or (0, 0, 0)
        commit_path = _commit_path
        gate_ran = True
    elif content_type == _HTML_CONTENT_TYPE:
        decision = "bypassed"
        violations = _count_sandbox_violations(content)
        commit_path = "direct_write"
        gate_ran = False
    else:
        decision = "not_applicable"
        violations = (0, 0, 0)
        commit_path = "direct_write"
        gate_ran = False

    _emit_gate_decision(
        trace_id=trace_id,
        session_id=str(session_id) if session_id is not None else None,
        user_id=user_id,
        artifact_id=artifact_id,
        slug=slug,
        content_type=content_type,
        size_bytes=size_bytes,
        decision=decision,
        commit_path=commit_path,
        gate_ran=gate_ran,
        violations=violations,
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
    from personal_agent.config.model_loader import ModelConfigError, load_model_config

    try:
        primary = load_model_config().models.get("primary")
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


# Detectors (used by _validate_html_output's terminal safety net AND as the strip gate).
# INVARIANT (FRE-496 review): strip ⊇ detect — every input these flag must be removable by
# the sanitizer below, or sanitized HTML re-trips the validator and the turn hard-fails. The
# event-handler detector is pinned to a real attribute boundary (^ / whitespace / quote /
# slash) so it neither (a) misses a glued `"onclick=` nor (b) false-positives on `data-on*`.
_SCRIPT_TAG_RE = re.compile(r"<\s*script", re.IGNORECASE)
_EVENT_HANDLER_RE = re.compile(r"""(?:^|[\s"'/])on\w+\s*=""", re.IGNORECASE)
# FRE-496: sanitizer regexes — strip sandbox-prohibited nodes so the artifact can still ship
# (graceful degradation) instead of hard-failing the turn (ADR-0070 D7). Order of application
# matters: BLOCK first (removes <script>…JS…</script> whole, so no dangling JS body text),
# then OPEN (self-closing / src= / stray openers, incl. an UNTERMINATED tail with no `>` —
# the FRE-478 cap-hit truncation case), then CLOSE (orphan closing tags). The CDN regex also
# matches unquoted href= values.
# Close-tag patterns use `</\s*script\b[^>]*>` (not `</\s*script\s*>`): HTML treats
# `</script bar>` / `</script\t\n>` as valid end tags, so the close pattern must tolerate
# whitespace/attributes before `>` or a block closed that way slips the strip (CWE-116,
# CodeQL py/bad-tag-filter). `\b` still pins the tag name to exactly "script".
_SCRIPT_BLOCK_RE = re.compile(
    r"<\s*script\b[^>]*>.*?</\s*script\b[^>]*>", re.IGNORECASE | re.DOTALL
)
# `(?:>|$)` so a draft truncated mid-tag (`…<script src="https://x/a.js` with no `>`) is also
# stripped — otherwise _SCRIPT_TAG_RE flags it but nothing removes it → hard-fail.
_SCRIPT_OPEN_RE = re.compile(r"<\s*script\b[^>]*(?:>|$)", re.IGNORECASE)
_SCRIPT_CLOSE_RE = re.compile(r"</\s*script\b[^>]*>", re.IGNORECASE)
# Group 1 captures the leading boundary char (start / whitespace / quote / slash) so it can be
# restored — this lets the strip match a handler glued to the prior attribute
# (`type="checkbox"onclick="t()"`) without eating the closing quote. Value group tolerates
# quoted, unquoted, and empty (`onclick=>`) forms so strip ⊇ detect holds.
_EVENT_HANDLER_ATTR_RE = re.compile(
    r"""(^|[\s"'/])on\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]*)""", re.IGNORECASE
)
_CDN_LINK_RE = re.compile(
    r'<\s*link\b[^>]*\bhref\s*=\s*["\']?https?://[^"\'>\s]*["\']?[^>]*>', re.IGNORECASE
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
    """Count sandbox-relevant constructs for the gate-decision label.

    Reuses the gate's own detectors so the counts match what the gate would act on.
    This is an analytics label only (ADR-0089 D1/D5) — nothing depends on it for
    safety; the served-CSP envelope is the boundary (D2/D3).

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
    gate_ran: bool,
    violations: tuple[int, int, int],
) -> None:
    """Emit the per-commit sandbox gate-decision label (FRE-506, ADR-0089 D5).

    An observation, never a security verdict: the served-CSP envelope is the boundary
    (ADR-0089 D2/D3, served by the Worker — FRE-509). A ``bypassed`` decision is the
    alarm signal that a commit path ran no content gate — the failure mode that took
    manual forensics to find on trace ``87cbd720``.

    Args:
        trace_id: Caller trace id.
        session_id: Caller session id, or None.
        user_id: Owning user id, or None.
        artifact_id: Committed artifact id, or None when no commit occurred (rejected).
        slug: Artifact slug.
        content_type: Committed MIME type.
        size_bytes: Committed byte length.
        decision: One of enforced_pass / stripped / rejected / bypassed / not_applicable.
        commit_path: ``draft`` or ``direct_write``.
        gate_ran: Whether a content gate ran on this commit path.
        violations: ``(script_count, handler_count, cdn_count)`` label.
    """
    script_count, handler_count, cdn_count = violations
    log.info(
        "artifact_gate_decision",
        trace_id=trace_id,
        session_id=session_id,
        user_id=str(user_id) if user_id is not None else None,
        # rejected path has no artifact_id yet → emit null, never the literal "None"
        # (codex review: downstream presence filters must not match a string).
        artifact_id=str(artifact_id) if artifact_id is not None else None,
        slug=slug,
        content_type=content_type,
        size_bytes=size_bytes,
        gate_decision=decision,
        commit_path=commit_path,
        gate_ran=gate_ran,
        script_count=script_count,
        handler_count=handler_count,
        cdn_count=cdn_count,
    )


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
  * No external CDN links — the document must be fully self-contained.
- INTERACTIVITY: this document renders in a JavaScript-free sandboxed iframe. \
Make it interactive with CSS only — :hover, :focus, :target (anchor-driven \
tabs/accordions), <details>/<summary>, the checkbox-hack (a hidden \
<input type="checkbox"> + <label> + sibling/general selectors), CSS \
transitions and animations, and scroll-snap. These are the ONLY interactivity \
mechanisms available; design the whole experience around them.
- SECURITY (hard constraint): any <script> tag — inline OR external (src=) — \
causes the artifact to be REJECTED and the user receives nothing. The same \
applies to inline event handlers (onclick, onload, onerror, onmouseover, etc.) \
and to remote/CDN frameworks (Tailwind CDN, Alpine.js, htmx, jQuery, etc.). \
Inline ALL CSS in the <style> block and load NO external resources. There is \
no exception — JavaScript cannot run here, so use the CSS-only patterns above.
- For diagrams and flowcharts, use <pre class="mermaid">…</pre> markup \
with Mermaid syntax — the server renders these to static inline SVG \
automatically. Never use <script>, CDN URLs, or the mermaid.js library. \
Example: <pre class="mermaid">graph LR; A[Start] --> B[End];</pre>
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


def _sanitize_sandbox_violations(html: str) -> tuple[str, list[str]]:
    """Strip sandbox-prohibited nodes (ADR-0070 D7) so the artifact can still ship.

    Graceful-degradation fallback for FRE-496: rather than hard-failing the whole turn
    when the model emits JavaScript for an "interactive" request, strip the offending
    nodes and deliver a static artifact. Best-effort regex pass mirroring
    :func:`_mermaid_fallback`; the last-resort guard remains :func:`_validate_html_output`.

    Removes, in order: ``<script>…</script>`` blocks (so no dangling JS body text), stray
    or self-closing ``<script>`` openers, orphan ``</script>`` closers, inline
    event-handler attributes (``onclick=…`` etc.), and external CDN ``<link href="http…">``
    references.

    Args:
        html: Generated HTML document text.

    Returns:
        ``(sanitized_html, notes)`` where ``notes`` lists human-readable descriptions of
        what was stripped (empty when the input was already clean). Note strings contain
        no literal ``<script`` token or ``onX=`` attribute so the re-validated banner that
        carries them does not self-trip :func:`_validate_html_output`.
    """
    notes: list[str] = []
    sanitized = html
    if _SCRIPT_TAG_RE.search(sanitized):
        sanitized = _SCRIPT_BLOCK_RE.sub("", sanitized)
        sanitized = _SCRIPT_OPEN_RE.sub("", sanitized)
        sanitized = _SCRIPT_CLOSE_RE.sub("", sanitized)
        notes.append("Removed embedded scripts — JavaScript cannot run in this sandbox.")
    if _EVENT_HANDLER_RE.search(sanitized):
        # Restore the captured boundary char (group 1) so a glued `"onclick=…` loses only
        # the handler, not the preceding attribute's closing quote.
        sanitized = _EVENT_HANDLER_ATTR_RE.sub(r"\1", sanitized)
        notes.append("Removed inline event-handler attributes.")
    if _CDN_LINK_RE.search(sanitized):
        sanitized = _CDN_LINK_RE.sub("", sanitized)
        notes.append("Removed external CDN resources — the artifact must be self-contained.")
    return sanitized, notes


def _inject_sanitization_banner(html: str, notes: Sequence[str]) -> str:
    """Insert a static banner listing what :func:`_sanitize_sandbox_violations` stripped.

    Mirrors the in-document mermaid fallback caption so the human sees why the artifact
    differs from the request. The banner uses an inline ``style=`` attribute only (no
    ``<script>``, no ``on*=``) so it passes :func:`_validate_html_output`.

    Args:
        html: Sanitized HTML document text.
        notes: Human-readable strings describing the removals (our own static text — no
            user data — so it is safe to inline without escaping).

    Returns:
        The HTML with a ``<aside class="artifact-sanitization-note">`` inserted after the
        opening ``<body>`` (else after ``<html>``, else after the doctype). If no anchor
        is found the input is returned unchanged (the notes still ride in the result dict).
    """
    if not notes:
        return html
    items = "".join(f"<li>{note}</li>" for note in notes)
    banner = (
        '<aside class="artifact-sanitization-note" style="margin:0 0 1rem;padding:0.75rem 1rem;'
        "border-left:4px solid #b45309;background:#fffbeb;color:#7c2d12;font-size:0.875rem;"
        'border-radius:0.25rem;">'
        "<strong>Note:</strong> this artifact was adjusted to run in a JavaScript-free sandbox."
        f'<ul style="margin:0.5rem 0 0;padding-left:1.25rem;">{items}</ul></aside>'
    )
    for anchor in (
        re.compile(r"<\s*body\b[^>]*>", re.IGNORECASE),
        re.compile(r"<\s*html\b[^>]*>", re.IGNORECASE),
        re.compile(r"<!doctype html>", re.IGNORECASE),
    ):
        match = anchor.search(html)
        if match:
            return html[: match.end()] + banner + html[match.end() :]
    return html


def _css_only_retry_reminder(violation_notes: Sequence[str]) -> str:
    """Build the bounded-retry instruction appended to the user prompt (FRE-496).

    Names the violation that triggered the retry and demands a CSS-only regeneration. The
    previous (large) HTML is deliberately NOT echoed back, so attempt 2 costs roughly the
    same as attempt 1 rather than double.

    Args:
        violation_notes: Notes from :func:`_sanitize_sandbox_violations` describing what
            the previous attempt emitted.

    Returns:
        A reminder string to append to the user message of the retry call.
    """
    violations = " / ".join(violation_notes) if violation_notes else "sandbox-prohibited content"
    return (
        f"The previous attempt was rejected ({violations}). JavaScript, inline event "
        "handlers, and external/CDN resources CANNOT run in this sandbox. Regenerate the "
        "COMPLETE document with CSS-only interactivity (:hover, :focus, :target, "
        "<details>/<summary>, the checkbox-hack, CSS transitions/animations). No <script>, "
        "no on* handlers, no external resources. Output only the HTML document."
    )


def _validate_html_output(html: str) -> None:
    """Validate sub-agent HTML before persisting (ADR-0077 D9, ADR-0070 D7).

    Sandbox-violating nodes are normally stripped first by
    :func:`_sanitize_sandbox_violations` (FRE-496 graceful degradation); the terminal
    raises below are the defense-in-depth safety net for anything the stripper misses
    (e.g. a literal ``onclick=`` token in body text) — we never ship a script to the
    sandbox.

    Raises:
        TerminalToolError: On a sandbox violation (script tags or inline event
            handlers) the stripper did not clear — non-recoverable (FRE-402).
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
    # FRE-402: sandbox violations are non-recoverable — the local model reliably
    # re-emits scripts when asked for interactivity, so a retry just burns another
    # slow reasoning + generation round-trip. Mark terminal so the turn short-circuits
    # instead of looping back through the model. (Malformation cases above stay
    # recoverable ToolExecutionError — truncated output can succeed on retry.)
    if _SCRIPT_TAG_RE.search(html):
        raise TerminalToolError(
            "Generated HTML contains <script> tags, which are prohibited (ADR-0070 D7 sandbox).",
            reason="The generated page used scripts, which the artifact sandbox blocks.",
            next_step=(
                "Ask for a static or CSS-only version (no JavaScript or CDN frameworks "
                "like Tailwind), or switch to Cloud."
            ),
        )
    if _EVENT_HANDLER_RE.search(html):
        raise TerminalToolError(
            "Generated HTML contains inline event handlers (onclick, onload, etc.), "
            "which are prohibited (ADR-0070 D7 sandbox).",
            reason="The generated page used inline event handlers, which the artifact sandbox blocks.",
            next_step=("Ask for a static or CSS-only version (no JavaScript), or switch to Cloud."),
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
    thinking disabled) generates the HTML. If the first draft emits sandbox-prohibited
    nodes (script / inline handlers / external resources) the sub-agent is retried once
    with strengthened CSS-only guidance (FRE-496); any residue is then stripped and the
    static artifact is delivered with a banner note rather than hard-failing the turn.
    The result is validated and persisted via ``artifact_write_executor``.

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
        ``sub_agent_duration_ms``, ``task_id``, ``plan_truncated``,
        ``plan_original_length``, ``sanitization_notes`` (list of strings describing any
        stripped sandbox violations; empty when none), and ``sub_agent_attempts`` (1 or 2).
        An oversized plan is truncated-with-warning rather than rejected (FRE-471).

    Raises:
        TerminalToolError: On a first-pass sub-agent timeout (FRE-402), or if the final
            HTML still violates the sandbox after stripping (defense-in-depth safety net).
        ToolExecutionError: On missing identity, empty plan, first-pass sub-agent failure,
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

    # --- Acquire sub-agent client (profile-driven: D2) ---
    sub_agent_client = get_llm_client(role_name="sub_agent")

    user_prompt = (
        f"Title: {title}\n"
        f"Summary: {summary}\n\n"
        f"Plan:\n{effective_plan}\n\n"
        "Generate the complete HTML document now."
    )

    base_messages: list[dict[str, Any]] = [
        {"role": "system", "content": _HTML_GENERATION_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    draft_timeout = _draft_timeout_s()
    draft_max_tokens = _draft_max_tokens()

    # --- Sub-agent inference with one bounded CSS-only retry (FRE-496) ---
    # Attempt 1 generates from the plan. If it emits sandbox-prohibited nodes (script,
    # inline handlers, external/CDN resources) we retry once with strengthened CSS-only
    # guidance that names the violation. A clean retry ships as-is; a still-violating or
    # failed retry falls through to strip-and-deliver below. Net: at most two sub-agent
    # calls, and the user always gets an artifact rather than a hard-failed turn.
    max_attempts = 2
    html_content = ""
    violation_notes: list[str] = []
    total_sub_agent_ms = 0
    attempts_made = 0
    retry_triggered = False

    for attempt in range(1, max_attempts + 1):
        # Count the attempt at call time so a retry that times out / errors (and breaks
        # below to the prior draft) still reports the true sub-agent call count.
        attempts_made = attempt
        messages = base_messages
        if attempt > 1:
            messages = [
                base_messages[0],
                {
                    "role": "user",
                    "content": f"{user_prompt}\n\n{_css_only_retry_reminder(violation_notes)}",
                },
            ]

        log.info(
            "artifact_draft_sub_agent_start",
            trace_id=trace_id,
            session_id=session_id,
            span_id=span_id,
            task_id=task_id,
            model_role=ModelRole.SUB_AGENT.value,
            max_tokens=draft_max_tokens,
            timeout_s=draft_timeout,
            attempt=attempt,
        )

        start_ms = int(time.monotonic() * 1000)
        try:
            response = await asyncio.wait_for(
                sub_agent_client.respond(
                    role=ModelRole.SUB_AGENT,
                    messages=messages,
                    max_tokens=draft_max_tokens,
                    trace_ctx=child_ctx,
                    timeout_s=draft_timeout,
                ),
                timeout=draft_timeout,
            )
        except asyncio.TimeoutError as exc:
            total_sub_agent_ms += int(time.monotonic() * 1000) - start_ms
            log.warning(
                "artifact_draft_sub_agent_complete",
                trace_id=trace_id,
                session_id=session_id,
                span_id=span_id,
                task_id=task_id,
                success=False,
                duration_ms=total_sub_agent_ms,
                error="timeout",
                attempt=attempt,
            )
            if attempt == 1:
                # FRE-402: a first-pass timeout is non-recoverable — surface immediately
                # instead of spending a full primary-model call to explain the failure.
                raise TerminalToolError(
                    f"HTML generation sub-agent timed out after {draft_timeout}s.",
                    reason="The artifact generator timed out — the document was too complex to build in time.",
                    next_step="Try a simpler artifact, or switch to Cloud for more capacity.",
                ) from exc
            # FRE-496: a failed retry still leaves the attempt-1 draft — degrade to it.
            log.warning(
                "artifact_draft_retry_failed_using_prior",
                trace_id=trace_id,
                session_id=session_id,
                span_id=span_id,
                task_id=task_id,
                error="timeout",
            )
            break
        except Exception as exc:
            total_sub_agent_ms += int(time.monotonic() * 1000) - start_ms
            log.warning(
                "artifact_draft_sub_agent_complete",
                trace_id=trace_id,
                session_id=session_id,
                span_id=span_id,
                task_id=task_id,
                success=False,
                duration_ms=total_sub_agent_ms,
                error=str(exc),
                attempt=attempt,
            )
            if attempt == 1:
                raise ToolExecutionError(
                    f"HTML generation sub-agent failed: {exc}. Use artifact_write directly as fallback."
                ) from exc
            log.warning(
                "artifact_draft_retry_failed_using_prior",
                trace_id=trace_id,
                session_id=session_id,
                span_id=span_id,
                task_id=task_id,
                error=str(exc),
            )
            break

        total_sub_agent_ms += int(time.monotonic() * 1000) - start_ms

        # --- Extract content from LLMResponse ---
        candidate: str = (
            response.get("content", "") if isinstance(response, dict) else str(response)
        )
        candidate = _strip_code_fences(candidate)

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
            duration_ms=total_sub_agent_ms,
            html_length=len(candidate),
            output_tokens=output_tokens,
            attempt=attempt,
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

        html_content = candidate
        _, violation_notes = _sanitize_sandbox_violations(candidate)
        if not violation_notes:
            break  # clean output — no retry needed
        if attempt < max_attempts:
            # FRE-496: sandbox violation → one bounded CSS-only retry before degrading.
            retry_triggered = True
            log.warning(
                "artifact_draft_sandbox_retry",
                trace_id=trace_id,
                session_id=session_id,
                span_id=span_id,
                task_id=task_id,
                attempt=attempt,
                violation_notes=violation_notes,
            )
        # else: retries exhausted — html_content keeps the last (violating) draft; the
        # strip-and-deliver pass below sanitizes it.

    sub_agent_duration_ms = total_sub_agent_ms

    # --- Render Mermaid blocks → inline SVG (FRE-396, ADR-0070 D7 amendment) ---
    html_content = await _render_mermaid_blocks(
        html_content, trace_id=trace_id, session_id=session_id
    )

    # FRE-506: capture the violation counts the gate is about to act on, for the
    # gate-decision label (non-load-bearing — ADR-0089 D1/D5).
    pre_strip_violations = _count_sandbox_violations(html_content)

    # --- Strip-and-deliver: sanitize any sandbox residue the retry could not clear (FRE-496) ---
    # Deliberate steering: _HTML_GENERATION_SYSTEM_PROMPT tells the model scripts are
    # "rejected", but here we strip the residue and ship a static artifact with a
    # user-visible banner rather than hard-failing the turn (ADR-0070 D7 graceful path).
    html_content, sanitize_notes = _sanitize_sandbox_violations(html_content)
    if sanitize_notes:
        html_content = _inject_sanitization_banner(html_content, sanitize_notes)
        log.warning(
            "artifact_draft_sanitized_sandbox_violations",
            trace_id=trace_id,
            session_id=session_id,
            span_id=span_id,
            task_id=task_id,
            notes=sanitize_notes,
            retry_triggered=retry_triggered,
        )

    # --- Validate HTML output (ADR-0077 D9, ADR-0070 D7) — safety net after sanitize ---
    # FRE-506: a sandbox TerminalToolError here is the gate's `rejected` decision — emit
    # it before propagating, since no commit (and thus no artifact_id) occurs on this path.
    try:
        _validate_html_output(html_content)
    except TerminalToolError:
        _emit_gate_decision(
            trace_id=trace_id,
            session_id=session_id,
            user_id=getattr(ctx, "user_id", None) if ctx else None,
            artifact_id=None,
            slug=slug,
            content_type=_HTML_CONTENT_TYPE,
            size_bytes=len(html_content.encode("utf-8")),
            decision="rejected",
            commit_path="draft",
            gate_ran=True,
            violations=pre_strip_violations,
        )
        raise

    log.info(
        "artifact_draft_html_validated",
        trace_id=trace_id,
        session_id=session_id,
        html_length=len(html_content),
        has_doctype=True,
    )

    # --- Chain to artifact_write_executor (D3: direct call, no governance re-check) ---
    # FRE-506: pass the gate decision so the single per-commit gate-decision event reflects
    # the gated draft path (enforced_pass when nothing was stripped, else stripped).
    result = await artifact_write_executor(
        slug=slug,
        content_type="text/html; charset=utf-8",
        content=html_content,
        title=title,
        summary=summary,
        tags=tags,
        ctx=ctx,
        _gate_decision="stripped" if sanitize_notes else "enforced_pass",
        _gate_violations=pre_strip_violations,
        _commit_path="draft",
    )

    result["generation_method"] = "draft"
    result["sub_agent_duration_ms"] = sub_agent_duration_ms
    result["task_id"] = task_id
    result["plan_truncated"] = plan_truncated
    result["plan_original_length"] = plan_original_length
    result["sanitization_notes"] = sanitize_notes
    result["sub_agent_attempts"] = attempts_made

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
