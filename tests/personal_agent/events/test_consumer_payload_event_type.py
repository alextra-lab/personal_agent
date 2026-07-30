"""FRE-1066: ConsumerRunner log calls must not pass event_type= directly.

es_handler.py derives the canonical ES event_type from the structlog message
name (e.g. "event_processed"). A caller-supplied event_type= kwarg silently
overwrites that value downstream, reproducing the exact defect this ticket
fixes for single-purpose streams (see redis_backend.py::publish). The
event's own type is carried under payload_event_type instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import orjson
import pytest

from personal_agent.cost_gate import BudgetDenied
from personal_agent.events.consumer import ConsumerRunner
from personal_agent.events.redis_backend import Subscription

from .conftest import _capturing_log, _consolidation_event_payload

_STREAM = "stream:consolidation.completed"
_GROUP = "cg:test"


def _budget_denial() -> BudgetDenied:
    return BudgetDenied(
        role="entity_extraction",
        time_window="daily",
        current_spend=Decimal("2.40"),
        cap=Decimal("2.50"),
        window_resets_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
        denial_reason="cap_exceeded",
    )


@pytest.mark.parametrize(
    ("handler_side_effect", "expected_event", "max_retries"),
    [
        (None, "event_processed", 3),
        (_budget_denial(), "consumer_budget_denied", 3),
        (RuntimeError("boom"), "consumer_handler_error", 2),
    ],
    ids=["happy_path", "budget_denied", "handler_error"],
)
@pytest.mark.asyncio
async def test_consumer_log_does_not_collide_with_es_event_type(
    handler_side_effect: object, expected_event: str, max_retries: int
) -> None:
    """None of ConsumerRunner's three log sites may pass event_type= directly."""
    bus = AsyncMock()
    bus.ack = AsyncMock()
    bus.dead_letter = AsyncMock()

    handler = AsyncMock(return_value=None, side_effect=handler_side_effect)
    sub = Subscription(stream=_STREAM, group=_GROUP, consumer_name="c0", handler=handler)
    runner = ConsumerRunner(bus)

    payload = _consolidation_event_payload()
    fields = {"data": orjson.dumps(payload).decode("utf-8")}

    mock_log, calls = _capturing_log()
    with patch("personal_agent.events.consumer.log", mock_log):
        await runner._process_message(  # type: ignore[reportPrivateUsage]
            sub=sub, message_id="0-1", fields=fields, max_retries=max_retries
        )

    matched = [kw for name, kw in calls if name == expected_event]
    assert matched, f"expected a {expected_event} log call"
    assert all("event_type" not in kw for kw in matched)
    assert all(kw["payload_event_type"] == "consolidation.completed" for kw in matched)
    assert all(kw["stream"] == _STREAM for kw in matched)
