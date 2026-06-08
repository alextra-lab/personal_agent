"""cost_gate emits its money fields as numbers, not strings (FRE-536).

The cost & budget dashboard (C1) sums these fields in Elasticsearch. Before
FRE-536 the gate emitted ``amount=str(Decimal)`` etc., so ES mapped them as
``keyword`` and they could not be aggregated. These tests pin the emit shape:
every money field is a ``float`` under a ``*_usd`` name.

Marker: ``integration`` — they exercise the real reserve/commit/refund path
against the running Postgres (mirrors ``test_gate.py``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import structlog

from personal_agent.cost_gate import CostGate

pytestmark = pytest.mark.integration


def _event(captured: list[dict], name: str) -> dict:
    """Return the single captured structlog entry whose event is ``name``."""
    matches = [e for e in captured if e.get("event") == name]
    assert matches, f"no {name!r} event captured (got {[e.get('event') for e in captured]})"
    return matches[-1]


@pytest.mark.asyncio
async def test_reserved_emits_amount_usd_as_float(cost_gate: CostGate, unique_role: str) -> None:
    """cost_gate_reserved carries amount_usd as a float, not a str."""
    with structlog.testing.capture_logs() as captured:
        await cost_gate.reserve(unique_role, Decimal("1.25"))
    evt = _event(captured, "cost_gate_reserved")
    assert isinstance(evt["amount_usd"], float), f"amount_usd is {type(evt['amount_usd'])}"
    assert evt["amount_usd"] == pytest.approx(1.25)
    assert "amount" not in evt, "legacy str field 'amount' should be gone"


@pytest.mark.asyncio
async def test_committed_emits_money_fields_as_float(cost_gate: CostGate, unique_role: str) -> None:
    """cost_gate_committed carries actual_cost_usd/reserved_usd/delta_usd as floats."""
    rid = await cost_gate.reserve(unique_role, Decimal("1.00"))
    with structlog.testing.capture_logs() as captured:
        await cost_gate.commit(rid, Decimal("0.40"))
    evt = _event(captured, "cost_gate_committed")
    for field in ("actual_cost_usd", "reserved_usd", "delta_usd"):
        assert isinstance(evt[field], float), f"{field} is {type(evt[field])}"
    assert evt["actual_cost_usd"] == pytest.approx(0.40)
    assert evt["reserved_usd"] == pytest.approx(1.00)
    assert evt["delta_usd"] == pytest.approx(-0.60)
    # role is carried so the dashboard can attribute actual spend by budget role.
    assert evt["role"] == unique_role


@pytest.mark.asyncio
async def test_refunded_emits_amount_usd_as_float(cost_gate: CostGate, unique_role: str) -> None:
    """cost_gate_refunded carries amount_usd as a float."""
    rid = await cost_gate.reserve(unique_role, Decimal("0.75"))
    with structlog.testing.capture_logs() as captured:
        await cost_gate.refund(rid)
    evt = _event(captured, "cost_gate_refunded")
    assert isinstance(evt["amount_usd"], float), f"amount_usd is {type(evt['amount_usd'])}"
    assert evt["amount_usd"] == pytest.approx(0.75)
