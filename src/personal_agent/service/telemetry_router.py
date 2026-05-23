"""FRE-368 — client-side telemetry endpoint for ADR-0070 D8 measurement.

The PWA POSTs a card_click event each time the user expands an artifact
inline card. The event flows to Elasticsearch via the structlog handler
(same path as tool-call telemetry) so the two-week post-deploy review can
join click-rates against artifact_write rates per ADR-0070 D8.

Security: uses ``get_request_user`` (same as chat/session endpoints) which
reads the ``Cf-Access-Authenticated-User-Email`` header injected by CF Access
at the edge. Best-effort — a verified request always returns 204 even when
the emit logic encounters a transient error, so telemetry never blocks the
user interaction that triggered it.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.telemetry.trace import SystemTraceContext

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
    responses={401: {"description": "Not authenticated via CF Access."}},
)
async def post_card_click(
    event: CardClickEvent,
    request_user: Annotated[RequestUser, Depends(get_request_user)],  # noqa: B008
) -> None:
    """Record an artifact card-click event for ADR-0070 D8 measurement."""
    user_id = request_user.user_id
    ctx = SystemTraceContext.new(
        "telemetry_card_click",
        session_id=str(event.session_id) if event.session_id else None,
    )

    try:
        log.info(
            "artifact_card_click",
            artifact_id=str(event.artifact_id),
            session_id=str(event.session_id) if event.session_id else None,
            user_id=str(user_id) if user_id else None,
            surface=event.surface,
            trace_id=ctx.trace_id,
        )
    except Exception:  # noqa: BLE001
        pass
