"""Session REST endpoints for the Seshat API Gateway.

Exposes PostgreSQL session data over HTTP under ``/sessions/*``.  All
endpoints require the ``sessions:read`` scope **and** a verified
``Cf-Access-Authenticated-User-Email`` header. Session rows are scoped to
the resolved user_id so a holder of the bearer token cannot read another
user's data — closes the cross-user data leak fixed in this hotfix.
"""

from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.gateway.auth import TokenInfo, require_scope
from personal_agent.gateway.errors import not_found, service_unavailable
from personal_agent.gateway.rate_limiting import get_rate_limiter
from personal_agent.llm_client.message_content import get_text_content
from personal_agent.llm_client.models import ModelConfig, ModelDefinition, ProviderDefinition
from personal_agent.service.auth import _CF_EMAIL_HEADER, _get_user_with_display_name
from personal_agent.service.models import SessionSelectionUpdate
from personal_agent.telemetry.trace import SystemTraceContext, TraceContext

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])

#: Sessionless config read (ADR-0121 T5, FRE-920) — no ``/sessions`` prefix,
#: mounted directly under ``/api/v1`` alongside ``router`` so a brand-new
#: conversation (no DB row yet) still has a model-picker read path.
config_router = APIRouter(tags=["config"])


# ---------------------------------------------------------------------------
# Dependency: resolve DB session factory from app state
# ---------------------------------------------------------------------------


async def _get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
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


async def _require_request_user_id(request: Request, db: AsyncSession) -> UUID:
    """Resolve the authenticated user's UUID from CF Access headers.

    The gateway router's bearer token authorizes *access* to the endpoint
    family; this helper additionally pins each request to one user so the
    bearer-token holder cannot enumerate other users' sessions. Mirrors
    :func:`personal_agent.service.auth.get_request_user` but returns just
    the ``user_id`` (no full ``RequestUser``) so the gateway router stays
    free of FastAPI-only types.

    Args:
        request: Incoming FastAPI request.
        db: Active async SQLAlchemy session for the users-table lookup.

    Returns:
        Stable ``user_id`` UUID for the requester.

    Raises:
        HTTPException(401): When the ``Cf-Access-Authenticated-User-Email``
            header is absent. The gateway never falls back to a default
            owner — token-only callers are rejected outright.
    """
    email = request.headers.get(_CF_EMAIL_HEADER)
    if not email:
        raise HTTPException(
            status_code=401,
            detail="Authentication required (missing CF Access user header)",
        )
    user_id, _ = await _get_user_with_display_name(db, email)
    return UUID(str(user_id))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_sessions(
    request: Request,
    limit: int = 20,
    token: TokenInfo = Depends(require_scope("sessions:read")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> list[dict[str, Any]]:
    """List recent sessions ordered by last activity.

    Args:
        request: FastAPI request (injected).
        limit: Maximum number of sessions to return (default 20).
        token: Validated bearer token with ``sessions:read`` scope.
        db: Async SQLAlchemy session (injected).

    Returns:
        List of session summary dicts.
    """
    get_rate_limiter().check(token)
    from personal_agent.service.repositories.session_repository import SessionRepository

    user_id = await _require_request_user_id(request, db)
    ctx = SystemTraceContext.new("session_api")
    log.info(
        "gateway_sessions_list",
        limit=limit,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
    )

    repo = SessionRepository(db)
    sessions = await repo.list_recent(limit, user_id=user_id)
    return [_session_to_dict(s) for s in sessions]


@router.get("/{session_id}")
async def get_session(
    request: Request,
    session_id: str,
    token: TokenInfo = Depends(require_scope("sessions:read")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> dict[str, Any]:
    """Retrieve a single session by ID.

    Args:
        request: FastAPI request (injected).
        session_id: UUID string of the session.
        token: Validated bearer token with ``sessions:read`` scope.
        db: Async SQLAlchemy session (injected).

    Returns:
        Session dict.

    Raises:
        HTTPException(422): When ``session_id`` is not a valid UUID.
        HTTPException(404): When the session does not exist.
    """
    get_rate_limiter().check(token)
    from personal_agent.service.repositories.session_repository import SessionRepository

    user_id = await _require_request_user_id(request, db)
    ctx = SystemTraceContext.new("session_api", session_id=session_id)
    log.info(
        "gateway_sessions_get",
        session_id=session_id,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
    )

    try:
        uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_parameter",
                "message": "session_id must be a valid UUID",
                "status": 422,
            },
        ) from exc

    repo = SessionRepository(db)
    # 404 (not 403) on ownership mismatch — do not confirm existence of
    # other users' sessions.
    session = await repo.get(uuid, user_id=user_id)
    if session is None:
        raise not_found("session")

    result = _session_to_dict(session)
    # FRE-426: server-authoritative context + cost so the PWA status bar is
    # populated on mount / session switch — before the first turn_status of the
    # next turn (kills the "blank meters until I run a turn" gap).
    from sqlalchemy import text as _sql_text  # noqa: PLC0415

    from personal_agent.orchestrator.context_window import (  # noqa: PLC0415
        estimate_messages_tokens,
    )

    result["context_tokens"] = estimate_messages_tokens(list(session.messages or []))

    # ADR-0121 §4: server-authoritative primary model selection + provenance, so
    # a client hydrates the picker on mount (localStorage is a cache, never the
    # authority — invariants 1/5/10). The stored key is run through the §6
    # guardrail so a stale/removed key fail-closes to the default here too.
    from personal_agent.config import load_model_config as _load_model_config  # noqa: PLC0415

    _config = _load_model_config()
    _stored = await _fetch_selections(db, uuid, roles=["primary"], ctx=ctx)
    _resolved_primary, _selection_provenance = _resolve_role_binding(
        "primary", _config, _stored.get("primary")
    )
    result["primary_selection"] = _resolved_primary
    result["selection_provenance"] = _selection_provenance

    # FRE-943: resolve context_max against this session's actual selection
    # (just resolved above), not the in-turn-only ContextVar that
    # ``executor._resolve_context_max()`` reads — empty on this plain HTTP
    # path, which is why it silently fell back to the role default (e.g.
    # local Qwen's 131K window) for a session actually running cloud Sonnet's
    # 200K.
    result["context_max"] = _resolve_session_context_max(_resolved_primary, _config, ctx=ctx)

    cost_scalar = (
        await db.execute(
            _sql_text("SELECT COALESCE(SUM(cost_usd), 0) FROM api_costs WHERE session_id = :sid"),
            {"sid": uuid},
        )
    ).scalar()
    result["cost_usd"] = float(cost_scalar or 0.0)
    return result


@router.get("/{session_id}/messages")
async def get_session_messages(
    request: Request,
    session_id: str,
    limit: int = 50,
    token: TokenInfo = Depends(require_scope("sessions:read")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> list[dict[str, Any]]:
    """Retrieve messages for a session.

    Args:
        request: FastAPI request (injected).
        session_id: UUID string of the session.
        limit: Maximum number of messages to return (default 50).
        token: Validated bearer token with ``sessions:read`` scope.
        db: Async SQLAlchemy session (injected).

    Returns:
        List of message dicts in chronological order.

    Raises:
        HTTPException(422): When ``session_id`` is not a valid UUID.
        HTTPException(404): When the session does not exist.
    """
    get_rate_limiter().check(token)
    from personal_agent.service.repositories.session_repository import SessionRepository

    user_id = await _require_request_user_id(request, db)
    ctx = SystemTraceContext.new("session_api", session_id=session_id)
    log.info(
        "gateway_sessions_get_messages",
        session_id=session_id,
        limit=limit,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
    )

    try:
        uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_parameter",
                "message": "session_id must be a valid UUID",
                "status": 422,
            },
        ) from exc

    repo = SessionRepository(db)
    session = await repo.get(uuid, user_id=user_id)
    if session is None:
        raise not_found("session")

    messages: list[dict[str, Any]] = list(session.messages or [])
    if limit > 0:
        messages = messages[-limit:]

    # FRE-426: attach each assistant turn's stored rating so the PWA can render
    # the rating control with the user's previously-submitted score (and the
    # rated-vs-default visual state) across reloads.
    es_client = getattr(request.app.state, "es_client", None)
    await _attach_turn_ratings(messages, es_client, ctx_trace_id=ctx.trace_id)
    return messages


async def _attach_turn_ratings(
    messages: list[dict[str, Any]],
    es_client: Any | None,
    *,
    ctx_trace_id: str,
) -> None:
    """Annotate assistant messages with their stored rating (FRE-426).

    Joins ``user-turn-ratings-*`` by ``trace_id`` in a single query and sets
    ``rating`` on each matching message. Best-effort: on ES miss/unavailable
    the messages are returned unannotated (the control falls back to its
    unrated default).

    Args:
        messages: Message dicts to annotate in place.
        es_client: AsyncElasticsearch client from app state, or None.
        ctx_trace_id: Request trace ID for log correlation.
    """
    if es_client is None:
        return
    trace_ids = [
        str(m["trace_id"]) for m in messages if m.get("role") == "assistant" and m.get("trace_id")
    ]
    if not trace_ids:
        return
    try:
        resp = await es_client.search(
            index="user-turn-ratings-*",
            query={"terms": {"trace_id": trace_ids}},
            size=len(trace_ids),
            _source=["trace_id", "rating"],
        )
    except Exception:
        log.warning("session_messages_rating_join_failed", trace_id=ctx_trace_id)
        return
    by_trace: dict[str, int] = {}
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        tid = src.get("trace_id")
        rating = src.get("rating")
        if isinstance(tid, str) and isinstance(rating, int):
            by_trace[tid] = rating
    for m in messages:
        # Only the assistant turn is rateable; the user message in the same turn
        # shares the trace_id, so guard on role to avoid tagging it.
        if m.get("role") != "assistant":
            continue
        tid = m.get("trace_id")
        if isinstance(tid, str) and tid in by_trace:
            m["rating"] = by_trace[tid]


# ---------------------------------------------------------------------------
# Model selection resolution — shared by GET /{id} and GET /{id}/config
# (ADR-0121 §3/§4, FRE-918)
# ---------------------------------------------------------------------------


def _resolve_role_binding(
    role: str,
    config: ModelConfig,
    stored: str | None,
) -> tuple[str, str]:
    """Resolve a session's effective deployment for ``role``, plus provenance.

    Generalizes the primary-only hydration ``get_session`` used before FRE-918
    to any role. ADR-0121 T5 (FRE-920) retired the ExecutionProfile bridge this
    used to fall back to — ``service/app.py``'s legacy ``POST /chat`` endpoint
    now always writes a selection-store row (on an explicit ``model`` or on
    first turn for a brand-new session), so a missing row degrades straight to
    the binding default rather than needing a profile-shaped stand-in.

    Order: stored selection (open roles only, guardrailed) -> binding default.

    Provenance is deliberately binary (``"server-hydrated"`` / ``"default"``),
    matching this endpoint family's existing, tested contract.

    Takes an already-fetched ``stored`` value rather than querying the
    selection store itself — callers batch the DB read once per session
    (:meth:`SessionModelSelectionRepository.get_all`) instead of once per role,
    so this is a pure function, trivially safe to call for a pinned role too
    (it is simply ignored: ``binding.open`` gates whether ``stored`` is
    honoured, matching AC-4a even when a row exists for a pinned role).

    Args:
        role: The role to resolve.
        config: The loaded catalog.
        stored: This role's already-fetched selection-store value, or
            ``None`` when no row exists (or the batched read failed).

    Returns:
        ``(resolved_deployment_key, provenance)``.
    """
    from personal_agent.config.model_loader import (  # noqa: PLC0415
        resolve_selected_deployment,
    )

    binding = config.roles.get(role)
    if binding is not None and binding.open and stored is not None:
        resolved = resolve_selected_deployment(role, stored, config)
        provenance = "server-hydrated" if resolved == stored else "default"
        return resolved, provenance

    return resolve_selected_deployment(role, None, config), "default"


def _resolve_session_context_max(model_key: str, config: ModelConfig, *, ctx: TraceContext) -> int:
    """Resolve the context window for a session's actual selected model (FRE-943).

    Unlike the in-turn ``executor._resolve_context_max`` — a best-effort
    telemetry helper that degrades to ``settings.context_window_max_tokens``
    on any failure — this surfaces a resolution failure instead of serving a
    plausible-looking wrong number. ``model_key`` is expected to already be
    guardrailed (:func:`_resolve_role_binding` never returns an off-catalog
    key), so a ``None`` definition here means the role's own binding default
    names no catalog deployment — a real catalog misconfiguration, not a
    transient condition to mask.

    Args:
        model_key: The session's resolved primary deployment key.
        config: The loaded model catalog.
        ctx: Trace context for the failure log.

    Returns:
        The resolved model's context length.

    Raises:
        HTTPException: 500 when ``model_key`` names no catalog deployment.
    """
    from personal_agent.config.model_loader import resolve_role_target  # noqa: PLC0415

    _, model_def = resolve_role_target("primary", model_key=model_key, config=config)
    if model_def is None:
        log.error(
            "context_max_resolve_failed",
            model_key=model_key,
            trace_id=ctx.trace_id,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "context_max_unresolved",
                "message": f"primary selection {model_key!r} has no catalog context_length",
                "status": 500,
            },
        )
    return model_def.context_length


async def _fetch_selections(
    db: AsyncSession, session_uuid: UUID, *, roles: list[str] | None, ctx: TraceContext
) -> dict[str, str]:
    """Fetch a session's stored selections, degrading to empty on a store failure.

    Args:
        db: Async SQLAlchemy session.
        session_uuid: The session's UUID.
        roles: When a single role is needed, ``[role]`` fetches just that row
            (matches the pre-FRE-918 single-query cost for ``get_session``).
            ``None`` batches every role in one query
            (:meth:`SessionModelSelectionRepository.get_all`), avoiding an
            N-role fan-out of round trips.
        ctx: The request's trace context, threaded into the failure log so a
            selection-store degradation is correlatable (ADR-0074).

    Returns:
        ``{role: deployment_key}`` for whatever rows exist; ``{}`` on a store
        read failure (e.g. migration lag before table 0020 exists) so callers
        degrade to binding defaults rather than 500ing.
    """
    from personal_agent.service.repositories.session_model_selection_repository import (  # noqa: PLC0415, E501
        SessionModelSelectionRepository,
    )

    repo = SessionModelSelectionRepository(db)
    try:
        if roles is not None:
            (role,) = roles
            value = await repo.get(session_uuid, role)
            return {} if value is None else {role: value}
        return await repo.get_all(session_uuid)
    except Exception as exc:  # noqa: BLE001 — availability guard (migration lag)
        log.warning(
            "selection_store_hydration_failed",
            session_id=str(session_uuid),
            roles=roles,
            error=str(exc),
            trace_id=ctx.trace_id,
        )
        return {}


def _deployment_view(key: str, model: ModelDefinition, config: ModelConfig) -> dict[str, Any]:
    """Build the picker-facing view of a single catalog deployment.

    Args:
        key: The deployment's catalog key.
        model: The deployment's ``ModelDefinition``.
        config: The loaded catalog (for ``placement_of``).

    Returns:
        A JSON-serializable dict of the deployment's picker-relevant fields.
    """
    return {
        "key": key,
        "id": model.id,
        "provider": model.provider,
        "placement": config.placement_of(key).value,
        "kind": model.kind.value,
        "status": model.status,
        "summary": model.summary,
        "context_length": model.context_length,
        "max_tokens": model.max_tokens,
        "supports_vision": model.supports_vision,
        "supports_pdf_document": model.supports_pdf_document,
        "input_cost_per_token": model.input_cost_per_token,
        "output_cost_per_token": model.output_cost_per_token,
    }


def _provider_view(key: str, provider: ProviderDefinition, available: bool) -> dict[str, Any]:
    """Build the observe-view row for a single provider.

    Args:
        key: The provider's catalog key.
        provider: The ``ProviderDefinition``.
        available: This provider's live/config availability (ADR-0121 §3).

    Returns:
        A JSON-serializable dict of the provider's observe-view fields.
    """
    return {
        "key": key,
        "placement": provider.placement.value,
        "available": available,
        "summary": provider.summary,
        "max_concurrency": provider.max_concurrency,
    }


@router.get("/{session_id}/config")
async def get_session_config(
    request: Request,
    session_id: str,
    token: TokenInfo = Depends(require_scope("sessions:read")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> dict[str, Any]:
    """Return the model-picker + observe-view read payload (ADR-0121 §3 / FRE-918).

    For every declared role: whether it is ``open`` or pinned, the effective
    resolved binding for **this session** (selection-applied — AC-9 slice:
    "what the next turn will actually use"), and for open roles the currently
    available, kind-compatible candidate list (AC-5). Also returns the
    provider table with placement and live-checked availability.

    404s until the session's first DB row exists (created on first message).
    A brand-new conversation has no row yet — use the sessionless
    ``GET /api/v1/config`` (:func:`get_config`) for that case; it returns the
    same ``roles``/``providers`` shape and resolves the same per-role
    ``resolved``/``provenance`` against no stored selection (FRE-938), so the
    two endpoints agree on what a role's default is by construction.

    Args:
        request: FastAPI request (injected).
        session_id: UUID string of the session.
        token: Validated bearer token with ``sessions:read`` scope.
        db: Async SQLAlchemy session (injected).

    Returns:
        ``{"session_id": ..., "roles": {role: {"open", "resolved",
        "provenance", "candidates"?}}, "providers": [...]}``.

    Raises:
        HTTPException(422): When ``session_id`` is not a valid UUID.
        HTTPException(404): When the session does not exist or is owned by
            another user.
    """
    get_rate_limiter().check(token)
    from personal_agent.config import load_model_config  # noqa: PLC0415
    from personal_agent.config import settings as _settings  # noqa: PLC0415
    from personal_agent.config.model_loader import role_candidates  # noqa: PLC0415
    from personal_agent.llm_client.provider_health import check_all_providers  # noqa: PLC0415
    from personal_agent.service.repositories.session_repository import SessionRepository

    user_id = await _require_request_user_id(request, db)
    ctx = SystemTraceContext.new("session_api", session_id=session_id)

    try:
        uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_parameter",
                "message": "session_id must be a valid UUID",
                "status": 422,
            },
        ) from exc

    session = await SessionRepository(db).get(uuid, user_id=user_id)
    if session is None:
        raise not_found("session")

    config = load_model_config()
    availability = await check_all_providers(config, _settings, trace_id=ctx.trace_id)
    stored_selections = await _fetch_selections(db, uuid, roles=None, ctx=ctx)

    roles: dict[str, Any] = {}
    for role, binding in config.roles.items():
        resolved, provenance = _resolve_role_binding(role, config, stored_selections.get(role))
        entry: dict[str, Any] = {
            "open": binding.open,
            "resolved": resolved,
            "provenance": provenance,
        }
        if binding.open:
            entry["candidates"] = [
                _deployment_view(key, config.models[key], config)
                for key in role_candidates(role, config, availability)
            ]
        roles[role] = entry

    providers = [
        _provider_view(key, provider, availability.get(key, False))
        for key, provider in config.providers.items()
    ]

    log.info(
        "gateway_sessions_get_config",
        session_id=session_id,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
    )
    return {"session_id": session_id, "roles": roles, "providers": providers}


@config_router.get("/config")
async def get_config(
    token: TokenInfo = Depends(require_scope("sessions:read")),  # noqa: B008
) -> dict[str, Any]:
    """Return the sessionless model-picker + observe-view read payload (ADR-0121 T5, FRE-920).

    A brand-new conversation has no session row yet — both
    ``GET /{session_id}/config`` and ``PATCH /{session_id}/selection`` 404
    before the first message creates one (codex plan-review finding). This
    endpoint serves the same ``roles``/``providers`` shape: for every
    declared role, whether it is ``open`` or pinned, its resolved deployment
    (there being no stored selection to apply, this is always the role's
    catalog default — FRE-938) and, for open roles, the currently available
    candidate list (AC-5); the provider table with placement and
    live-checked availability.

    Args:
        token: Validated bearer token with ``sessions:read`` scope.

    Returns:
        ``{"roles": {role: {"open", "resolved", "provenance", "candidates"?}},
        "providers": [...]}`` — the same shape as ``GET /{session_id}/config``;
        ``provenance`` is always ``"default"`` here since there is no session
        to hydrate a stored selection from.
    """
    get_rate_limiter().check(token)
    from personal_agent.config import load_model_config  # noqa: PLC0415
    from personal_agent.config import settings as _settings  # noqa: PLC0415
    from personal_agent.config.model_loader import role_candidates  # noqa: PLC0415
    from personal_agent.llm_client.provider_health import check_all_providers  # noqa: PLC0415

    ctx = SystemTraceContext.new("session_api")
    config = load_model_config()
    availability = await check_all_providers(config, _settings, trace_id=ctx.trace_id)

    roles: dict[str, Any] = {}
    for role, binding in config.roles.items():
        resolved, provenance = _resolve_role_binding(role, config, None)
        entry: dict[str, Any] = {
            "open": binding.open,
            "resolved": resolved,
            "provenance": provenance,
        }
        if binding.open:
            entry["candidates"] = [
                _deployment_view(key, config.models[key], config)
                for key in role_candidates(role, config, availability)
            ]
        roles[role] = entry

    providers = [
        _provider_view(key, provider, availability.get(key, False))
        for key, provider in config.providers.items()
    ]

    log.info("gateway_config_get", token_name=token.name, trace_id=ctx.trace_id)
    return {"roles": roles, "providers": providers}


@router.patch("/{session_id}/selection")
async def update_session_selection(
    request: Request,
    session_id: str,
    body: SessionSelectionUpdate,
    token: TokenInfo = Depends(require_scope("sessions:write")),  # noqa: B008
    db: AsyncSession = Depends(_get_db),  # noqa: B008
) -> dict[str, Any]:
    """Set a session's server-owned model selection (ADR-0121 §4 / FRE-917).

    The canonical write path for the model picker that replaces the Path pill —
    the selection-store analog of :func:`update_session_profile`. The write is
    guarded two ways (ADR-0121 §6): the ``(role, deployment_key)`` is validated
    server-side **before any storage** (an ``open`` role naming a valid,
    kind-compatible catalog key — a pinned role or non-catalog/wrong-kind key is
    rejected 422), and the write is scoped to the authenticated user so a token
    holder cannot mutate another user's session (404 on mismatch). The change is
    persisted and emitted to the single active client as a ``session_selection``
    STATE_DELTA (ADR-0075).

    Args:
        request: FastAPI request (injected).
        session_id: UUID string of the session.
        body: The role and deployment key to select.
        token: Validated bearer token with ``sessions:write`` scope.
        db: Async SQLAlchemy session (injected).

    Returns:
        The session dict (unchanged shape; the stored selection is the effect).

    Raises:
        HTTPException(422): When ``session_id`` is not a valid UUID, or the
            ``(role, deployment_key)`` is not a permitted selection.
        HTTPException(404): When the session does not exist or is owned by
            another user.
    """
    get_rate_limiter().check(token)
    from personal_agent.config import load_model_config
    from personal_agent.config.model_loader import is_selectable_binding
    from personal_agent.service.repositories.session_model_selection_repository import (
        SessionModelSelectionRepository,
    )
    from personal_agent.service.repositories.session_repository import SessionRepository
    from personal_agent.transport.agui.transport import emit_session_selection

    user_id = await _require_request_user_id(request, db)
    ctx = SystemTraceContext.new("session_api", session_id=session_id)

    try:
        uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_parameter",
                "message": "session_id must be a valid UUID",
                "status": 422,
            },
        ) from exc

    # §6 server-side validation BEFORE any storage: reject a pinned role or a
    # non-catalog / wrong-kind key (never trust the client).
    if not is_selectable_binding(body.role, body.deployment_key, load_model_config()):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_parameter",
                "message": (
                    f"'{body.deployment_key}' is not a selectable model for role '{body.role}'"
                ),
                "status": 422,
            },
        )

    # Ownership: a user-scoped read returns None for a wrong owner → 404, leaving
    # the stored value untouched (the write below never runs).
    session = await SessionRepository(db).get(uuid, user_id=user_id)
    if session is None:
        raise not_found("session")

    await SessionModelSelectionRepository(db).upsert(
        session_id=uuid, role=body.role, deployment_key=body.deployment_key
    )
    await emit_session_selection(
        session_id=session_id, role=body.role, deployment_key=body.deployment_key
    )
    log.info(
        "gateway_sessions_set_selection",
        session_id=session_id,
        role=body.role,
        deployment_key=body.deployment_key,
        token_name=token.name,
        user_id=str(user_id),
        trace_id=ctx.trace_id,
    )
    return _session_to_dict(session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_title(messages: list[dict[str, Any]]) -> str | None:
    """Derive a session title from the first user message.

    Args:
        messages: List of message dicts from the session.

    Returns:
        First 60 characters of the first user message, with a trailing
        ellipsis when truncated, or ``None`` when no user message exists.
    """
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content"):
            text = get_text_content(msg["content"]).strip()
            if text:
                return text[:60] + ("…" if len(text) > 60 else "")
    return None


def _session_to_dict(session: Any) -> dict[str, Any]:
    """Serialise a ``SessionModel`` to a plain dict.

    Args:
        session: SQLAlchemy ``SessionModel`` instance.

    Returns:
        Dict with serialised session fields including a derived ``title``.
    """
    msgs = list(session.messages or [])
    return {
        "session_id": str(session.session_id),
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "last_active_at": session.last_active_at.isoformat() if session.last_active_at else None,
        "mode": session.mode,
        "channel": session.channel,
        "message_count": len(msgs),
        "turn_count": sum(1 for m in msgs if m.get("role") == "user"),
        "title": _extract_title(msgs),
    }
