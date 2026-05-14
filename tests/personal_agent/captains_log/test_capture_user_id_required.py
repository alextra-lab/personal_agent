"""TaskCapture.user_id is required (FRE-343 — non-optional after FRE-213).

Background: get_request_user (service/auth.py) always resolves a user_id —
either from Cf-Access-Authenticated-User-Email or from the
settings.agent_owner_email fallback (or raises 401). user_id=None at write
time is now a real bug, not a silent fallback.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from personal_agent.captains_log.capture import TaskCapture


def _base_kwargs() -> dict:
    return {
        "trace_id": "trace-1",
        "session_id": "sess-1",
        "timestamp": datetime.now(timezone.utc),
        "user_message": "hi",
        "outcome": "completed",
    }


def test_user_id_required_raises_when_missing() -> None:
    """Constructing TaskCapture without user_id raises a Pydantic ValidationError."""
    with pytest.raises(ValidationError):
        TaskCapture(**_base_kwargs())  # type: ignore[call-arg]


def test_user_id_required_raises_when_none() -> None:
    """Explicit user_id=None is rejected after FRE-343."""
    with pytest.raises(ValidationError):
        TaskCapture(**_base_kwargs(), user_id=None)  # type: ignore[arg-type]


def test_user_id_accepts_uuid_instance() -> None:
    """A UUID instance is accepted (happy path)."""
    uid = uuid4()
    capture = TaskCapture(**_base_kwargs(), user_id=uid)
    assert capture.user_id == uid


def test_user_id_coerces_string() -> None:
    """Pydantic coerces a UUID string to a UUID instance."""
    uid = uuid4()
    capture = TaskCapture(**_base_kwargs(), user_id=str(uid))
    assert capture.user_id == uid
