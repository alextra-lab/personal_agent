"""Service model compatibility tests."""

from datetime import datetime, timezone
from uuid import uuid4

from personal_agent.service.models import SessionResponse


class _SessionOrmLike:
    """Minimal ORM-like object with metadata_ attribute."""

    def __init__(self) -> None:
        self.session_id = uuid4()
        self.created_at = datetime.now(timezone.utc)
        self.last_active_at = datetime.now(timezone.utc)
        self.mode = "NORMAL"
        self.channel = "CLI"
        self.metadata_ = {"source": "test"}
        self.messages: list[dict[str, str]] = []


def test_session_response_maps_metadata_alias_from_attributes() -> None:
    """SessionResponse should read metadata_ from ORM object as metadata."""
    orm_obj = _SessionOrmLike()
    response = SessionResponse.model_validate(orm_obj, from_attributes=True)

    assert response.metadata == {"source": "test"}
