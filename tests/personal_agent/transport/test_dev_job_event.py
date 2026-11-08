"""FRE-1688 (ADR-0159 D10.6): a job card reaches the session as a ``dev_job`` STATE_DELTA."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.transport.agui.adapter import to_agui_event
from personal_agent.transport.agui.transport import emit_dev_job


@pytest.mark.asyncio
async def test_the_event_carries_the_card_payload() -> None:
    payload: dict[str, object] = {"event": "end", "state": "succeeded", "spend_usd": "0.5"}
    with patch(
        "personal_agent.transport.agui.transport._push_event", AsyncMock(return_value=True)
    ) as push:
        persisted = await emit_dev_job(session_id="s1", payload=payload)

    event, session_id = push.await_args.args
    assert persisted is True
    assert session_id == "s1"
    assert to_agui_event(event) == {
        "type": "STATE_DELTA",
        "data": {"key": "dev_job", "value": payload},
        "session_id": "s1",
        "seq": None,
    }
