"""FRE-1622 (ADR-0157 D2): a level change reaches the active socket as a STATE_DELTA (AC-3)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.transport.agui.adapter import to_agui_event
from personal_agent.transport.agui.transport import emit_session_effort


@pytest.mark.asyncio
async def test_the_event_carries_the_new_level() -> None:
    with patch("personal_agent.transport.agui.transport._push_event", AsyncMock()) as push:
        await emit_session_effort(session_id="s1", seshat_effort="fast")

    event, session_id = push.await_args.args
    assert session_id == "s1"
    assert to_agui_event(event) == {
        "type": "STATE_DELTA",
        "data": {
            "key": "session_effort",
            "value": {"seshat_effort": "fast", "effort_provenance": "server-hydrated"},
        },
        "session_id": "s1",
        "seq": None,
    }
