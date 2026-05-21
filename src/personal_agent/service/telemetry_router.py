"""FRE-368 — client-side telemetry endpoint for ADR-0070 D8 measurement.

The PWA POSTs a card_click event each time the user expands an artifact
inline card. The event flows to Elasticsearch via the structlog handler
(same path as tool-call telemetry) so the two-week post-deploy review can
join click-rates against artifact_write rates per ADR-0070 D8.

Security: same CF Access JWT verification as the public artifact endpoints.
The endpoint is best-effort — a verified request always returns 204 even
when the emit logic encounters a transient error, so telemetry never blocks
the user interaction that triggered it.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.service.auth import get_or_create_user_by_email
from personal_agent.service.cf_access_jwt import (
    CFAccessVerifierError,
    get_verifier,
)
from personal_agent.service.database import get_db_session

log = structlog.get_logger(__name__)

router = APIRouter(tags=["telemetry"])


class CardClickEvent(BaseModel):
    """Body for a PWA artifact card-click telemetry event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: UUID
    session_id: UUID | None = None
    kind: str = Field(default="card_click", pattern=r"^card_click$")
    surface: str = Field(default="inline", pattern=r"^(inline|drawer|standalone)$")


@router.post(
    "/api/v1/telemetry/card_click",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"description": "Missing or invalid CF Access JWT."},
        503: {"description": "CF Access verifier not configured."},
    },
)
async def post_card_click(
    event: CardClickEvent,
    request: Request,
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> None:
    """Record an artifact card-click event for ADR-0070 D8 measurement.

    The verified user identity ensures the telemetry is attributable (for
    debugging click-through rates per user segment) while the best-effort
    emit means a transient ES failure never breaks the UX interaction.
    """
    verifier = get_verifier()
    if verifier is None:
        log.error("telemetry_card_click_verifier_missing")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    jwt_token = request.headers.get("cf-access-jwt-assertion") or request.headers.get(
        "x-cf-access-jwt-assertion"
    )
    if not jwt_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        claims = await verifier.verify(jwt_token)
    except CFAccessVerifierError as exc:
        log.info(
            "telemetry_card_click_jwt_invalid",
            error_class=type(exc).__name__,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from exc

    user_id = await get_or_create_user_by_email(db, claims.email)

    # Best-effort emit — swallow any downstream error so telemetry
    # never propagates a 5xx to the browser.
    try:
        log.info(
            "artifact_card_click",
            artifact_id=str(event.artifact_id),
            session_id=str(event.session_id) if event.session_id else None,
            user_id=str(user_id),
            surface=event.surface,
        )
    except Exception:  # noqa: BLE001
        pass
