"""Consumer-runner BudgetDenied semantics (FRE-306).

Verifies that when a handler raises ``BudgetDenied``, the consumer:

1. ACKs the message (so it doesn't pile up in the pending entries list or
   the dead-letter stream — it's not a poison pill, just transient cost
   pressure).
2. Does NOT call the retry loop or dead-letter the event.
3. Logs ``consumer_budget_denied`` with the structured payload so FRE-307
   telemetry can aggregate.

Recovery from BudgetDenied happens via the next scheduled consolidation
tick re-picking the trace, not via stream redelivery — the codebase
doesn't yet implement XCLAIM-based reclamation. Documented in the runner.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import orjson
import pytest

from personal_agent.cost_gate import BudgetDenied
from personal_agent.events.consumer import ConsumerRunner


class _FakeSubscription:
    """Minimal stand-in for ``Subscription`` — only the fields the runner reads."""

    def __init__(self, handler) -> None:  # noqa: ANN001
        self.stream = "stream:test"
        self.group = "cg:test"
        self.handler = handler


@pytest.mark.asyncio
async def test_handler_raising_budget_denied_acks_and_skips_dead_letter() -> None:
    """A BudgetDenied raised by the handler ACKs without dead-lettering."""
    bus = AsyncMock()
    bus.ack = AsyncMock()
    bus.dead_letter = AsyncMock()

    denial = BudgetDenied(
        role="entity_extraction",
        time_window="daily",
        current_spend=Decimal("2.40"),
        cap=Decimal("2.50"),
        window_resets_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
        denial_reason="cap_exceeded",
    )

    handler = AsyncMock(side_effect=denial)
    sub = _FakeSubscription(handler)

    runner = ConsumerRunner(bus)

    # Construct a parseable event payload — the runner deserializes from
    # the ``data`` field in the XREADGROUP fields dict.
    payload = _idle_event_payload()
    fields = {"data": orjson.dumps(payload).decode("utf-8")}

    await runner._process_message(  # type: ignore[reportPrivateUsage]
        sub=sub,
        message_id="0-1",
        fields=fields,
        max_retries=3,
    )

    # ACK was called exactly once — the runner shouldn't retry on
    # BudgetDenied (that's the bug fix from the spec).
    bus.ack.assert_called_once_with("stream:test", "cg:test", "0-1")
    bus.dead_letter.assert_not_called()
    # Handler called exactly once (no retry loop on BudgetDenied).
    assert handler.call_count == 1


@pytest.mark.asyncio
async def test_handler_raising_other_exception_still_dead_letters() -> None:
    """Non-BudgetDenied errors keep the existing retry → dead-letter path."""
    bus = AsyncMock()
    bus.ack = AsyncMock()
    bus.dead_letter = AsyncMock()

    handler = AsyncMock(side_effect=RuntimeError("boom"))
    sub = _FakeSubscription(handler)

    runner = ConsumerRunner(bus)

    payload = _idle_event_payload()
    fields = {"data": orjson.dumps(payload).decode("utf-8")}

    await runner._process_message(  # type: ignore[reportPrivateUsage]
        sub=sub,
        message_id="0-2",
        fields=fields,
        max_retries=2,
    )

    # 2 attempts, then dead-letter, then ACK.
    assert handler.call_count == 2
    bus.dead_letter.assert_called_once()
    bus.ack.assert_called_once()


def _idle_event_payload() -> dict[str, object]:
    """Build a minimal ``system.idle`` event payload accepted by parse_stream_event."""
    from datetime import datetime, timezone

    return {
        "event_type": "system.idle",
        "event_id": str(uuid4()),
        "trace_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_component": "test",
        "idle_seconds": 5.0,
    }
