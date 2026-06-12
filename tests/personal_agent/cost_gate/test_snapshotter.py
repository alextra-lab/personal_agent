"""Budget-counter snapshot emit tests (FRE-547).

The Cost & Budget dashboard's cap-utilization panel reads a periodic
``budget_counter_snapshot`` event that mirrors Postgres ``budget_counters``
into Elasticsearch. These tests pin the emit contract that is most likely to
be wrong (the FRE-536 "mappings wrong first pass" traps):

* money / ratio fields land as ``float`` (not ``Decimal``/``str``) so ES maps
  them ``double`` and Kibana can aggregate them;
* ``window_start`` is an ISO-8601 string (``T`` separator) so ES parses it as
  a ``date`` rather than rejecting the space-separated ``str(datetime)``;
* one event is emitted per configured cap, and a single ``now`` drives every
  window computation (no straddling a midnight / week boundary mid-batch).

The unit tests use a fake asyncpg pool so they run under ``make test`` with no
live database. One ``integration`` test (``make test-infra-up``) exercises the
real SQL + schema path, mirroring ``test_gate_emit_types.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
import structlog

from personal_agent.cost_gate import (
    BudgetConfig,
    CapEntry,
    CostGate,
    OnDenialBehaviour,
    RoleConfig,
)

# ---------------------------------------------------------------------------
# Unit tests — fake pool, no live DB
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal asyncpg connection stub for ``snapshot_counters``.

    ``rows`` maps ``(time_window, role)`` to a stored ``running_total``; a
    missing key models "no counter row yet this window". ``transaction`` is an
    async-context-manager no-op so ``snapshot_counters`` can open its
    ``repeatable_read`` transaction.
    """

    def __init__(self, rows: Mapping[tuple[str, str], Decimal]) -> None:
        self._rows = dict(rows)

    def transaction(self, **_kwargs: Any) -> _FakeConn:
        return self

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def fetchrow(
        self, _query: str, time_window: str, role: str, _window_start: datetime
    ) -> dict[str, Decimal] | None:
        value = self._rows.get((time_window, role))
        if value is None:
            return None
        return {"running_total": value}


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _config(caps: Sequence[CapEntry]) -> BudgetConfig:
    """Build a config with the given caps and a matching role per cap role."""
    roles = {
        cap.role: RoleConfig(
            default_output_tokens=256, safety_factor=1.2, on_denial=OnDenialBehaviour.RAISE
        )
        for cap in caps
        if cap.role != "_total"
    }
    return BudgetConfig(version=1, roles=roles, caps=list(caps))


def _gate(config: BudgetConfig, rows: Mapping[tuple[str, str], Decimal]) -> CostGate:
    """A CostGate whose pool is a fake (no ``connect()``/DB needed)."""
    gate = CostGate(config=config, db_url="postgresql://unused/db")
    gate.pool = _FakePool(_FakeConn(rows))  # type: ignore[assignment]
    return gate


def _snapshots(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in captured if e.get("event") == "budget_counter_snapshot"]


@pytest.mark.asyncio
async def test_snapshot_emits_one_event_per_cap_with_float_and_iso_types() -> None:
    """Each cap yields one snapshot; money is float, window_start is ISO."""
    config = _config(
        [
            CapEntry(time_window="daily", role="main_inference", cap_usd=Decimal("5.00")),
            CapEntry(time_window="weekly", role="_total", cap_usd=Decimal("50.00")),
        ]
    )
    gate = _gate(config, {("daily", "main_inference"): Decimal("2.50")})

    with structlog.testing.capture_logs() as captured:
        emitted = await gate.snapshot_counters()

    snaps = _snapshots(captured)
    assert emitted == 2
    assert len(snaps) == 2

    daily = next(s for s in snaps if s["role"] == "main_inference")
    assert daily["time_window"] == "daily"
    assert isinstance(daily["running_total"], float) and daily["running_total"] == pytest.approx(
        2.5
    )
    assert isinstance(daily["cap_usd"], float) and daily["cap_usd"] == pytest.approx(5.0)
    assert isinstance(daily["utilization_ratio"], float)
    assert daily["utilization_ratio"] == pytest.approx(0.5)
    # window_start must be an ISO string (T separator), not a space-separated str(datetime).
    assert isinstance(daily["window_start"], str)
    assert "T" in daily["window_start"]


@pytest.mark.asyncio
async def test_snapshot_missing_counter_row_is_zero() -> None:
    """A cap with no counter row this window snapshots at running_total 0.0."""
    config = _config([CapEntry(time_window="weekly", role="_total", cap_usd=Decimal("50.00"))])
    gate = _gate(config, {})  # no rows at all

    with structlog.testing.capture_logs() as captured:
        emitted = await gate.snapshot_counters()

    snaps = _snapshots(captured)
    assert emitted == 1
    assert snaps[0]["running_total"] == 0.0
    assert snaps[0]["utilization_ratio"] == 0.0


@pytest.mark.asyncio
async def test_snapshot_zero_cap_does_not_divide_by_zero() -> None:
    """A cap_usd of 0 yields ratio 0.0 rather than raising ZeroDivisionError."""
    config = _config([CapEntry(time_window="daily", role="main_inference", cap_usd=Decimal("0"))])
    gate = _gate(config, {("daily", "main_inference"): Decimal("1.00")})

    with structlog.testing.capture_logs() as captured:
        emitted = await gate.snapshot_counters()

    snaps = _snapshots(captured)
    assert emitted == 1
    assert snaps[0]["utilization_ratio"] == 0.0


@pytest.mark.asyncio
async def test_snapshot_empty_caps_emits_nothing() -> None:
    """No configured caps → no snapshot events."""
    config = BudgetConfig(version=1, roles={}, caps=[])
    gate = _gate(config, {})

    with structlog.testing.capture_logs() as captured:
        emitted = await gate.snapshot_counters()

    assert emitted == 0
    assert _snapshots(captured) == []


@pytest.mark.asyncio
async def test_snapshot_uses_single_now_across_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every cap's window is computed from one captured ``now`` (no boundary straddle)."""
    import personal_agent.cost_gate.gate as gate_mod

    seen_now: list[datetime | None] = []
    real_window_start = gate_mod._window_start

    def _spy(time_window: str, now: datetime | None = None) -> datetime:
        seen_now.append(now)
        return real_window_start(time_window, now)

    monkeypatch.setattr(gate_mod, "_window_start", _spy)

    config = _config(
        [
            CapEntry(time_window="daily", role="main_inference", cap_usd=Decimal("5.00")),
            CapEntry(time_window="weekly", role="_total", cap_usd=Decimal("50.00")),
        ]
    )
    gate = _gate(config, {})

    await gate.snapshot_counters()

    assert len(seen_now) == 2
    assert all(n is not None for n in seen_now)
    assert seen_now[0] == seen_now[1], "all caps must share one captured now"


@pytest.mark.asyncio
async def test_snapshot_requires_connect() -> None:
    """snapshot_counters before connect() raises a clear RuntimeError."""
    config = _config(
        [CapEntry(time_window="daily", role="main_inference", cap_usd=Decimal("5.00"))]
    )
    gate = CostGate(config=config, db_url="postgresql://unused/db")  # pool is None
    with pytest.raises(RuntimeError, match="connect"):
        await gate.snapshot_counters()


# ---------------------------------------------------------------------------
# Integration test — real Postgres (make test-infra-up)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshot_reflects_reserved_amount(cost_gate: CostGate, unique_role: str) -> None:
    """After a reserve, the role's snapshot carries the reserved running_total + ratio.

    Exercises the real SQL + schema path. The ``cost_gate`` fixture builds a
    config with a daily cap of $10.00 for ``unique_role`` (see conftest).
    """
    await cost_gate.reserve(unique_role, Decimal("2.00"))

    with structlog.testing.capture_logs() as captured:
        await cost_gate.snapshot_counters()

    snaps = [
        s for s in _snapshots(captured) if s["role"] == unique_role and s["time_window"] == "daily"
    ]
    assert snaps, "expected a daily snapshot for the reserved role"
    snap = snaps[-1]
    assert isinstance(snap["running_total"], float)
    assert snap["running_total"] == pytest.approx(2.0)
    assert snap["cap_usd"] == pytest.approx(10.0)
    assert snap["utilization_ratio"] == pytest.approx(0.2)
