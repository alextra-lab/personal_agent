"""Share links for anyone: the creator endpoints and the internal resolve (ADR-0153 Amendment 1).

This module is the only writer of ``artifact_share_links`` (L10). No tool, skill, MCP server or
sub-agent imports it; a structural test holds that.

* ``POST /api/v1/artifacts/{artifact_id}/share-links``, ``GET /api/v1/share-links`` and
  ``DELETE /api/v1/share-links/{link_id}`` need a verified main-app JWT (``get_request_user``,
  D11) and a link creator (L9). A creator shares only items that it owns, and lists and revokes
  only its own links.
* ``POST /internal/share-links/resolve`` is the Worker's call on every open (L6 steps 3 and 4). It
  takes ``X-Internal-Token`` and no JWT. One ``UPDATE ... RETURNING`` checks an active link and
  counts the view.

The token is shown once, in the create response. Postgres stores only its SHA-256 (L3). No log
event holds the token, its hash, or a request address (L3, L11).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, StrictInt, field_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.config import settings
from personal_agent.service.artifacts_router import _build_asset_fetcher, _verify_internal_token
from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.service.database import AsyncSessionLocal, get_db_session
from personal_agent.service.share_link_policy import (
    is_link_creator,
    is_shareable,
    new_token,
    safe_filename,
    served_content_type,
    token_hash_of,
)
from personal_agent.storage import ArtifactStoreError, get_artifact_store
from personal_agent.storage.artifact_export import (
    ArtifactExportError,
    export_artifact_html,
    load_substitution_map,
)
from personal_agent.telemetry.trace import SystemTraceContext

log = structlog.get_logger(__name__)

router = APIRouter(tags=["share-links"])

_EXPIRY_CHOICES: frozenset[int] = frozenset({1, 7, 30})
_NO_STORE = {"Cache-Control": "private, no-store"}
_NOT_SELF_CONTAINED = "this artifact loads files that a link cannot carry"
#: Fixed bodies, so a response names nothing about the link (L8).
_NOT_FOUND_BODY = "not found"
_INACTIVE_BODY = "this link is no longer active"
_MAX_RESOLVE_BODY_BYTES = 256


class ShareLinkCreateRequest(BaseModel):
    """Body of the create call. Only 1, 7 and 30 days are accepted (L4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expires_in_days: StrictInt = 7

    @field_validator("expires_in_days")
    @classmethod
    def _one_of_the_choices(cls, value: int) -> int:
        if value not in _EXPIRY_CHOICES:
            raise ValueError("expires_in_days must be 1, 7 or 30")
        return value


class ShareLinkCreated(BaseModel):
    """The create response. The only place that the URL, and so the token, ever appears."""

    model_config = ConfigDict(frozen=True)

    link_id: UUID
    url: str
    expires_at: datetime


class ShareLinkSummary(BaseModel):
    """One active link in the creator's list. No URL, token or hash."""

    model_config = ConfigDict(frozen=True)

    link_id: UUID
    artifact_id: UUID
    artifact_title: str | None
    created_at: datetime
    expires_at: datetime
    view_count: int
    last_viewed_at: datetime | None


class ShareLinkListResponse(BaseModel):
    """Response body for ``GET /api/v1/share-links``."""

    model_config = ConfigDict(frozen=True)

    items: list[ShareLinkSummary]


class ShareLinkResolved(BaseModel):
    """What the Worker needs to serve an open link (L6 step 4, L7)."""

    model_config = ConfigDict(frozen=True)

    r2_key: str
    content_type: str
    filename: str
    content_disposition: str


def require_link_creator(
    user: Annotated[RequestUser, Depends(get_request_user)],
) -> RequestUser:
    """FastAPI dependency: the verified caller, who must be a link creator (L9).

    It runs before any lookup and before the body is checked, so a 403 reveals nothing.

    Args:
        user: The verified request identity.

    Returns:
        The same identity.

    Raises:
        HTTPException: 403 when the caller is not a link creator.
    """
    if not is_link_creator(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user


async def _html_snapshot(*, r2_key: str, trace_id: str) -> str:
    """Run the export in ``inline`` mode and check that the result is self-contained (L5).

    Args:
        r2_key: R2 key of the artifact's HTML.
        trace_id: Trace id for the store's failure logs.

    Returns:
        The snapshot HTML.

    Raises:
        HTTPException: 503 when R2 is not configured, 502 when the read or the export fails, 422
            when the snapshot still names the artifact host.
    """
    store = get_artifact_store()
    if store is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    sub_map = load_substitution_map()
    try:
        raw = await store.get(r2_key, trace_id=trace_id)
        snapshot = await export_artifact_html(
            html=raw.decode("utf-8", errors="replace"),
            mode="inline",
            sub_map=sub_map,
            fetcher=_build_asset_fetcher(sub_map),
        )
    except (ArtifactStoreError, ArtifactExportError) as exc:
        log.warning(
            "share_link_snapshot_failed",
            error_class=type(exc).__name__,
            trace_id=trace_id,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY) from exc
    artifact_host = urlparse(sub_map.origin).netloc.lower()
    if artifact_host and artifact_host in snapshot.lower():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_NOT_SELF_CONTAINED
        )
    return snapshot


async def _link_row_committed(link_id: UUID) -> bool | None:
    """Return whether a link row exists, read on a new session, or ``None`` if the read fails."""
    try:
        async with AsyncSessionLocal() as check:
            found = (
                await check.execute(
                    text("SELECT 1 FROM artifact_share_links WHERE id = :link_id"),
                    {"link_id": link_id},
                )
            ).one_or_none()
    except SQLAlchemyError:
        return None
    return found is not None


async def _undo_failed_create(
    db: AsyncSession,
    *,
    link_id: UUID,
    snapshot_key: str | None,
    commit_sent: bool,
    trace_id: str,
) -> None:
    """Leave no row and no object after a failed create (L5, V-L13).

    The rollback and the delete run independently, so a failure of one does not skip the other.
    When the commit was sent, its outcome is unknown, so the row is read again on a new session:
    an object that a committed row names is kept, and so is the object when that read fails,
    because a row without its object would break a live link, while an object that no row
    names is served by nothing.

    Args:
        db: The request's session.
        link_id: The id of the link being created.
        snapshot_key: The snapshot's R2 key, or ``None`` for an upload.
        commit_sent: Whether the commit was attempted.
        trace_id: Trace id for the log lines.
    """
    try:
        await db.rollback()
    except SQLAlchemyError:
        log.warning("share_link_create_rollback_failed", trace_id=trace_id)
    if snapshot_key is None:
        return
    if commit_sent and await _link_row_committed(link_id) is not False:
        log.warning("share_link_create_commit_unknown", link_id=str(link_id), trace_id=trace_id)
        return
    store = get_artifact_store()
    if store is None:
        return
    try:
        await store.delete(snapshot_key, trace_id=trace_id)
    except ArtifactStoreError:
        # The store logs the failure. Nothing serves an object that no row names.
        log.warning("share_link_snapshot_cleanup_failed", link_id=str(link_id), trace_id=trace_id)


@router.post(
    "/api/v1/artifacts/{artifact_id}/share-links",
    response_model=ShareLinkCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["share-links"],
    responses={
        400: {"description": "This item cannot be shared by link."},
        401: {"description": "Not authenticated via CF Access."},
        403: {"description": "The caller is not a link creator."},
        404: {"description": "Artifact not found or not owned by the caller."},
        422: {"description": "Invalid expiry, or a snapshot that is not self-contained."},
        502: {"description": "The snapshot could not be made."},
        503: {"description": "Share links are not configured on this deployment."},
    },
)
async def create_share_link(
    artifact_id: UUID,
    user: Annotated[RequestUser, Depends(require_link_creator)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    body: Annotated[ShareLinkCreateRequest | None, Body()] = None,
) -> ShareLinkCreated:
    """Create a link to one item that the caller owns, and return its URL once (L3, L5, L9).

    Args:
        artifact_id: The item to share.
        user: The verified caller, a link creator.
        db: Database session.
        body: The expiry choice. No body means 7 days.

    Returns:
        The link id, the URL and the expiry.

    Raises:
        HTTPException: As listed in ``responses``.
    """
    ctx = SystemTraceContext.new("share_link_create")
    days = (body or ShareLinkCreateRequest()).expires_in_days

    row = (
        await db.execute(
            text(
                """
                SELECT id, type, slug, title, content_type, r2_key, upload_pending
                FROM artifacts
                WHERE id = :artifact_id AND user_id = :user_id
                """
            ),
            {"artifact_id": artifact_id, "user_id": user.user_id},
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not is_shareable(
        item_type=row.type, content_type=row.content_type, upload_pending=row.upload_pending
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="this item cannot be shared by link",
        )
    base_url = settings.share_public_base_url
    if not base_url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    link_id = uuid4()
    token = new_token()
    snapshot: str | None = None
    if row.type == "artifact":
        snapshot = await _html_snapshot(r2_key=row.r2_key, trace_id=ctx.trace_id)
    # The key is fixed before the put, so every failure after this line can remove the object,
    # including a put that wrote the object and then failed or was cancelled.
    snapshot_key = f"share/{user.user_id}/{link_id}.html" if snapshot is not None else None
    commit_sent = False
    committed = False
    try:
        if snapshot is not None and snapshot_key is not None:
            store = get_artifact_store()
            if store is None:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
            try:
                await store.put(
                    r2_key=snapshot_key,
                    content=snapshot.encode("utf-8"),
                    content_type="text/html; charset=utf-8",
                    trace_id=ctx.trace_id,
                )
            except ArtifactStoreError as exc:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY) from exc
        created = (
            await db.execute(
                text(
                    """
                    INSERT INTO artifact_share_links
                        (id, artifact_id, created_by, token_hash, snapshot_r2_key, expires_at)
                    VALUES
                        (:id, :artifact_id, :created_by, :token_hash, :snapshot_r2_key,
                         now() + make_interval(days => :days))
                    RETURNING expires_at
                    """
                ),
                {
                    "id": link_id,
                    "artifact_id": artifact_id,
                    "created_by": user.user_id,
                    "token_hash": token.token_hash,
                    "snapshot_r2_key": snapshot_key,
                    "days": days,
                },
            )
        ).one()
        commit_sent = True
        await db.commit()
        committed = True
    finally:
        if not committed:
            # Shielded: a cancelled request still runs its cleanup to the end.
            await asyncio.shield(
                _undo_failed_create(
                    db,
                    link_id=link_id,
                    snapshot_key=snapshot_key,
                    commit_sent=commit_sent,
                    trace_id=ctx.trace_id,
                )
            )

    log.info(
        "share_link_created",
        link_id=str(link_id),
        artifact_id=str(artifact_id),
        owner_user_id=str(user.user_id),
        expires_at=created.expires_at.isoformat(),
        trace_id=ctx.trace_id,
    )
    return ShareLinkCreated(
        link_id=link_id,
        url=f"{base_url.rstrip('/')}/s/{token.token}",
        expires_at=created.expires_at,
    )


@router.get(
    "/api/v1/share-links",
    response_model=ShareLinkListResponse,
    tags=["share-links"],
    responses={
        401: {"description": "Not authenticated via CF Access."},
        403: {"description": "The caller is not a link creator."},
    },
)
async def list_share_links(
    user: Annotated[RequestUser, Depends(require_link_creator)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ShareLinkListResponse:
    """List the caller's active links, newest first (L9).

    Args:
        user: The verified caller, a link creator.
        db: Database session.

    Returns:
        The caller's links that are neither revoked nor expired.
    """
    rows = (
        await db.execute(
            text(
                """
                SELECT l.id, l.artifact_id, a.title, l.created_at, l.expires_at,
                       l.view_count, l.last_viewed_at
                FROM artifact_share_links l
                JOIN artifacts a ON a.id = l.artifact_id
                WHERE l.created_by = :user_id
                  AND l.revoked_at IS NULL
                  AND l.expires_at > now()
                ORDER BY l.created_at DESC
                """
            ),
            {"user_id": user.user_id},
        )
    ).all()
    return ShareLinkListResponse(
        items=[
            ShareLinkSummary(
                link_id=r.id,
                artifact_id=r.artifact_id,
                artifact_title=r.title,
                created_at=r.created_at,
                expires_at=r.expires_at,
                view_count=r.view_count,
                last_viewed_at=r.last_viewed_at,
            )
            for r in rows
        ]
    )


@router.delete(
    "/api/v1/share-links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["share-links"],
    responses={
        401: {"description": "Not authenticated via CF Access."},
        403: {"description": "The caller is not a link creator."},
        404: {"description": "No link with this id belongs to the caller."},
    },
)
async def revoke_share_link(
    link_id: UUID,
    user: Annotated[RequestUser, Depends(require_link_creator)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    """Revoke one of the caller's links. A second revoke keeps the first time (L9).

    Args:
        link_id: The link to revoke.
        user: The verified caller, a link creator.
        db: Database session.

    Returns:
        An empty 204 response.

    Raises:
        HTTPException: 404 when the caller has no link with this id.
    """
    ctx = SystemTraceContext.new("share_link_revoke")
    params = {"link_id": link_id, "user_id": user.user_id}
    revoked = (
        await db.execute(
            text(
                """
                UPDATE artifact_share_links
                   SET revoked_at = now()
                 WHERE id = :link_id AND created_by = :user_id AND revoked_at IS NULL
                RETURNING artifact_id
                """
            ),
            params,
        )
    ).one_or_none()
    if revoked is None:
        exists = (
            await db.execute(
                text(
                    "SELECT 1 FROM artifact_share_links WHERE id = :link_id AND created_by = :user_id"
                ),
                params,
            )
        ).one_or_none()
        if exists is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    await db.commit()
    log.info(
        "share_link_revoked",
        link_id=str(link_id),
        artifact_id=str(revoked.artifact_id),
        owner_user_id=str(user.user_id),
        trace_id=ctx.trace_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _presented_token_hash(raw_body: bytes) -> bytes | None:
    """Return the hash of the token in a resolve body, or ``None`` for any other shape."""
    if len(raw_body) > _MAX_RESOLVE_BODY_BYTES:
        return None
    try:
        parsed = json.loads(raw_body)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or set(parsed) != {"token"}:
        return None
    token = parsed["token"]
    return token_hash_of(token) if isinstance(token, str) else None


def _refused(
    outcome: Literal["unknown", "inactive"], *, link_id: UUID | None, trace_id: str
) -> HTTPException:
    """Log a refused open and build its fixed response (L8, L11)."""
    if outcome == "inactive":
        log.info("share_link_refused", outcome=outcome, link_id=str(link_id), trace_id=trace_id)
        return HTTPException(
            status_code=status.HTTP_410_GONE, detail=_INACTIVE_BODY, headers=_NO_STORE
        )
    log.info("share_link_refused", outcome=outcome, trace_id=trace_id)
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_BODY, headers=_NO_STORE
    )


@router.post(
    "/internal/share-links/resolve",
    response_model=ShareLinkResolved,
    tags=["share-links-internal"],
    dependencies=[Depends(_verify_internal_token)],
    responses={
        401: {"description": "Missing or wrong X-Internal-Token."},
        404: {"description": "No link has this token."},
        410: {"description": "The link expired or was revoked."},
    },
)
async def resolve_share_link(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ShareLinkResolved:
    """Check an open of a link and count it, in one statement (L6 steps 3 and 4, L11).

    No JWT: the token is the authority. ``X-Internal-Token`` only filters callers that are not
    the Worker. This endpoint reads no request header other than that one, and logs no address.

    The body is read here, not by a model: a validation error echoes its input, and the input is
    the token. Every malformed body gets the same 404 as an unknown token (L8).

    Args:
        request: The request, whose JSON body is ``{"token": "<token>"}``.
        response: The outgoing response, for its cache header.
        db: Database session.

    Returns:
        The R2 key, the content type and the safe filename.

    Raises:
        HTTPException: 404 for an unknown or malformed token, 410 for an expired or revoked link.
    """
    ctx = SystemTraceContext.new("share_link_resolve")
    token_hash = _presented_token_hash(await request.body())
    if token_hash is None:
        raise _refused("unknown", link_id=None, trace_id=ctx.trace_id)

    opened = (
        await db.execute(
            text(
                """
                UPDATE artifact_share_links AS l
                   SET view_count = l.view_count + 1,
                       last_viewed_at = now()
                  FROM artifacts AS a
                 WHERE l.token_hash = :token_hash
                   AND l.revoked_at IS NULL
                   AND l.expires_at > now()
                   AND a.id = l.artifact_id
                   AND a.upload_pending = FALSE
                RETURNING l.id, l.artifact_id, l.snapshot_r2_key,
                          a.r2_key, a.content_type, a.slug, a.title
                """
            ),
            {"token_hash": token_hash},
        )
    ).one_or_none()
    if opened is None:
        known = (
            await db.execute(
                text("SELECT id FROM artifact_share_links WHERE token_hash = :token_hash"),
                {"token_hash": token_hash},
            )
        ).one_or_none()
        if known is None:
            raise _refused("unknown", link_id=None, trace_id=ctx.trace_id)
        raise _refused("inactive", link_id=known.id, trace_id=ctx.trace_id)

    await db.commit()
    is_snapshot = opened.snapshot_r2_key is not None
    content_type = served_content_type(content_type=opened.content_type, is_snapshot=is_snapshot)
    filename = safe_filename(slug=opened.slug, title=opened.title, content_type=content_type)
    log.info(
        "share_link_opened",
        link_id=str(opened.id),
        artifact_id=str(opened.artifact_id),
        trace_id=ctx.trace_id,
    )
    response.headers.update(_NO_STORE)
    return ShareLinkResolved(
        r2_key=opened.snapshot_r2_key if is_snapshot else opened.r2_key,
        content_type=content_type,
        filename=filename,
        content_disposition=f'inline; filename="{filename}"',
    )
