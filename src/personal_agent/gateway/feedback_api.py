"""Per-turn user value rating endpoint (FRE-407).

POST /turns/{trace_id}/rating — record a 0–3 value score for an assistant
turn.  The turn is identified by ``trace_id``; one Elasticsearch document per
turn is maintained (re-rate overwrites via ``doc_id=trace_id``).

An append-only NDJSON audit log is written to
``telemetry/user_feedback/{YYYY-MM-DD}.ndjson`` — it is NOT an aggregation
source; re-rates intentionally leave multiple lines.

Ownership enforcement (decision §5):
  - Caller's CF user is resolved; ``session_id`` must be owned by that user.
  - The ``trace_id``'s model_call_completed.session_id is verified against the
    supplied ``session_id`` — prevents cross-user trace attribution.
  - Any mismatch → 404 (do not leak existence).

Prompt-identity join (decision §2/§6):
  - Query agent-logs-* for model_call_completed rows for the trace.
  - Prefer ``orchestrator.primary`` → ``role.primary`` → ``gateway.chat``
    → most-recent any callsite.
  - On ES miss, wait 2 s and retry once (handles the ~5 s refresh_interval
    race). Still-missing identity → store with null fields; Insights consumer
    joins at read time and treats that as authoritative.

Bus event (decision §3):
  - Published to ``STREAM_USER_TURN_RATED`` **only** when the rating value
    changes. Re-rating to the same score → no event. Best-effort; swallowed
    on Redis down.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.captains_log.es_indexer import schedule_es_index
from personal_agent.gateway.auth import TokenInfo, require_scope
from personal_agent.gateway.errors import not_found, service_unavailable
from personal_agent.gateway.feedback_models import UserTurnRating
from personal_agent.service.auth import _CF_EMAIL_HEADER, _get_user_with_display_name
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.telemetry.trace import SystemTraceContext

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/turns", tags=["feedback"])

# Callsite preference order for prompt-identity resolution (decision §2).
_CALLSITE_PREFERENCE = [
    "orchestrator.primary",
    "role.primary",
    "gateway.chat",
]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RatingRequest(BaseModel):
    """Body for POST /turns/{trace_id}/rating.

    Attributes:
        rating: User value score — 0 (no value) through 3 (wow).
        session_id: Session the rated turn belongs to.
    """

    model_config = ConfigDict(frozen=True)

    rating: int
    session_id: str


# ---------------------------------------------------------------------------
# DB session dependency (mirrors session_api.py pattern)
# ---------------------------------------------------------------------------


async def _get_db(
    request: Request,
) -> AsyncGenerator[AsyncSession, None]:
    """Provide an async database session from the app-state factory.

    Args:
        request: Incoming FastAPI request.

    Yields:
        Async SQLAlchemy session.

    Raises:
        HTTPException(503): When no session factory is attached.
    """
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise service_unavailable("Database session factory is not available")

    async with factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------


@router.post("/{trace_id}/rating")
async def submit_turn_rating(
    request: Request,
    trace_id: str,
    body: RatingRequest,
    token: TokenInfo = Depends(require_scope("feedback:write")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> JSONResponse:
    """Record a 0–3 value rating for an assistant turn (FRE-407).

    The rating is stored as a single Elasticsearch document keyed on
    ``trace_id`` — re-rating overwrites the existing doc (idempotent per
    turn).  An append-only NDJSON audit log is also written locally.

    The bus event ``user.turn_rated`` is emitted only when the rating value
    *changes* from any previously stored value, preventing double-counting.

    Args:
        request: FastAPI request (injected).
        trace_id: UUID of the assistant turn being rated.
        body: ``RatingRequest`` with ``rating`` (0–3) and ``session_id``.
        token: Validated bearer token with ``feedback:write`` scope.
        db: Async SQLAlchemy session (injected).

    Returns:
        ``{"status": "received"}`` on success.

    Raises:
        HTTPException(400): When rating is outside 0–3.
        HTTPException(401): When CF Access header is absent.
        HTTPException(404): When ``session_id`` is not owned by the caller
            or the ``trace_id`` is not associated with that session.
    """
    # --- Validate rating range first (cheap — before any DB/ES work) ---
    if not (0 <= body.rating <= 3):
        log.warning(
            "feedback_rating_out_of_range",
            rating=body.rating,
            trace_id=trace_id,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_parameter",
                "message": "rating must be an integer in [0, 3]",
                "status": 400,
            },
        )

    ctx = SystemTraceContext.new("feedback_api")

    # --- Resolve caller identity ---
    email = request.headers.get(_CF_EMAIL_HEADER)
    if not email:
        raise HTTPException(
            status_code=401,
            detail="Authentication required (missing CF Access user header)",
        )
    user_id, _ = await _get_user_with_display_name(db, email)
    user_uuid = UUID(str(user_id))

    log.info(
        "feedback_rating_received",
        trace_id=trace_id,
        rating=body.rating,
        session_id=body.session_id,
        user_id=str(user_uuid),
        token_name=token.name,
        request_trace_id=ctx.trace_id,
    )

    # --- Ownership: assert session is owned by this user (decision §5) ---
    repo = SessionRepository(db)
    try:
        session_uuid = UUID(body.session_id)
    except ValueError:
        raise not_found("turn") from None

    session = await repo.get(session_uuid, user_id=user_uuid)
    if session is None:
        # 404, not 403 — do not reveal existence of other users' sessions
        log.warning(
            "feedback_rating_session_not_owned",
            trace_id=trace_id,
            session_id=body.session_id,
            user_id=str(user_uuid),
            request_trace_id=ctx.trace_id,
        )
        raise not_found("turn")

    # --- Ownership: verify trace_id belongs to the owned session (decision §5) ---
    es_client = getattr(request.app.state, "es_client", None)
    trace_owned = await _verify_trace_session_ownership(
        trace_id=trace_id,
        session_id=body.session_id,
        es_client=es_client,
        ctx_trace_id=ctx.trace_id,
    )
    if not trace_owned:
        log.warning(
            "feedback_rating_trace_not_in_session",
            trace_id=trace_id,
            session_id=body.session_id,
            user_id=str(user_uuid),
            request_trace_id=ctx.trace_id,
        )
        raise not_found("turn")

    # --- Resolve prompt identity (decision §2/§6) — best-effort with one retry ---
    identity = await _lookup_prompt_identity(
        trace_id=trace_id,
        session_id=body.session_id,
        es_client=es_client,
        ctx_trace_id=ctx.trace_id,
    )

    # --- Check for existing rating (re-rate semantics) ---
    existing = await _get_existing_rating(
        trace_id=trace_id,
        es_client=es_client,
        ctx_trace_id=ctx.trace_id,
    )
    existing_rating: int | None = None
    if existing is not None:
        existing_rating = existing.get("rating")

    # --- Build rating record ---
    now = datetime.now(timezone.utc)
    component_ids: tuple[str, ...] = ()
    if identity:
        raw_ids = identity.get("prompt_component_ids") or []
        component_ids = tuple(str(c) for c in raw_ids)

    turn_rating = UserTurnRating(
        trace_id=trace_id,
        session_id=body.session_id,
        rating=body.rating,
        prompt_callsite=identity.get("prompt_callsite") if identity else None,
        prompt_static_prefix_hash=identity.get("prompt_static_prefix_hash") if identity else None,
        prompt_dynamic_hash=identity.get("prompt_dynamic_hash") if identity else None,
        prompt_component_ids=component_ids,
        rated_at=now,
    )

    # --- Persist: ES (source of truth, idempotent overwrite) ---
    index_name = f"user-turn-ratings-{now.strftime('%Y.%m.%d')}"
    es_doc = turn_rating.to_es_doc()
    schedule_es_index(index_name, es_doc, doc_id=trace_id)
    log.info(
        "feedback_rating_es_scheduled",
        trace_id=trace_id,
        index=index_name,
        rating=body.rating,
        prompt_callsite=turn_rating.prompt_callsite,
        request_trace_id=ctx.trace_id,
    )

    # --- Persist: NDJSON audit log (append-only — NOT an aggregation source) ---
    # Re-rates intentionally produce multiple lines; nothing reads this for means.
    _append_ndjson_audit(es_doc, now)

    # --- Bus: publish only when rating value changed (decision §3) ---
    if existing_rating is None or existing_rating != body.rating:
        await _publish_rating_event(turn_rating, ctx.trace_id)

    return JSONResponse({"status": "received"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _verify_trace_session_ownership(
    trace_id: str,
    session_id: str,
    es_client: Any | None,
    ctx_trace_id: str,
) -> bool:
    """Verify that trace_id's model_call_completed event belongs to session_id.

    Queries agent-logs-* for a model_call_completed doc for this trace.
    If no doc exists (e.g. old turn, ES miss), we conservatively allow the
    write — the worst-case is an audit-only mismatch, not a data leak.

    Args:
        trace_id: Turn trace identifier.
        session_id: Session the caller claims owns this trace.
        es_client: Optional ES client from app state.
        ctx_trace_id: Request trace ID for log correlation.

    Returns:
        True when the trace belongs to the session (or ES is unavailable).
        False when ES confirms the trace belongs to a different session.
    """
    if es_client is None:
        # ES unavailable — cannot verify; permit conservatively.
        return True

    try:
        from personal_agent.config import settings as _settings

        index = f"{_settings.elasticsearch_index_prefix}-*"
        resp = await es_client.search(
            index=index,
            query={
                "bool": {
                    "filter": [
                        {"term": {"trace_id": trace_id}},
                        {"term": {"event.keyword": "model_call_completed"}},
                    ]
                }
            },
            size=1,
            _source=["session_id"],
        )
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            # No model_call_completed for this trace — allow conservatively.
            return True
        doc_session = str(hits[0].get("_source", {}).get("session_id", "") or "")
        return bool(doc_session == session_id)
    except Exception:
        log.warning(
            "feedback_trace_ownership_check_failed",
            trace_id=trace_id,
            request_trace_id=ctx_trace_id,
            exc_info=True,
        )
        return True  # conservative


async def _lookup_prompt_identity(
    trace_id: str,
    session_id: str,
    es_client: Any | None,
    ctx_trace_id: str,
) -> dict[str, Any] | None:
    """Resolve prompt identity for a trace from agent-logs-* (decision §2/§6).

    Prefers ``orchestrator.primary`` callsite, then ``role.primary``, then
    ``gateway.chat``, else the most-recent callsite with ``prompt_static_prefix_hash``.
    Retries once after 2 s on ES miss (handles the ~5 s refresh_interval race).

    Args:
        trace_id: Turn trace identifier.
        session_id: Session the turn belongs to (for scoping the query).
        es_client: Optional ES client from app state.
        ctx_trace_id: Request trace ID for log correlation.

    Returns:
        Dict with ``prompt_callsite``, ``prompt_static_prefix_hash``,
        ``prompt_dynamic_hash``, ``prompt_component_ids`` when found, else None.
    """
    if es_client is None:
        return None

    async def _query() -> dict[str, Any] | None:
        try:
            from personal_agent.config import settings as _settings

            index = f"{_settings.elasticsearch_index_prefix}-*"
            resp = await es_client.search(
                index=index,
                query={
                    "bool": {
                        "filter": [
                            {"term": {"trace_id": trace_id}},
                            {"term": {"session_id": session_id}},
                            {"term": {"event.keyword": "model_call_completed"}},
                            {"exists": {"field": "prompt_static_prefix_hash"}},
                        ]
                    }
                },
                size=20,
                sort=[{"@timestamp": {"order": "desc"}}],
                _source=[
                    "prompt_callsite",
                    "prompt_static_prefix_hash",
                    "prompt_dynamic_hash",
                    "prompt_component_ids",
                ],
            )
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                return None
            # Preference: orchestrator.primary → role.primary → gateway.chat → most-recent
            docs: list[dict[str, Any]] = [dict(h.get("_source", {})) for h in hits]
            for preferred in _CALLSITE_PREFERENCE:
                for doc in docs:
                    if doc.get("prompt_callsite") == preferred:
                        return doc
            # Fallback: most-recent (first in desc-sorted list)
            return dict(docs[0])
        except Exception:
            log.warning(
                "feedback_identity_lookup_failed",
                trace_id=trace_id,
                request_trace_id=ctx_trace_id,
                exc_info=True,
            )
            return None

    result = await _query()
    if result is None:
        # One delayed retry for the refresh-interval race (decision §6)
        await asyncio.sleep(2)
        result = await _query()

    return result


async def _get_existing_rating(
    trace_id: str,
    es_client: Any | None,
    ctx_trace_id: str,
) -> dict[str, Any] | None:
    """Fetch any existing rating doc for this trace_id from user-turn-ratings-*.

    Used to detect whether the rating value changed (re-rate suppression).

    Args:
        trace_id: Turn trace identifier.
        es_client: Optional ES client from app state.
        ctx_trace_id: Request trace ID for log correlation.

    Returns:
        Existing doc dict with at least ``rating`` key, or None if not found.
    """
    if es_client is None:
        return None

    try:
        resp = await es_client.search(
            index="user-turn-ratings-*",
            query={"term": {"trace_id": trace_id}},
            size=1,
            _source=["rating"],
        )
        hits = resp.get("hits", {}).get("hits", [])
        if hits:
            return dict(hits[0].get("_source", {}))
        return None
    except Exception:
        log.debug(
            "feedback_existing_rating_lookup_failed",
            trace_id=trace_id,
            request_trace_id=ctx_trace_id,
            exc_info=True,
        )
        return None


async def _publish_rating_event(rating: UserTurnRating, ctx_trace_id: str) -> None:
    """Publish UserTurnRatingEvent to the bus (best-effort, swallowed on Redis down).

    Args:
        rating: The rating that was written.
        ctx_trace_id: Request trace ID for log correlation.
    """
    try:
        from personal_agent.events.bus import get_event_bus
        from personal_agent.events.models import STREAM_USER_TURN_RATED, UserTurnRatingEvent

        bus = get_event_bus()
        event = UserTurnRatingEvent(
            trace_id=rating.trace_id,
            session_id=rating.session_id,
            rating=rating.rating,
            prompt_callsite=rating.prompt_callsite,
            prompt_static_prefix_hash=rating.prompt_static_prefix_hash,
            source_component="gateway.feedback_api",
        )
        await bus.publish(STREAM_USER_TURN_RATED, event)
        log.debug(
            "feedback_rating_event_published",
            trace_id=rating.trace_id,
            rating=rating.rating,
            request_trace_id=ctx_trace_id,
        )
    except Exception:
        log.warning(
            "feedback_rating_event_publish_failed",
            trace_id=rating.trace_id,
            request_trace_id=ctx_trace_id,
            exc_info=True,
        )


def _append_ndjson_audit(doc: dict[str, Any], now: datetime) -> None:
    """Append the rating doc to the local NDJSON audit log.

    The NDJSON file is an append-only audit trail — NOT an aggregation source.
    Re-rates intentionally leave multiple lines; nothing reads this file for
    computing means. The authoritative rating is the ES doc (keyed on trace_id,
    overwritten on re-rate).

    Args:
        doc: ES doc dict to append.
        now: Timestamp for determining the daily filename.
    """
    try:
        project_root = pathlib.Path(__file__).resolve().parent.parent.parent.parent
        feedback_dir = project_root / "telemetry" / "user_feedback"
        feedback_dir.mkdir(parents=True, exist_ok=True)
        date_str = now.strftime("%Y-%m-%d")
        ndjson_path = feedback_dir / f"{date_str}.ndjson"
        with ndjson_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc) + "\n")
    except Exception:
        log.warning(
            "feedback_ndjson_audit_write_failed",
            trace_id=doc.get("trace_id"),
            exc_info=True,
        )
